"""Checks that diagnostics cannot turn uncertain visible prices into trusted ones."""
from agents import competitors
from agents.competitor_diagnostics import page_snapshot, resource_summary
from agents.exchange import Product


def test_letoile_volume_suffix_and_brand_identity():
    assert competitors.product_name(
        "CREED Парфюмерная вода Green Irish Tweed, 100", "Creed"
    ) == "green irish tweed"
    assert competitors.product_name("Carolina Herrera 212", "Carolina Herrera") == "212"


def test_snapshot_without_a_page_is_safe():
    product = Product("Creed", "Green Irish Tweed", "EDP", "100", "retail")
    report = page_snapshot(None, product, "letu")
    assert report["price_lines"] == []
    assert report["browser_url"] == ""


def test_resource_report_contains_only_host_not_url_or_query():
    requests = [
        {"url": "https://cdn.example/assets/app.js?secret=value", "method": "GET",
         "resource_type": "script", "skipped": "Внешний домен"},
        {"url": "https://cdn.example/assets/app2.js", "method": "GET",
         "resource_type": "script", "skipped": "Внешний домен"},
        {"url": "https://www.letu.ru/api/catalog", "method": "POST",
         "resource_type": "fetch", "skipped": "Запрос не GET"},
        {"url": "https://www.letu.ru/product/a", "method": "GET",
         "resource_type": "document", "http_status": 200},
    ]
    result = resource_summary(requests)
    assert result == [
        {"method": "GET", "type": "script", "host": "cdn.example", "count": 2},
        {"method": "POST", "type": "fetch", "host": "www.letu.ru", "count": 1},
    ]
    assert "secret=value" not in str(result)
