"""Strict product identity and public-card extraction. No network or browser state."""
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from datetime import datetime
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

CONCENTRATIONS=[('EXTRAIT',r'extrait(?:\s+de\s+parfum)?|экстракт(?:\s+духов)?'),
 ('EDP',r'eau\s+de\s+parfum|парфюмерная\s+вода|парфюмированная\s+вода|\bedp\b'),
 ('EDT',r'eau\s+de\s+toilette|туалетная\s+вода|\bedt\b'),
 ('PARFUM',r'\bparfum\b|\bдухи\b')]
STORES=('goldapple','letu','randewoo','rivegauche')
SELLERS={'goldapple':'Золотое Яблоко','letu':'Лэтуаль','randewoo':'Randewoo','rivegauche':'Рив Гош'}

def now():return datetime.now(ZoneInfo('Europe/Moscow')).isoformat(timespec='seconds')
def norm(v):
    s=''.join(c for c in unicodedata.normalize('NFKD',str(v).casefold().replace('ё','е')) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[a-zа-я0-9]+',s))
def concentrations(text):
    text=str(text).casefold();out=set()
    for code,pattern in CONCENTRATIONS:
        if re.search(pattern,text,re.I):out.add(code);text=re.sub(pattern,' ',text,flags=re.I)
    return out

def concentration(text):
    cs=concentrations(text)
    return next(iter(cs)) if len(cs)==1 else ''

def scent_name(text):
    text=str(text)
    for _,pattern in CONCENTRATIONS:text=re.sub(pattern,' ',text,flags=re.I)
    text=re.sub(r'(?<!\d)\d+(?:[.,]\d+)?\s*(?:мл|ml)\b',' ',text,flags=re.I)
    text=re.sub(r'для\s+(?:мужчин|женщин)|мужская|женская|унисекс|парфюмерия',' ',text,flags=re.I)
    return norm(text)

def plain_product(text):
    return not re.search(r'\b(?:tester|testeur|тестер|набор|комплект|set|gift|пробник|sample|отливант|уценка)\b',norm(text))

def same_scent(candidate,requested):
    return plain_product(candidate) and scent_name(candidate)==scent_name(requested)

def volume_values(text):
    return {Decimal(x.replace(',','.')) for x in re.findall(r'(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(?:мл|ml)\b',str(text),re.I)}

def money(value):
    if value is None or isinstance(value,bool):return None
    s=str(value).replace('\xa0','').replace('\u202f','').replace(' ','').replace('₽','')
    s=re.sub(r'руб\.?','',s,flags=re.I).replace(',','.')
    try:
        n=Decimal(s)
        return float(n) if n.is_finite() and 0<n<Decimal('100000000') else None
    except InvalidOperation:return None

def result(store,name,volume,**updates):
    row=dict(store=store,query=name,volume_ml=volume,label='',url='',price_rub=None,status='unavailable',reason='',
             found=False,volume_verified=False,concentration_verified=False,identity_verified=False,price_received=False,
             checked_at=now(),seller='',availability='unknown',discount_conditions='',price_source='',third_party=None,
             loyalty_price=None,confirmed=False)
    row.update(updates);return row

def identity(title,brand,detail,name,volume):
    full=title if scent_name(title).startswith(norm(brand)+' ') or not brand else brand+' '+title
    same=same_scent(full,name)
    wanted=concentration(name); got=concentration(title+' '+detail)
    conc=bool(got and (not wanted or wanted==got))
    sizes=volume_values(title+' '+detail)
    vol=sizes=={Decimal(str(volume))}
    return same,conc,vol

def extract_card(html,url,store,name,volume):
    soup=BeautifulSoup(html,'html.parser');products=[]
    def walk(obj):
        if isinstance(obj,list):
            for x in obj:walk(x)
        elif isinstance(obj,dict):
            types=obj.get('@type',[]);types=[types] if isinstance(types,str) else types
            if 'Product' in types:products.append(obj)
            for k in ('@graph','mainEntity'):walk(obj.get(k))
    for node in soup.select('script[type="application/ld+json"]'):
        try:walk(json.loads(node.get_text()))
        except (ValueError,TypeError):pass
    rows=[]
    for obj in products:
        if obj.get('hasVariant'):continue
        if obj.get('url') and str(obj['url']).split('?')[0].rstrip('/')!=url.split('?')[0].rstrip('/'):continue
        title=str(obj.get('name',''));brand=obj.get('brand','');brand=brand.get('name','') if isinstance(brand,dict) else str(brand)
        props=obj.get('additionalProperty',[]);props=props if isinstance(props,list) else [props]
        details=' '.join(str(x.get('value','')) for x in props if isinstance(x,dict) and norm(x.get('name','')) in ('объем','объем мл','концентрация','тип продукта','volume','size','concentration'))
        size=obj.get('size','');size=size.get('name','') if isinstance(size,dict) else str(size)
        detail=details+' '+size
        if not concentration(title+' '+detail):detail+=' '+str(obj.get('description',''))
        match,conc,vol=identity(title,brand,detail,name,volume)
        r=result(store,name,volume,url=url,label=title,found=True,identity_verified=match,concentration_verified=conc,volume_verified=vol)
        if not (match and conc and vol) or not plain_product(title+' '+detail):
            r['reason']='Не подтверждено точное название/концентрация/объём или другая комплектация';rows.append(r);continue
        offers=obj.get('offers',[]);offers=offers if isinstance(offers,list) else [offers]
        candidates=[]
        for offer in offers:
            if not isinstance(offer,dict) or 'AggregateOffer' in str(offer.get('@type')) or offer.get('itemOffered'):continue
            p=money(offer.get('price'))
            if not p or offer.get('priceCurrency')!='RUB':continue
            if offer.get('priceValidUntil') and str(offer['priceValidUntil'])[:10]<now()[:10]:continue
            seller=offer.get('seller',{});seller=seller.get('name','') if isinstance(seller,dict) else str(seller)
            conditions=(' '.join(str(offer.get(k,'')) for k in ('name','description','validForMemberTier','eligibleCustomerType'))+' '+str(obj.get('description',''))).strip()
            if offer.get('validForMemberTier') or re.search(r'карт|лояльн|промокод|member|coupon',conditions,re.I):
                r['loyalty_price']=p;r['discount_conditions']=conditions;continue
            candidates.append((p,seller,str(offer.get('availability','unknown')).split('/')[-1]))
        if len(set(candidates))==1:
            p,seller,stock=candidates[0]
            r.update(price_rub=p,status='ok',reason='',confirmed=True,price_received=True,seller=seller,availability=stock,
                     price_source='Product/Offer JSON-LD',third_party=(norm(seller)!=norm(SELLERS[store])) if seller else None,
                     discount_conditions='В Offer условия скидки не указаны')
        else:r['reason']='Нет единственного подтверждённого предложения RUB; диапазон/условная цена/нет цены'
        rows.append(r)
    accepted=[r for r in rows if r['confirmed']]
    if len(accepted)==1:return accepted[0]
    if len(accepted)>1:return result(store,name,volume,url=url,found=True,reason='Несколько предложений; выбранный SKU неоднозначен')
    # DOM fallback: primary product only; never infer concentration from recommendations.
    h=soup.select_one('h1');title=h.get_text(' ',strip=True) if h else ''
    brand_node=soup.select_one('[itemprop="brand"], [data-test-id="product-brand"], [data-testid="product-brand"]')
    brand=brand_node.get_text(' ',strip=True) if brand_node else ''
    selected=soup.select('product-variant button.item.active, [data-test-id*="volume"][aria-selected="true"], [data-testid*="volume"][aria-pressed="true"], [data-test-id*="size"][data-state="active"]')
    sizes={x.get_text(' ',strip=True) for x in selected}
    detail=' '.join(x for x in sizes if re.search(r'\d+\s*(?:ml|мл)',x,re.I))
    match,conc,vol=identity(title,brand,detail,name,volume)
    r=result(store,name,volume,url=url,label=title,found=bool(h),identity_verified=match,concentration_verified=conc,volume_verified=vol)
    if not(match and conc and vol):
        return rows[0] if rows else {**r,'reason':'HTML не подтверждает точный товар, концентрацию и выбранный объём'}
    primary=h.find_parent(['main','article']) if h else None
    if not primary or primary.select('h2, h3, [data-testid*="recommend"], .recommendations'):
        return {**r,'reason':'Не удалось отделить цену основного товара от рекомендаций'}
    nodes=primary.select('[itemprop="price"], .price-block__price-current, [data-test-id="price"], [data-testid="product-price"], product-price .price .gold.highlight')
    prices=set()
    for node in nodes:
        if node.find_parent(['del','s']) or any(x.has_attr('hidden') or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden',x.get('style',''),re.I) for x in [node]+list(node.parents) if getattr(x,'attrs',None) is not None):continue
        p=money(node.get('content') or node.get_text(' ',strip=True))
        if p:prices.add(p)
    currency=soup.select_one('[itemprop="priceCurrency"]')
    rub=(currency and currency.get('content')=='RUB') or any(re.search(r'₽|руб',x.get_text(),re.I) for x in nodes)
    if len(prices)==1 and rub and re.search(r'по карте|с картой|по промокоду|для участников',primary.get_text(' ',strip=True),re.I):
        r.update(loyalty_price=next(iter(prices)),discount_conditions=primary.get_text(' ',strip=True)[:400],reason='Подтверждена только условная цена; обычная розничная цена не установлена')
        return r
    if len(prices)==1 and rub:
        r.update(price_rub=prices.pop(),confirmed=True,price_received=True,status='ok',reason='',price_source='DOM: точный h1/активный вариант',discount_conditions='Условия скидки в структурированных данных не подтверждены')
        seller=soup.select_one('[itemprop="seller"], [data-test-id="seller"], [data-testid="seller"]')
        if seller:r['seller']=seller.get_text(' ',strip=True)
        stock=soup.select_one('[itemprop="availability"]')
        if stock:r['availability']=(stock.get('href') or stock.get('content') or stock.get_text()).split('/')[-1]
    else:r['reason']='Цена отсутствует или неоднозначна; сумма рассрочки не используется'
    return r
