"""Recreate a local Playwright profile after deletion; credentials stay on user's PC."""

from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


def main():
    print('Откроется Chromium. Войдите в свой аккаунт Delice Opt вручную.')
    print('Логин и пароль не вводятся в программу и не загружаются в GitHub.')
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / 'supplier_browser_profile'), headless=False, locale='ru-RU')
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto('https://application.delice-opt.ru/', wait_until='domcontentloaded', timeout=60000)
            input('После успешного входа вернитесь в это окно и нажмите Enter, чтобы закрыть браузер: ')
        finally:
            context.close()
    print('Профиль сохранён локально. Теперь можно запускать агента №2.')


if __name__ == '__main__':
    main()
