"""Agent 1: collect only four retailers; do not open the supplier site."""

import argparse
import json
from pathlib import Path

from agent_protocol import checkpoint, load_specs, new_snapshot
from monitor_core import STORES, result

ROOT = Path(__file__).resolve().parent


def run_competitors(specs, output, collector, registry=None):
    from hybrid_store import hybrid_prices

    snapshot = new_snapshot('competitors')
    for spec in specs:
        name, volume = spec['query'], spec['volume_ml']
        try:
            collected = collector(name, volume)
            if not isinstance(collected, list):
                raise ValueError('Сборщик должен вернуть список')
        except Exception as exc:
            collected = [result(store, name, volume, reason='Сборщик недоступен: ' + type(exc).__name__) for store in STORES]
        collected = [next((row for row in collected if isinstance(row, dict) and row.get('store') == store),
                          result(store, name, volume, reason='Нет диагностической записи')) for store in STORES]
        if registry is not None:
            collected = hybrid_prices(name, volume, collected, registry)
        snapshot['products'].append({'spec': spec, 'records': collected})
        checkpoint(snapshot, output)
        print(f'Конкуренты: {name}, {volume} мл — {sum(bool(r.get("confirmed")) for r in collected)}/4 цен подтверждено', flush=True)
    return snapshot


def main():
    ap = argparse.ArgumentParser(description='Агент №1: Золотое Яблоко, Randewoo, Л’Этуаль и Рив Гош')
    ap.add_argument('--input', required=True, help='Excel: Аромат, Объём, мл')
    ap.add_argument('--output', required=True, help='Свой JSON-файл результатов')
    ap.add_argument('--offline', action='store_true', help='Без интернет-запросов; только актуальные ручные подтверждения')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--browser-executable')
    ap.add_argument('--verified-transport', action='store_true')
    args = ap.parse_args()
    try:
        specs = load_specs(args.input)
    except (ValueError, OSError) as exc:
        ap.error(str(exc))

    from hybrid_store import Registry
    registry = Registry()
    registry.bootstrap(ROOT)

    if args.offline:
        def disabled(name, volume):
            return [result(store, name, volume, reason='Автоматический поиск отключён') for store in STORES]
        run_competitors(specs, args.output, disabled, registry)
        return

    from browser_access import PublicAccess
    from discover_perfumes import collect, launch_browser
    from playwright.sync_api import sync_playwright

    cfg_path = ROOT / 'store_policies.json'
    cfg = json.loads(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
    access = PublicAccess(Path(args.output).parent / 'http_diagnostics', verified_transport=args.verified_transport)
    with sync_playwright() as playwright:
        try:
            browser = launch_browser(playwright, args.headless, args.browser_executable)
        except Exception as exc:
            reason = 'Браузер не запущен: ' + type(exc).__name__
            def disabled(name, volume):
                return [result(store, name, volume, reason=reason) for store in STORES]
            run_competitors(specs, args.output, disabled, registry)
            return
        try:
            run_competitors(specs, args.output, lambda name, volume: collect(browser, name, volume, access, cfg, registry), registry)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
