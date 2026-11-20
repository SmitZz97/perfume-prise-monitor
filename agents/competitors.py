"""Independent public-card collection with guarded HTTP transport."""
import argparse
from pathlib import Path
from urllib.parse import urljoin

from browser_access import AccessError, PublicAccess
from monitor_core import extract_card, scent_name

from .exchange import STORES, check_url, confirmations, load, normalized, observation, save, stock, targets

POLICY = Path(__file__).resolve().parents[1] / "store_policies.json"


def fetch_card(access, source, url):
    seen = set()
    for _ in range(5):
        check_url(source, url)
        if url in seen:
            break
        seen.add(url)
        access.policy(url)
        response = access.request(url)
        if response.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, response.headers.get("Location", ""))
            continue
        if response.status_code in (404, 410):
            raise AccessError("no_card", "Карточка не найдена")
        if response.status_code != 200 or "html" not in response.headers.get("Content-Type", "").lower():
            raise AccessError("unavailable", f"Карточка недоступна: HTTP {response.status_code}")
        return url, response.text
    raise AccessError("unavailable", "Редирект карточки не подтверждён")


def collect(product, source, url, access, policy):
    if url:
        check_url(source, url)
    if policy != "guarded_public":
        return observation(product, source, "blocked", url=url or "", reason="Автоматический доступ отключён политикой")
    if not url:
        return observation(product, source, "no_card", reason="Укажите точную ссылку")
    if product.package != "retail" or product.configuration:
        return observation(product, source, "manual_review", url=url, reason="Требуется проверить комплектацию и цену")
    try:
        final_url, html = fetch_card(access, source, url)
    except AccessError as exc:
        return observation(product, source, exc.status, url=url, reason=str(exc))
    except ValueError:
        return observation(product, source, "unavailable", url=url, reason="Редирект вышел за пределы разрешённого URL")
    query = f"{product.brand} {product.name} {product.concentration}"
    card = extract_card(html, final_url, source, query, product.volume_ml)
    name = scent_name(card.get("product_name", ""))
    brand = scent_name(product.brand)
    actual_name = name.removeprefix(brand + " ")
    if (card["confirmed"] and card["price_source"] == "Product/Offer JSON-LD"
            and normalized(card.get("product_brand", "")) == normalized(product.brand)
            and actual_name == scent_name(product.name)
            and card.get("matched_concentration") == product.concentration
            and card.get("retail_packaging")
            and card["third_party"] is False):
        return observation(product, source, "confirmed", url=final_url, price=card["price_rub"],
                           stock=stock(card["availability"]), variant=card["label"], evidence="automatic")
    return observation(product, source, "manual_review", url=final_url,
                       variant=card["label"], reason=card["reason"] or "Проверьте продавца, вариант и публичную цену")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirmations")
    args = parser.parse_args()
    manual = confirmations(args.confirmations, "competitors")
    policies = load(POLICY)
    access = PublicAccess(verified_transport=True)
    try:
        rows = [collect(product, source, urls.get(source), access, policies.get(source, {}).get("access_policy"))
                for product, urls in targets(args.catalog) for source in STORES]
    finally:
        access.session.close()
    save(args.output, "competitors", rows + manual)


if __name__ == "__main__":
    main()
