import json,hashlib
from pathlib import Path
import pytest
from openpyxl import load_workbook
from monitor_core import same_scent,identity,concentration,extract_card,result,money
from pricing_recommendation import calculate
from supplier_compare import exact_item,money as supplier_money,remembered_url
from batch_monitor import run_batch,read_entries
from browser_access import protection,PublicAccess,AccessError

@pytest.mark.parametrize('name,other,expected',[
 ('Creed Aventus EDP','Creed Aventus Eau de Parfum',True),
 ('Creed Aventus EDP','Creed Aventus Cologne EDP',False),
 ('Ex Nihilo Fleur Narcotique EDP','Ex Nihilo Fleur Narcotique Extrait',False),
 ('Armaf Club De Nuit Intense Man EDT','Armaf Club De Nuit Intense Man туалетная вода',True),
 ('Creed Green Irish Tweed EDP','Creed Green Irish Tweed tester EDP',False),
 ('Creed Aventus EDP','Creed Aventus Набор EDP',False),
 ('Creed Aventus EDP','Creed Aventus Absolu EDP',False)])
def test_scent(name,other,expected):
    # Concentration is a separate mandatory check.
    matched,conc,vol=identity(other,'','100 ml',name,100)
    assert (matched and conc and vol)==expected

@pytest.mark.parametrize('text,code',[('eau de parfum','EDP'),('туалетная вода','EDT'),('extrait de parfum','EXTRAIT'),('Parfum','PARFUM'),('EDP EDT','')])
def test_concentration(text,code):assert concentration(text)==code

def page(name='Creed Green Irish Tweed Eau de Parfum 100 ml',price='25000',**changes):
    offer={'@type':'Offer','price':price,'priceCurrency':'RUB','seller':{'name':'Randewoo'},'availability':'https://schema.org/InStock',**changes}
    obj={'@type':'Product','name':name,'brand':{'name':'Creed'},'offers':offer}
    return '<h1>'+name+'</h1><script type="application/ld+json">'+json.dumps(obj)+'</script>'

def extract(html):return extract_card(html,'https://randewoo.ru/product/fixture','randewoo','Creed Green Irish Tweed EDP',100)

def test_jsonld_offer():
    r=extract(page());assert r['confirmed'] and r['price_rub']==25000 and r['availability']=='InStock'
    assert r['volume_verified'] and r['concentration_verified'] and r['seller']=='Randewoo'

@pytest.mark.parametrize('name', ['Creed Green Irish Tweed EDP 50 ml','Creed Green Irish Tweed EDT 100 ml','Creed Green Irish Tweed EDP 100 ml tester','Creed Green Irish Tweed gift set EDP 100 ml','Creed Aventus EDP 100 ml'])
def test_wrong_variant_no_price(name):assert extract(page(name))['price_rub'] is None

@pytest.mark.parametrize('patch',[{'price':None},{'price':'NaN'},{'price':'1e999'},{'price':-1},{'priceCurrency':'USD'},{'@type':'AggregateOffer'},{'validForMemberTier':'VIP'}])
def test_unavailable_prices(patch):
    r=extract(page(**patch));assert not r['confirmed'] and r['price_rub'] is None

def test_expired_offer():assert not extract(page(priceValidUntil='2000-01-01'))['confirmed']

def test_supplier_must_not_take_last_price():
    assert supplier_money('Цена 12 345 ₽ Другая цена 27 000 ₽') is None
    assert supplier_money('Цена 12 345 ₽')==12345

def test_supplier_green_irish_strict():
    assert exact_item('Creed Green Irish Tweed EDP','Creed Green Irish Tweed EDP',100,'100 мл')
    assert not exact_item('Creed Green Irish Tweed EDT','Creed Green Irish Tweed EDP',100,'100 мл')
    assert not exact_item('Creed Green Irish Tweed EDP','Creed Green Irish Tweed EDP',100,'100 мл + 7 мл')
    assert not exact_item('Creed Green Irish Tweed EDP тестер','Creed Green Irish Tweed EDP',100,'100 мл')
    assert remembered_url('Creed Green Irish Tweed EDP',100)==remembered_url('Creed Green Irish Tweed',100)

@pytest.mark.parametrize('fees',[0,10,35,99])
def test_markup_not_margin(fees):
    r=calculate(1000,shipping=100,other=50,commission_pct=fees)
    assert r['recommended']>=r['minimum']
    assert r['profit']>=150

def test_exact_floor_and_low_competitor():
    assert calculate(1000)['minimum']==1150
    assert calculate(1000,competitor_min=900)['recommended']==1150
    assert calculate(1000,shipping=100,commission_pct=10,tax_pct=5)['minimum']==1471

@pytest.mark.parametrize('patch',[{'markup_pct':14},{'purchase':0},{'shipping':-1},{'tax_pct':float('nan')},{'commission_pct':100}])
def test_bad_expenses(patch):
    with pytest.raises(ValueError):calculate(**{'purchase':1000,**patch})

def test_batch_failure_does_not_stop_or_hide_retail(tmp_path):
    calls=[]
    def collect(name,vol):
        calls.append(name)
        if name=='fail':raise RuntimeError('site failed')
        return [result('randewoo',name,vol,confirmed=True,identity_verified=True,volume_verified=True,concentration_verified=True,price_received=True,price_rub=20000,status='ok')]
    def supplier(name,vol):raise RuntimeError('profile missing')
    out=tmp_path/'report.xlsx'
    run_batch([('first',100),('fail',100),('last',105)],out,collect,supplier)
    w=load_workbook(out);assert w['Сводный отчёт'].max_row==4
    assert w['Диагностика'].max_row==16
    assert w['Сводный отчёт']['F2'].value==20000
    assert w['Сводный отчёт']['C2'].value is None
    assert calls==['first','fail','last']

def test_batch_saves_even_invalid_last_row(tmp_path):
    out=tmp_path/'x.xlsx'
    run_batch([('x','bad')],out,lambda *_:[],lambda *_:None)
    assert load_workbook(out)['Сводный отчёт']['K2'].value=='input_error'

def test_checkpoint_after_every_product(tmp_path,monkeypatch):
    import batch_monitor
    original=batch_monitor.checkpoint;counts=[]
    def save(wb,path):original(wb,path);counts.append(load_workbook(path)['Сводный отчёт'].max_row)
    monkeypatch.setattr(batch_monitor,'checkpoint',save)
    run_batch([('one',100),('two',100)],tmp_path/'x.xlsx',lambda *_:[],lambda n,v:result('delice opt',n,v))
    assert 2 in counts and 3 in counts

@pytest.mark.parametrize('body',['<html>Доступ к сайту временно ограничен владельцем</html>','<html><script>__jhash_ __js_p_</script></html>','<div class="g-recaptcha"></div>'])
def test_no_captcha_bypass(body):assert protection(body)

def test_robots_denial(monkeypatch):
    from requests import Response
    c=PublicAccess();calls=[]
    def get(url,**kwargs):
        calls.append(url);r=Response();r.url=url;r.status_code=200;r.headers['Content-Type']='text/plain';r._content=b'User-agent: *\nDisallow: /search*';return r
    monkeypatch.setattr(c.session,'get',get)
    with pytest.raises(AccessError):c.policy('https://randewoo.ru/search?q=x')
    assert calls==['https://randewoo.ru/robots.txt']

@pytest.mark.parametrize('code',[403,429])
def test_site_block_stops_retries(monkeypatch,code):
    from requests import Response
    c=PublicAccess();calls=[]
    def get(url,**kwargs):
        calls.append(url);r=Response();r.url=url;r.status_code=code;r._content=b'';return r
    monkeypatch.setattr(c.session,'get',get)
    for _ in range(2):
        with pytest.raises(AccessError):c.request('https://goldapple.ru/')
    assert len(calls)==1

def test_recommendation_cannot_supply_missing_primary_price():
    html='<main><h1>Creed Green Irish Tweed EDP 100 ml</h1><h2>Рекомендуем</h2><div data-testid="product-price">5000 ₽</div></main>'
    assert not extract(html)['confirmed']

def test_hidden_price_is_not_current_dom_price():
    html='<main><h1>Creed Green Irish Tweed EDP 100 ml</h1><div hidden><span data-testid="product-price">5000 ₽</span></div></main>'
    assert not extract(html)['confirmed']

def test_supplier_registry_corruption_never_erases_links(monkeypatch,tmp_path):
    import supplier_compare
    path=tmp_path/'links.json';path.write_text('{damaged registry')
    monkeypatch.setattr(supplier_compare,'LINKS',path)
    assert not supplier_compare.remember_url('Creed Aventus',100,'https://application.delice-opt.ru/catalog/1')
    assert path.read_text()=='{damaged registry'

def test_supplier_registry_keeps_all_other_entries(monkeypatch,tmp_path):
    import supplier_compare
    path=tmp_path/'links.json';path.write_text(json.dumps({'existing|100':'https://application.delice-opt.ru/catalog/9'}))
    monkeypatch.setattr(supplier_compare,'LINKS',path)
    supplier_compare.remember_url('Creed Aventus',100,'https://application.delice-opt.ru/catalog/1')
    assert json.loads(path.read_text())['existing|100'].endswith('/9')

def test_card_price_is_not_regular_dom_price():
    html='<main><h1>Creed Green Irish Tweed EDP 100 ml</h1><p>Цена по карте</p><div data-testid="product-price">5000 ₽</div></main>'
    r=extract(html);assert not r['confirmed'] and r['price_rub'] is None and r['loyalty_price']==5000

def test_product_description_card_condition_is_not_ignored():
    html=page();html=html.replace('"@type": "Product"','"@type": "Product", "description": "Цена по карте"')
    assert not extract(html)['confirmed']
