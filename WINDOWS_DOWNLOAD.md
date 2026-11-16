# Скачать и установить обновление Windows

Скачайте [один ZIP обновления](https://github.com/SmitZz97/perfume-prise-monitor/raw/refs/heads/fix/perfume-browser-agent-monitoring-20261116/downloads/perfume-browser-agent-update.zip).

1. Закройте агент и сохраните копию исходников.
2. Распакуйте в существующую папку проекта с заменой исходников. Не удаляйте supplier_links.json, products.csv, supplier_browser_profile и local_data.
3. Выполните `python -m pip install -r requirements.txt`; при необходимости `python -m playwright install chromium`.
4. Запустите `python perfume_monitor.py`.
5. Используйте раздел «Подтвердить цены» и массовый мониторинг. Полное описание — HYBRID_GUIDE.md.

Если test_products.xlsx ещё нет, `python create_test_input.py` создаст пять тестовых строк без цен. Существующий файл генератор не перезаписывает.

ZIP содержит только исходники, настройки, тесты и инструкции, без локальной базы цен, профиля и пользовательских отчётов. 143 теста прошли. Живые розничные цены не заявлены как подтверждённые.
