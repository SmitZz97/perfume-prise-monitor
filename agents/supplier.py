"""Independent supplier collection from a local, dedicated Chromium profile."""
import argparse
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error, sync_playwright
from supplier_compare import assess_offer, normalize_supplier_label, read_offer_rows
from monitor_core import plain_product, scent_name

from .exchange import SUPPLIER, check_url, confirmations, normalized, observation, save, stock, targets

RETAIL_LABELS = {"retail", "retail box", "флакон в коробке", "фирменная упаковка"}


def parse_offer(product, url, offer):
    title = normalize_supplier_label(offer.get("title", ""))
    actual_name = scent_name(title).removeprefix(scent_name(product.brand) + " ")
    assessed = assess_offer(offer, f"{product.brand} {product.name} {product.concentration}", product.volume_ml)
    packaging = normalized(offer.get("package", ""))
    availability = stock(offer.get("availability", ""))
    label = " | ".join(filter(None, (offer.get("supplier"), offer.get("title"),
                                      offer.get("volume"), offer.get("package"))))
    exact_product = (
        assessed["identity_verified"] and assessed["concentration_verified"] and assessed["volume_verified"]
        and normalized(offer.get("brand", "")) == normalized(product.brand)
        and actual_name == scent_name(product.name) and plain_product(title + " " + offer.get("volume", ""))
    )
    if (exact_product and product.package == "retail" and not product.configuration and packaging in RETAIL_LABELS
            and assessed["price"] is not None):
        return observation(product, SUPPLIER, "confirmed", url=url, variant=label,
                           price=assessed["price"], stock=availability, evidence="automatic")
    return observation(product, SUPPLIER, "manual_review", url=url, variant=label,
                       stock=availability, reason=assessed["reason"] or "Проверьте комплектацию и вариант предложения")


def read_card(context, url):
    page = context.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response is None:
            return "unavailable", [], "Карточка не ответила"
        if response.status in (401, 403) or page.locator('input[type="password"]').count():
            return "no_access", [], "Нужен вход в локальном Chromium"
        if response.status == 404:
            return "no_card", [], "Карточка не найдена"
        if response.status >= 400:
            return "unavailable", [], f"HTTP {response.status}"
        if page.url != url:
            return "no_access", [], "Вместо точной карточки открыт другой URL"
        page.get_by_text("Товар у поставщиков", exact=False).first.wait_for(timeout=12000)
        page.locator('[class*="product-detail-table__info"]').first.wait_for(timeout=8000)
        return "confirmed", read_offer_rows(page, url), ""
    except Error:
        if page.locator('input[type="password"]').count() or page.url != url:
            return "no_access", [], "Проверьте вход в локальном Chromium"
        return "no_card", [], "Строки предложения в точной карточке не подтверждены"
    finally:
        page.close()


def collect(product, url, reader):
    if not url:
        return [observation(product, SUPPLIER, "no_card", reason="Укажите точную карточку поставщика")]
    check_url(SUPPLIER, url)
    if reader is None:
        return [observation(product, SUPPLIER, "no_access", url=url, reason="Локальная сессия Chromium недоступна")]
    status, offers, reason = reader(url)
    if not offers:
        return [observation(product, SUPPLIER, status if status != "confirmed" else "no_card",
                            url=url, reason=reason or "В карточке нет строк предложения")]
    return [parse_offer(product, url, offer) for offer in offers]


def browser_route(route):
    request = route.request
    url = urlsplit(request.url)
    if (request.method in {"GET", "HEAD"} and url.scheme == "https" and url.hostname == SUPPLIER
            and url.port in (None, 443) and url.username is None):
        try:
            response = route.fetch(max_redirects=0, timeout=15000)
        except Error:
            route.abort()
        else:
            route.fulfill(response=response)
    else:
        route.abort()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--confirmations")
    args = parser.parse_args()
    manual = confirmations(args.confirmations, "supplier")
    catalog = [(product, urls.get("supplier")) for product, urls in targets(args.catalog)]
    for _, url in catalog:
        if url:
            check_url(SUPPLIER, url)
    rows = None
    if args.profile:
        profile = args.profile.resolve()
        if profile.is_relative_to(Path(__file__).resolve().parents[1]):
            parser.error("Храните профиль Chromium за пределами репозитория")
        if profile.is_dir():
            with sync_playwright() as playwright:
                try:
                    context = playwright.chromium.launch_persistent_context(
                        str(profile), headless=False, locale="ru-RU", service_workers="block",
                        accept_downloads=False,
                    )
                except Error:
                    context = None
                if context is not None:
                    try:
                        context.route_web_socket("**/*", lambda socket: socket.close())
                        context.route("**/*", browser_route)
                        rows = [row for product, url in catalog
                                for row in collect(product, url, lambda link: read_card(context, link))]
                    finally:
                        context.close()
    if rows is None:
        rows = [row for product, url in catalog for row in collect(product, url, None)]
    save(args.output, "supplier", rows + manual)


if __name__ == "__main__":
    main()
