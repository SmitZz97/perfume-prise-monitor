import json
from types import SimpleNamespace
import pytest
import discover_perfumes as discovery
from browser_access import AccessError
from hybrid_store import Registry

OLD='https://goldapple.ru/123456789-old-fixture'
NEW='https://goldapple.ru/123456790-new-fixture'
NAME='Creed Aventus EDP'

def html():
    data={'@type':'Product','name':'Creed Aventus EDP 100 ml','brand':{'name':'Creed'},'offers':{'@type':'Offer','price':'2000','priceCurrency':'RUB','availability':'https://schema.org/InStock','seller':{'name':'Золотое Яблоко'}}}
    return '<h1>Creed Aventus EDP 100 ml</h1><script type="application/ld+json">'+json.dumps(data)+'</script>'

@pytest.fixture
def setup(tmp_path,monkeypatch):
    registry=Registry(tmp_path/'registry.sqlite3');registry.save_link('Creed','Aventus','EDP',100,'goldapple',OLD)
    monkeypatch.setattr(discovery,'SEARCHES',{'goldapple':'https://goldapple.ru/'})
    visits=[]
    class Page:
        url=OLD
        mode='ok'
        def goto(self,url,**kw):
            visits.append(url);self.url=url
            if url==OLD and self.mode in ('404','blocked'):raise AccessError('unavailable' if self.mode=='404' else 'blocked','HTTP '+('404' if self.mode=='404' else '403'))
        def content(self):return '<h1>unknown</h1>' if self.url==OLD and self.mode=='empty' else html()
        def wait_for_timeout(self,*a):pass
    page=Page()
    class Context:
        def new_page(self):return page
        def close(self):pass
    access=SimpleNamespace(context=lambda *_:(Context(),[]))
    def search(*_):visits.append('search');return [{'url':NEW,'text':'Creed Aventus'}]
    monkeypatch.setattr(discovery,'search_goldapple',search)
    return registry,page,access,visits


def test_saved_link_read_before_search_and_persisted(setup):
    registry,page,access,visits=setup
    row=discovery.collect(None,NAME,100,access,registry=registry)[0]
    assert row['confirmed'] and visits==[OLD]
    assert Registry(registry.path).lookup(NAME,100,'goldapple')['verification_status']=='automatic_identity_confirmed'

@pytest.mark.parametrize('mode',['empty','404'])
def test_failed_saved_card_falls_back_to_allowed_search(setup,mode):
    registry,page,access,visits=setup;page.mode=mode
    row=discovery.collect(None,NAME,100,access,registry=registry)[0]
    assert row['confirmed'] and row['url']==NEW
    assert visits==[OLD,'search',NEW]
    assert registry.lookup(NAME,100,'goldapple')['url']==NEW


def test_site_protection_does_not_trigger_search_bypass(setup):
    registry,page,access,visits=setup;page.mode='blocked'
    row=discovery.collect(None,NAME,100,access,registry=registry)[0]
    assert not row['confirmed'] and row['status']=='blocked' and visits==[OLD]
