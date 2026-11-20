"""Sequential launcher; each independent worker has its own process and JSON report."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description='Запуск трёх агентов мониторинга парфюмерии')
    ap.add_argument('--input', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--workdir', default=str(ROOT / 'work'))
    ap.add_argument('--links-file', default=str(ROOT / 'supplier_links.json'))
    ap.add_argument('--offline', action='store_true', help='Без запросов к магазинам; ручные подтверждения сохраняются')
    ap.add_argument('--supplier-disabled', action='store_true', help='Не обращаться к Delice Opt')
    for name in ('shipping', 'other', 'commission-pct', 'tax-pct', 'undercut', 'markup-pct'):
        ap.add_argument('--' + name, type=float, default=100 if name == 'undercut' else 15 if name == 'markup-pct' else 0)
    a = ap.parse_args()
    work = Path(a.workdir)
    work.mkdir(parents=True, exist_ok=True)
    competitor_report = work / 'competitors.json'
    supplier_report = work / 'supplier.json'

    commands = [
        [sys.executable, '-u', str(ROOT / 'agent_competitors.py'), '--input', a.input, '--output', str(competitor_report)] + (['--offline'] if a.offline else []),
        [sys.executable, '-u', str(ROOT / 'agent_supplier.py'), '--input', a.input, '--output', str(supplier_report), '--links-file', a.links_file] + (['--supplier-disabled'] if a.supplier_disabled else []),
        [sys.executable, '-u', str(ROOT / 'agent_pricing.py'), '--competitors', str(competitor_report), '--supplier', str(supplier_report), '--output', a.output,
         '--shipping', str(a.shipping), '--other', str(a.other), '--commission-pct', str(a.commission_pct),
         '--tax-pct', str(a.tax_pct), '--undercut', str(a.undercut), '--markup-pct', str(a.markup_pct)],
    ]
    for number, command in enumerate(commands, 1):
        print(f'--- Агент {number}/3 ---', flush=True)
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode != 0:
            raise SystemExit(f'Агент №{number} завершился с ошибкой. Остальные не запускаются; старые цены не используются.')
    print('Три агента завершили работу. Отчёт: ' + a.output, flush=True)


if __name__ == '__main__':
    main()
