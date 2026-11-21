"""Search public perfume listings and safely read Randewoo EDP variant prices."""
import argparse
import csv
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from openpyxl import Workbook
from playwright.sync_api import sync_playwright

SEARCHES = {
    'letu': 'https://www.letu.ru/search?q=',
    'randewoo': 'https://randewoo.ru/search?q=',
    'rivegauche': 'https://rivegauche.ru/search?text=',
    'goldapple': 'https://goldapple.ru/',
}
DOMAINS = {'letu': 'letu.ru', 'randewoo': 'randewoo.ru', 'rivegauche': 'rivegauche.ru', 'goldapple': 'goldapple.ru'}
FIELDS = ['store', 'query', 'volume_ml', 'label', 'url', 'price_rub', 'status']
BLOCKED = ('limited', 'edition', 'anniversary', 'extrait', 'extract', 'intense',
           'set', 'gift', 'набор', 'лимит', 'экстракт', 'уценка', 'тестер', 'tester', 'cologne', 'absolu', 'elixir', 'for her', 'for-her')


def tokens(s):
    return [t for t in re.findall(r'[a-zа-яё0-9]+', s.casefold()) if len(t) > 1]


def normalized_words(s):
    return re.findall(r'[a-zа-яё0-9]+', str(s).casefold().replace('ё','е'))


def exact_scent(text, requested):
    wanted = normalized_words(requested)
    got = normalized_words(text)
    if not wanted or not all(x in got for x in wanted):
        return False
    # Aventus != Aventus Cologne; Fleur Narcotique != Fleur Narcotique Extrait.
    editions = ('cologne', 'absolu', 'elixir', 'extrait', 'extract', 'intense',
                'her', 'limited', 'edition', 'tester', 'тестер', 'набор', 'экстракт')
    return not any(x in got and x not in wanted for x in editions)


def match_title(page, name):
    title = page.locator('h1:visible')
    if not title.count():
        return False, 'Не найден заголовок карточки h1'
    heading = ' '.join(title.first.inner_text(timeout=5000).split())
    # Some stores put brand in a separate block; use only nearby product content,
    # but reject different scent variants using the heading itself.
    if not exact_scent(heading, name):
        body = page.locator('body').inner_text(timeout=8000)[:1300]
        if not exact_scent(heading + ' ' + body[:300], name):
            return False, 'Бренд/название или версия аромата не совпадают'
        if any(x in normalized_words(heading) and x not in normalized_words(name)
               for x in ('cologne','absolu','elixir','extrait','intense','her')):
            return False, 'Другая версия аромата в заголовке'
    return True, heading


def edition_allowed(url, name):
    path = urlparse(url).path.replace('-', ' ')
    return exact_scent(path, name)


def randewoo_price(page, name, volume, url):
    """Conservative extraction: only one plain eau de parfum volume row."""
    path = urlparse(url).path.casefold().replace('-', ' ')
    if not edition_allowed(url, name):
        return '', 'Особая версия/набор: автоматическое сравнение запрещено'
    ok, detail = match_title(page, name)
    if not ok:
        return '', detail
    variants = page.locator('li.s-productType')
    candidates = []
    for i in range(min(variants.count(), 80)):
        row = variants.nth(i)
        label = row.get_attribute('data-variant-name') or ''
        visible = row.inner_text(timeout=6000)
        # Prefer variant title to avoid matching the crossed-out price or other volumes.
        title = row.locator('.s-productType__name, .s-productType__title')
        if title.count():
            label = title.first.inner_text(timeout=4000)
        elif not label:
            label = visible.splitlines()[0]
        label = ' '.join(label.casefold().replace('ё', 'е').split())
        normalized = re.sub(r'\s+', '', label)
        if not re.search(rf'(?<!\d){volume}мл(?!\d)', normalized):
            continue
        if not ('парфюмернаявода' in normalized or 'eaudeparfum' in normalized or 'edp' in normalized):
            continue
        if any(word in label for word in BLOCKED):
            continue
        price = row.locator('.s-productType__priceNewValue:visible')
        if price.count() != 1:
            continue
        raw = price.first.inner_text(timeout=5000)
        digits = re.sub(r'\D', '', raw)
        if digits and 100 <= int(digits) <= 1000000:
            candidates.append(int(digits))
    unique = sorted(set(candidates))
    if len(unique) == 1:
        return unique[0], f'Цена варианта EDP {volume} мл; требуется сверка комплектации'
    if len(unique) > 1:
        return '', 'Несколько разных цен подходящих вариантов: нужна проверка'
    return '', f'Вариант EDP {volume} мл с однозначной ценой не обнаружен'



def plain_price(raw):
    # Read exactly one displayed RUB amount, never old crossed-out prices.
    vals = re.findall(r'(\d[\d\s\u00a0\u202f]{0,12})\s*(?:₽|руб\.?)(?!\w)', raw, flags=re.I)
    numbers = [int(re.sub(r'\D', '', x)) for x in vals]
    numbers = [x for x in numbers if 100 <= x <= 1000000]
    if len(numbers) == 1: return numbers[0]
    if len(numbers) > 1: return None
    raw = raw.strip()
    if re.fullmatch(r'[\d\s\u00a0\u202f]+', raw):
        v = int(re.sub(r'\D', '', raw))
        return v if 100 <= v <= 1000000 else None
    return None


def active_volume(page, volume):
    # For Rive Gauche, require the active SKU button to be the exact volume.
    active = page.locator('product-variant button.item.active')
    if active.count() != 1:
        return False
    label = re.sub(r'\s+', '', active.first.inner_text(timeout=4000).casefold())
    return bool(re.fullmatch(rf'{volume}(?:мл|ml)', label))


def rivegauche_price(page, name, volume, url):
    ok, detail = match_title(page, name)
    if not ok: return '', detail
    if not active_volume(page, volume):
        return '', 'Не подтверждён активный вариант требуемого объёма'
    body = page.locator('body').inner_text(timeout=8000).casefold()
    if not any(x in body[:3500] for x in ('парфюмерная вода', 'eau de parfum', ' edp')):
        return '', 'Концентрация EDP не подтверждена'
    if not edition_allowed(url, name):
        return '', 'Особая версия или набор'
    node = page.locator('product-price .price .gold.highlight:visible')
    if node.count() != 1:
        return '', 'Цена на карточке неоднозначна'
    value = plain_price(node.first.inner_text(timeout=4000))
    return (value, f'Цена варианта EDP {volume} мл; требуется сверка комплектации') if value else ('', 'Не удалось однозначно прочитать цену')


def letu_price(page, name, volume, url):
    ok, detail = match_title(page, name)
    if not ok: return '', detail
    if not edition_allowed(url, name):
        return '', 'Особая версия или набор'
    # L'Etoile displays multiple volumes; do not assume the default SKU is 100 ml.
    # Only read when the page has a single explicit volume mention in its SKU controls.
    selected = page.locator('[aria-pressed="true"], [aria-selected="true"]')
    labels=[]
    for i in range(min(selected.count(), 35)):
        text = ' '.join(selected.nth(i).inner_text(timeout=2500).split()).casefold()
        if re.search(r'\d+\s*(?:мл|ml)\b', text): labels.append(text)
    labels=list(dict.fromkeys(labels))
    if len(labels)!=1 or not re.fullmatch(rf'{volume}\s*(?:мл|ml)', labels[0]):
        return '', 'Активный объём не подтверждён: нужна проверка карточки'
    price=page.locator('.price-block__price-current:visible')
    if price.count()!=1: return '', 'Цена неоднозначна'
    value=plain_price(price.first.inner_text(timeout=4000))
    return (value, f'Цена варианта EDP {volume} мл; требуется сверка комплектации') if value else ('', 'Цена не распознана')


SEARCH_INPUTS = ', '.join(x+':visible' for x in (
    'input[type="search"]','input[role="searchbox"]',
    'input[placeholder*="Поиск"]','input[placeholder*="поиск"]',
    'input[placeholder*="Найти"]','input[placeholder*="найти"]',
    'input[placeholder*="Search"]','input[placeholder*="search"]',
    'input[name="q"]','input[name="query"]','input[name="search"]',
    'header input[aria-label*="оиск"]','header input[aria-label*="айти"]',
    'header input[data-testid*="search"]','header input[data-test-id*="search"]'))


def search_goldapple(page,query,errors=None):
    """Use visible live UI; never guess an endpoint or treat UI failure as no stock."""
    from browser_access import AccessError,protection
    errors=errors if errors is not None else []
    def check():
        if errors:raise errors[0]
        why=protection(page.content())
        if why:raise AccessError('blocked',why)
    page.goto('https://goldapple.ru/',wait_until='domcontentloaded',timeout=45000)
    check()
    entry = page.locator(SEARCH_INPUTS).or_(page.get_by_role('button',name=re.compile(r'поиск|найти|search',re.I)))
    try:entry.first.wait_for(state='visible',timeout=5500)
    except Exception:pass
    check()
    field=page.locator(SEARCH_INPUTS)
    if not field.count():
        openers=page.get_by_role('button',name=re.compile(r'поиск|найти|search',re.I))
        candidates=[openers.nth(i) for i in range(min(openers.count(),4))]
        css=page.locator('button[data-test-id*="search"], button[data-testid*="search"], [role="button"][data-testid*="search"], button:has(svg[class*="search"]), [data-test-id="header-search"], header [aria-controls*="search"], header [title*="Поиск"]')
        candidates += [css.nth(i) for i in range(min(css.count(),4))]
        for opener in candidates:
            check()
            try:
                if not opener.is_visible():continue
                opener.click(timeout=2500)
                page.locator(SEARCH_INPUTS).first.wait_for(state='visible',timeout=2500)
            except Exception:
                check()
            field=page.locator(SEARCH_INPUTS)
            if field.count():break
    if not field.count():
        # Only follow a search link actually present on the current merchant page.
        links=page.locator('header a[href*="search"], a[aria-label*="Поиск"][href], a[aria-label*="Search"][href]')
        for i in range(min(links.count(),3)):
            href=links.nth(i).get_attribute('href')
            if not href:continue
            from urllib.parse import urljoin
            destination=urljoin(page.url,href);u=urlparse(destination)
            if u.scheme!='https' or u.hostname not in ('goldapple.ru','www.goldapple.ru'):continue
            page.goto(destination,wait_until='domcontentloaded',timeout=30000);check()
            try:page.locator(SEARCH_INPUTS).first.wait_for(state='visible',timeout=3000)
            except Exception:pass
            field=page.locator(SEARCH_INPUTS)
            if field.count():break
    if not field.count():raise AccessError('unavailable','Не найден видимый поисковый интерфейс: проверены поля, кнопки и опубликованные ссылки; отсутствие товара не установлено')
    field.first.fill(query,timeout=5000);check()
    # React/Vue autocomplete may update before submitting and may have no GET form.
    page.wait_for_timeout(700);check()
    def links_now():return page.locator('a[href]').evaluate_all("els=>els.map(a=>({url:a.href,text:a.innerText||''}))")
    suggestions=links_now()
    try:field.first.press('Enter',timeout=4000);page.wait_for_timeout(900)
    except Exception:
        check()
        if not any(is_product('goldapple',urlparse(x['url']).path) for x in suggestions):raise
    check()
    return suggestions+links_now()


def goldapple_search(page,name):
    # Backward-compatible entry point without stale suggestions from a previous perfume.
    goldapple_search.suggestions=[]
    try:goldapple_search.suggestions=search_goldapple(page,name)
    except Exception as exc:return False,str(exc)
    return True,''


def goldapple_price(page, name, volume, url):
    # Without a verified selected SKU, a page price may refer to another volume.
    ok, detail = match_title(page,name)
    if not ok: return '',detail
    if not edition_allowed(url,name): return '', 'Другая версия аромата'
    selected = page.locator('[aria-selected="true"], [aria-pressed="true"], [data-state="active"]')
    labels=[]
    for i in range(min(selected.count(),30)):
        label=' '.join(selected.nth(i).inner_text(timeout=2500).split()).casefold()
        if re.search(r'\d+\s*(?:мл|ml)',label): labels.append(label)
    labels=list(dict.fromkeys(labels))
    if len(labels)!=1 or not re.fullmatch(rf'{volume}\s*(?:мл|ml)',labels[0]):
        # A single-SKU page can have no volume picker. Require exactly one volume
        # in the product title and no competing volumes in the main product heading.
        heading = page.locator('h1:visible').first.inner_text(timeout=4000)
        sizes = set(re.findall(r'(?<!\d)(\d{1,3})\s*(?:мл|ml)\b', heading.casefold()))
        if sizes != {str(volume)}:
            return '', 'Выбранный объём не подтверждён; цену не используем'
    top=page.locator('body').inner_text(timeout=5000)[:2000].casefold()
    if re.search(r'\b(?:edt|eau de toilette)\b|туалетная вода',top[:550]):
        return '', 'Карточка EDT, а не EDP'
    if not any(x in top for x in ('парфюмерная вода','eau de parfum','edp')):
        return '', 'Концентрация EDP не подтверждена'
    price=page.locator('[data-test-id="price"]:visible')
    if price.count()!=1: return '', 'Цена на карточке неоднозначна'
    value=plain_price(price.first.inner_text(timeout=3000))
    return (value,f'Цена варианта EDP {volume} мл; требуется сверка комплектации') if value else ('','Цена не распознана')


def external_candidates(page, store, name):
    """Fallback to a public search result page; only product links on the exact store domain."""
    q = f'site:{DOMAINS[store]} {name} парфюмерная вода'
    try:
        page.goto('https://www.bing.com/search?q=' + quote(q), wait_until='domcontentloaded', timeout=22000)
        page.wait_for_timeout(800)
        return page.locator('a[href]').evaluate_all('''els => els.map(a => ({url:a.href,text:(a.innerText||'').trim()}))''')
    except Exception:
        return []


def is_product(store, path):
    return (('/product/' in path or '/products/' in path) if store != 'goldapple'
            else bool(re.match(r'^/\d{5,}', path)))

def collect(browser,name,volume,access,config=None,registry=None):
    """One error never suppresses the remaining stores. Search misses are not stock claims."""
    from monitor_core import result,extract_card,scent_name,same_scent
    from browser_access import AccessError,protection
    import json
    config=config or {}
    root=Path(__file__).resolve().parent
    links=[]
    if (root/'products.csv').exists():
        with (root/'products.csv').open(encoding='utf-8-sig',newline='') as f:links=list(csv.DictReader(f))
    output=[]
    for store,base in SEARCHES.items():
        context=None;found=[];errors=[]
        policy=config.get(store,{}).get('access_policy','guarded_public')
        if policy!='guarded_public':
            output.append(result(store,name,volume,status='blocked' if policy=='prohibited' else 'unavailable',reason='Условия сайта не разрешают текущий режим запроса'));continue
        try:
            host=DOMAINS[store];context,errors=access.context(browser,host)
            page=context.new_page()
            for row in links:
                full=(row.get('brand','')+' '+row.get('product','')).strip()
                if row.get('store')==store and same_scent(full,name) and str(row.get('volume_ml'))==str(volume) and row.get('url'):
                    found.append((row['url'],full))
            saved=registry.lookup(name,volume,store) if registry else None
            if saved:found=[(saved['url'],saved['name'])]+[x for x in found if x[0]!=saved['url']]
            query=scent_name(name)
            def search_links():
                errors.clear()
                if store=='goldapple':anchors=search_goldapple(page,query,errors)
                else:
                    page.goto(base+quote(query),wait_until='domcontentloaded',timeout=45000)
                    page.wait_for_timeout(1200)
                    if errors:raise errors[0]
                    anchors=page.locator('a[href]').evaluate_all("els=>els.map(a=>({url:a.href,text:a.innerText||''}))")
                candidates=[]
                for item in anchors:
                    parsed=urlparse(item['url']);candidate=item['text']+' '+parsed.path.replace('-',' ')
                    wanted=scent_name(name).split()
                    if parsed.scheme=='https' and parsed.hostname in (host,'www.'+host) and is_product(store,parsed.path) and all(t in normalized_words(candidate) for t in wanted):
                        if item['url'] not in {x[0] for x in candidates}:candidates.append((item['url'],item['text']))
                return candidates
            searched=not bool(found)
            if searched:found=search_links()
            checked=[]
            def check_links(candidates):
                for url,label in candidates[:3]:
                    parsed=urlparse(url)
                    if parsed.scheme!='https' or parsed.hostname not in (DOMAINS[store],'www.'+DOMAINS[store]):continue
                    errors.clear()
                    try:
                        page.goto(url,wait_until='domcontentloaded',timeout=45000)
                        if errors:raise errors[0]
                        page.wait_for_timeout(1000)
                        why=protection(page.content())
                        if why:raise AccessError('blocked',why)
                        checked.append(extract_card(page.content(),page.url,store,name,volume))
                    except Exception as exc:
                        error=errors[0] if errors else exc
                        if isinstance(error,AccessError):
                            if error.status=='unavailable' and any(code in str(error) for code in ('HTTP 404','HTTP 410')):
                                checked.append(result(store,name,volume,url=url,reason=str(error)));errors.clear();continue
                            raise error
                        checked.append(result(store,name,volume,url=url,reason='Ошибка карточки: '+type(error).__name__))
                    if errors:raise errors[0]
            check_links(found)
            if not any(r['confirmed'] for r in checked) and not searched:
                extra=search_links()
                extra=[x for x in extra if x[0] not in {a[0] for a in found}]
                check_links(extra)
            if not checked:checked=[result(store,name,volume,reason='Поиск не дал проверяемой карточки; отсутствие товара не установлено')]
            good=[r for r in checked if r['confirmed']]
            selected=min(good,key=lambda r:r['price_rub']) if good else checked[0]
            if registry:registry.record_automatic(name,volume,selected)
            output.append(selected)
        except Exception as exc:
            error=errors[0] if errors else exc
            output.append(result(store,name,volume,url=found[0][0] if found else '',status=getattr(error,'status','unavailable'),reason=str(error)[:500] or type(error).__name__))
        finally:
            if context:context.close()
    return output


def launch_browser(p,headless=False,executable=None):
    import os
    from urllib.parse import urlsplit
    kwargs={'headless':headless}
    if executable:kwargs['executable_path']=executable
    proxy_url=os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    if proxy_url:
        proxy=urlsplit(proxy_url);config={'server':f'{proxy.scheme}://{proxy.hostname}:{proxy.port}'}
        if proxy.username:config.update(username=proxy.username,password=proxy.password or '')
        kwargs['proxy']=config
    # Browser traffic is fulfilled via TLS-verified requests preserving the inherited proxy.
    return p.chromium.launch(**kwargs)


def write_candidates(path,output):
    from monitor_core import result
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fields=list(dict.fromkeys(list(result('','','').keys())+[key for row in output for key in row]))
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(output)
    wb=Workbook();ws=wb.active;ws.title='Кандидаты';ws.append(fields)
    for r in output:ws.append([r.get(k) for k in fields])
    ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
    wb.save(path.with_suffix('.xlsx'))


def main():
    from browser_access import PublicAccess
    import json
    ap=argparse.ArgumentParser();ap.add_argument('--name',required=True);ap.add_argument('--volume',type=int,required=True);ap.add_argument('--output',required=True)
    ap.add_argument('--headless',action='store_true');ap.add_argument('--browser-executable');ap.add_argument('--verified-transport',action='store_true',help='TLS-проверяемый requests для ограниченного облачного отображения')
    a=ap.parse_args()
    if not a.name.strip() or a.volume<=0:ap.error('Нужны название и положительный объём')
    from hybrid_store import Registry,hybrid_prices
    registry=Registry();registry.bootstrap(Path(__file__).resolve().parent)
    access=PublicAccess(Path(a.output).parent/'http_diagnostics',verified_transport=a.verified_transport)
    cfgpath=Path(__file__).resolve().parent/'store_policies.json'
    cfg=json.loads(cfgpath.read_text(encoding='utf-8')) if cfgpath.exists() else {}
    try:
        with sync_playwright() as p:
            browser=launch_browser(p,a.headless,a.browser_executable)
            try:rows=collect(browser,a.name,a.volume,access,cfg,registry)
            finally:browser.close()
    except Exception as exc:
        from monitor_core import result
        rows=[result(store,a.name,a.volume,reason='Браузер недоступен: '+type(exc).__name__) for store in DOMAINS]
    rows=hybrid_prices(a.name,a.volume,rows,registry)
    write_candidates(a.output,rows)
    for r in rows:print(r['store'],r['status'],r['price_rub'],r['reason'],flush=True)


if __name__=='__main__':main()
