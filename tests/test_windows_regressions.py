"""Windows regressions: routes, robots, live-search UI and exact supplier identity."""
import time
from types import SimpleNamespace
import pytest
from requests import Response
from browser_access import PublicAccess,AccessError
from supplier_compare import assess_offer

@pytest.fixture
def access(monkeypatch):
    a=PublicAccess();monkeypatch.setattr('browser_access.time.sleep',lambda _:None)
    return a

def response(url,status,body='',ct='text/plain',location=None):
    r=Response();r.url=url;r.status_code=status;r.headers['Content-Type']=ct;r._content=body.encode();r.encoding='utf-8'
    if location:r.headers['Location']=location
    return r

@pytest.mark.parametrize('status',[404,410])
def test_absent_robots_allows_search_with_cached_empty_rules(access,monkeypatch,status):
    calls=[]
    def get(url,**kw):calls.append(url);return response(url,status,'<h1>Not found</h1>','text/html')
    monkeypatch.setattr(access.session,'get',get)
    access.policy('https://www.letu.ru/search?q=creed');access.policy('https://www.letu.ru/product/1')
    assert calls==['https://www.letu.ru/robots.txt']

@pytest.mark.parametrize('status',[401,403,429,503])
def test_http_error_robots_does_not_allow_search(access,monkeypatch,status):
    monkeypatch.setattr(access.session,'get',lambda url,**kw:response(url,status))
    with pytest.raises(AccessError):access.policy('https://randewoo.ru/search?q=creed')

@pytest.mark.parametrize('status',[200,404,410,503])
def test_challenge_is_not_absent_robots_even_with_404(access,monkeypatch,status):
    monkeypatch.setattr(access.session,'get',lambda url,**kw:response(url,status,'<html><script>__jhash_ __js_p_</script></html>','text/html'))
    with pytest.raises(AccessError) as exc:access.policy('https://randewoo.ru/search?q=x')
    assert exc.value.status=='blocked'


def test_robots_real_disallow_is_enforced(access,monkeypatch):
    monkeypatch.setattr(access.session,'get',lambda url,**kw:response(url,200,'User-agent: *\nDisallow: /search*'))
    with pytest.raises(AccessError,match='robots'):access.policy('https://randewoo.ru/search?q=x')


def test_robots_redirect_to_absent_file(access,monkeypatch):
    calls=[]
    def get(url,**kw):
        calls.append(url)
        return response(url,301,location='https://letu.ru/robots.txt') if 'www.' in url else response(url,404,ct='text/html')
    monkeypatch.setattr(access.session,'get',get)
    access.policy('https://www.letu.ru/search?q=x')
    assert len(calls)==2


class FakeRoute:
    def __init__(self,*,code=200,kind='document',fulfill_error=False,abort_error=False,fetch_error=None):
        self.request=SimpleNamespace(url='https://randewoo.ru/product/x',method='GET',resource_type=kind)
        self.code=code;self.calls=[];self.fulfill_error=fulfill_error;self.abort_error=abort_error;self.fetch_error=fetch_error
    def fetch(self,**kw):
        if self.fetch_error:raise RuntimeError(self.fetch_error)
        return SimpleNamespace(status=self.code,headers={'content-type':'text/html'},text=lambda:'<main>Product</main>')
    def fulfill(self,**kw):
        self.calls.append('fulfill')
        if self.fulfill_error:raise RuntimeError('Route is already handled!')
    def abort(self,**kw):
        self.calls.append('abort')
        if self.abort_error:raise RuntimeError('Route is already handled!')


def test_failed_fulfill_never_aborts_same_route(access,monkeypatch):
    monkeypatch.setattr(access,'policy',lambda _:None);route=FakeRoute(fulfill_error=True);errors=[]
    access._handle_route(route,'randewoo.ru',time.monotonic()+20,errors)
    assert route.calls==['fulfill']


def test_failed_abort_not_repeated(access,monkeypatch):
    monkeypatch.setattr(access,'policy',lambda _:(_ for _ in ()).throw(AccessError('blocked','robots.txt')))
    route=FakeRoute(abort_error=True)
    access._handle_route(route,'randewoo.ru',time.monotonic()+20,[])
    assert route.calls==['abort']


def test_already_completed_fetch_does_not_complete_again(access,monkeypatch):
    monkeypatch.setattr(access,'policy',lambda _:None);route=FakeRoute(fetch_error='Route is already handled!')
    access._handle_route(route,'randewoo.ru',time.monotonic()+20,[])
    assert route.calls==[]


def test_optional_asset_disallow_does_not_poison_document(access,monkeypatch):
    monkeypatch.setattr(access,'policy',lambda _:(_ for _ in ()).throw(AccessError('blocked','robots.txt')))
    route=FakeRoute(kind='image');errors=[]
    access._handle_route(route,'randewoo.ru',time.monotonic()+20,errors)
    assert route.calls==['abort'] and not errors


def test_503_document_logged_and_only_one_abort(access,monkeypatch):
    monkeypatch.setattr(access,'policy',lambda _:None);route=FakeRoute(code=503);errors=[]
    access._handle_route(route,'randewoo.ru',time.monotonic()+20,errors)
    assert route.calls==['abort'] and '503' in str(errors[0])
    assert any(x.get('http_status')==503 for x in access.events)

@pytest.mark.parametrize('name',[ 'Creed Aventus EDP', 'Creed Green Irish Tweed EDP'])
@pytest.mark.parametrize('suffix',['п.в.','п/в','парф. вода','Eau de Parfum'])
def test_creed_exact_concentration_aliases(name,suffix):
    item={'title':name.replace(' EDP','')+' '+suffix,'volume':'100 мл','price':12345}
    assert assess_offer(item,name,100)['confirmed']


def test_separate_supplier_brand_field_is_explicit_evidence():
    assert assess_offer({'title':'Aventus EDP','brand':'Creed','volume':'100 ml','price':12345},'Creed Aventus EDP',100)['confirmed']
    assert not assess_offer({'title':'Aventus EDP','volume':'100 ml','price':12345},'Creed Aventus EDP',100)['confirmed']


def test_bare_volume_needs_proven_unit():
    item={'title':'Creed Aventus EDP','volume':'100','price':12345}
    assert not assess_offer(item,'Creed Aventus EDP',100)['confirmed']
    assert assess_offer({**item,'volume_unit':'мл'},'Creed Aventus EDP',100)['confirmed']

@pytest.mark.parametrize('title',['Creed Aventus Cologne EDP','Creed Aventus EDT','Creed Aventus Absolu EDP','Creed Aventus EDP tester','Creed Aventus EDP набор'])
def test_supplier_still_rejects_other_versions(title):
    r=assess_offer({'title':title,'volume':'100 ml','price':12345},'Creed Aventus EDP',100)
    assert not r['confirmed'] and r['reason']


def test_supplier_reports_each_specific_failure():
    r=assess_offer({'title':'Creed Green Irish Tweed EDT','volume':'50 ml','price':None},'Creed Green Irish Tweed EDP',100)
    assert not r['confirmed']
    assert all(x in r['reason'] for x in ('Концентрация','Объём','цены'))

def test_rive_503_does_not_stop_following_store(monkeypatch):
    import discover_perfumes
    seen=[]
    class Page:
        def goto(self,*a,**k):pass
        def wait_for_timeout(self,*a):pass
        def locator(self,*a):return SimpleNamespace(evaluate_all=lambda *_:[])
    class Context:
        def new_page(self):return Page()
        def close(self):pass
    class Access:
        def context(self,browser,host):
            seen.append(host)
            if host=='rivegauche.ru':raise AccessError('unavailable','HTTP 503')
            return Context(),[]
    monkeypatch.setattr(discover_perfumes,'search_goldapple',lambda *a:[])
    rows=discover_perfumes.collect(None,'Creed Aventus EDP',100,Access())
    assert len(rows)==4 and 'goldapple.ru' in seen
    rg=next(x for x in rows if x['store']=='rivegauche')
    assert rg['status']=='unavailable' and '503' in rg['reason'] and rg['price_rub'] is None

def test_supplier_workbook_explains_creed_rejections(monkeypatch,tmp_path):
    import supplier_compare
    from openpyxl import load_workbook
    (tmp_path/'supplier_browser_profile').mkdir()
    monkeypatch.setattr(supplier_compare,'ROOT',tmp_path)
    monkeypatch.setattr(supplier_compare,'read_live',lambda url:[
        {'supplier':'Fixture','title':'Creed Aventus EDT','volume':'100 ml','price':12345,'url':url},
        {'supplier':'Fixture','title':'Creed Aventus EDP','volume':'50 ml','price':12000,'url':url},
    ])
    dest=tmp_path/'comparison.xlsx'
    monkeypatch.setattr('sys.argv',['supplier_compare.py','--name','Creed Aventus EDP','--volume','100','--supplier-url','https://application.delice-opt.ru/catalog/1','--output',str(dest),'--candidates',str(tmp_path/'missing.csv')])
    supplier_compare.main()
    w=load_workbook(dest)
    assert w['Сравнение']['B8'].value is None
    assert 'Концентрация' in w['Проверка закупки']['K2'].value
    assert 'Объём' in w['Проверка закупки']['K3'].value
