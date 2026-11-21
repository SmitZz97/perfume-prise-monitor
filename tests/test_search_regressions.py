"""Search UI exercised in a real Chromium with local route fixtures, no live prices."""
import pytest
from test_browser_fixtures import browser
from discover_perfumes import search_goldapple
from browser_access import AccessError
from supplier_compare import row_price,explicit_volume_unit

LINK='<a href="https://goldapple.ru/123456789-test-product">Creed Aventus</a>'

def fixture_page(browser,html,next_html=None):
    context=browser.new_context();context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html; charset=utf-8',body=(next_html if next_html is not None and '/search-page' in route.request.url else html)))
    return context,context.new_page()


def test_visible_input_selected_instead_of_hidden_first(browser):
    html='<input type="search" hidden><form><input type="search" name="q"></form>'+LINK
    c,p=fixture_page(browser,html)
    try:assert any(x['url'].endswith('test-product') for x in search_goldapple(p,'Creed Aventus'))
    finally:c.close()


def test_search_button_opens_dialog_input(browser):
    html='<button aria-label="Поиск по каталогу" onclick="document.querySelector(\'input\').hidden=false">Открыть</button><input type="search" hidden>'+LINK
    c,p=fixture_page(browser,html)
    try:assert any(x['url'].endswith('test-product') for x in search_goldapple(p,'Creed Aventus'))
    finally:c.close()


def test_delayed_react_style_input_is_waited_for(browser):
    html='<script>setTimeout(()=>{document.body.insertAdjacentHTML("beforeend",\'<input role="searchbox">\')},200)</script>'+LINK
    c,p=fixture_page(browser,html)
    try:assert search_goldapple(p,'Creed Aventus')
    finally:c.close()


def test_autocomplete_preserved_before_enter_clears_it(browser):
    html='''<input type="search" id="q"><section></section><script>
    const field=document.getElementById('q');
    field.addEventListener('input',()=>{
      const a=document.createElement('a');a.href='https://goldapple.ru/123456789-test-product';
      a.textContent='Creed Aventus';document.querySelector('section').replaceChildren(a);
    });
    field.addEventListener('keydown',event=>{
      if(event.key==='Enter'){document.querySelector('section').replaceChildren();event.preventDefault();}
    });</script>'''
    c,p=fixture_page(browser,html)
    try:assert any(x['url'].endswith('test-product') for x in search_goldapple(p,'Creed Aventus'))
    finally:c.close()


def test_only_published_search_link_is_followed(browser):
    c,p=fixture_page(browser,'<header><a href="/search-page">Поиск</a></header>','<input name="q">'+LINK)
    try:
        assert search_goldapple(p,'Creed Aventus')
        assert '/search-page' in p.url
    finally:c.close()


def test_challenge_stops_ui_interaction(browser):
    c,p=fixture_page(browser,'<h1>Доступ к сайту временно ограничен владельцем</h1><input type="search">')
    try:
        with pytest.raises(AccessError) as e:search_goldapple(p,'Creed Aventus')
        assert e.value.status=='blocked'
    finally:c.close()


def test_numeric_supplier_price_uses_its_own_column_currency(browser):
    p=browser.new_page()
    try:
        p.set_content('''<table><thead><tr><th>Название</th><th>Объём, мл</th><th>Цена, USD</th></tr></thead><tbody><tr><td><div class="product-detail-table__info">Creed</div></td><td><div class="product-detail-table__volume">100</div></td><td><div class="product-detail-table__price">12000</div></td></tr></tbody></table>''')
        # Price is outside an unambiguously single-row scope in a table; must not borrow another column.
        card=p.locator('.product-detail-table__info')
        assert row_price(card) is None
    finally:p.close()

def test_numeric_volume_and_price_need_matching_column_units(browser):
    p=browser.new_page()
    try:
        p.set_content('''<table><thead><tr><th>Название</th><th>Объём, мл</th><th>Цена, RUB ₽</th></tr></thead><tbody><tr><td><div class="product-detail-table__info">Creed Aventus EDP</div></td><td><div class="product-detail-table__volume">100</div></td><td><div class="product-detail-table__price">12000</div></td></tr></tbody></table>''')
        card=p.locator('.product-detail-table__info')
        assert explicit_volume_unit(card)=='ml'
        assert row_price(card)==12000
    finally:p.close()
