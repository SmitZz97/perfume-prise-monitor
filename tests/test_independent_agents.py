import json
from datetime import timedelta
from subprocess import run
import sys
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook
from browser_access import AccessError
from agents import competitors, exchange, pricing, supplier

PRODUCT = exchange.Product("Example", "Synthetic", "EDP", "100", "retail")
PUBLIC = "https://goldapple.ru/synthetic"
WHOLESALE = "https://application.delice-opt.ru/synthetic"


def test_competitor_checks_robots_before_each_redirect():
    calls = []
    document = {
        "@type": "Product", "name": "Synthetic EDP 100 ml", "brand": {"name": "Example"},
        "additionalProperty": [{"name": "packaging", "value": "retail box"}],
        "offers": {"price": "2000", "priceCurrency": "RUB", "seller": {"name": "Золотое Яблоко"},
                   "availability": "https://schema.org/InStock"},
    }

    def request(url):
        calls.append(("GET", url))
        if url == PUBLIC:
            return SimpleNamespace(status_code=302, headers={"Location": "/exact"})
        html = '<script type="application/ld+json">' + json.dumps(document) + '</script>'
        return SimpleNamespace(status_code=200, headers={"Content-Type": "text/html"}, text=html)

    access = SimpleNamespace(policy=lambda url: calls.append(("robots", url)), request=request)
    row = competitors.collect(PRODUCT, "goldapple", PUBLIC, access, "guarded_public")
    assert row["status"] == "confirmed"
    assert exchange.number(row["price_rub"]) == 2000
    assert calls == [("robots", PUBLIC), ("GET", PUBLIC),
                     ("robots", "https://goldapple.ru/exact"), ("GET", "https://goldapple.ru/exact")]
    document["additionalProperty"] = []
    row = competitors.collect(PRODUCT, "goldapple", PUBLIC, access, "guarded_public")
    assert row["status"] == "manual_review" and row["price_rub"] is None

    def denied(url):
        raise AccessError("blocked", "robots")

    access.policy = denied
    calls.clear()
    assert competitors.collect(PRODUCT, "goldapple", PUBLIC, access, "guarded_public")["status"] == "blocked"
    assert calls == []


def test_supplier_reports_access_and_preserves_variants():
    assert supplier.collect(PRODUCT, WHOLESALE, None)[0]["status"] == "no_access"
    assert supplier.collect(PRODUCT, None, None)[0]["status"] == "no_card"
    offer = {"brand": "Example", "title": "Synthetic п/в", "volume": "100", "volume_unit": "мл",
             "supplier": "Synthetic wholesale", "price": 1000, "package": "retail box",
             "availability": "В наличии"}
    tester = dict(offer, title="Synthetic EDP тестер", package="тестер", price=500)
    confirmed, review = supplier.collect(PRODUCT, WHOLESALE, lambda _: ("confirmed", [offer, tester], ""))
    assert confirmed["availability"] == "in_stock" and confirmed["status"] == "confirmed"
    assert "Synthetic wholesale" in confirmed["variant"]
    assert review["status"] == "manual_review" and review["price_rub"] is None


def confirmed(product, source, url, price, date, stock="in_stock"):
    return exchange.observation(product, source, "confirmed", url=url, price=price, stock=stock,
                                evidence="manual", reason="Синтетическая запись", checked_at=date.isoformat())


def test_pricing_rejects_mismatches_stale_future_and_unknown_stock():
    now = exchange.clock()
    retail = [
        confirmed(PRODUCT, "goldapple", PUBLIC, 200, now - timedelta(hours=24, seconds=1)),
        confirmed(PRODUCT, "randewoo", "https://randewoo.ru/future", 300, now + timedelta(seconds=1)),
        confirmed(exchange.Product("Example", "Synthetic", "EDP", "100", "tester"),
                  "goldapple", PUBLIC, 400, now),
    ]
    costs = dict(shipping=100, other=50, commission_pct=10, tax_pct=5, undercut=100, markup_pct=15)
    purchase = confirmed(PRODUCT, exchange.SUPPLIER, WHOLESALE, 1000, now - timedelta(hours=24))
    quotes = {product.package: quote for product, quote in pricing.quote_rows(retail, [purchase], costs, now)}
    assert quotes["retail"]["minimum"] == 1530
    assert quotes["retail"]["total_cost"] == 1379.5
    assert quotes["retail"]["market"] is None
    assert "recommended" not in quotes["tester"]
    purchase["availability"] = "unknown"
    assert all("minimum" not in quote for _, quote in pricing.quote_rows(retail, [purchase], costs, now))


def test_exchange_rejects_unconfirmed_price():
    row = exchange.observation(PRODUCT, "goldapple", "manual_review", url=PUBLIC, price=10)
    with pytest.raises(ValueError, match="cannot carry"):
        exchange.validate({"version": 1, "agent": "competitors", "observations": [row]}, "competitors")


def test_all_three_cli_commands_produce_excel(tmp_path):
    now = exchange.clock()
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"products": [{"product": PRODUCT.__dict__, "urls": {"supplier": WHOLESALE}}]}))
    retail, wholesale, report = [tmp_path / name for name in ("retail.json", "wholesale.json", "report.xlsx")]

    def cli(agent, *args):
        run([sys.executable, "-m", "agents." + agent, *map(str, args)], check=True)

    for agent, path, row in [
        ("competitors", retail, confirmed(PRODUCT, "goldapple", PUBLIC, 2000, now)),
        ("supplier", wholesale, confirmed(PRODUCT, exchange.SUPPLIER, WHOLESALE, 1000, now)),
    ]:
        manual = tmp_path / f"{agent}-manual.json"
        exchange.save(manual, agent, [row])
        cli(agent, "--catalog", catalog, "--output", path, "--confirmations", manual)
    cli("pricing", "--competitors", retail, "--supplier", wholesale, "--output", report,
        "--shipping", 100, "--other", 50, "--commission-pct", 10, "--tax-pct", 5)
    with report.open("rb") as stream:
        wb = load_workbook(stream)
        results = dict(zip((cell.value for cell in wb.active[1]), (cell.value for cell in wb.active[2])))
        assert results["Минимальная цена, ₽"] == 1530
        assert results["Рекомендованная цена, ₽"] == 1900
        assert results["Себестоимость с расходами, ₽"] == 1435
        assert "no_access" in [row[7] for row in wb["Поставщик"].values]
        wb.close()
