"""Offline contract tests for three independent perfume workers."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook, Workbook

from agent_protocol import (checkpoint, fresh, load_specs, matching_products,
                            new_snapshot, read_snapshot, spec_for, usable)
from agent_competitors import run_competitors
from agent_supplier import allowed_supplier_url, collect_supplier, run_supplier, supplier_link
from agent_pricing import render_report
from monitor_core import result


NOW = datetime(2026, 11, 20, 12, tzinfo=ZoneInfo('Europe/Moscow'))
NAME = 'Creed Green Irish Tweed EDP'
URL = 'https://application.delice-opt.ru/catalog/12345'


def record(store, price=20000, *, checked=NOW, **changes):
    row = result(store, NAME, 100, price_rub=price, status='ok', confirmed=True,
                 identity_verified=True, concentration_verified=True,
                 volume_verified=True, price_received=True, source='automatically',
                 checked_at=checked.isoformat(), seller='Золотое Яблоко',
                 availability='InStock', third_party=False)
    row.update(changes)
    return row


def snapshots(*, buy=10000, competitor=20000, **changes):
    spec = spec_for(NAME, 100)
    first = new_snapshot('competitors')
    rows = [record(store, competitor, **changes) for store in ('goldapple', 'randewoo', 'letu', 'rivegauche')]
    first['products'] = [{'spec': spec, 'records': rows}]
    second = new_snapshot('supplier')
    second['products'] = [{'spec': spec, 'records': [record('delice opt', buy, seller='Delice Opt', availability='unknown')]}]
    return first, second


@pytest.mark.parametrize('bad_name', [
    'Creed Green Irish Tweed', 'Creed Green Irish Tweed EDP EDT',
    'Creed Green Irish Tweed tester EDP', 'Creed Green Irish Tweed gift set EDP', ''])
def test_spec_requires_explicit_unambiguous_plain_concentration(bad_name):
    with pytest.raises(ValueError):
        spec_for(bad_name, 100)


@pytest.mark.parametrize('volume', [0, -1, 'bad', 12.5, float('inf')])
def test_spec_rejects_bad_volume(volume):
    with pytest.raises(ValueError):
        spec_for(NAME, volume)


def test_spec_keeps_extrait_separate_from_edp():
    assert spec_for('Ex Nihilo Fleur Narcotique EDP', 100)['id'] != spec_for('Ex Nihilo Fleur Narcotique Extrait', 100)['id']


def test_excel_rejects_duplicate_products(tmp_path):
    path = tmp_path / 'items.xlsx'
    wb = Workbook()
    wb.active.append(['Аромат', 'Объём, мл'])
    wb.active.append([NAME, 100])
    wb.active.append([NAME, 100])
    wb.save(path)
    with pytest.raises(ValueError, match='повторяются'):
        load_specs(path)


def test_competitors_writes_four_stores_without_supplier(tmp_path):
    called = []
    def collect(name, volume):
        called.append((name, volume))
        return [record('goldapple')]
    output = tmp_path / 'competitors.json'
    run_competitors([spec_for(NAME, 100)], output, collect)
    data = read_snapshot(output, 'competitors')
    assert called == [(NAME, 100)]
    assert [x['store'] for x in data['products'][0]['records']] == ['goldapple', 'letu', 'randewoo', 'rivegauche']


def test_supplier_validates_url_and_does_not_assume_profile(tmp_path):
    assert allowed_supplier_url(URL)
    assert not allowed_supplier_url('https://example.com/catalog/12345')
    assert not allowed_supplier_url('https://application.delice-opt.ru.evil.com/catalog/12345')
    assert not allowed_supplier_url('http://application.delice-opt.ru/catalog/12345')
    missing = collect_supplier(NAME, 100, url=URL, profile=tmp_path / 'deleted-profile')
    assert not missing['confirmed'] and missing['price_rub'] is None


def test_supplier_links_do_not_mix_variants():
    links = {'creed green irish tweed edp|100': URL}
    assert supplier_link(NAME, 100, links) == URL
    assert supplier_link('Creed Green Irish Tweed Extrait', 100, links) == ''


def test_supplier_exact_offer_only(tmp_path):
    offers = [
        {'title': 'Creed Green Irish Tweed EDT', 'brand': 'Creed', 'volume': '100 мл', 'price': 3000, 'supplier': 'Wrong'},
        {'title': 'Creed Green Irish Tweed EDP', 'brand': 'Creed', 'volume': '100 мл', 'price': 9000, 'supplier': 'Wholesale A'},
        {'title': 'Creed Green Irish Tweed EDP tester', 'brand': 'Creed', 'volume': '100 мл', 'price': 4000, 'supplier': 'Wrong'},
        {'title': 'Creed Green Irish Tweed EDP', 'brand': 'Creed', 'volume': '100 мл', 'price': 9500, 'supplier': 'Wholesale B'},
    ]
    outfile = tmp_path / 'supplier.json'
    run_supplier([spec_for(NAME, 100)], outfile, {NAME.casefold() + '|100': URL},
                 fetch_offers=lambda url: offers)
    row = read_snapshot(outfile, 'supplier')['products'][0]['records'][0]
    assert row['price_rub'] == 9000
    assert row['seller'] == 'Wholesale A'
    assert sum(v['confirmed'] for v in row['variants']) == 2


def test_supplier_disabled_never_fetches(tmp_path):
    def forbidden(url):
        raise AssertionError('No network allowed')
    snapshot = run_supplier([spec_for(NAME, 100)], tmp_path / 'supplier.json', {},
                            disabled=True, fetch_offers=forbidden)
    assert not snapshot['products'][0]['records'][0]['confirmed']


def test_prices_respect_24_hour_boundary_and_manual_expiry():
    assert fresh(record('goldapple', checked=NOW - timedelta(hours=23, minutes=59)), at=NOW)
    assert not fresh(record('goldapple', checked=NOW - timedelta(hours=24)), at=NOW)
    assert not fresh(record('goldapple', checked=NOW + timedelta(seconds=1)), at=NOW)
    r = record('goldapple', expires_at=(NOW - timedelta(seconds=1)).isoformat())
    assert not fresh(r, at=NOW)


def test_only_instock_store_own_regular_current_competitors():
    assert usable(record('goldapple'), at=NOW, competitor=True)
    assert not usable(record('goldapple', third_party=True), at=NOW, competitor=True)
    assert not usable(record('goldapple', availability='OutOfStock'), at=NOW, competitor=True)
    assert not usable(record('goldapple', source=''), at=NOW, competitor=True)
    assert not usable(record('goldapple', discount_conditions='Цена по карте'), at=NOW, competitor=True)


def test_price_agent_reads_only_source_snapshots_and_calculates(tmp_path):
    competitors, supplier = snapshots()
    output = tmp_path / 'prices.xlsx'
    render_report(competitors, supplier, output, at=NOW)
    wb = load_workbook(output, data_only=True)
    ws = wb['Рекомендации']
    assert ws['D2'].value == 10000
    assert ws['E2'].value == 20000
    assert ws['G2'].value == 19900
    assert ws['H2'].value == 9900
    assert ws['P2'].value == 49.75
    assert ws['J2'].value == 'Предварительная рекомендация'
    assert wb['Проверка источников'].max_row == 6


def test_price_agent_without_competitors_labels_only_floor(tmp_path):
    competitors, supplier = snapshots(availability='OutOfStock')
    output = tmp_path / 'prices.xlsx'
    render_report(competitors, supplier, output, at=NOW)
    row = load_workbook(output, data_only=True)['Рекомендации']
    assert row['E2'].value is None
    assert row['G2'].value == 11500
    assert row['J2'].value == 'Только минимальная цена'


def test_price_agent_without_purchase_has_no_recommendation(tmp_path):
    competitors, supplier = snapshots()
    supplier['products'][0]['records'][0]['confirmed'] = False
    path = tmp_path / 'prices.xlsx'
    render_report(competitors, supplier, path, at=NOW)
    ws = load_workbook(path, data_only=True)['Рекомендации']
    assert ws['G2'].value is None
    assert ws['J2'].value == 'Нет подтверждённой закупки'


def test_price_agent_rejects_mixed_product_files(tmp_path):
    competitors, supplier = snapshots()
    supplier['products'][0]['spec'] = spec_for('Creed Aventus EDP', 100)
    with pytest.raises(ValueError):
        render_report(competitors, supplier, tmp_path / 'bad.xlsx', at=NOW)


def test_read_snapshot_rejects_tampered_record_identity(tmp_path):
    competitors, supplier = snapshots()
    competitors['products'][0]['records'][0]['query'] = 'Creed Aventus EDP'
    path = tmp_path / 'bad.json'
    checkpoint(competitors, path)
    with pytest.raises(ValueError, match='другому аромату'):
        read_snapshot(path, 'competitors')


def test_read_snapshot_rejects_wrong_agent(tmp_path):
    competitors, supplier = snapshots()
    path = tmp_path / 'wrong.json'
    checkpoint(competitors, path)
    with pytest.raises(ValueError):
        read_snapshot(path, 'supplier')
