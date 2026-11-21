"""Tkinter manual confirmation panel; browser sessions stay in the user's browser."""
import webbrowser
from hybrid_store import Registry,clock,valid_url
from monitor_core import concentration,scent_name,SELLERS

def open_confirmation_window(parent,initial_name='',initial_volume=100):
    import tkinter as tk
    from tkinter import ttk,messagebox
    from pathlib import Path
    registry=Registry();registry.bootstrap(Path(__file__).resolve().parent)
    window=tk.Toplevel(parent);window.title('Подтвердить цены');window.geometry('1100x760')
    canvas=tk.Canvas(window,highlightthickness=0)
    scrollbar=ttk.Scrollbar(window,orient='vertical',command=canvas.yview);scrollbar.pack(side='right',fill='y')
    canvas.pack(side='left',fill='both',expand=True);canvas.configure(yscrollcommand=scrollbar.set)
    box=ttk.Frame(canvas,padding=12);item=canvas.create_window((0,0),window=box,anchor='nw')
    box.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>',lambda event:canvas.itemconfigure(item,width=event.width))
    ttk.Label(box,text='Ссылка не подтверждает цену. Откройте карточку обычным способом и сверяйте выбранный вариант.',wraplength=1050).pack(anchor='w')
    tree=ttk.Treeview(box,columns=('brand','name','conc','volume','store','status','url'),show='headings',height=7)
    for key,label,width in [('brand','Бренд',100),('name','Аромат',180),('conc','Конц.',60),('volume','мл',50),('store','Магазин',100),('status','Проверка ссылки',160),('url','URL',350)]:
        tree.heading(key,text=label);tree.column(key,width=width)
    tree.pack(fill='x',pady=8)
    form=ttk.Frame(box);form.pack(fill='x');fields={}
    labels=[('brand','Бренд'),('name','Название без бренда'),('conc','Концентрация'),('volume','Объём, мл'),('store','Магазин'),('url','Прямая ссылка'),('price','Увиденная цена, RUB'),('checked','Дата проверки ISO (+03:00)'),('ttl','Срок актуальности, часов'),('seller','Продавец'),('seller_type','Тип продавца'),('availability','Наличие'),('discount','Условия цены'),('conditions','Описание скидки / примечание')]
    choices={'conc':['EDP','EDT','EXTRAIT','PARFUM'],'store':list(SELLERS),'seller_type':['unknown','store','third_party'],'availability':['unknown','InStock','OutOfStock'],'discount':['regular','loyalty','coupon']}
    defaults={'name':scent_name(initial_name),'conc':concentration(initial_name) or 'EDP','volume':str(initial_volume),'store':'goldapple','checked':clock().isoformat(timespec='seconds'),'ttl':str(registry.ttl()),'seller_type':'unknown','availability':'unknown','discount':'regular'}
    for index,(key,label) in enumerate(labels):
        ttk.Label(form,text=label).grid(row=index,column=0,sticky='w',pady=2)
        v=tk.StringVar(value=defaults.get(key,''));fields[key]=v
        widget=ttk.Combobox(form,textvariable=v,values=choices[key],state='readonly',width=90) if key in choices else ttk.Entry(form,textvariable=v,width=94)
        widget.grid(row=index,column=1,sticky='ew',pady=2)
    form.columnconfigure(1,weight=1)
    ttk.Label(box,text='store = сам магазин, third_party = сторонний продавец; regular = обычная цена, loyalty = по карте, coupon = по промокоду.\nOutOfStock и неизвестный продавец не участвуют в рыночном ориентире.',wraplength=1050).pack(anchor='w',pady=4)
    flags={}
    flagbox=ttk.Frame(box);flagbox.pack(fill='x')
    for key,label in [('name','Бренд и точное название совпадают; не тестер/не набор'),('conc','Концентрация подтверждена'),('volume','Объём подтверждён')]:
        flags[key]=tk.BooleanVar(value=False);ttk.Checkbutton(flagbox,text=label,variable=flags[key]).pack(anchor='w')
    status=tk.StringVar(value='Цена не сохранена.');ttk.Label(box,textvariable=status,wraplength=1050).pack(anchor='w',pady=4)
    def refresh():
        tree.delete(*tree.get_children())
        for row in registry.links():tree.insert('', 'end',iid=row['identity'],values=[row[k] for k in ('brand','name','concentration','volume','store','verification_status','url')])
    def select(_=None):
        selection=tree.selection()
        if not selection:return
        row=next(x for x in registry.links() if x['identity']==selection[0])
        for target,source in [('brand','brand'),('name','name'),('conc','concentration'),('volume','volume'),('store','store'),('url','url')]:fields[target].set(str(row[source]))
        fields['price'].set('');fields['checked'].set(clock().isoformat(timespec='seconds'))
        for v in flags.values():v.set(False)
    tree.bind('<<TreeviewSelect>>',select)
    def save_link():
        if not fields['brand'].get().strip():raise ValueError('Укажите бренд')
        return registry.save_link(fields['brand'].get(),fields['name'].get(),fields['conc'].get(),fields['volume'].get(),fields['store'].get(),fields['url'].get())
    def action(kind):
        try:
            if kind=='open':
                webbrowser.open(valid_url(fields['store'].get(),fields['url'].get()));return
            if kind=='ttl':registry.set_ttl(fields['ttl'].get());status.set('Срок по умолчанию сохранён для новых подтверждений.');return
            if kind=='confirm':
                full=(fields['brand'].get()+' '+fields['name'].get()).strip()
                saved=registry.lookup(full+' '+fields['conc'].get(),fields['volume'].get(),fields['store'].get())
                if not saved or saved['url']!=fields['url'].get():raise ValueError('Сначала нажмите «Сохранить ссылку» для этого товара')
                identity=saved['identity']
                registry.attest(identity,observed_name=full,observed_concentration=fields['conc'].get(),observed_volume=fields['volume'].get(),price=fields['price'].get(),checked_at=fields['checked'].get(),ttl_hours=fields['ttl'].get(),confirmed_name=flags['name'].get(),confirmed_concentration=flags['conc'].get(),confirmed_volume=flags['volume'].get(),seller=fields['seller'].get(),seller_type=fields['seller_type'].get(),availability=fields['availability'].get(),discount_kind=fields['discount'].get(),conditions=fields['conditions'].get())
                status.set('Ручная цена сохранена с исходным временем проверки. Для обновления Excel повторите мониторинг.');fields['price'].set('')
                for v in flags.values():v.set(False)
            else:
                save_link();status.set('Ссылка сохранена без подтверждения цены.')
            refresh()
        except Exception as exc:messagebox.showerror('Проверка данных',str(exc),parent=window)
    buttons=ttk.Frame(box);buttons.pack(fill='x',pady=5)
    for text,kind in [('Открыть карточку','open'),('Сохранить ссылку','link'),('Сохранить подтверждение цены','confirm'),('Срок по умолчанию','ttl')]:ttk.Button(buttons,text=text,command=lambda k=kind:action(k)).pack(side='left',padx=4)
    refresh()
