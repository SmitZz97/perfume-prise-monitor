from types import SimpleNamespace

import pytest

from agents import competitors
from agents.exchange import Product
from browser_access import AccessError, PublicAccess
from test_browser_fixtures import browser
from test_competitor_search import FixtureAccess, document

LIBRE = Product("Yves Saint Laurent", "Libre", "EDP", "90", "retail")
URL = "https://www.letu.ru/product/libre"


def product_page(*, update=True, binding=True, extra="", package="Флакон в коробке"):
    script = """
      const option = document.getElementById('next');
      option.onclick = () => {
        document.querySelector('#first').setAttribute('aria-pressed', 'false');
        option.setAttribute('aria-pressed', 'true');
        if (UPDATE) setTimeout(() => {
          const price = document.querySelector('.price-block__price-current');
          price.textContent = '12 345,50 ₽';
          if (BINDING) price.dataset.sku = 'libre90';
        }, 220);
      };""".replace("UPDATE", str(update).lower()).replace("BINDING", str(binding).lower())
    return f"""
        <main><article data-testid="product-card"><h1>Libre</h1>
        <div itemprop="brand">Yves Saint Laurent</div><div itemprop="concentration">Парфюмерная вода</div>
        <div itemprop="packaging">{package}</div>
        <div data-testid="volume-group">
        <button id="first" aria-pressed="true" data-sku="libre50">50 мл</button>
        <button id="next" aria-pressed="false" data-sku="libre90">90 мл</button>
        </div><div class="price-block">
        <span class="price-block__price-current" data-sku="libre50">9 999 ₽</span>
        <del itemprop="price">15 000 ₽</del>{extra}</div>
        <div itemprop="seller">Лэтуаль</div><div itemprop="availability">В наличии</div></article></main>
        <aside><span itemprop="price">1 ₽</span></aside><script>{script}</script>
    """


@pytest.mark.parametrize(("options", "expected"), [
    ({}, "confirmed"),
    ({"update": False}, "manual_review"),
    ({"binding": False}, "manual_review"),
    ({"package": "Тестер"}, "manual_review"),
    ({"extra": "<span>Цена по карте</span>"}, "manual_review"),
])
def test_dynamic_card_rechecks_selected_volume_and_public_price(browser, options, expected):
    access = FixtureAccess({
        URL: document("Libre EDP 50 ml", brand="Yves Saint Laurent"),
        "letu.ru": product_page(**options),
    })
    row = competitors.collect(LIBRE, "letu", URL, access, "guarded_public",
                              render=lambda *args: competitors.rendered_card(browser, access, *args))
    if options.get("package"):
        assert row["status"] == "no_card"  # DOM proved a different variant than the target.
    else:
        assert row["status"] == expected
    if expected == "confirmed":
        assert row["price_rub"] == "12345.5"
        assert row["availability"] == "in_stock"
    else:
        assert row["price_rub"] is None


def test_randewoo_direct_without_search_and_scoped_rows(browser):
    url = "https://randewoo.ru/product/libre"
    html = """
      <article><h1>Libre</h1><span itemprop="brand">Yves Saint Laurent</span>
      <span itemprop="seller">Randewoo</span><ul>
      <li class="s-productType"><span class="s-productType__name">EDP 50 мл Флакон в коробке</span>
      <span class="s-productType__priceNewValue">2000 ₽</span></li>
      <li class="s-productType"><span class="s-productType__name">EDP 90 мл Флакон в коробке</span>
      <span class="s-productType__priceNewValue">6000 ₽</span></li></ul></article>
    """
    access = FixtureAccess({url: html, "randewoo.ru": html})
    kwargs = dict(search=lambda *_: pytest.fail("Запрещённый поиск Randewoo"),
                  render=lambda *args: competitors.rendered_card(browser, access, *args))
    assert competitors.collect(LIBRE, "randewoo", None, access, "guarded_public", **kwargs)["status"] == "no_card"
    row = competitors.collect(LIBRE, "randewoo", url, access, "guarded_public", **kwargs)
    assert row["status"] == "confirmed" and row["price_rub"] == "6000.0"
    assert ("robots", url) in access.requests


def test_503_stops_store_even_on_another_hostname_or_catalog_item(monkeypatch):
    access = competitors.CompetitorAccess()
    access.policy = lambda _: None
    requests = []

    def request(_, url):
        requests.append(url)
        return SimpleNamespace(status_code=503)

    monkeypatch.setattr(PublicAccess, "request", request)
    first = "https://rivegauche.ru/product/libre"
    second = "https://www.rivegauche.ru/product/another"
    for url in (first, second):
        row = competitors.collect(LIBRE, "rivegauche", url, access, "guarded_public",
                                  search=lambda *_: pytest.fail("HTTP 503 must stop search"))
        assert row["status"] == "blocked" and row["price_rub"] is None
    assert requests == [first]


def test_letu_waits_for_delayed_results_and_falls_back_to_brand_name(browser):
    class SearchAccess(FixtureAccess):
        def context(self, browser, host, budget):
            from urllib.parse import unquote
            context = browser.new_context()

            def route(request):
                destination = unquote(request.request.url)
                self.requests.append(destination)
                html = ('<main>Ничего не найдено</main>' if "90 мл" in destination else
                        f"""<main></main><script>setTimeout(() => document.querySelector('main').innerHTML =
                            '<a href="{URL}">Yves Saint Laurent Libre</a>', 1400)</script>""")
                request.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

            context.route("**/*", route)
            return context, []

    access = SearchAccess({})
    assert competitors.search_links(browser, access, "letu", LIBRE) == [URL]
    assert any("90 мл" in request for request in access.requests)
    assert any(request.endswith("Yves Saint Laurent Libre") for request in access.requests)


def test_goldapple_finds_delayed_public_header_search(browser):
    url = "https://goldapple.ru/123456789-libre"
    html = f"""
      <header></header><main><a href="{url}">Yves Saint Laurent Libre</a></main>
      <script>setTimeout(() => {{
        const button = document.createElement('button');
        button.title = 'Поиск по каталогу';
        button.textContent = 'Найти';
        button.onclick = () => {{
          const input = document.createElement('input');
          input.setAttribute('aria-label', 'Найти');
          button.replaceWith(input);
        }};
        document.querySelector('header').append(button);
      }}, 700);</script>
    """
    assert competitors.search_links(browser, FixtureAccess({"goldapple.ru": html}), "goldapple", LIBRE) == [url]


def test_store_title_descriptors_do_not_change_edition():
    assert competitors.product_name("Женская парфюмерная вода Libre, 90 мл", LIBRE.brand) == "libre"
    assert competitors.product_name("YSL Libre EDP 90 мл", LIBRE.brand) == "libre"
    assert competitors.product_name("Libre Intense EDP 90 мл", LIBRE.brand) == "libre intense"
    assert competitors.product_name("CREED Парфюмерная вода Green Irish Tweed, 100", "Creed") == "green irish tweed"
    assert competitors.product_name("Carolina Herrera 212", "Carolina Herrera") == "212"


def test_letu_visible_marketplace_prices_are_diagnostic_not_confirmed(browser):
    from agents.competitor_diagnostics import resource_summary

    product = Product("Creed", "Green Irish Tweed", "EDP", "100", "retail")
    url = "https://www.letu.ru/product/creed-green-irish-tweed/170100853/sku/190700489"
    html = """<main><article data-testid="product-card">
      <h1>CREED Парфюмерная вода Green Irish Tweed, 100</h1>
      <span itemprop="brand">Creed</span><span itemprop="packaging">Флакон в коробке</span>
      <div data-testid="volume-group"><button aria-pressed="true">100 мл</button></div>
      <div>Цена для новых клиентов</div><div>65 910 ₽</div>
      <strong itemprop="price">67 948 ₽</strong><s>79 939 ₽</s>
      <button>В корзину</button><div>Продавец</div><div itemprop="seller">БОРДЕРЛЕСС ПОРТАЛ КО ЛЛС</div>
      </article></main>"""
    access = FixtureAccess({url: html, "letu.ru": html})
    diagnostics = []
    quote, exact, variant, detail = competitors.rendered_card(
        browser, access, product, "letu", url, diagnostics=diagnostics,
    )
    assert quote is None and exact
    assert "Продавец магазина" in detail
    assert diagnostics[0]["headings"] == ["CREED Парфюмерная вода Green Irish Tweed, 100"]
    assert diagnostics[0]["price_lines"] == ["65 910 ₽", "67 948 ₽", "79 939 ₽"]
    assert diagnostics[0]["visible_prices"] == [
        {"text": "65 910 ₽", "kind": "special"},
        {"text": "67 948 ₽", "kind": "regular"},
        {"text": "79 939 ₽", "kind": "crossed_out"},
    ]
    assert "БОРДЕРЛЕСС ПОРТАЛ КО ЛЛС" in diagnostics[0]["seller_lines"]
    assert resource_summary([{"url": "https://assets.example/style.css", "method": "GET",
                              "resource_type": "stylesheet", "skipped": "untrusted"}]) == [
        {"method": "GET", "type": "stylesheet", "host": "assets.example", "count": 1}]


def test_large_catalog_writes_review_queue_without_duplicate_products(tmp_path, monkeypatch):
    import json
    from agents.exchange import load

    products = [Product("Example", f"Aroma {index}", "EDP", "100", "retail") for index in range(130)]
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"products": [{"product": product.__dict__} for product in products + products]}))
    output, review, cache, diagnostics = [tmp_path / filename for filename in
                                          ("out.json", "review.json", "cache.json", "diagnostics.json")]
    monkeypatch.setattr("sys.argv", ["competitors", "--catalog", str(catalog), "--output", str(output),
                                    "--review-output", str(review), "--link-cache", str(cache),
                                    "--diagnostics", str(diagnostics), "--http-only"])
    competitors.main()
    assert len(load(output)["observations"]) == 520
    assert load(review) == load(output)
    assert load(diagnostics) == {"version": 1, "agent": "competitors", "rendered_cards": [],
                                 "browser_resources_skipped": []}


def test_published_sku_link_confirms_price_in_new_document(browser):
    initial, selected_url = URL + "/sku/50", URL + "/sku/90"

    class SkuAccess(FixtureAccess):
        def context(self, browser, host, budget):
            context = browser.new_context()

            def route(request):
                is_selected = request.request.url == selected_url
                price = "9 000 ₽" if is_selected else "5 000 ₽"
                html = f"""
                  <article><h1>Женская парфюмерная вода Libre</h1>
                  <span itemprop="brand">Yves Saint Laurent</span>
                  <span itemprop="packaging">Флакон в коробке</span>
                  <div data-testid="volume-options">
                  <a href="{initial}" aria-selected="{str(not is_selected).lower()}">50 мл</a>
                  <a href="{selected_url}" aria-selected="{str(is_selected).lower()}">90 мл</a>
                  </div><span class="price-block__price-current">{price}</span>
                  <span itemprop="seller">Лэтуаль</span></article>
                """
                self.requests.append(request.request.url)
                request.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

            context.route("**/*", route)
            return context, []

    access = SkuAccess({initial: document("Libre EDP 50 ml", brand=LIBRE.brand)})
    row = competitors.collect(LIBRE, "letu", initial, access, "guarded_public",
                              render=lambda *args: competitors.rendered_card(browser, access, *args))
    assert row["status"] == "confirmed" and row["price_rub"] == "9000.0"
    assert access.requests[-2:] == [initial, selected_url]


def test_randewoo_rejects_search_redirect_without_request(monkeypatch):
    access = competitors.CompetitorAccess()
    requested = []

    def policy(_, url):
        requested.append(("robots", url))

    def request(_, url):
        requested.append(("GET", url))
        return SimpleNamespace(status_code=302, headers={"Location": "/search?q=libre"})

    monkeypatch.setattr(PublicAccess, "policy", policy)
    monkeypatch.setattr(PublicAccess, "request", request)
    url = "https://randewoo.ru/product/libre"
    row = competitors.collect(LIBRE, "randewoo", url, access, "guarded_public")
    assert row["status"] == "blocked"
    assert requested == [("robots", url), ("GET", url)]
