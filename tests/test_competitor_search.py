from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from agents import competitors
from agents.exchange import Product, STORES
from browser_access import AccessError
from test_browser_fixtures import browser  # noqa: F401

PRODUCT = Product("Example", "Synthetic", "EDP", "100", "retail")


def document(name="Synthetic EDP 100 ml", brand="Example", package="retail box", **extra):
    item = {"@type": "Product", "name": name, "brand": {"name": brand}, **extra}
    if package:
        item["additionalProperty"] = [{"name": "packaging", "value": package}]
    return '<script type="application/ld+json">' + json.dumps(item) + '</script>'


class FixtureAccess:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []
        self.blocked = {}

    def policy(self, url):
        self.requests.append(("robots", url))

    def request(self, url):
        self.requests.append(("GET", url))
        return SimpleNamespace(status_code=200 if url in self.pages else 404,
                               headers={"Content-Type": "text/html"}, text=self.pages.get(url, ""))

    def context(self, browser, host, budget):
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=self.pages[host]))
        context.on("request", lambda req: self.requests.append(("browser", req.url)))
        return context, []


@pytest.mark.parametrize("source", STORES)
def test_search_finds_exact_card_with_empty_urls_and_prefers_cache(source, browser):
    domain = STORES[source]
    prefix = "/123456-" if source == "goldapple" else "/product/"
    wrong = f"https://{domain}{prefix}wrong"
    right = f"https://{domain}{prefix}right"
    search_page = ('<input type="search"><a href="' + wrong + '">Example Synthetic</a>'
                   '<a href="' + right + '?tracking=x">Example Synthetic</a>'
                   '<a href="https://example.com/product/synthetic">Example Synthetic</a>')
    access = FixtureAccess({domain: search_page, wrong: document("Synthetic EDT 100 ml"), right: document()})
    cached = []
    row = competitors.collect(PRODUCT, source, None, access, "guarded_public",
                              search=lambda store, item: competitors.search_links(browser, access, store, item),
                              remember=cached.append)
    assert row["url"] == right and row["status"] == "manual_review" and row["price_rub"] is None
    assert cached == [right]
    assert ("GET", wrong) in access.requests
    assert ("robots", right) in access.requests
    if source != "goldapple":
        queries = [parse_qs(urlsplit(url).query) for method, url in access.requests if method == "browser"]
        assert {"Example Synthetic EDP 100 мл"} == {value for query in queries for values in query.values() for value in values}
    access.requests.clear()
    row = competitors.collect(PRODUCT, source, None, access, "guarded_public", saved_url=cached[0],
                              search=lambda *_: pytest.fail("Exact cached card must be checked first"))
    assert row["url"] == right and access.requests == [("robots", right), ("GET", right)]


@pytest.mark.parametrize(("html", "verdict"), [
    (document("Synthetic EDT 100 ml"), "mismatch"),
    (document("Synthetic Extrait de Parfum 100 ml"), "mismatch"),
    (document("Synthetic EDP 50 ml"), "mismatch"),
    (document("Synthetic EDP 100 ml", package="тестер"), "mismatch"),
    (document("Synthetic EDP 100 ml", package="набор"), "mismatch"),
    (document("Synthetic Intense EDP 100 ml"), "mismatch"),
    (document(brand="Other"), "mismatch"),
    (document("Synthetic EDP 100 ml 50 ml"), "uncertain"),
    (document(package=None), "uncertain"),
    (document(hasVariant=[{}]), "uncertain"),
    (document("Example Synthetic Eau de Parfum 100 ml"), "exact"),
])
def test_exact_identity(html, verdict):
    assert competitors.identity(PRODUCT, html, "https://goldapple.ru/123456-item")[0] == verdict


def test_search_miss_and_prohibited_store():
    access = FixtureAccess({})
    for status, policy in [("no_card", "guarded_public"), ("blocked", "prohibited")]:
        row = competitors.collect(PRODUCT, "goldapple", None, access, policy, search=lambda *_: [])
        assert row["status"] == status
    assert not access.requests


def test_antibot_stops_search_and_alternate_links():
    blocked = AccessError("blocked", "CAPTCHA")
    first = "https://goldapple.ru/123456-first"
    alternate = "https://www.goldapple.ru/123457-alternate"
    access = FixtureAccess({})

    def challenge(url):
        assert url == first
        access.blocked["https://goldapple.ru"] = blocked
        raise blocked

    access.request = challenge
    row = competitors.collect(PRODUCT, "goldapple", alternate, access, "guarded_public", saved_url=first,
                              search=lambda *_: pytest.fail("Antibot response must stop the store"))
    assert row["status"] == "blocked"


@pytest.mark.parametrize("expiry", [
    "2000-01-01", "invalid",
    (datetime.now(ZoneInfo("Europe/Moscow")) - timedelta(minutes=1)).isoformat(),
])
def test_expired_price_is_not_used(expiry):
    url = "https://goldapple.ru/123456-item"
    html = document(offers={"price": 2000, "priceCurrency": "RUB",
                            "priceValidUntil": expiry, "seller": {"name": "Золотое Яблоко"}})
    row = competitors.collect(PRODUCT, "goldapple", url, FixtureAccess({url: html}), "guarded_public")
    assert row["status"] == "manual_review" and row["price_rub"] is None


def test_cli_link_cache_is_separate_from_manual_confirmations(tmp_path, monkeypatch):
    from agents import exchange

    catalog, output, cache, manual = [tmp_path / name for name in ("catalog", "output", "cache", "manual")]
    catalog.write_text(json.dumps({"products": [{"product": PRODUCT.__dict__, "urls": {}}]}))
    url = "https://goldapple.ru/123456-item"
    exchange.save(manual, "competitors", [
        exchange.observation(PRODUCT, "goldapple", "confirmed", url=url, price=2000,
                             reason="Синтетическая проверка", evidence="manual", stock="in_stock"),
    ])
    before = manual.read_bytes()
    access = FixtureAccess({url: document()})
    access.session = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(competitors, "PublicAccess", lambda **_: access)
    monkeypatch.setattr(competitors, "sync_playwright", lambda: playwright_fixture())
    monkeypatch.setattr(competitors, "search_links", lambda browser, transport, source, product:
                        [url] if source == "goldapple" else [])
    monkeypatch.setattr("sys.argv", ["competitors", "--catalog", str(catalog), "--output", str(output),
                                    "--link-cache", str(cache), "--confirmations", str(manual)])
    competitors.main()
    assert manual.read_bytes() == before
    assert exchange.load(cache) == {competitors.cache_key(PRODUCT, "goldapple"): url}
    assert len(exchange.validate(exchange.load(output), "competitors")) == 5
    monkeypatch.setattr("sys.argv", ["competitors", "--catalog", str(catalog), "--output", str(manual),
                                    "--link-cache", str(cache), "--confirmations", str(manual)])
    with pytest.raises(SystemExit) as error:
        competitors.main()
    assert error.value.code == 2 and manual.read_bytes() == before


def playwright_fixture():
    from contextlib import nullcontext

    return nullcontext(SimpleNamespace(chromium=SimpleNamespace(
        launch=lambda: SimpleNamespace(close=lambda: None))))
