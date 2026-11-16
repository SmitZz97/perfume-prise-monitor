"""Real Playwright on local HTML; no website or supplier session is impersonated."""
import os,shutil
import pytest
from playwright.sync_api import sync_playwright
from supplier_compare import row_price
from monitor_core import extract_card

@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as p:
        kwargs={'headless':True}
        exe=os.environ.get('BROWSER_EXECUTABLE') or shutil.which('chromium')
        if exe:kwargs['executable_path']=exe
        b=p.chromium.launch(**kwargs)
        yield b
        b.close()

def test_supplier_row_scope_with_two_different_offers(browser):
    page=browser.new_page()
    page.set_content('''<article><div class="product-detail-table__info">Creed Green Irish Tweed EDP 100 мл</div><div class="product-detail-table__price">12 345 ₽</div></article><article><div class="product-detail-table__info">Creed Green Irish Tweed EDP 50 мл</div><div class="product-detail-table__price">8 000 ₽</div></article>''')
    cards=page.locator('.product-detail-table__info')
    assert row_price(cards.nth(0))==12345 and row_price(cards.nth(1))==8000
    page.close()

def test_ambiguous_supplier_old_and_new_prices_not_guessed(browser):
    page=browser.new_page();page.set_content('<article><div class="product-detail-table__info">Creed</div><div class="product-detail-table__price">12 345 ₽ 16 000 ₽</div></article>')
    assert row_price(page.locator('.product-detail-table__info')) is None
    page.close()

def test_real_browser_exact_edt_105_dom(browser):
    page=browser.new_page();page.set_content('<main><h1>Armaf Club De Nuit Intense Man EDT 105 мл</h1><div data-testid="product-price">5 000 ₽</div></main>')
    r=extract_card(page.content(),'https://randewoo.ru/product/fixture','randewoo','Armaf Club De Nuit Intense Man EDT',105)
    assert r['confirmed'] and r['price_rub']==5000 and r['volume_verified'] and r['concentration_verified']
    r=extract_card(page.content(),'https://randewoo.ru/product/fixture','randewoo','Armaf Club De Nuit Intense Man EDP',105)
    assert not r['confirmed']
    page.close()
