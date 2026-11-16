"""Combine supplier offers with latest competitor candidate report; conservative matching."""
import argparse, csv, re, json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from openpyxl import Workbook, load_workbook
from playwright.sync_api import sync_playwright
from monitor_core import same_scent, identity, scent_name, money as parse_money, concentration, plain_product, volume_values

ROOT = Path(__file__).resolve().parent
LINKS = ROOT / 'supplier_links.json'

def remembered_url(name, volume):
    if not LINKS.exists(): return ''
    try:
        data = json.loads(LINKS.read_text(encoding='utf-8'))
        return data.get(norm(name) + '|' + str(volume), '') or data.get(norm(scent_name(name)) + '|' + str(volume), '')
    except (ValueError, OSError, AttributeError): return ''

def remember_url(name, volume, url):
    # Never replace a damaged/unknown registry with an empty dictionary.
    try:
        data=json.loads(LINKS.read_text(encoding='utf-8')) if LINKS.exists() else {}
    except (ValueError,OSError):return False
    if not isinstance(data,dict):return False
    key=norm(name)+'|'+str(volume)
    if data.get(key)==url:return True
    data[key]=url
    temporary=LINKS.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(LINKS)
    return True


def same_query(candidate, requested):
    return same_scent(candidate,requested) and (not concentration(requested) or concentration(candidate)==concentration(requested))


def norm(s):
    return ' '.join(re.findall(r'[a-zа-яё0-9]+', str(s).casefold().replace('ё','е')))

def terms(s):
    return [t for t in norm(s).split() if len(t)>1 and t not in ('edp','eau','de','parfum','мл','ml')]

def normalize_supplier_label(text):
    # Exact concentration aliases only; no fuzzy matching, no inferred concentration.
    text=str(text)
    for pattern,canonical in (
        (r'(?<!\w)п\s*[./]\s*в\.?(?!\w)|парф\.\s*вода', ' EDP '),
        (r'(?<!\w)т\s*[./]\s*в\.?(?!\w)|туал\.\s*вода', ' EDT '),
    ):text=re.sub(pattern,canonical,text,flags=re.I)
    return text


def assess_offer(item,query,volume):
    title=normalize_supplier_label(item.get('title',''));brand=str(item.get('brand','')).strip()
    text=normalize_supplier_label(item.get('volume',''))
    # Bare numbers become ml only when the offer's explicit unit/header says so.
    if re.fullmatch(r'\d+(?:[.,]\d+)?',text.strip()) and norm(item.get('volume_unit','')) in ('ml','мл'):
        text+=' ml'
    match,conc,vol=identity(title,brand,text,query,volume)
    standard=plain_product(title+' '+text)
    price=parse_money(item.get('price'))
    reasons=[]
    if not match:reasons.append('Точное имя/бренд не совпадают или не подтверждены')
    if not conc:reasons.append('Концентрация: требуется '+(concentration(query) or 'явная')+', получено '+(concentration(title+' '+text) or 'не подтверждена'))
    if not vol:reasons.append('Объём: требуется '+str(volume)+' мл, получено '+(', '.join(str(x) for x in sorted(volume_values(title+' '+text))) or 'нет явного объёма с единицей'))
    if not standard:reasons.append('Другая комплектация: тестер/набор/пробник')
    if price is None:reasons.append(item.get('price_reason') or 'Однозначное поле цены не подтверждено')
    return {'identity_verified':match,'concentration_verified':conc,'volume_verified':vol,
            'confirmed':match and conc and vol and standard and price is not None,
            'price':price,'reason':'; '.join(reasons),
            'observed_concentration':concentration(title+' '+text),'observed_volume':text}


def exact_item(title,query,volume,volume_text):
    item=assess_offer({'title':title,'volume':volume_text,'price':1},query,volume)
    return item['identity_verified'] and item['concentration_verified'] and item['volume_verified'] and plain_product(title+' '+str(volume_text))


def money(text):
    vals=re.findall(r'(\d[\d\s\u00a0\u202f]*(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?)',str(text),re.I)
    amounts={parse_money(x) for x in vals};amounts.discard(None)
    return next(iter(amounts)) if len(amounts)==1 else None


def offer_scope(card):
    # The closest container with exactly one offer info row, never the whole multi-offer table.
    scope=card
    for _ in range(3):
        parent=scope.locator('xpath=..')
        if not parent.count() or parent.locator('[class*="product-detail-table__info"]').count()!=1:break
        scope=parent
        if scope.locator('[class*="product-detail-table__price"], [data-testid="offer-price"]').count():break
    return scope


def column_header(node):
    cell=node.locator('xpath=ancestor-or-self::td[1]')
    if not cell.count():return ''
    table=cell.locator('xpath=ancestor::table[1]')
    if not table.count():return ''
    index=cell.evaluate('el=>el.cellIndex')
    headers=table.locator('thead tr').last.locator('th,td') if table.locator('thead tr').count() else None
    if headers is None or headers.count()<=index:return ''
    if any(x not in (None,'1') for x in headers.evaluate_all("els=>els.map(el=>el.getAttribute('colspan'))")):return ''
    return headers.nth(index).inner_text()


def row_price_details(card):
    scope=offer_scope(card)
    nodes=scope.locator('[class*="product-detail-table__price"]:visible, [data-testid="offer-price"]:visible')
    values=[]
    for i in range(nodes.count()):
        node=nodes.nth(i)
        if node.evaluate("el=>!!el.closest('del,s,[data-old-price]')"):continue
        raw=node.inner_text(timeout=4000).strip();value=money(raw)
        if value is None and re.fullmatch(r'[\d\s\u00a0\u202f]+(?:[.,]\d{1,2})?',raw):
            # A currency-free scalar is only accepted with currency proof in the same field/column.
            currency=node.get_attribute('data-currency') or ''
            header=column_header(node)
            if currency=='RUB' or re.search(r'₽|руб',header,re.I):value=parse_money(raw)
        if value is None:return None,'Поле цены отсутствует/содержит несколько сумм или валюта не подтверждена'
        values.append(value)
    unique=set(values)
    if len(unique)==1:return values[0],''
    return None,('Явное поле цены в строке предложения не найдено' if not values else 'В строке несколько разных цен')


def row_price(card):return row_price_details(card)[0]


def explicit_volume_unit(card):
    node=offer_scope(card).locator('[class*="product-detail-table__volume"]').first
    if not node.count():return ''
    for attr in ('data-unit','data-volume-unit'):
        unit=node.get_attribute(attr)
        if norm(unit or '') in ('ml','мл'):return unit
    header=column_header(node)
    if re.search(r'(объ[её]м|volume).*?(мл|ml)',header,re.I):return 'ml'
    return ''


def read_live(url):
    profile = ROOT/'supplier_browser_profile'
    result=[]
    with sync_playwright() as p:
        context=p.chromium.launch_persistent_context(user_data_dir=str(profile),headless=False,locale='ru-RU')
        try:
            page=context.pages[0] if context.pages else context.new_page()
            page.goto(url,wait_until='domcontentloaded',timeout=60000)
            page.get_by_text('Товар у поставщиков').first.wait_for(timeout=20000)
            page.wait_for_timeout(1800)
            cards=page.locator('[class*="product-detail-table__info"]')
            for i in range(cards.count()):
                card=cards.nth(i);scope=offer_scope(card)
                def part(selector):
                    el=scope.locator(selector)
                    return el.first.inner_text(timeout=5000).strip() if el.count() else ''
                price,price_reason=row_price_details(card)
                result.append({'supplier':part('[class*="product-detail-table__supplier"]'),
                    'title':part('[class*="product-detail-table__title"]'),
                    'brand':part('[class*="product-detail-table__brand"]'),
                    'volume_unit':explicit_volume_unit(card),
                    'volume':part('[class*="product-detail-table__volume"]'),
                    'price':price,'price_reason':price_reason,'url':url})
        finally: context.close()
    return result

def cached_offers():
    path=ROOT/'supplier_prices.xlsx'
    if not path.exists(): return []
    wb=load_workbook(path,read_only=True,data_only=True)
    out=[]
    try:
        ws=wb['Закупочные цены'] if 'Закупочные цены' in wb else wb.active
        for row in list(ws.values)[1:]:
            if len(row)<6: continue
            out.append({'supplier':str(row[1] or ''),'title':str(row[2] or ''),'volume':str(row[3] or ''),'price':row[4],'url':str(row[5] or '')})
    finally: wb.close()
    return out

def latest_candidates(name, volume):
    paths=sorted(ROOT.glob('candidates_*.csv'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in paths:
        with path.open(encoding='utf-8-sig',newline='') as f:
            rows=list(csv.DictReader(f))
        if rows and any(same_query(r.get('query',''), name) and str(r.get('volume_ml',''))==str(volume) for r in rows):
            return rows,path.name
    return [],''

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--name',required=True)
    ap.add_argument('--volume',type=int,required=True)
    ap.add_argument('--supplier-url',default='')
    ap.add_argument('--output',required=True)
    ap.add_argument('--candidates',default='')
    ap.add_argument('--supplier-disabled',action='store_true',help='Явно не обращаться к профилю поставщика')
    a=ap.parse_args()
    if not a.name.strip() or a.volume<=0: ap.error('Нужны аромат и объём')
    supplier_url = a.supplier_url.strip() or remembered_url(a.name, a.volume)
    supplier_error=''
    offers=[]
    if a.supplier_disabled:
        supplier_error='Закупка не проверялась: режим supplier-disabled'
        source=supplier_error
    elif supplier_url:
        parsed=urlparse(supplier_url)
        if parsed.scheme!='https' or parsed.hostname!='application.delice-opt.ru' or not re.fullmatch(r'/catalog/\d+/?',parsed.path):
            supplier_error='Сохранённая ссылка Delice Opt не прошла проверку домена/пути'
        elif not (ROOT/'supplier_browser_profile').is_dir():
            supplier_error='Локальный авторизованный профиль supplier_browser_profile отсутствует; вход не изменялся'
        else:
            try:offers=read_live(supplier_url)
            except Exception as exc:supplier_error='Ошибка текущей карточки Delice Opt: '+type(exc).__name__
        source=supplier_error or 'Delice Opt — текущая карточка'
    else:
        supplier_error='Нет сохранённой ссылки поставщика; сохранённые ранее цены не считаются текущей закупкой'
        source=supplier_error
    valid=[];assessments=[]
    for item in offers:
        check=assess_offer(item,a.name,a.volume);assessments.append((item,check))
        if check['confirmed']:valid.append((check['price'],item))
    if offers and not valid and not supplier_error:
        supplier_error=' | '.join(dict.fromkeys(check['reason'] for _,check in assessments if check['reason']))[:1500]
    if not offers and not supplier_error:supplier_error='Карточка открыта, но строки предложений поставщиков не найдены'
    best=min(valid,key=lambda x:x[0]) if valid else None
    print("ПРОВЕРКА СОХРАНЕНИЯ:", "Закупка найдена =", bool(best), "Ссылка =", repr(supplier_url))
    if best and a.supplier_url:
        remember_url(a.name, a.volume, supplier_url)
    if a.candidates:
        candidate_path=Path(a.candidates)
        if candidate_path.is_file():
            with candidate_path.open(encoding='utf-8-sig',newline='') as f:
                rows=list(csv.DictReader(f))
            candidates_source=candidate_path.name
        else:
            rows,candidates_source=[],''
    else:
        rows,candidates_source=latest_candidates(a.name,a.volume)
    retailers={}
    for r in rows:
        store=r.get('store','')
        if store not in ('randewoo','letu','rivegauche','goldapple'): continue
        price=parse_money(r.get('price_rub'))
        status=r.get('status','')
        from hybrid_store import manual_snapshot_current
        if r.get('source')=='manually':continue  # Re-read the persistent attestation below; CSV is not a new confirmation.
        if not manual_snapshot_current(r.get('source'),r.get('checked_at'),r.get('expires_at')):continue
        if not price or status!='ok' or any(str(r.get(k,'')).lower()!='true' for k in ('confirmed','identity_verified','volume_verified','concentration_verified')):continue
        if not same_query(r.get('query',''),a.name) or str(r.get('volume_ml',''))!=str(a.volume):continue
        if store not in retailers or price<retailers[store][0]: retailers[store]=(price,r)
    # The standalone comparison button must see the same fresh manual attestations as batch.
    from hybrid_store import Registry,hybrid_prices
    from monitor_core import result as make_result,STORES
    registry=Registry(ROOT/'local_data/competitors.sqlite3');registry.bootstrap(ROOT)
    automatic=[]
    for store in STORES:
        if store in retailers:
            price,row=retailers[store]
            entry={**row,'store':store,'price_rub':price,'confirmed':True,'identity_verified':True,'volume_verified':True,'concentration_verified':True}
        else:entry=make_result(store,a.name,a.volume,reason='Автоматическая цена в свежем сравнении не подтверждена')
        automatic.append(entry)
    for entry in hybrid_prices(a.name,a.volume,automatic,registry):
        if entry.get('confirmed') and entry.get('source')=='manually':retailers[entry['store']]=(entry['price_rub'],entry)
    wb=Workbook();ws=wb.active;ws.title='Сравнение'
    ws.append(['Аромат',a.name]);ws.append(['Объём, мл',a.volume]);ws.append(['Дата формирования',datetime.now()]);ws.append(['Источник закупки',source]);ws.append(['Источник конкурентов',candidates_source or 'Подходящий CSV не найден']);ws.append([])
    ws.append(['Источник','Цена, ₽','URL','Статус','Разница, ₽','Наценка к закупке, %','Канал цены','Дата проверки','Действует до'])
    ws.append(['Delice Opt',best[0] if best else None,best[1]['url'] if best else supplier_url,'ok' if best else supplier_error or 'Точное название, концентрация, объём и однозначная цена закупки не подтверждены',None,None])
    for store in ('goldapple','letu','randewoo','rivegauche'):
        entry=retailers.get(store)
        price=entry[0] if entry else None
        r=entry[1] if entry else {}
        diff=price-best[0] if price is not None and best else None
        pct=round(diff/best[0]*100,1) if diff is not None else None
        ws.append([store,price,r.get('url',''),r.get('status','') if entry else '; '.join(dict.fromkeys(x.get('status','') for x in rows if x.get('store')==store and x.get('status')))[:450] or 'Поиск не дал подтверждённой цены',diff,pct,r.get('source'),r.get('checked_at'),r.get('expires_at')])
    ws.append([]);ws.append(['Предложения поставщиков, подходящие по названию и объёму'])
    ws.append(['Поставщик','Цена, ₽','Название','Объём','URL'])
    for price,item in sorted(valid, key=lambda x: x[0]): ws.append([item['supplier'],price,item['title'],item['volume'],item['url']])
    diagnostic=wb.create_sheet('Проверка закупки')
    diagnostic.append(['Запрос','Запрошенный объём','Название строки','Бренд строки','Объём строки','Концентрация строки','Совпало имя','Совпала концентрация','Совпал объём','Цена подтверждена','Причина отказа','URL','Поставщик','Комплектация','Цена строки (не означает совпадение)'])
    for item,check in assessments:
        diagnostic.append([a.name,a.volume,item.get('title',''),item.get('brand',''),check['observed_volume'],check['observed_concentration'],check['identity_verified'],check['concentration_verified'],check['volume_verified'],check['confirmed'],check['reason'],item.get('url',''),item.get('supplier',''),'обычный товар по названию' if plain_product(item.get('title','')) else 'тестер/набор/пробник/уценка',parse_money(item.get('price'))])
    if not assessments:diagnostic.append([a.name,a.volume,'','','','',False,False,False,False,supplier_error,supplier_url])
    diagnostic.freeze_panes='C2'
    ws.freeze_panes='A8'
    for col,width in {'A':30,'B':22,'C':72,'D':55,'E':22,'F':27}.items():ws.column_dimensions[col].width=width
    for row in ws.iter_rows():
        for cell in row:
            if cell.column in (2,5) and isinstance(cell.value,(int,float)):cell.number_format='#,##0 "₽"'
    path=Path(a.output);wb.save(path)
    print('Закупочных совпадений:',len(valid),flush=True)
    if supplier_url and best: print('Ссылка Delice Opt сохранена для повторных запусков.',flush=True)
    print('Закупка:',best[0] if best else 'не подтверждена',flush=True)
    print('Цены конкурентов:',', '.join(f'{k}: {v[0]}' for k,v in retailers.items()) or 'нет подтверждённых',flush=True)
    print('Отчёт:',path,flush=True)

if __name__=='__main__': main()
