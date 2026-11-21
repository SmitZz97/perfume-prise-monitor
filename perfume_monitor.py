import csv
from monitor_core import same_scent,concentration
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

ROOT = Path(__file__).resolve().parent
CATALOG = ROOT / 'products.csv'
AGENT = ROOT / 'monitor_auto_precise_v9.py'
DISCOVER = ROOT / 'discover_perfumes.py'
COMPARE = ROOT / 'supplier_compare.py'
PRICING = ROOT / 'pricing_recommendation.py'
STORES = ('goldapple', 'letu', 'randewoo', 'rivegauche')
FIELDS = ('store', 'brand', 'product', 'volume_ml', 'url')

def selected_rows(name, volume):
    tokens = re.findall(r'\w+', name.casefold())
    result = []
    if CATALOG.exists():
        with CATALOG.open(encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                full = f"{row.get('brand','')} {row.get('product','')}".casefold()
                try:
                    vol = int(float(row.get('volume_ml') or 0))
                except ValueError:
                    continue
                if same_scent(full,name) and (not concentration(name) or concentration(full)==concentration(name)) and vol == volume and row.get('store','').lower() in STORES:
                    result.append({key: row.get(key, '') for key in FIELDS})
    # An unmatched perfume can still be entered, but prices cannot be inferred.
    for store in STORES:
        if not any(r['store'].lower() == store for r in result):
            result.append({'store':store,'brand':'','product':name,'volume_ml':str(volume),'url':''})
    return result

class App:
    def __init__(self, root):
        self.root = root
        root.title('Мониторинг парфюмерии')
        root.geometry('1080x700')
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Мониторинг парфюмерии', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Введите аромат и объём. Полный мониторинг попробует найти карточку Delice Opt автоматически; сомнительные совпадения не учитываются.').pack(anchor='w', pady=(6, 14))
        form = ttk.Frame(frame)
        form.pack(fill='x')
        ttk.Label(form, text='Аромат').grid(row=0,column=0,sticky='w')
        self.name = ttk.Entry(form, width=60)
        self.name.grid(row=1,column=0,sticky='ew',padx=(0,12))
        self.name.insert(0, 'Creed Green Irish Tweed EDP')
        ttk.Label(form, text='Объём, мл').grid(row=0,column=1,sticky='w')
        self.volume = ttk.Entry(form,width=12)
        self.volume.grid(row=1,column=1)
        self.volume.insert(0,'100')
        form.columnconfigure(0,weight=1)
        actions = ttk.Frame(frame)
        actions.pack(fill='x',pady=12)
        ttk.Button(actions,text='Показать позиции',command=self.search).pack(side='left',padx=(0,8))
        self.run_button = ttk.Button(actions,text='Найти цены выбранного аромата',command=self.run_agent)
        self.run_button.pack(side='left',padx=(0,8))
        self.discover_button = ttk.Button(actions,text='Искать карточки и цены',command=self.discover)
        self.discover_button.pack(side='left',padx=(0,8))
        self.full_button=ttk.Button(actions,text='ПОЛНЫЙ МОНИТОРИНГ',command=self.full_monitor)
        self.full_button.pack(side='left',padx=(0,8))
        ttk.Button(actions,text='Закупка + сравнение',command=self.compare).pack(side='left',padx=(0,8))
        ttk.Button(actions,text='Рассчитать цену продажи',command=self.calculate_price).pack(side='left',padx=(0,8))
        ttk.Button(actions,text='Открыть папку отчётов',command=lambda: os.startfile(str(ROOT))).pack(side='left')
        batch_bar=ttk.Frame(frame)
        batch_bar.pack(fill='x',pady=(0,8))
        self.batch_button=ttk.Button(batch_bar,text='МАССОВЫЙ МОНИТОРИНГ ИЗ EXCEL',command=self.start_batch)
        self.batch_button.pack(side='left')
        ttk.Button(batch_bar,text='Подтвердить цены',command=self.confirm_prices).pack(side='left',padx=8)
        self.manual_only=tk.BooleanVar(value=False)
        ttk.Checkbutton(batch_bar,text='Без автосбора конкурентов (для массового запуска)',variable=self.manual_only).pack(side='left')
        supplier_frame=ttk.Frame(frame)
        supplier_frame.pack(fill='x',pady=(0,10))
        ttk.Label(supplier_frame,text='Ссылка Delice Opt (необязательно):').pack(side='left')
        self.supplier_url=ttk.Entry(supplier_frame,width=85)
        self.supplier_url.pack(side='left',fill='x',expand=True,padx=8)
        expenses=ttk.Frame(frame)
        expenses.pack(fill='x',pady=(0,10))
        self.expense_inputs={}
        for idx,(key,label,default) in enumerate((('shipping','Доставка/упаковка, ₽','0'),('other','Прочие расходы, ₽','0'),('commission','Комиссия, %','0'),('tax','Налоги с выручки, %','0'),('undercut','Ниже конкурента на, ₽','100'))):
            ttk.Label(expenses,text=label).grid(row=0,column=idx,sticky='w',padx=(0,8))
            entry=ttk.Entry(expenses,width=18)
            entry.grid(row=1,column=idx,sticky='w',padx=(0,8))
            entry.insert(0,default)
            self.expense_inputs[key]=entry
        ttk.Label(frame,text='Минимальная наценка: 15% к закупке после указанных расходов. Нули в расходах — только если расходов действительно нет.').pack(anchor='w',pady=(0,8))
        ttk.Label(frame,text='Выбранные позиции (пустая ссылка = нет подтверждённой карточки)').pack(anchor='w')
        self.tree=ttk.Treeview(frame,columns=('store','product','volume','url'),show='headings',height=7)
        for key,label,width in [('store','Магазин',120),('product','Аромат',250),('volume','мл',55),('url','Ссылка',420)]:
            self.tree.heading(key,text=label)
            self.tree.column(key,width=width,stretch=key in ('product','url'))
        self.tree.pack(fill='both',expand=True,pady=(4,12))
        ttk.Label(frame,text='Журнал запуска').pack(anchor='w')
        self.logbox=tk.Text(frame,height=12,wrap='word')
        self.logbox.pack(fill='both',expand=True)
        self.log('Готово. Введите аромат и нажмите «Найти цены выбранного аромата».')

    def confirm_prices(self):
        from hybrid_ui import open_confirmation_window
        open_confirmation_window(self.root,self.name.get().strip(),self.volume.get().strip())

    def log(self, text):
        self.root.after(0,lambda t=text:(self.logbox.insert('end',t+'\n'),self.logbox.see('end')))

    def prepare(self):
        name=self.name.get().strip()
        if not name:
            messagebox.showerror('Аромат','Введите название аромата.')
            return None
        try:
            volume=int(self.volume.get().strip())
            if volume<=0: raise ValueError()
        except ValueError:
            messagebox.showerror('Объём','Введите положительное целое число миллилитров.')
            return None
        rows=selected_rows(name,volume)
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self.tree.insert('', 'end',values=(row['store'],(row['brand']+' '+row['product']).strip(),volume,row['url'] or 'Нет подтверждённой ссылки'))
        found=sum(bool(r['url']) for r in rows)
        self.log(f'Аромат: {name}, {volume} мл. Подтверждённых ссылок: {found} из {len(rows)}.')
        return rows

    def search(self):
        self.prepare()

    def run_agent(self):
        # The old v9 runner used permissive substring matching; use the repaired pipeline.
        self.discover()

    def discover(self):
        if not DISCOVER.exists():
            messagebox.showerror('Файл отсутствует', 'Положите discover_perfumes.py рядом с программой.')
            return
        name=self.name.get().strip()
        try:
            volume=int(self.volume.get().strip())
            if volume<=0: raise ValueError()
        except ValueError:
            messagebox.showerror('Объём','Введите положительный целый объём.')
            return
        if not name:
            messagebox.showerror('Аромат','Введите название аромата.')
            return
        report=ROOT/('candidates_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.csv')
        self.discover_button.config(state='disabled')
        self.log(f'Ищем карточки и проверяем цены Randewoo для {name}, {volume} мл.')
        threading.Thread(target=self.discover_worker,args=(name,volume,report),daemon=True).start()

    def discover_worker(self,name,volume,report):
        try:
            cmd=[sys.executable,'-u',str(DISCOVER),'--name',name,'--volume',str(volume),'--output',str(report)]
            proc=subprocess.Popen(cmd,cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1'})
            for line in proc.stdout:self.log(line.rstrip())
            code=proc.wait()
            self.log(f'Поиск завершён, код {code}. CSV: {report.name}. Excel: {report.with_suffix(".xlsx").name}')
            if code==0 and report.exists():
                self.root.after(0,lambda:self.show_candidates(report))
        except Exception as e:
            self.log(f'Ошибка поиска: {e}')
        finally:
            self.root.after(0,lambda:self.discover_button.config(state='normal'))

    def show_candidates(self,report):
        window=tk.Toplevel(self.root)
        window.title('Найденные карточки и цены')
        window.geometry('1180x520')
        ttk.Label(window,text='Цены Randewoo автоматически прочитаны из варианта EDP указанного объёма; перед сравнением проверьте комплектацию. Остальные ссылки — кандидаты.',wraplength=1120).pack(anchor='w',padx=12,pady=10)
        tree=ttk.Treeview(window,columns=('store','label','price','url','status'),show='headings')
        for key,title,width in [('store','Магазин',100),('label','Товар / подсказка',225),('price','Цена, ₽',100),('url','Ссылка',400),('status','Статус',330)]:
            tree.heading(key,text=title);tree.column(key,width=width)
        tree.pack(fill='both',expand=True,padx=12,pady=8)
        with report.open(encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f):tree.insert('', 'end',values=(row['store'],row['label'],row.get('price_rub',''),row['url'],row['status']))
        def open_selected():
            import webbrowser
            sel=tree.selection()
            if sel:
                url=tree.item(sel[0],'values')[3]
                if url.startswith('https://'):webbrowser.open(url)
        ttk.Button(window,text='Открыть выбранную ссылку для проверки',command=open_selected).pack(pady=8)

    def full_monitor(self):
        """One click: fresh competitor candidates -> supplier comparison -> price calculation."""
        name=self.name.get().strip()
        try:
            volume=int(self.volume.get().strip())
            if not name or volume<=0: raise ValueError()
            values={k:float(e.get().strip().replace(',','.')) for k,e in self.expense_inputs.items()}
            if any(v<0 for v in values.values()) or values['commission']+values['tax']>=100: raise ValueError()
        except ValueError:
            messagebox.showerror('Проверка','Введите аромат, объём и корректные расходы. Комиссии и налог суммарно меньше 100%.')
            return
        missing=[x.name for x in (DISCOVER,COMPARE,PRICING) if not x.exists()]
        if missing:
            messagebox.showerror('Нет модулей',', '.join(missing))
            return
        url=self.supplier_url.get().strip()
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
        candidates=ROOT/f'candidates_{stamp}.csv'
        comparison=ROOT/f'comparison_{stamp}.xlsx'
        pricing=ROOT/f'pricing_{stamp}.xlsx'
        self.full_button.config(state='disabled')
        self.log('=== ПОЛНЫЙ МОНИТОРИНГ: '+name+f', {volume} мл ===')
        self.log('Шаг 1/3: поиск свежих цен конкурентов...')
        threading.Thread(target=self.full_worker,args=(name,volume,url,values,candidates,comparison,pricing),daemon=True).start()

    def full_worker(self,name,volume,url,values,candidates,comparison,pricing):
        env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1'}
        def run(cmd):
            proc=subprocess.Popen(cmd,cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                text=True,encoding='utf-8',errors='replace',bufsize=1,env=env)
            for line in proc.stdout:
                self.log(line.rstrip())
            return proc.wait()
        try:
            command=[sys.executable,'-u',str(DISCOVER),'--name',name,'--volume',str(volume),'--output',str(candidates)]
            if run(command)!=0 or not candidates.exists():
                self.log('Поиск конкурентов завершился ошибкой. Не используем старые цены как новые.')
                return
            self.log('Шаг 2/3: закупка Delice Opt и сравнение...')
            command=[sys.executable,'-u',str(COMPARE),'--name',name,'--volume',str(volume),'--candidates',str(candidates),'--output',str(comparison)]
            if url: command+=['--supplier-url',url]
            if run(command)!=0 or not comparison.exists():
                self.log('Сравнение не завершилось. Расчёт цены остановлен.')
                return
            self.log('Шаг 3/3: рекомендуемая цена (минимальная наценка 15%)...')
            command=[sys.executable,'-u',str(PRICING),'--comparison',str(comparison),'--name',name,
                '--volume',str(volume),'--shipping',str(values['shipping']),'--other',str(values['other']),
                '--commission-pct',str(values['commission']),'--tax-pct',str(values['tax']),
                '--undercut',str(values['undercut']),'--markup-pct','15','--output',str(pricing)]
            code=run(command)
            if code==0 and pricing.exists():
                self.log('ГОТОВО. Итоговый Excel: '+str(pricing))
            else:
                self.log('Расчёт не выполнен. Проверьте совпадение закупочного товара, ссылку Delice Opt и отчёт сравнения.')
                self.log('Отчёт сравнения: '+str(comparison))
        except Exception as exc:
            self.log('Ошибка полного мониторинга: '+str(exc))
        finally:
            self.root.after(0,lambda:self.full_button.config(state='normal'))

    def start_batch(self):
        path=filedialog.askopenfilename(title='Выберите список ароматов Excel',filetypes=[('Excel','*.xlsx')])
        if not path:return
        script=ROOT/'batch_monitor.py'
        if not script.exists():
            messagebox.showerror('Нет модуля','Не найден batch_monitor.py')
            return
        missing=[x.name for x in (DISCOVER,COMPARE,PRICING) if not x.exists()]
        if missing:
            messagebox.showerror('Нет модулей',', '.join(missing))
            return
        try:
            values={k:float(e.get().strip().replace(',','.')) for k,e in self.expense_inputs.items()}
            if any(v<0 for v in values.values()) or values['commission']+values['tax']>=100:raise ValueError()
        except ValueError:
            messagebox.showerror('Расходы','Проверьте расходы, комиссии и налоги.')
            return
        report=ROOT/('batch_result_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.xlsx')
        cmd=[sys.executable,'-u',str(script),'--input',path,'--output',str(report),
            '--shipping',str(values['shipping']),'--other',str(values['other']),
            '--commission-pct',str(values['commission']),'--tax-pct',str(values['tax']),
            '--undercut',str(values['undercut'])]
        if self.manual_only.get():cmd.append('--offline')
        self.batch_button.config(state='disabled')
        self.log('МАССОВЫЙ МОНИТОРИНГ: '+path)
        self.log('Итоговый файл: '+str(report))
        threading.Thread(target=self.batch_worker,args=(cmd,),daemon=True).start()

    def batch_worker(self,cmd):
        try:
            proc=subprocess.Popen(cmd,cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                text=True,encoding='utf-8',errors='replace',bufsize=1,
                env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1'})
            for line in proc.stdout:self.log(line.rstrip())
            self.log('Массовый мониторинг завершён. Код: '+str(proc.wait()))
        except Exception as exc:self.log('Ошибка массового мониторинга: '+str(exc))
        finally:self.root.after(0,lambda:self.batch_button.config(state='normal'))

    def compare(self):
        if not COMPARE.exists():
            messagebox.showerror('Нет файла','Не найден supplier_compare.py')
            return
        name=self.name.get().strip()
        try:
            volume=int(self.volume.get().strip())
            if volume<=0: raise ValueError()
        except ValueError:
            messagebox.showerror('Объём','Введите корректный объём в мл.')
            return
        if not name:
            messagebox.showerror('Аромат','Введите название аромата.')
            return
        report=ROOT/('comparison_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.xlsx')
        url=self.supplier_url.get().strip()
        self.log('Сравниваем закупку с последним поиском этого аромата. При ссылке Delice Opt откроется Chromium.')
        threading.Thread(target=self.compare_worker,args=(name,volume,url,report),daemon=True).start()

    def compare_worker(self,name,volume,url,report):
        try:
            cmd=[sys.executable,'-u',str(COMPARE),'--name',name,'--volume',str(volume),'--output',str(report)]
            if url:cmd.extend(['--supplier-url',url])
            proc=subprocess.Popen(cmd,cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1'})
            for line in proc.stdout:self.log(line.rstrip())
            self.log('Сравнение завершено. Код: '+str(proc.wait()))
        except Exception as exc:
            self.log('Ошибка сравнения: '+str(exc))

    def calculate_price(self):
        if not PRICING.exists():
            messagebox.showerror('Нет файла','Не найден pricing_recommendation.py')
            return
        name=self.name.get().strip()
        try:
            volume=int(self.volume.get().strip())
            if not name or volume<=0: raise ValueError()
            values={k:float(e.get().strip().replace(',','.')) for k,e in self.expense_inputs.items()}
            if any(v<0 for v in values.values()) or values['commission']+values['tax']>=100: raise ValueError()
        except ValueError:
            messagebox.showerror('Поля','Введите аромат, объём и неотрицательные расходы. Комиссия + налог должны быть меньше 100%.')
            return
        reports=sorted(ROOT.glob('comparison_*.xlsx'),key=lambda p:p.stat().st_mtime,reverse=True)
        if not reports:
            messagebox.showerror('Нет отчёта','Сначала нажмите «Закупка + сравнение» и дождитесь завершения.')
            return
        from openpyxl import load_workbook
        selected=None
        for path in reports:
            try:
                wb=load_workbook(path,read_only=True,data_only=True)
                ws=wb['Сравнение'] if 'Сравнение' in wb else wb.active
                ok=str(ws['B1'].value or '').strip().casefold()==name.casefold() and int(ws['B2'].value)==volume
                wb.close()
                if ok:
                    selected=path;break
            except Exception:
                continue
        if selected is None:
            messagebox.showerror('Нет отчёта','Нет сравнительного отчёта для этого аромата и объёма. Сначала нажмите «Закупка + сравнение».')
            return
        output=ROOT/('pricing_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.xlsx')
        cmd=[sys.executable,'-u',str(PRICING),'--comparison',str(selected),'--name',name,'--volume',str(volume),
             '--shipping',str(values['shipping']),'--other',str(values['other']),
             '--commission-pct',str(values['commission']),'--tax-pct',str(values['tax']),
             '--undercut',str(values['undercut']),'--markup-pct','15','--output',str(output)]
        self.log('Рассчитываем цену по отчёту '+selected.name)
        threading.Thread(target=self.pricing_worker,args=(cmd,),daemon=True).start()

    def pricing_worker(self,cmd):
        try:
            proc=subprocess.Popen(cmd,cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                text=True,encoding='utf-8',errors='replace',bufsize=1,
                env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1'})
            for line in proc.stdout:self.log(line.rstrip())
            self.log('Расчёт завершён. Код: '+str(proc.wait()))
        except Exception as exc:
            self.log('Ошибка расчёта: '+str(exc))

    def worker(self,catalog,report):
        try:
            proc=subprocess.Popen([sys.executable,'-u',str(AGENT),'--catalog',str(catalog),'--report',str(report)],cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1'})
            for line in proc.stdout:
                self.log(line.rstrip())
            code=proc.wait()
            self.log(f'Завершено. Код выхода: {code}. Отчёт: {report.name}')
        except Exception as e:
            self.log(f'Ошибка: {e}')
        finally:
            self.root.after(0,lambda:self.run_button.config(state='normal'))

if __name__=='__main__':
    root=tk.Tk()
    App(root)
    root.mainloop()
