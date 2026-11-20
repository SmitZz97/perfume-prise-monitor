"""Agent 3: offline cross-check and cautious sales-price recommendations."""

import argparse
import os
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from agent_protocol import matching_products, read_snapshot, usable, fresh
from batch_monitor import safe_append
from monitor_core import STORES, money
from pricing_recommendation import calculate

SUMMARY = ['Аромат', 'Объём, мл', 'Концентрация', 'Закупка Delice Opt, ₽',
           'Самый дешёвый допустимый конкурент, ₽', 'Минимальная цена, ₽',
           'Рекомендуемая цена, ₽', 'Прибыль после расходов, ₽', 'Наценка к закупке после расходов, %',
           'Результат', 'Основание', 'Продавец закупки', 'Ссылка на закупку',
           'Дата проверки закупки', 'Конкурентов в расчёте', 'Маржа от выручки после расходов, %']
SOURCE_HEADERS = ['Аромат', 'Объём, мл', 'Источник', 'Цена в источнике, ₽', 'Допущено к расчёту',
                  'Причина', 'Дата проверки', 'URL', 'Продавец', 'Наличие', 'Канал цены', 'Источник цены']
VARIANT_HEADERS = ['Аромат', 'Объём, мл', 'Название строки Delice Opt', 'Бренд', 'Объём строки',
                   'Поставщик', 'Цена строки, ₽', 'Точное совпадение', 'Причина отказа', 'URL']


def render_report(competitors, supplier, output, *, at=None, expenses=None):
    left, right = matching_products(competitors, supplier)
    expenses = expenses or {}
    # Invalid financial parameters must stop the job even when purchases are absent.
    calculate(1, **expenses)

    wb = Workbook()
    summary = wb.active
    summary.title = 'Рекомендации'
    summary.append(SUMMARY)
    sources = wb.create_sheet('Проверка источников')
    sources.append(SOURCE_HEADERS)
    variants = wb.create_sheet('Проверка закупки')
    variants.append(VARIANT_HEADERS)

    for identity, retail_item in left.items():
        spec = retail_item['spec']
        supplier_row = right[identity]['records'][0]
        eligible_retail = [row for row in retail_item['records'] if usable(row, at=at, competitor=True)]
        supplier_ok = usable(supplier_row, at=at)
        purchase = money(supplier_row.get('price_rub')) if supplier_ok else None
        minimum_competitor = min((money(r['price_rub']) for r in eligible_retail), default=None)

        if purchase is None:
            result = [''] * 4
            status = 'Нет подтверждённой закупки'
            reason = 'Рекомендация не рассчитывается без свежей, точно совпадающей закупки Delice Opt'
        else:
            calculation = calculate(purchase, competitor_min=minimum_competitor, **expenses)
            result = [calculation['minimum'], calculation['recommended'],
                      calculation['profit'], calculation['markup_after_costs_pct']]
            if minimum_competitor is None:
                status = 'Только минимальная цена'
                reason = 'Рыночный оптимум неизвестен: ни одной пригодной цены конкурентов. Наличие у поставщика перепроверить'
            else:
                status = 'Предварительная рекомендация'
                reason = 'Ориентир на проверенные обычные цены магазинов; проверить наличие и финальную закупку перед публикацией'
        safe_append(summary, [spec['query'], spec['volume_ml'], spec['concentration'],
                              purchase, minimum_competitor, *result, status, reason,
                              supplier_row.get('seller'), supplier_row.get('url'),
                              supplier_row.get('checked_at') if supplier_ok else None,
                              len(eligible_retail),
                              round(result[2] / result[1] * 100, 2) if purchase is not None else None])

        for record in [supplier_row] + retail_item['records']:
            competitor = record['store'] in STORES
            accepted = usable(record, at=at, competitor=competitor)
            if accepted:
                reason = 'Подходит (наличие поставщика требует подтверждения)' if not competitor else 'Подходит'
            elif not record.get('confirmed'):
                reason = record.get('reason') or 'Цена/идентичность не подтверждена'
            elif not fresh(record, at=at):
                reason = 'Цена старше 24 часов, указана будущая дата или истёк срок подтверждения'
            elif competitor:
                reason = 'Требуются наличие InStock и подтверждённый продавец самого магазина'
            else:
                reason = 'Закупка не прошла проверку'
            safe_append(sources, [spec['query'], spec['volume_ml'], record['store'],
                                  money(record.get('price_rub')), 'Да' if accepted else 'Нет',
                                  reason, record.get('checked_at'), record.get('url'),
                                  record.get('seller'), record.get('availability'),
                                  record.get('source'), record.get('price_source')])
        for variant in supplier_row.get('variants', []):
            safe_append(variants, [spec['query'], spec['volume_ml'], variant.get('title'),
                                   variant.get('brand'), variant.get('volume'), variant.get('supplier'),
                                   variant.get('price_rub'), 'Да' if variant.get('confirmed') else 'Нет',
                                   variant.get('reason'), variant.get('url')])

    for sheet in wb:
        sheet.freeze_panes = 'D2' if sheet == summary else 'C2'
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill(fill_type='solid', fgColor='234E52')
        for column in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(column)].width = (65 if column in (1, 11) else 30)
    for row in summary.iter_rows(min_row=2):
        for col in (4, 5, 6, 7, 8):
            row[col - 1].number_format = '#,##0.00 "₽"'
        row[8].number_format = '0.0"%"'
        row[15].number_format = '0.0"%"'

    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + '.partial.xlsx')
    wb.save(temporary)
    os.replace(temporary, target)
    print(f'Ценообразование: обработано {len(left)} товаров. Итоговый Excel: {target}', flush=True)
    return wb


def main():
    ap = argparse.ArgumentParser(description='Агент №3: без браузера, только отчёты агентов №1 и №2')
    ap.add_argument('--competitors', required=True)
    ap.add_argument('--supplier', required=True)
    ap.add_argument('--output', required=True)
    for name in ('shipping', 'other', 'commission-pct', 'tax-pct', 'undercut', 'markup-pct'):
        ap.add_argument('--' + name, type=float, default=100 if name == 'undercut' else 15 if name == 'markup-pct' else 0)
    args = ap.parse_args()
    try:
        options = dict(shipping=args.shipping, other=args.other, commission_pct=args.commission_pct,
                       tax_pct=args.tax_pct, undercut=args.undercut, markup_pct=args.markup_pct)
        render_report(read_snapshot(args.competitors, 'competitors'),
                      read_snapshot(args.supplier, 'supplier'), args.output, expenses=options)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        ap.error(str(exc))


if __name__ == '__main__':
    main()
