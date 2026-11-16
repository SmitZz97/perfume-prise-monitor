# Обновление на Windows

Скачайте [ZIP только с исправлениями](https://github.com/SmitZz97/perfume-prise-monitor/raw/refs/heads/fix/perfume-browser-agent-monitoring-20261116/downloads/perfume-browser-agent-update.zip).

1. Закройте запущенный агент и сохраните копию исходных Python-файлов.
2. Распакуйте ZIP в `C:\Users\User\perfume-browser-agent` с заменой файлов.
3. Ваши `supplier_links.json`, `products.csv` и `supplier_browser_profile` оставьте на месте. Архив их не содержит.
4. Установите зависимости: `python -m pip install -r requirements.txt`. При необходимости установите Chromium: `python -m playwright install chromium`.
5. Создайте входной файл: `python create_test_input.py`.
6. Запустите: `python batch_monitor.py --input test_products.xlsx --output monitoring.xlsx`.

ZIP содержит исправления, тесты, настройки и скрипт создания входного Excel с пятью тестовыми ароматами. Авторизационные данные не опубликованы.

В облачной проверке прошло 56 тестов. Реальные цены с пяти источников подтвердить не удалось: авторизованного профиля поставщика нет, а розничные сайты ограничили доступ или не дали проверяемого интерфейса. Ограничения подробно описаны в README_FIX.md. Автоматический сбор не заявлен как полностью подтверждённая интеграция.

Excel-файлы не опубликованы: автоматическая проверка отклонила отдельный бинарный Excel из-за риска раскрытия конфиденциальных данных. В ZIP также нет Excel; исходник create_test_input.py позволяет проверить и создать данные локально.
