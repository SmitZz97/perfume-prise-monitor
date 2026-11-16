"""Persistent hybrid workflow; all prices and supplier offers below are synthetic fixtures."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from openpyxl import load_workbook
from hybrid_store import Registry,hybrid_prices,valid_url,clock,key
from monitor_core import result,STORES
from batch_monitor import run_batch

AT=datetime(2026,11,16,12,tzinfo=timezone.utc)
PRODUCTS=[('Creed','Aventus','EDP',100),('Ex Nihilo','Fleur Narcotique','EDP',100),('Creed','Green Irish Tweed','EDP',100),('Armaf','Club De Nuit Intense Man','EDT',105),('Attar Collection','Musk Kashmir','EDP',100)]

@pytest.fixture
def registry(tmp_path):return Registry(tmp_path/'data/competitors.sqlite3')

def link(registry,store='goldapple',brand='Creed',name='Aventus',conc='EDP',volume=100):
    return registry.save_link(brand,name,conc,volume,store,'https://'+{'goldapple':'goldapple.ru','letu':'www.letu.ru','randewoo':'randewoo.ru','rivegauche':'rivegauche.ru'}[store]+'/product/fixture')

def confirm(registry,identity,**changes):
    args=dict(observed_name='Creed Aventus',observed_concentration='EDP',observed_volume=100,price=2000,checked_at=AT,at=AT,confirmed_name=True,confirmed_concentration=True,confirmed_volume=True,seller_type='store',availability='InStock')
    args.update(changes);registry.attest(identity,**args)

def blocked(name='Creed Aventus EDP',volume=100):return [result(s,name,volume,status='blocked',reason='Fixture: автоматический доступ ограничен') for s in STORES]


def test_links_survive_restart(registry):
    link(registry)
    other=Registry(registry.path)
    r=other.lookup('Creed Aventus EDP',100,'goldapple')
    assert r['brand']=='Creed' and r['name']=='Aventus' and r['concentration']=='EDP' and r['verification_status']=='unverified'
    assert not other.lookup('Creed Aventus Cologne EDP',100,'goldapple')


def test_manual_survives_restart_and_is_not_retimestamped(registry):
    confirm(registry,link(registry))
    row=hybrid_prices('Creed Aventus EDP',100,blocked(),Registry(registry.path),at=AT+timedelta(hours=23))[0]
    assert row['price_rub']==2000 and row['source']=='manually' and row['freshness']=='current'
    assert row['checked_at']==AT.isoformat(timespec='seconds')
    assert row['auto_status']=='blocked'

@pytest.mark.parametrize('hours,current',[(0,True),(23.999,True),(24,False),(25,False)])
def test_default_ttl_boundary(registry,hours,current):
    confirm(registry,link(registry))
    row=hybrid_prices('Creed Aventus EDP',100,blocked(),registry,at=AT+timedelta(hours=hours))[0]
    assert row['confirmed'] is current
    assert (row['price_rub'] is not None) is current
    if not current:assert row['freshness']=='expired'


def test_ttl_config_and_override(registry):
    registry.set_ttl(48);confirm(registry,link(registry))
    assert registry.manual('Creed Aventus EDP',100,'goldapple',at=AT+timedelta(hours=30))['freshness']=='current'
    assert registry.manual('Creed Aventus EDP',100,'goldapple',at=AT+timedelta(hours=30),ttl_override=12)['freshness']=='expired'
    assert Registry(registry.path).ttl()==48

@pytest.mark.parametrize('patch',[{'confirmed_name':False},{'confirmed_volume':False},{'confirmed_concentration':False},{'observed_name':'Creed Aventus Cologne'},{'observed_concentration':'EDT'},{'observed_volume':50},{'observed_name':'Creed Aventus tester'},{'observed_name':'Creed Aventus EDT'},{'price':'NaN'},{'price':-1},{'checked_at':AT+timedelta(seconds=1)},{'checked_at':'2026-11-16T12:00:00'},{'ttl_hours':0},{'confirmed_name':'false'}])
def test_no_unconfirmed_or_mismatched_manual_prices(registry,patch):
    identity=link(registry)
    with pytest.raises((ValueError,TypeError)):confirm(registry,identity,**patch)
    assert registry.manual('Creed Aventus EDP',100,'goldapple',at=AT) is None


def test_changing_url_invalidates_price_for_previous_card(registry):
    confirm(registry,link(registry))
    registry.save_link('Creed','Aventus','EDP',100,'goldapple','https://goldapple.ru/product/another')
    assert registry.manual('Creed Aventus EDP',100,'goldapple',at=AT) is None


def test_automatic_price_has_priority_without_destroying_manual_history(registry):
    confirm(registry,link(registry))
    rows=blocked();rows[0].update(price_rub=2100,status='ok',confirmed=True,identity_verified=True,volume_verified=True,concentration_verified=True,url='https://goldapple.ru/product/fixture',product_brand='Creed',product_name='Aventus')
    row=hybrid_prices('Creed Aventus EDP',100,rows,registry,at=AT)[0]
    assert row['price_rub']==2100 and row['source']=='automatically'
    assert registry.manual('Creed Aventus EDP',100,'goldapple',at=AT)['price']==2000

@pytest.mark.parametrize('kind',['loyalty','coupon'])
def test_conditional_price_saved_but_not_used_as_regular_price(registry,kind):
    confirm(registry,link(registry),discount_kind=kind,conditions='Fixture condition')
    r=hybrid_prices('Creed Aventus EDP',100,blocked(),registry,at=AT)[0]
    assert r['price_rub'] is None and not r['confirmed']
    assert registry.manual('Creed Aventus EDP',100,'goldapple',at=AT)['price']==2000


def test_seed_import_does_not_overwrite_user_link(registry):
    root=Path(__file__).resolve().parents[1]
    registry.bootstrap(root)
    r=registry.lookup('Creed Green Irish Tweed EDP',100,'goldapple')
    assert r['url']=='https://goldapple.ru/26542200005-green-irish-tweed'
    registry.save_link('Creed','Green Irish Tweed','EDP',100,'goldapple','https://goldapple.ru/product/my-choice')
    registry.bootstrap(root)
    assert registry.lookup('Creed Green Irish Tweed EDP',100,'goldapple')['url'].endswith('my-choice')

@pytest.mark.parametrize('url',['http://goldapple.ru/product/x','https://evil.test/product/x','https://goldapple.ru@evil.test/product/x','https://goldapple.ru/'])
def test_card_url_validation(url):
    with pytest.raises(ValueError):valid_url('goldapple',url)


def test_all_five_without_store_access_export_hybrid_and_variants(registry,tmp_path):
    entries=[];now=clock()
    for brand,name,conc,volume in PRODUCTS:
        full=brand+' '+name+' '+conc;entries.append((full,volume))
        for store in STORES:
            identity=link(registry,store,brand,name,conc,volume)
            registry.attest(identity,observed_name=brand+' '+name,observed_concentration=conc,observed_volume=volume,price=2000,checked_at=now,at=now,confirmed_name=True,confirmed_volume=True,confirmed_concentration=True,seller_type='store',availability='InStock')
    def fail(name,volume):raise RuntimeError('Fixture: network unavailable')
    def supplier(name,volume):
        from supplier_compare import assess_offer
        check=assess_offer({'title':name,'volume':str(volume)+' ml','price':1000},name,volume)
        return result('delice opt',name,volume,price_rub=1000,confirmed=check['confirmed'],identity_verified=check['identity_verified'],volume_verified=check['volume_verified'],concentration_verified=check['concentration_verified'],price_received=True,variants=[[name,'',str(volume)+' ml',check['observed_concentration'],True,True,True,True,'','https://application.delice-opt.ru/catalog/1','Fixture','обычный',1000]])
    out=tmp_path/'hybrid.xlsx';run_batch(entries,out,fail,supplier,registry=registry)
    w=load_workbook(out)
    assert w.sheetnames==['Сводный отчёт','Диагностика','Проверка закупки','Ручное подтверждение']
    assert w['Сводный отчёт'].max_row==6 and w['Диагностика'].max_row==26 and w['Ручное подтверждение'].max_row==21
    for row in w['Сводный отчёт'].iter_rows(min_row=2,values_only=True):
        assert row[2]==1000 and list(row[3:7])==[2000]*4
        assert row[7]>=1150 and row[8]>=row[7] and row[9]>=150
        assert row[15]=='вручную'  # GoldApple channel follows three supplier metadata cells.


def test_no_competitors_uses_cost_floor_not_market_optimum(tmp_path):
    def supplier(n,v):return result('delice opt',n,v,price_rub=1000,confirmed=True,identity_verified=True,volume_verified=True,concentration_verified=True)
    path=tmp_path/'floor.xlsx';run_batch([('Creed Aventus EDP',100)],path,lambda n,v:blocked(n,v),supplier)
    s=load_workbook(path)['Сводный отчёт']
    assert s['H2'].value==1150 and s['I2'].value==1150
    assert 'рыночный оптимум не установлен' in s.cell(2,s.max_column).value

def test_manual_expiry_rechecked_in_saved_comparison():
    from hybrid_store import manual_snapshot_current
    assert manual_snapshot_current('manually',AT.isoformat(),(AT+timedelta(hours=24)).isoformat(),at=AT+timedelta(hours=23))
    assert not manual_snapshot_current('manually',AT.isoformat(),(AT+timedelta(hours=24)).isoformat(),at=AT+timedelta(hours=24))
    assert not manual_snapshot_current('manually',AT.isoformat(),'bad',at=AT)

@pytest.mark.parametrize('name',['Creed Aventus EDP 50 ml','Creed Aventus EDP EDT 100 ml'])
def test_observed_title_cannot_contradict_selected_fields(registry,name):
    identity=link(registry)
    with pytest.raises(ValueError):confirm(registry,identity,observed_name=name)

@pytest.mark.parametrize('name',['Aventus EDT','Aventus EDP 50 ml'])
def test_link_metadata_cannot_contradict_embedded_identity(registry,name):
    with pytest.raises(ValueError):link(registry,name=name)

def test_manual_competitor_survives_supplier_failure(registry,tmp_path):
    current=clock();identity=link(registry)
    confirm(registry,identity,checked_at=current,at=current)
    def supplier(*_):raise RuntimeError('Fixture: supplier offline')
    dest=tmp_path/'no-buy.xlsx'
    run_batch([('Creed Aventus EDP',100)],dest,lambda n,v:blocked(n,v),supplier,registry=registry)
    sheet=load_workbook(dest)['Сводный отчёт']
    assert sheet['C2'].value is None and sheet['D2'].value==2000
    assert sheet['H2'].value is None and sheet['I2'].value is None

def test_standalone_comparison_reads_fresh_manual_prices(monkeypatch,tmp_path):
    import supplier_compare
    r=Registry(tmp_path/'local_data/competitors.sqlite3');current=clock()
    confirm(r,link(r),checked_at=current,at=current)
    monkeypatch.setattr(supplier_compare,'ROOT',tmp_path)
    monkeypatch.setattr(supplier_compare,'LINKS',tmp_path/'no-supplier-links.json')
    dest=tmp_path/'comparison.xlsx'
    monkeypatch.setattr('sys.argv',['supplier_compare.py','--name','Creed Aventus EDP','--volume','100','--output',str(dest),'--supplier-disabled','--candidates',str(tmp_path/'none.csv')])
    supplier_compare.main()
    s=load_workbook(dest)['Сравнение']
    assert s['B8'].value is None and s['B9'].value==2000
    assert s['G9'].value=='manually' and s['I9'].value
