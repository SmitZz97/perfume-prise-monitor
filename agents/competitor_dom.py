"""Read a visible, selected offer in the main product card."""
import re
from time import monotonic
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error

from browser_access import AccessError, protection
from monitor_core import SELLERS, concentrations, money, volume_values
from .exchange import normalized, number, stock

BRAND = '[itemprop="brand"], [data-testid="product-brand"], [data-test-id="product-brand"], .product-brand'
PACKAGE = '[data-testid="product-package"], [data-test-id="product-package"], [itemprop="packaging"]'
CONCENTRATION = '[data-testid="product-concentration"], [itemprop="concentration"], .product-concentration'
VOLUME_GROUP = (
    'product-variant, [data-testid*="volume"], [data-test-id*="volume"], '
    '[data-testid*="size"], [role="group"][aria-label*="бъём"]'
)
VOLUME_BUTTON = 'button, [role="option"], [role="radio"], a[href*="/sku/"]'
PRICE = (
    '[itemprop="price"], [data-testid="product-price"], [data-test-id="price"], '
    '.price-block__price-current, product-price .price .gold.highlight, .s-productType__priceNewValue'
)
SELLER = '[itemprop="seller"], [data-testid="seller"], [data-test-id="seller"]'
AVAILABILITY = '[itemprop="availability"], [data-testid="product-availability"]'
CONDITIONAL = re.compile(
    r'по карте|с картой|промокод|для участников|лояльност|персональн|для новых|от\s+\d|'
    r'в месяц|рассроч|сплит|coupon|members? only', re.I
)
RUB_PRICE = re.compile(r'[\d \u00a0\u202f]+(?:[.,]\d{1,2})?\s*(?:₽|руб\.?)', re.I)


def visible_text(scope, selector):
    return scope.locator(selector).filter(visible=True).all_inner_texts()


def value_set(scope, selector):
    return {normalized(text) for text in visible_text(scope, selector) if text.strip()}


def selected(button):
    return button.evaluate("""el => ['aria-selected', 'aria-pressed', 'aria-checked']
        .some(attr => el.getAttribute(attr) === 'true') ||
        ['active', 'selected', 'checked', 'on'].includes(el.dataset.state) ||
        el.classList.contains('active') || el.classList.contains('selected')""", timeout=800)


def find_scope(page):
    heading = page.locator("main h1:visible, article h1:visible, h1[itemprop='name']:visible")
    heading.first.wait_for(state="visible", timeout=6000)
    if heading.count() != 1:
        return None
    primary = heading.locator('xpath=ancestor::*[@itemtype="https://schema.org/Product" or @data-testid="product-card" or self::article or self::main][1]')
    return primary if primary.count() == 1 else None


def prices(scope):
    nodes = scope.locator(PRICE).filter(visible=True)
    quotes = set()
    for node in nodes.all():
        data = node.evaluate("""el => {
            const box = el.closest('product-price, [data-testid="product-price"], .price-block, [data-price-scope]');
            const skuNode = el.closest('[data-sku], [data-sku-id]');
            const sku = skuNode?.dataset.sku || skuNode?.dataset.skuId || '';
            return {text: el.innerText.trim(), context: box?.innerText || el.parentElement?.innerText || '',
                sku, currency: el.dataset.currency || '',
                old: !!el.closest('del,s,[data-old-price],[data-testid*="old-price"],[aria-hidden="true"]'),
                related: !!el.closest('aside,[data-testid*="recommend"],.recommendations,[data-testid*="related"]')};
        }""", timeout=1000)
        if data["old"] or data["related"]:
            continue
        if CONDITIONAL.search(data["context"]):
            return None, "Цена зависит от карты, акции или условий платежа"
        text = data["text"]
        if not (RUB_PRICE.fullmatch(text) or data["currency"] == "RUB" and re.fullmatch(r'[\d\s.,]+', text)):
            return None, "В блоке цены нет одной суммы с подтверждённой валютой RUB"
        amount = money(text)
        if amount is None:
            return None, "Сумма в блоке цены не распознана"
        quotes.add((amount, data["sku"]))
    if len(quotes) != 1:
        return None, "В основном блоке отсутствует единственная публичная цена"
    return quotes.pop(), ""


def offer(page, product, source, *, title_name, package_patterns, cologne, check):
    scope = find_scope(page)
    if scope is None:
        return None, False, "", "Не удалось выделить основной блок карточки"
    heading = scope.locator("h1:visible").inner_text(timeout=1500)
    if title_name(heading, product.brand) != normalized(product.name):
        return None, False, heading, "Название основного товара отличается"
    brands = value_set(scope, BRAND)
    if brands != {normalized(product.brand)}:
        return None, False, heading, "Бренд в основном блоке отсутствует или отличается"
    size = number(product.volume_ml)
    controls = scope.locator(VOLUME_GROUP).locator(VOLUME_BUTTON).filter(visible=True)
    buttons = [(button, button.inner_text(timeout=1000)) for button in controls.all()[:40]]
    named = [(button, text) for button, text in buttons if volume_values(text)]
    # Randewoo exposes independently priced volume rows, with no search or selection click.
    rows = scope.locator("li.s-productType:visible") if source == "randewoo" else None
    navigation = ""
    if rows is not None and rows.count():
        matching = []
        for row in rows.all()[:80]:
            labels = value_set(row, '.s-productType__name, .s-productType__title')
            detail = " ".join(labels)
            if volume_values(detail) == {size} and concentrations(detail) == {product.concentration}:
                matching.append((row, detail))
        if len(matching) != 1:
            return None, False, heading, "Не найдена единственная строка Randewoo с нужной концентрацией и объёмом"
        price_scope, variant = matching[0]
        changed = False
        sku = ""
        selected_button = None
    else:
        matched = [(button, text) for button, text in named if volume_values(text) == {size}]
        if len(matched) > 1 or named and not matched:
            return None, False, heading, "Выбор нужного объёма неоднозначен или недоступен"
        selected_button, variant = matched[0] if matched else (None, heading)
        if not matched and volume_values(heading) != {size}:
            return None, False, heading, "Объём не указан в заголовке или доступных вариантах"
        price_scope = scope
        sku = ""
        changed = selected_button is not None and not selected(selected_button)
        if selected_button is not None:
            sku = (selected_button.get_attribute("data-sku", timeout=1000)
                   or selected_button.get_attribute("data-sku-id", timeout=1000) or "")
            href = selected_button.get_attribute("href", timeout=1000)
            if href and changed:
                current = urlsplit(page.url)
                destination = urlsplit(urljoin(page.url, href))
                if (current.netloc, current.scheme, current.path.split("/sku/")[0]) != (
                    destination.netloc, destination.scheme, destination.path.split("/sku/")[0]
                ) or "/sku/" not in destination.path or destination.query or destination.fragment:
                    return None, False, variant, "Ссылка варианта ведёт за пределы текущей карточки"
                navigation = destination.geturl()
                sku = sku or destination.path.rsplit("/sku/", 1)[-1]
            if changed:
                check()
                if navigation:
                    page.goto(navigation, wait_until="domcontentloaded", timeout=15000)
                else:
                    selected_button.click(timeout=2500)
    deadline = monotonic() + (5 if changed else 2)
    stable = None
    exact = False
    reason = "Не подтверждено обновление цены после переключения объёма"
    while monotonic() < deadline:
        check()
        if selected_button is not None:
            active = [(button, text) for button, text in named if selected(button)]
            if len(active) != 1 or volume_values(active[0][1]) != {size}:
                page.wait_for_timeout(150)
                continue
        current_title = scope.locator("h1:visible").inner_text(timeout=1200)
        if title_name(current_title, product.brand) != normalized(product.name) or value_set(scope, BRAND) != {normalized(product.brand)}:
            return None, False, variant, "После выбора объёма открылась другая карточка"
        detail = " ".join([current_title, variant, *value_set(scope, CONCENTRATION)])
        conc = concentrations(detail)
        if re.search(cologne, detail, re.I):
            conc.add("EDC")
        pack_text = " ".join([current_title, variant, *value_set(scope, PACKAGE)])
        if price_scope is not scope:
            pack_text += " " + " ".join(value_set(price_scope, PACKAGE))
        packs = {kind for kind, pattern in package_patterns.items() if re.search(pattern, pack_text, re.I)}
        if conc != {product.concentration} or packs != {"retail"} or product.package != "retail" or product.configuration:
            return None, False, variant, "Концентрация или комплектация обычного флакона не подтверждена"
        exact = True
        sellers = value_set(price_scope, SELLER) or value_set(scope, SELLER)
        if sellers != {normalized(SELLERS[source])}:
            return None, exact, variant, "Продавец магазина не указан однозначно"
        quote, price_reason = prices(price_scope)
        if quote is None:
            reason = price_reason
        elif quote[1] and sku and quote[1] != sku:
            reason = "Цена относится к прежнему или другому SKU"
        elif quote == stable and (not changed or (sku and quote[1] == sku) or (navigation and page.url == navigation)):
            available = {stock(text) for text in visible_text(price_scope, AVAILABILITY)}
            availability = available.pop() if len(available) == 1 else "unknown"
            return (quote[0], availability), exact, variant, "Публичная цена подтверждена на выбранном варианте"
        else:
            # A changed number without explicit SKU binding can also be an intermediate or
            # unrelated client-side update. Require binding when this run switched the variant.
            stable = quote
            reason = "После переключения цена не связана с артикулом выбранного варианта"
        page.wait_for_timeout(150)
    return None, exact, variant, reason


def check_page(page, errors, blocked):
    if errors:
        raise errors[0]
    if blocked():
        raise AccessError("blocked", "Магазин ограничил доступ; повторные запросы остановлены")
    why = protection(page.content())
    if why:
        raise AccessError("blocked", why)
