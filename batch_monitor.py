"""Batch orchestration: strict identity, isolated failures, checkpoint after every input row."""
import argparse,csv,json,math,os,subprocess,sys
from pathlib import Path
from openpyxl import Workbook,load_workbook
from openpyxl.styles import Font
from monitor_core import result,now,money,STORES
from pricing_recommendation import calculate
ROOT=Path(__file__).resolve().parent
HEADERS=['Аромат','Объём','Закупка Delice Opt','Золотое Яблоко','Л’Этуаль','Randewoo','Рив Гош','Минимальная цена','Рекомендованная цена','Прибыль после расходов','Статус','Комментарий']
DIAG=['Аромат','Объём','Магазин','Найден ли товар','Проверен ли объём','Проверена ли концентрация','Получена ли цена','Цена','Статус','Причина ошибки или пропуска','Ссылка на карточку','Дата проверки','Продавец','Наличие','Условия скидки','Источник цены']

def read_entries(path):
    wb=load_workbook(path,read_only=True,data_only=True)
    try:
        ws=wb['Ароматы'] if 'Ароматы' in wb.sheetnames else wb.active
        data=list(ws.values)
    finally:wb.close()
    if not data:raise ValueError('Пустой Excel')
    headers=[str(x or '').strip().casefold().replace('ё','е') for x in data[0]]
    if 'аромат' not in headers or 'объем, мл' not in headers:raise ValueError('Нужны колонки «Аромат» и «Объём, мл»')
    ni=headers.index('аромат');vi=headers.index('объем, мл')
    return [(str(row[ni] or '').strip() if len(row)>ni else '',row[vi] if len(row)>vi else None) for row in data[1:] if any(x is not None for x in row)]

def safe_append(ws,values):
    ws.append(["'"+x if isinstance(x,str) and x.startswith(('=','+','-','@')) else x for x in values])

def checkpoint(wb,path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    for ws in wb:
        ws.freeze_panes='C2';ws.auto_filter.ref=ws.dimensions
    tmp=path.with_name(path.stem+'.partial.xlsx');wb.save(tmp);os.replace(tmp,path)

def run_batch(entries,output,collector,supplier,expenses=None,registry=None,manual_ttl=None):
    expenses=expenses or {};wb=Workbook();ws=wb.active;ws.title='Сводный отчёт';ws.append(HEADERS)
    dg=wb.create_sheet('Диагностика');dg.append(DIAG+['Канал цены','Актуальность','Автоматический статус','Причина автоматического отказа'])
    purchases=wb.create_sheet('Проверка закупки');purchases.append(['Запрос','Объём','Название строки','Бренд','Объём строки','Концентрация','Имя совпало','Концентрация совпала','Объём совпал','Цена подтверждена','Причина','URL','Поставщик','Комплектация','Цена строки (не означает совпадение)'])
    manual_sheet=wb.create_sheet('Ручное подтверждение');manual_sheet.append(['Аромат','Объём','Магазин','URL','Цена в истории','Дата подтверждения','Актуальность','Действует до','Условия','Наличие','Продавец','Действие'])
    for store in ('Delice Opt',)+STORES:
        for label in ('Источник цены','Последняя проверка','Актуальность'):ws.cell(1,ws.max_column+1,store+' — '+label)
    ws.cell(1,ws.max_column+1,'Основание рекомендации')
    for sheet in wb:
        for cell in sheet[1]:cell.font=Font(bold=True)
        for col in range(1,len(sheet[1])+1):sheet.column_dimensions[__import__('openpyxl').utils.get_column_letter(col)].width=25 if col<9 else 60
    for name,rawvol in entries:
        buy={};recommendation_basis='Не рассчитано: нет подтверждённой закупки'
        row=[name,rawvol]+[None]*8+['',''];records=[]
        try:
            volume=float(rawvol)
            if not math.isfinite(volume) or volume<=0 or not volume.is_integer() or not name:raise ValueError('Нужны название и целый положительный объём')
            volume=int(volume);row[1]=volume
            try:retail=collector(name,volume)
            except Exception as exc:retail=[result(s,name,volume,reason='Ошибка поиска: '+type(exc).__name__) for s in STORES]
            if registry:
                from hybrid_store import hybrid_prices
                retail=hybrid_prices(name,volume,retail,registry,ttl_override=manual_ttl)
            for store in STORES:
                entry=next((x for x in retail if x['store']==store),result(store,name,volume,reason='Магазин не вернул диагностическую запись'))
                records.append(entry)
                if entry.get('confirmed') and all(entry.get(k) for k in ('identity_verified','volume_verified','concentration_verified')):row[3+STORES.index(store)]=money(entry.get('price_rub'))
            try:buy=supplier(name,volume)
            except Exception as exc:buy=result('delice opt',name,volume,reason='Ошибка поставщика: '+type(exc).__name__)
            records.insert(0,buy)
            if buy.get('confirmed') and all(buy.get(k) for k in ('identity_verified','volume_verified','concentration_verified')):row[2]=money(buy.get('price_rub'))
            if row[2] is not None:
                eligible=[r['price_rub'] for r in records[1:] if r.get('confirmed') and r.get('availability')=='InStock' and r.get('third_party') is False]
                price=calculate(row[2],**expenses,competitor_min=min(eligible) if eligible else None)
                row[7:10]=[price['minimum'],price['recommended'],price['profit']]
                recommendation_basis='По подтверждённым доступным конкурентам с учётом минимального порога' if eligible else 'Только минимальная цена по закупке и расходам; рыночный оптимум не установлен'
                row[10]='ok' if all(x is not None for x in row[3:7]) else 'partial'
            else:row[10]='no_purchase'
            row[11]=' | '.join(r['store']+': '+r.get('reason','') for r in records if not r.get('confirmed'))
        except Exception as exc:
            row[10]='input_error';row[11]=str(exc)[:500]
            if not records:records=[result(s,name,rawvol,reason='Ошибка входной строки: '+str(exc)) for s in ('delice opt',)+STORES]
        finally:
            for store in ('delice opt',)+STORES:
                record=next((r for r in records if r['store']==store),{})
                channel=record.get('source') or ('automatically' if record.get('confirmed') else '')
                label={'automatically':'автоматически','manually':'вручную'}.get(channel,channel)
                row.extend([label,record.get('checked_at'),record.get('freshness') or ('current' if record.get('confirmed') else 'missing')])
            row.append(recommendation_basis)
            safe_append(ws,row)
            for variant in buy.get('variants',[]):safe_append(purchases,[name,rawvol]+list(variant)[:13])
            if not buy.get('variants'):safe_append(purchases,[name,rawvol,'','','','',False,False,False,False,buy.get('reason') or 'Подробности поставщика недоступны',buy.get('url')])
            for store in STORES:
                entry=registry.manual(name,rawvol,store,ttl_override=manual_ttl) if registry and isinstance(rawvol,(int,float)) and rawvol>0 else None
                record=next((r for r in records if r['store']==store),{})
                url=(entry or {}).get('url') or record.get('url','')
                safe_append(manual_sheet,[name,rawvol,store,url,(entry or {}).get('price'),(entry or {}).get('checked_at'),(entry or {}).get('freshness','missing'),(entry or {}).get('expires_at'),(entry or {}).get('conditions'),(entry or {}).get('availability'),(entry or {}).get('seller'),'Подтверждение актуально' if entry and entry['freshness']=='current' else 'Откройте «Подтвердить цены»'])
                if url.startswith('https://'):manual_sheet.cell(manual_sheet.max_row,4).hyperlink=url
            for r in records:
                safe_append(dg,[name,rawvol,r['store'],r.get('found'),r.get('volume_verified'),r.get('concentration_verified'),r.get('price_received'),r.get('price_rub') if r.get('confirmed') else None,r.get('status'),r.get('reason'),r.get('url'),r.get('checked_at'),r.get('seller'),r.get('availability'),r.get('discount_conditions'),r.get('price_source'),r.get('source') or ('automatically' if r.get('confirmed') else ''),r.get('freshness') or ('current' if r.get('confirmed') else 'missing'),r.get('auto_status'),r.get('auto_reason')])
                if r.get('url','').startswith('https://'):dg.cell(dg.max_row,11).hyperlink=r['url']
            checkpoint(wb,output)
            print(name,row[10],row[11],flush=True)
    checkpoint(wb,output);return wb

def main():
    from playwright.sync_api import sync_playwright
    from discover_perfumes import collect,launch_browser
    from browser_access import PublicAccess
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--output',required=True)
    p.add_argument('--offline',action='store_true',help='Ручные подтверждения и закупка; без обращения к конкурентам');p.add_argument('--manual-ttl-hours',type=float);p.add_argument('--headless',action='store_true');p.add_argument('--browser-executable');p.add_argument('--verified-transport',action='store_true');p.add_argument('--supplier-disabled',action='store_true')
    for flag in ('shipping','other','commission-pct','tax-pct','undercut'):p.add_argument('--'+flag,type=float,default=100 if flag=='undercut' else 0)
    a=p.parse_args();expenses=dict(shipping=a.shipping,other=a.other,commission_pct=a.commission_pct,tax_pct=a.tax_pct,undercut=a.undercut)
    try:calculate(1,**expenses);entries=read_entries(a.input)
    except (ValueError,OSError) as exc:p.error(str(exc))
    from hybrid_store import Registry,positive_hours
    registry=Registry();registry.bootstrap(ROOT)
    if a.manual_ttl_hours is not None:positive_hours(a.manual_ttl_hours)
    details=Path(a.output).parent/'batch_details';details.mkdir(parents=True,exist_ok=True)
    access=PublicAccess(details/'http',verified_transport=a.verified_transport)
    cfg=json.loads((ROOT/'store_policies.json').read_text(encoding='utf-8')) if (ROOT/'store_policies.json').exists() else {}
    counter=[0]
    def supplier(name,volume):
        counter[0]+=1;path=details/f'{counter[0]:03d}_supplier.xlsx'
        command=[sys.executable,str(ROOT/'supplier_compare.py'),'--name',name,'--volume',str(volume),'--candidates',str(details/'no-candidates.csv'),'--output',str(path)]
        if a.supplier_disabled:command.append('--supplier-disabled')
        proc=subprocess.run(command,cwd=ROOT,timeout=120,capture_output=True,text=True,encoding='utf-8',errors='replace')
        if proc.returncode!=0:raise RuntimeError('supplier_compare не завершился: '+proc.stderr[-200:])
        w=load_workbook(path,read_only=True,data_only=True)
        try:
            cells=list(w['Сравнение'].iter_rows(min_row=8,max_row=8,values_only=True))[0]
            variants=list(w['Проверка закупки'].iter_rows(min_row=2,values_only=True)) if 'Проверка закупки' in w.sheetnames else []
        finally:w.close()
        confirmed=cells[3]=='ok' and money(cells[1]) is not None
        return result('delice opt',name,volume,variants=[list(row)[2:] for row in variants],price_rub=money(cells[1]) if confirmed else None,url=str(cells[2] or ''),status='ok' if confirmed else 'unavailable',reason='' if confirmed else str(cells[3] or ''),confirmed=confirmed,found=confirmed,identity_verified=confirmed,volume_verified=confirmed,concentration_verified=confirmed,price_received=confirmed,price_source='Текущая строка предложения Delice Opt' if confirmed else '')
    if a.offline:
        run_batch(entries,a.output,lambda name,volume:[result(s,name,volume,reason='Автосбор конкурента отключён для офлайн-режима') for s in STORES],supplier,expenses,registry,a.manual_ttl_hours);return
    with sync_playwright() as pw:
        try:browser=launch_browser(pw,a.headless,a.browser_executable)
        except Exception as exc:
            run_batch(entries,a.output,lambda name,volume:[result(s,name,volume,reason='Браузер не запущен: '+type(exc).__name__) for s in STORES],supplier,expenses,registry,a.manual_ttl_hours);return
        try:run_batch(entries,a.output,lambda name,volume:collect(browser,name,volume,access,cfg,registry),supplier,expenses,registry,a.manual_ttl_hours)
        finally:browser.close()

if __name__=='__main__':main()
