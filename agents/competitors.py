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
from playwright.sync_api import Error, TimeoutError as BrowserTimeout, sync_playwright

from browser_access import AccessError, PublicAccess, protection
from discover_perfumes import SEARCHES, is_product, search_goldapple
from monitor_core import CONCENTRATIONS, concentrations, extract_card, norm, scent_name, volume_values

from . import competitor_dom, competitor_diagnostics
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


def guard_randewoo_search(url):
    parts = urlsplit(url)
    if (parts.hostname in {"randewoo.ru", "www.randewoo.ru"}
            and unquote(parts.path).rstrip("/").casefold() == "/search"):
        raise AccessError("blocked", "Randewoo: /search отключён; используйте прямую карточку")


class CompetitorAccess(PublicAccess):
    def policy(self, url):
        guard_randewoo_search(url)
        super().policy(url)

    def request(self, url):
        host = urlsplit(url).hostname or ""
        for domain in STORES.values():
            if host == domain or host.endswith("." + domain):
                for origin, error in self.blocked.items():
                    blocked_host = urlsplit(origin).hostname or ""
                    if blocked_host == domain or blocked_host.endswith("." + domain):
                        raise error
                break
        response = super().request(url)
        if response.status_code == 503:
            error = AccessError("blocked", "HTTP 503: магазин временно недоступен; повторные запросы остановлены")
            self.blocked[self.origin(url)] = error
            raise error
        return response


def fetch_card(access, source, url):
    seen = set()
    for _ in range(5):
        guard_randewoo_search(url)
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
    if source == "randewoo":
        raise AccessError("no_card", "Randewoo: используйте разрешённую прямую ссылку; поиск /search отключён")
    domain = STORES[source]
    # The existing context guards every network request, including API calls and redirects.
    context, errors = access.context(browser, domain, budget=65)
    try:
        context.route_web_socket("**/*", lambda socket: socket.close())
        page = context.new_page()
        query = search_query(product)
        try:
            def run(query):
                if source == "goldapple":
                    return search_goldapple(page, query, errors)
                return catalogue_search(page, source, query, errors)

            links = run(query)
            words = set(scent_name(product.name).split())
            if not blocked_store(access, source) and not any(
                words <= set(norm(link["text"] + " " + unquote(urlsplit(link["url"]).path)).split())
                for link in links
            ):
                links += run(f"{product.brand} {product.name}")
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


def catalogue_search(page, source, query, errors):
    destination = SEARCHES[source] + quote(query)
    response = page.goto(destination, wait_until="commit", timeout=20000)
    if errors:
        raise errors[0]
    if response is None or response.status >= 400 or not urlsplit(page.url).path.startswith("/search"):
        raise AccessError("unavailable", "Страница поиска недоступна или перенаправлена")
    results = page.locator('main a[href*="/product/"]:visible, main a[href*="/products/"]:visible, '
                           'a[href*="/product/"]:visible, a[href*="/products/"]:visible')
    empty = page.get_by_text(re.compile(r"ничего не найдено|товары не найдены|нет результатов", re.I))
    try:
        results.or_(empty).first.wait_for(state="visible", timeout=8500)
    except Error:
        if errors:
            raise errors[0]
        # A partially loaded page may still expose public links, even if styles or images failed.
    return results.evaluate_all("els=>els.map(a=>({url:a.href,text:a.innerText||''}))")


def rendered_card(browser, access, product, source, url, diagnostics=None):
    context, errors = access.context(browser, STORES[source], budget=55)
    page = None
    failure = ""
    navigation_status = None
    navigation_timeout = False
    try:
        context.route_web_socket("**/*", lambda socket: socket.close())
        page = context.new_page()

        def check():
            try:
                competitor_dom.check_page(page, errors, lambda: blocked_store(access, source))
            except AccessError as exc:
                if exc.status == "blocked" and exc not in errors:
                    access.blocked[access.origin(url)] = exc
                raise

        try:
            try:
                response = page.goto(url, wait_until="commit", timeout=20000)
                navigation_status = response.status if response else None
            except BrowserTimeout:
                navigation_timeout = True
                failure = "TimeoutError: Chromium не подтвердил завершение навигации"
            check()
            if navigation_timeout and page.url != url:
                return None, False, "", "Тайм-аут навигации: адрес запрошенной карточки не открылся"
            check_url(source, page.url)
            if navigation_status is not None and navigation_status >= 400:
                raise AccessError("unavailable", f"Карточка недоступна: HTTP {navigation_status}")
            if not current_offer_dates(page.content()):
                return None, False, "", "Срок действия предложения истёк или указан некорректно"
            result = competitor_dom.offer(page, product, source, title_name=product_name,
                                          package_patterns=PACKAGE, cologne=COLOGNE, check=check)
            check()
            if navigation_timeout and not result[0]:
                quote, exact, variant, reason = result
                return quote, exact, variant, "Тайм-аут навигации: " + reason
            if result[0] and not current_offer_dates(page.content()):
                return None, result[1], result[2], "Срок действия цены выбранного варианта истёк или указан некорректно"
            return result
        except Error as exc:
            if not navigation_timeout:
                failure = type(exc).__name__ + ": ошибка Chromium при проверке карточки"
            try:
                check()
            except Error:
                pass
            except AccessError as restriction:
                failure = restriction.status + ": " + str(restriction)[:160]
                raise
            reason = "Chromium не дождался основного блока карточки или выбранного варианта"
            if navigation_timeout:
                reason = "Тайм-аут навигации: основной блок карточки не подтвердился"
            return None, False, "", reason
        except AccessError as exc:
            failure = exc.status + ": " + str(exc)[:160]
            raise
        except ValueError:
            failure = "InvalidProductURL"
            return None, False, "", "Карточка перенаправлена на другой домен или неподтверждённый URL"
    finally:
        if diagnostics is not None:
            snapshot = competitor_diagnostics.page_snapshot(page, product, source)
            snapshot["error"] = failure
            snapshot["navigation_status"] = navigation_status
            if navigation_timeout:
                snapshot["navigation_timeout"] = True
            diagnostics.append(snapshot)
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
    # Some merchants render a selected 100 ml SKU as "Name, 100" rather than
    # "Name, 100 мл". The comma distinguishes this from names such as "212".
    title = re.sub(r",\s*\d{1,4}(?:[.,]\d{1,2})?\s*$", "", title)
    title = re.sub(
        r"\b(?:женская|мужская|унисекс)\s+(?=(?:парфюмерная|парфюмированная|туалетная)\s+вода)",
        "", title, flags=re.I,
    )
    title = re.sub(r"\s*,?\s*для\s+(?:женщин|мужчин)\s*$", "", title, flags=re.I)
    for _, pattern in CONCENTRATIONS:
        title = re.sub(pattern, " ", title, flags=re.I)
    for pattern in (COLOGNE, VOLUME, *PACKAGE.values()):
        title = re.sub(pattern, " ", title, flags=re.I)
    title = normalized(title).strip(" ,–—")
    title = title.removeprefix(normalized(brand) + " ")
    parts = normalized(brand).split()
    if len(parts) >= 3:
        initials = "".join(part[0] for part in parts)
        title = title.removeprefix(initials + " ")
    return title.strip(" ,–—")


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


def inspect(product, source, url, access, *, saved, render=None):
    try:
        final_url, html = fetch_card(access, source, url)
    except AccessError as exc:
        return observation(product, source, exc.status, url=url, reason=str(exc)), False
    except ValueError:
        return observation(product, source, "unavailable", url=url,
                           reason="Редирект вышел за пределы разрешённого URL"), False
    verdict, title = identity(product, html, final_url)
    if verdict == "mismatch" and not render:
        return observation(product, source, "no_card", url=final_url,
                           reason="Карточка относится к другому товару или варианту"), False
    fresh = current_offer_dates(html)
    reason = "Бренд, название, концентрация, объём или комплектация не подтверждены в исходном HTML"
    if not fresh:
        reason = "Срок действия предложения истёк или указан некорректно"
    elif verdict == "exact" and product.package == "retail" and not product.configuration:
        query = f"{product.brand} {product.name} {product.concentration}"
        card = extract_card(html, final_url, source, query, product.volume_ml)
        if (card["confirmed"] and card["price_source"] == "Product/Offer JSON-LD"
                and card["third_party"] is False):
            return observation(product, source, "confirmed", url=final_url, price=card["price_rub"],
                               stock=stock(card["availability"]), variant=title, evidence="automatic",
                               reason="Публичная цена точного варианта из Product/Offer JSON-LD"), True
        reason = card["reason"] or "В структурированных данных нет однозначной цены продавца магазина"
    same_base = any(
        product_name(str(item.get("name", "")), product.brand) == normalized(product.name)
        for item in product_objects(html)
    )
    if render and fresh and (verdict != "mismatch" or same_base):
        try:
            quote, exact, variant, detail = render(product, source, final_url)
        except AccessError as exc:
            return observation(product, source, exc.status, url=final_url, reason=str(exc)), False
        if quote:
            price, availability = quote
            return observation(product, source, "confirmed", url=final_url, price=price, stock=availability,
                               variant=variant, evidence="automatic", reason=detail), True
        if (verdict == "mismatch" and not exact) or (verdict == "unverified" and not saved and not variant):
            return observation(product, source, "no_card", url=final_url, reason=detail), False
        return observation(product, source, "manual_review", url=final_url, variant=variant or title,
                           reason=detail), exact or verdict == "exact"
    if verdict == "mismatch" or (verdict == "unverified" and not saved):
        return observation(product, source, "no_card", url=final_url, reason=reason), False
    return observation(product, source, "manual_review", url=final_url, variant=title,
                       reason=reason), verdict == "exact"


def blocked_store(access, source):
    domain = STORES[source]
    hosts = (urlsplit(origin).hostname or "" for origin in access.blocked)
    return any(host == domain or host.endswith("." + domain) for host in hosts)


def collect(product, source, url, access, policy, *, saved_url=None, search=None, remember=None, render=None):
    if policy != "guarded_public":
        return observation(product, source, "blocked", url=url or "", reason="Автоматический доступ отключён политикой")
    if blocked_store(access, source):
        return observation(product, source, "blocked", url=saved_url or url or "",
                           reason="Доступ остановлен после ограничения магазина; проверьте карточку вручную")
    checked = set()
    review = None
    failure = None

    def attempt(link, saved):
        nonlocal review, failure
        check_url(source, link)
        checked.add(link)
        row, exact = inspect(product, source, link, access, saved=saved, render=render)
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
    if search and source != "randewoo" and not blocked_store(access, source):
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
    reason = ("Randewoo: добавьте разрешённую прямую ссылку или ручное подтверждение; /search отключён"
              if source == "randewoo" else "Точная карточка не найдена")
    return review or failure or observation(product, source, "no_card", reason=reason)


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
    parser.add_argument("--review-output", type=Path, help="Отдельная очередь карточек на ручную проверку")
    parser.add_argument("--link-cache", type=Path, default=Path("local_data/competitor-links.json"),
                        help="Локальный JSON-кэш подтверждённых ссылок (без цен)")
    parser.add_argument("--browser-executable", type=Path)
    parser.add_argument("--diagnostics", type=Path,
                        help="Записать локальный отчёт о загрузке страниц и пропущенных ресурсах (без cookies/HTML)")
    parser.add_argument("--no-search", action="store_true", help="Проверять только сохранённые ссылки")
    parser.add_argument("--http-only", action="store_true", help="Проверять сохранённые ссылки без Chromium")
    args = parser.parse_args()
    paths = [Path(path).resolve() for path in (args.catalog, args.output, args.confirmations,
             args.link_cache, args.review_output, args.diagnostics) if path]
    if len(set(paths)) != len(paths):
        parser.error("Каталог, результат, ручные подтверждения и кэш должны быть разными файлами")

    manual = confirmations(args.confirmations, "competitors")
    policies = load(POLICY)
    links = load(args.link_cache) if args.link_cache.exists() else {}
    if not isinstance(links, dict):
        parser.error("Некорректный кэш ссылок")
    seeds = seed_links()
    initial = links.copy()
    access = CompetitorAccess(verified_transport=True)
    diagnostics = []
    browser = None
    browser_error = None
    with ExitStack() as stack:
        stack.callback(access.session.close)

        def chromium():
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
            return browser

        def search(source, product):
            return search_links(chromium(), access, source, product)

        def render(product, source, url):
            args_for_card = (chromium(), access, product, source, url)
            if args.diagnostics:
                return rendered_card(*args_for_card, diagnostics=diagnostics)
            return rendered_card(*args_for_card)

        jobs = {}
        for product, urls in targets(args.catalog):
            for source in STORES:
                key = cache_key(product, source)
                supplied = urls.get(source)
                if key not in jobs or (supplied and not jobs[key][2]):
                    jobs[key] = (product, source, supplied)
        rows = [
            collect(product, source, supplied or seeds.get(key), access,
                    policies.get(source, {}).get("access_policy"), saved_url=links.get(key),
                    search=None if args.no_search or args.http_only else search,
                    remember=lambda url: links.__setitem__(key, url),
                    render=None if args.http_only else render)
            for key, (product, source, supplied) in jobs.items()
        ]
    save(args.output, "competitors", rows + manual)
    if args.review_output:
        save(args.review_output, "competitors", [row for row in rows if row["status"] != "confirmed"])
    if links != initial:
        write_cache(args.link_cache, links)
    if args.diagnostics:
        write_cache(args.diagnostics, {
            "version": 1, "agent": "competitors",
            "rendered_cards": diagnostics,
            "service_workers": access.service_workers,
            "browser_resources_skipped": competitor_diagnostics.resource_summary(access.events),
        })
        print(f"Диагностика записана: {args.diagnostics}")


if __name__ == "__main__":
    main()
