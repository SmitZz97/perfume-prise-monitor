"""Find and verify competitor cards using each merchant's public search."""
import argparse
from collections import defaultdict
from contextlib import ExitStack
from datetime import date, datetime
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import Error, sync_playwright

from browser_access import AccessError, PublicAccess, protection
from discover_perfumes import SEARCHES, is_product, search_goldapple
from monitor_core import CONCENTRATIONS, concentrations, extract_card, norm, scent_name, volume_values

from .exchange import Product, STORES, check_url, confirmations, load, normalized, number, observation, save, stock, targets

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "store_policies.json"
SEEDS = ROOT / "competitor_link_seeds.json"
MAX_CARDS = 8
VOLUME = r"(?<![\d.,])\d+(?:[.,]\d+)?\s*(?:мл|ml)\b"
COLOGNE = r"\beau\s+de\s+cologne\b|\bedc\b|\bодеколон\b"
PACKAGE = {
    "retail": r"\b(?:retail(?: box)?|флакон в коробке|фирменная упаковка|обычный флакон)\b",
    "tester": r"\b(?:tester|testeur|тестер)\b",
    "set": r"\b(?:gift set|set|набор|комплект)\b",
    "sample": r"\b(?:sample|пробник|отливант|decant)\b",
    "refill": r"\b(?:refill|сменный блок|рефил|рефилл)\b",
}
PACKAGE_QUERY = {"tester": "тестер", "set": "набор", "sample": "пробник", "refill": "refill"}


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


def search_query(product):
    return " ".join(filter(None, (product.brand, product.name, product.concentration,
                                  f"{number(product.volume_ml):f} мл", PACKAGE_QUERY.get(product.package),
                                  product.configuration)))


def search_links(browser, access, source, product):
    domain = STORES[source]
    # The existing context guards every network request, including API calls and redirects.
    context, errors = access.context(browser, domain, budget=40)
    try:
        context.route_web_socket("**/*", lambda socket: socket.close())
        page = context.new_page()
        query = search_query(product)
        try:
            if source == "goldapple":
                links = search_goldapple(page, query, errors)
            else:
                response = page.goto(SEARCHES[source] + quote(query), wait_until="domcontentloaded", timeout=20000)
                if errors:
                    raise errors[0]
                if response is None or response.status >= 400 or not urlsplit(page.url).path.startswith("/search"):
                    raise AccessError("unavailable", "Страница поиска недоступна")
                page.wait_for_timeout(1200)
                links = page.locator("a[href]").evaluate_all(
                    "els=>els.map(a=>({url:a.href,text:a.innerText||''}))"
                )
            if errors:
                raise errors[0]
            if blocked_store(access, source):
                raise AccessError("blocked", "Магазин ограничил автоматический доступ")
            why = protection(page.content())
            if why:
                raise AccessError("blocked", why)
        except Error as exc:
            if errors:
                raise errors[0] from exc
            raise AccessError("unavailable", "Поиск не завершился в Chromium") from exc
        wanted = set(scent_name(product.name).split())
        found = {}
        for item in links:
            parts = urlsplit(item["url"])
            if not is_product(source, parts.path):
                continue
            url = urlunsplit(parts._replace(query="", fragment=""))
            try:
                check_url(source, url)
            except ValueError:
                continue
            terms = set(norm(item["text"] + " " + unquote(parts.path)).split())
            # Matching links first. A bounded remainder covers numeric URLs and image-only links.
            found[url] = max(found.get(url, 0), len(wanted & terms))
        if not found and not re.search(
            r"ничего не найдено|товары не найдены|по вашему запросу ничего|нет результатов|no results",
            normalized(BeautifulSoup(page.content(), "html.parser").get_text(" ", strip=True)),
        ):
            raise AccessError("unavailable", "Поиск не показал карточки или явное сообщение об отсутствии результатов")
        return sorted(found, key=found.get, reverse=True)[:MAX_CARDS]
    finally:
        context.close()


def product_objects(html):
    def walk(value):
        if isinstance(value, list):
            for entry in value:
                yield from walk(entry)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if "Product" in (types if isinstance(types, list) else [types]):
                yield value
            for key in ("@graph", "mainEntity"):
                yield from walk(value.get(key))

    for script in BeautifulSoup(html, "html.parser").select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(script.get_text()))
        except (ValueError, TypeError):
            continue


def product_name(title, brand):
    for _, pattern in CONCENTRATIONS:
        title = re.sub(pattern, " ", title, flags=re.I)
    for pattern in (COLOGNE, VOLUME, *PACKAGE.values()):
        title = re.sub(pattern, " ", title, flags=re.I)
    title = normalized(title).strip(" ,–—")
    return title.removeprefix(normalized(brand) + " ").strip(" ,–—")


def identity(product, html, url):
    """Return an identity verdict independent of price and availability."""
    verdicts = []
    other_product = False
    for item in product_objects(html):
        if item.get("url") and urljoin(url, str(item["url"])).rstrip("/") != url.rstrip("/"):
            continue
        title = str(item.get("name", ""))
        brand = item.get("brand") or ""
        brand = str(brand.get("name", "") if isinstance(brand, dict) else brand)
        if product_name(title, brand or product.brand) != normalized(product.name):
            other_product = other_product or bool(title and brand)
            continue
        if brand and normalized(brand) != normalized(product.brand):
            verdicts.append(("mismatch", title))
            continue
        properties = item.get("additionalProperty", [])
        if isinstance(properties, dict):
            properties = [properties]
        if not isinstance(properties, list):
            properties = []
        attrs = defaultdict(list)
        for prop in properties:
            if isinstance(prop, dict):
                attrs[norm(prop.get("name", ""))].append(str(prop.get("value", "")))
        size = item.get("size", "")
        size = size.get("name", "") if isinstance(size, dict) else size
        detail = " ".join([title, str(size), *(value for key in (
            "объем", "объем мл", "volume", "size", "концентрация", "concentration") for value in attrs[key])])
        conc = concentrations(detail)
        if re.search(COLOGNE, detail, re.I):
            conc.add("EDC")
        volumes = volume_values(detail)
        package_text = " ".join([title, *(value for key in (
            "комплектация", "упаковка", "package", "packaging", "тип продукта") for value in attrs[key])])
        packages = {kind for kind, pattern in PACKAGE.items() if re.search(pattern, package_text, re.I)}
        configurations = {normalized(value) for value in attrs["комплектация"] + attrs["configuration"]}
        if item.get("hasVariant"):
            verdicts.append(("uncertain", title))
        elif ((conc and product.concentration not in conc)
              or (volumes and number(product.volume_ml) not in volumes)
              or (packages and product.package not in packages)):
            verdicts.append(("mismatch", title))
        elif (brand and conc == {product.concentration} and volumes == {number(product.volume_ml)}
              and packages == {product.package}
              and (not product.configuration or configurations == {normalized(product.configuration)})):
            verdicts.append(("exact", title))
        else:
            verdicts.append(("uncertain", title))
    if len(verdicts) == 1:
        return verdicts[0]
    if verdicts:
        return "uncertain", verdicts[0][1]
    if other_product:
        return "mismatch", ""
    soup = BeautifulSoup(html, "html.parser")
    for heading in soup.select("main h1, article h1, h1[itemprop='name']"):
        title = heading.get_text(" ", strip=True)
        if product_name(title, product.brand) == normalized(product.name):
            return "uncertain", title
    return "unverified", ""


def current_offer_dates(html):
    """The legacy parser compares dates; also reject expired timestamps and malformed dates."""
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    for item in product_objects(html):
        offers = item.get("offers", [])
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict) or not offer.get("priceValidUntil"):
                continue
            expiry = str(offer["priceValidUntil"])
            try:
                if len(expiry) == 10:
                    if date.fromisoformat(expiry) < now.date():
                        return False
                else:
                    end = datetime.fromisoformat(expiry)
                    if end.utcoffset() is None or end <= now:
                        return False
            except ValueError:
                return False
    return True


def inspect(product, source, url, access, *, saved):
    try:
        final_url, html = fetch_card(access, source, url)
    except AccessError as exc:
        return observation(product, source, exc.status, url=url, reason=str(exc)), False
    except ValueError:
        return observation(product, source, "unavailable", url=url,
                           reason="Редирект вышел за пределы разрешённого URL"), False
    verdict, title = identity(product, html, final_url)
    if verdict == "mismatch" or (verdict == "unverified" and not saved):
        return observation(product, source, "no_card", url=final_url,
                           reason="Точное соответствие карточки не установлено"), False
    if (verdict == "exact" and product.package == "retail" and not product.configuration
            and current_offer_dates(html)):
        query = f"{product.brand} {product.name} {product.concentration}"
        card = extract_card(html, final_url, source, query, product.volume_ml)
        if (card["confirmed"] and card["price_source"] == "Product/Offer JSON-LD"
                and card["third_party"] is False):
            return observation(product, source, "confirmed", url=final_url, price=card["price_rub"],
                               stock=stock(card["availability"]), variant=title, evidence="automatic"), True
    return observation(product, source, "manual_review", url=final_url, variant=title,
                       reason="Проверьте точный вариант, продавца и публичную цену"), verdict == "exact"


def blocked_store(access, source):
    domain = STORES[source]
    hosts = (urlsplit(origin).hostname or "" for origin in access.blocked)
    return any(host == domain or host.endswith("." + domain) for host in hosts)


def collect(product, source, url, access, policy, *, saved_url=None, search=None, remember=None):
    if policy != "guarded_public":
        return observation(product, source, "blocked", url=url or "", reason="Автоматический доступ отключён политикой")
    checked = set()
    review = None
    failure = None

    def attempt(link, saved):
        nonlocal review, failure
        check_url(source, link)
        checked.add(link)
        row, exact = inspect(product, source, link, access, saved=saved)
        if exact or row["status"] == "confirmed":
            if remember:
                remember(row["url"])
            return row
        if row["status"] == "manual_review":
            review = review or row
        elif row["status"] != "no_card":
            failure = row
        # A path disallowed by robots does not necessarily block all merchant paths.
        if row["status"] == "blocked" and blocked_store(access, source):
            return review or row
        return None

    for link in dict.fromkeys(filter(None, (saved_url, url))):
        result = attempt(link, saved=True)
        if result:
            return result
    if search and not blocked_store(access, source):
        try:
            links = search(source, product)
        except AccessError as exc:
            failure = observation(product, source, exc.status, reason=str(exc))
        else:
            for link in links:
                if link not in checked:
                    result = attempt(link, saved=False)
                    if result:
                        return result
    return review or failure or observation(product, source, "no_card", reason="Точная карточка не найдена")


def cache_key(product, source):
    brand, name, concentration, volume, package, configuration = product.key()
    return json.dumps([source, brand, name, concentration, format(volume.normalize(), "f"),
                       package, configuration], ensure_ascii=False)


def seed_links():
    return {cache_key(Product(row["brand"], row["name"], row["concentration"], row["volume"], "retail"),
                      row["store"]): check_url(row["store"], row["url"]) for row in load(SEEDS)}


def write_cache(path, links):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            json.dump(links, file, ensure_ascii=False, indent=2)
            file.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Для поиска установите Chromium: python -m playwright install chromium.",
    )
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirmations")
    parser.add_argument("--link-cache", type=Path, default=Path("local_data/competitor-links.json"),
                        help="Локальный JSON-кэш подтверждённых ссылок (без цен)")
    parser.add_argument("--browser-executable", type=Path)
    parser.add_argument("--no-search", action="store_true", help="Проверять только сохранённые ссылки")
    args = parser.parse_args()
    paths = [Path(path).resolve() for path in (args.catalog, args.output, args.confirmations, args.link_cache) if path]
    if len(set(paths)) != len(paths):
        parser.error("Каталог, результат, ручные подтверждения и кэш должны быть разными файлами")

    manual = confirmations(args.confirmations, "competitors")
    policies = load(POLICY)
    links = load(args.link_cache) if args.link_cache.exists() else {}
    if not isinstance(links, dict):
        parser.error("Некорректный кэш ссылок")
    seeds = seed_links()
    initial = links.copy()
    access = PublicAccess(verified_transport=True)
    browser = None
    browser_error = None
    with ExitStack() as stack:
        stack.callback(access.session.close)

        def search(source, product):
            nonlocal browser, browser_error
            if browser is None and browser_error is None:
                try:
                    playwright = stack.enter_context(sync_playwright())
                    options = {"executable_path": str(args.browser_executable)} if args.browser_executable else {}
                    browser = playwright.chromium.launch(**options)
                    stack.callback(browser.close)
                except Error:
                    browser_error = AccessError("unavailable", "Установите Chromium: python -m playwright install chromium")
            if browser_error:
                raise browser_error
            return search_links(browser, access, source, product)

        rows = []
        for product, urls in targets(args.catalog):
            for source in STORES:
                key = cache_key(product, source)
                rows.append(collect(product, source, urls.get(source) or seeds.get(key), access,
                                    policies.get(source, {}).get("access_policy"), saved_url=links.get(key),
                                    search=None if args.no_search else search,
                                    remember=lambda url: links.__setitem__(key, url)))
    save(args.output, "competitors", rows + manual)
    if links != initial:
        write_cache(args.link_cache, links)


if __name__ == "__main__":
    main()
