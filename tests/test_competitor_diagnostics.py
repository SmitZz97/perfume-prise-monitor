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


def test_route_rejections_record_only_safe_metadata(tmp_path):
    from types import SimpleNamespace
    from time import monotonic

    from browser_access import AccessError, PublicAccess

    access = PublicAccess(tmp_path)
    calls = []

    def policy(url):
        calls.append(url)
        raise AccessError("blocked", "Запрещено robots.txt: /private-path", "robots_disallowed")

    access.policy = policy
    requests = [
        ("POST", "www.letu.ru", "method_not_get"),
        ("POST", "aps.letu.ru", "method_not_get"),
        ("GET", "example.org", "external_host"),
        ("POST", "example.org", "external_host+method_not_get"),
        ("GET", "www.letu.ru", "robots_disallowed"),
    ]
    for method, host, reason in requests:
        url = f"https://{host}/private-path?token=private-token"
        actions = []
        req = SimpleNamespace(url=url, method=method, resource_type="fetch",
                              post_data="private-body", headers={"Cookie": "private-cookie"})
        route = SimpleNamespace(request=req, abort=lambda: actions.append("abort"))
        access._handle_route(route, "letu.ru", monotonic() + 5, [])
        assert actions == ["abort"]
        assert access.events[-1]["skipped"] == reason
    assert len(calls) == 1
    report = resource_summary(access.events)
    assert {row["reason"] for row in report} == {reason for _, _, reason in requests}
    assert "private-" not in str(report)
    assert "private-" not in (tmp_path / "requests.json").read_text(encoding="utf-8")
