# Восстановление проекта на Windows

В браузерном Codex откройте репозиторий `SmitZz97/perfume-prise-monitor` и выберите ветку `feature/three-independent-agents-recovery`. Браузерная сессия Codex работает в облачной среде. Профиль Chromium и вход поставщика восстанавливаются на Windows. Следующие команды выполняйте в обычном PowerShell на своём компьютере.

```powershell
git clone --single-branch --branch feature/three-independent-agents-recovery https://github.com/SmitZz97/perfume-prise-monitor.git
Set-Location perfume-prise-monitor
py -3.12 -m venv .venv
$Py = ".\.venv\Scripts\python.exe"
& $Py -m pip install -r requirements.txt
New-Item -ItemType Directory -Force local_data
Copy-Item examples\agents-catalog.json local_data\agents-catalog.json
```

Установите Git и Python 3.12, если соответствующие команды недоступны. В `local_data\agents-catalog.json` замените синтетический товар своим каталогом. Для каждого товара укажите отдельно бренд, название без бренда, код концентрации, объём и упаковку. Используйте `retail` для обычного флакона, `tester` для тестера, `set` для набора, `sample` для пробника или `refill` для сменного блока. Для набора, пробника и сменного блока укажите также `configuration` с точным составом или форматом. Указывайте одинаковое описание комплектации во всех ручных подтверждениях.

В объекте `urls` используйте ключи `goldapple`, `randewoo`, `letu`, `rivegauche` и `supplier`. Значения — ссылки HTTPS на точные карточки без параметров и фрагментов. Если точная карточка неизвестна, оставьте ключ отсутствующим. Автоматические цены принимаются для явно подтверждённого обычного флакона. Для других упаковок предусмотрено ручное подтверждение.

## Локальный Chromium поставщика

```powershell
& $Py -m playwright install chromium
$ProfileDir = Join-Path $env:LOCALAPPDATA "PerfumePriceMonitor\Chromium"
& $Py -m playwright open --browser chromium --user-data-dir "$ProfileDir" https://application.delice-opt.ru
```

Войдите в поставщика руками, откройте точную карточку и закройте окно перед запуском агента. При истечении сессии повторите вход. Профиль хранится за пределами репозитория. Локальный профиль, пароли, cookies, токены, реестр карточек, SQLite с ручными ценами и отчёты не отправляйте в Codex и не добавляйте в Git. Восстановите удалённые локальные ссылки, каталог и базу подтверждений из резервной копии при её наличии. Команда поставщика записывает статус `no_access`, если профиля или доступа нет.

## Команды агентов

```powershell
& $Py -m agents.competitors --catalog local_data/agents-catalog.json --output local_data/competitors.json
& $Py -m agents.supplier --catalog local_data/agents-catalog.json --profile "$ProfileDir" --output local_data/supplier.json
& $Py -m agents.pricing --competitors local_data/competitors.json --supplier local_data/supplier.json --shipping 100 --other 50 --commission-pct 10 --tax-pct 5 --output local_data/pricing.xlsx
```

Оба сборщика можно запускать независимо. Для конкурентов Chromium не нужен. Перед автоматическим обращением проверьте правила сайта и выключите сбор в `store_policies.json` через `prohibited`, если правила его ограничивают. Агент проверяет `robots.txt` перед запросом и после редиректа, соблюдает задержки и прекращает запросы к источнику при обнаружении защиты. Если сайт отображает цену только после выполнения JavaScript или ограничивает доступ, проверьте карточку вручную в рамках правил сайта. Для поставщика используются только запросы чтения по HTTPS к указанному хосту. Если поставщик изменил разметку, авторизацию или требует другой способ чтения данных, нужна локальная проверка карточки и ручное подтверждение.

Конкуренты и поставщик записывают версионированные JSON-файлы. Пример наблюдения с вручную проверенной ценой приведён в [examples/agents-manual.json](examples/agents-manual.json). Создайте отдельный файл в `local_data`, установите `agent` в `competitors` или `supplier`, укажите соответствующий источник и добавьте `--confirmations <путь>` в команду сборщика. Укажите дату и часовой пояс фактической проверки, точный вариант, наличие, цену в рублях и пояснение, что проверено. Цены, требующие проверки, остаются пустыми в основном файле.

Агент ценообразования читает только два файла наблюдений. Он использует подтверждённые варианты в наличии, проверенные за последние 24 часа, и сопоставляет все поля идентичности. Excel содержит рекомендации, исходные статусы и параметры. Параметры расходов в команде — примеры. Подставьте свои значения на единицу, комиссию и налог в процентах от выручки. `--markup-pct` по умолчанию равен 15 и задаёт чистый остаток к закупочной цене после указанных расходов. `--undercut` по умолчанию равен 100 ₽. Рекомендация ограничена снизу минимальной ценой. При отсутствии сравнимой розничной цены в Excel виден только расчёт по собственным расходам и наценке. Без подтверждённой закупки в наличии суммы продажи пусты.

```powershell
& $Py -m pytest -q tests/test_independent_agents.py
& $Py -m pytest -q
```

Для прежнего интерфейса и ручного реестра см. [HYBRID_GUIDE.md](HYBRID_GUIDE.md).
