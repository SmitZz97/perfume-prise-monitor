"""Price recommendation from an existing comparison workbook; no live price claims."""
import argparse, math, re
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

ROOT=Path(__file__).resolve().parent

def amount(value):
    from monitor_core import money
    return money(value)


def calculate(purchase, shipping=0, other=0, commission_pct=0, tax_pct=0, undercut=100, markup_pct=15, competitor_min=None, *, minimum_markup=15):
    values=[Decimal(str(x)) for x in (purchase,shipping,other,commission_pct,tax_pct,undercut,markup_pct)]
    if any(not x.is_finite() or x<0 for x in values) or values[0]<=0 or values[3]+values[4]>=100 or values[6]<minimum_markup:
        raise ValueError(f'Закупка должна быть положительной, наценка не меньше {minimum_markup}%, расходы конечными и неотрицательными, комиссия + налог <100%')
    buy,ship,extra,commission,tax,cut,markup=values
    fees=(commission+tax)/100
    floor=((buy*(1+markup/100)+ship+extra)/(1-fees)).to_integral_value(rounding=ROUND_CEILING)
    target=floor
    if competitor_min is not None:
        comp=Decimal(str(competitor_min))
        if not comp.is_finite() or comp<=0:raise ValueError('Некорректная цена конкурента')
        target=max(floor,(comp-cut).to_integral_value(rounding=ROUND_FLOOR))
    costs=(target*fees+ship+extra).quantize(Decimal('.01'))
    net=(target-buy-costs).quantize(Decimal('.01'))
    return {'minimum':float(floor),'recommended':float(target),'costs':float(costs),'profit':float(net),'markup_after_costs_pct':float(net/buy*100)}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--comparison',required=True)
    ap.add_argument('--name',required=True)
    ap.add_argument('--volume',type=int,required=True)
    ap.add_argument('--shipping',type=float,required=True)
    ap.add_argument('--other',type=float,required=True)
    ap.add_argument('--commission-pct',type=float,required=True)
    ap.add_argument('--tax-pct',type=float,required=True)
    ap.add_argument('--undercut',type=float,default=100)
    ap.add_argument('--markup-pct',type=float,default=15)
    ap.add_argument('--output',required=True)
    a=ap.parse_args()
    if any(not math.isfinite(v) or v<0 for v in (a.shipping,a.other,a.commission_pct,a.tax_pct,a.undercut,a.markup_pct)) or a.commission_pct+a.tax_pct>=100 or a.markup_pct<15:
        ap.error('Расходы не могут быть отрицательными; комиссия + налог должны быть меньше 100%')
    source=Path(a.comparison)
    if not source.exists(): ap.error('Нет сравнительного отчёта: '+str(source))
    wb=load_workbook(source)
    ws=wb['Сравнение'] if 'Сравнение' in wb else wb.active
    oldname=str(ws['B1'].value or '').casefold().strip()
    oldvolume=amount(ws['B2'].value)
    if re.sub(r'\s+',' ',oldname)!=re.sub(r'\s+',' ',a.name.casefold().strip()) or oldvolume!=a.volume:
        ap.error('Отчёт относится к другому аромату или объёму; сначала выполните «Закупка + сравнение»')
    purchase=None; competitors=[]; supplier_status=''; supplier_url=''
    for row in ws.iter_rows(min_row=8,max_row=min(ws.max_row,30),values_only=True):
        store=str(row[0] or '').strip().lower()
        price=amount(row[1])
        if store=='delice opt':
            purchase=price if str(row[3] or '')=='ok' else None; supplier_url=str(row[2] or '');supplier_status=str(row[3] or '')
        elif store in ('randewoo','letu','goldapple','rivegauche') and str(row[3] or '')=='ok' and price is not None and price>0:
            from hybrid_store import manual_snapshot_current
            if not manual_snapshot_current(row[6] if len(row)>6 else None,row[7] if len(row)>7 else None,row[8] if len(row)>8 else None):continue
            competitors.append((store,price,str(row[3] or ''),str(row[2] or '')))
    if purchase is None or purchase<=0: ap.error('Нет подтверждаемой закупочной цены в отчёте')
    competitor_min=min((x[1] for x in competitors),default=None)
    calculation=calculate(purchase,a.shipping,a.other,a.commission_pct,a.tax_pct,a.undercut,a.markup_pct,competitor_min)
    floor=calculation['minimum'];recommended=calculation['recommended'];costs=calculation['costs'];net=calculation['profit'];margin=calculation['markup_after_costs_pct']
    decision='Минимальная наценка к закупке после указанных расходов соблюдена; это не чистая маржа 15% от выручки'
    sheetname='Рекомендуемая цена'
    if sheetname in wb: del wb[sheetname]
    out=wb.create_sheet(sheetname,0)
    entries=[
        ('Аромат',a.name),('Объём, мл',a.volume),('Дата расчёта',datetime.now()),
        ('Закупка, ₽',purchase),('Источник закупки',supplier_url),('Статус закупки',supplier_status),
        ('Минимальная наценка к закупке, %',a.markup_pct),('Доставка и упаковка, ₽',a.shipping),
        ('Прочие постоянные расходы, ₽',a.other),('Комиссии с продажи, %',a.commission_pct),
        ('Налоги с продажи, %',a.tax_pct),('Желаемое снижение относительно конкурента, ₽',a.undercut),
        ('Минимальная допустимая цена, ₽',floor),('Минимальная цена конкурента, ₽',competitor_min),
        ('РЕКОМЕНДУЕМАЯ ЦЕНА, ₽',recommended),('Расходы при этой цене, ₽',costs),
        ('Остаток после закупки и расходов, ₽',net),('Остаток к закупке, %',margin),
        ('Решение',decision),('Ограничение','Расчёт предварительный: проверить совпадение EDP, объёма, комплектации, наличие и актуальность цен; налоги введены как процент выручки'),
        ('Источники конкурентов',', '.join(f'{s}: {p:.0f} ₽' for s,p,_,_ in competitors) or 'Нет'),
    ]
    for label,value in entries: out.append([label,value])
    out.column_dimensions['A'].width=51;out.column_dimensions['B'].width=100
    for row in out:
        row[0].font=Font(bold=True)
        row[1].alignment=Alignment(wrap_text=True,vertical='top')
    out['A15'].fill=PatternFill('solid',fgColor='DCECE5');out['B15'].fill=PatternFill('solid',fgColor='DCECE5')
    out['B15'].font=Font(bold=True,size=14)
    out.freeze_panes='B4'
    for n in (4,8,9,12,13,14,15,16,17): out[f'B{n}'].number_format='#,##0.00 "₽"'
    out['B3'].number_format='DD.MM.YYYY HH:MM'
    dest=Path(a.output);wb.save(dest)
    print(f'Закупка: {purchase:.0f} ₽')
    print(f'Минимальная цена с расходами и наценкой {a.markup_pct:g}%: {floor} ₽')
    print('Минимальная цена конкурента:',f'{competitor_min:.0f} ₽' if competitor_min is not None else 'нет')
    print(f'Рекомендуемая цена: {recommended} ₽')
    print(f'Остаток после закупки и указанных расходов: {net:.2f} ₽')
    print('Пояснение:',decision)
    print('ВНИМАНИЕ: закупка и розница предварительные, проверьте EDP, объём и комплектацию.')
    print('Отчёт:',dest)

if __name__=='__main__': main()
