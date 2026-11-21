"""Local-only link registry and explicit manual attestations. Never stores credentials."""
import csv,json,math,sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from monitor_core import scent_name,concentration,norm,money,plain_product,result,STORES,SELLERS,concentrations,volume_values

DOMAINS={'goldapple':{'goldapple.ru','www.goldapple.ru'},'letu':{'letu.ru','www.letu.ru'},'randewoo':{'randewoo.ru','www.randewoo.ru'},'rivegauche':{'rivegauche.ru','www.rivegauche.ru'}}

def clock():return datetime.now(ZoneInfo('Europe/Moscow'))
def aware(value):
    dt=datetime.fromisoformat(value) if isinstance(value,str) else value
    if not isinstance(dt,datetime) or dt.utcoffset() is None:raise ValueError('Укажите дату с часовым поясом, например +03:00')
    return dt

def positive_hours(value):
    hours=float(value)
    if not math.isfinite(hours) or hours<=0:raise ValueError('Срок актуальности должен быть положительным числом часов')
    return hours

def valid_url(store,url):
    u=urlsplit(str(url))
    if store not in DOMAINS or u.scheme!='https' or u.netloc not in DOMAINS[store] or u.username or u.password or u.path in ('','/'):
        raise ValueError('Нужна прямая HTTPS-ссылка выбранного магазина')
    return str(url)

def key(name,conc,volume,store):
    if store not in STORES:raise ValueError('Неизвестный магазин')
    code=concentration(conc)
    if not code:raise ValueError('Укажите одну концентрацию: EDP, EDT, Extrait или Parfum')
    v=float(volume)
    if not math.isfinite(v) or v<=0:raise ValueError('Неверный объём')
    return json.dumps([scent_name(name),code,v,store],ensure_ascii=False,separators=(',',':'))

class Registry:
    def __init__(self,path=None):
        self.path=Path(path) if path else Path(__file__).resolve().parent/'local_data/competitors.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS links (
              identity TEXT PRIMARY KEY,brand TEXT NOT NULL,name TEXT NOT NULL,concentration TEXT NOT NULL,
              volume REAL NOT NULL,store TEXT NOT NULL,url TEXT NOT NULL,verification_status TEXT NOT NULL,verified_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS manual_prices (
              id INTEGER PRIMARY KEY AUTOINCREMENT,identity TEXT NOT NULL,url TEXT NOT NULL,
              price REAL NOT NULL,checked_at TEXT NOT NULL,ttl_hours REAL NOT NULL,seller TEXT NOT NULL,
              seller_type TEXT NOT NULL,availability TEXT NOT NULL,discount_kind TEXT NOT NULL,conditions TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY,value TEXT NOT NULL);''')
            c.execute("INSERT OR IGNORE INTO settings VALUES ('ttl_hours','24')")
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback();raise
        finally:c.close()
    def ttl(self):
        with self.connect() as c:return positive_hours(c.execute("SELECT value FROM settings WHERE name='ttl_hours'").fetchone()[0])
    def set_ttl(self,hours):
        hours=positive_hours(hours)
        with self.connect() as c:c.execute("INSERT OR REPLACE INTO settings VALUES ('ttl_hours',?)",(str(hours),))
    def save_link(self,brand,name,conc,volume,store,url,status='unverified',*,replace=True):
        if not str(name).strip():raise ValueError('Укажите название аромата')
        if not plain_product(str(brand)+' '+name):raise ValueError('Тестеры, наборы и пробники не входят в этот режим')
        embedded=concentrations(name)
        if embedded and embedded!={concentration(conc)}:raise ValueError('Концентрация в названии противоречит полю концентрации')
        volumes=volume_values(name)
        if volumes and {float(x) for x in volumes}!={float(volume)}:raise ValueError('Объём в названии противоречит полю объёма')
        url=valid_url(store,url);full=(str(brand).strip()+' '+name.strip()).strip();identity=key(full,conc,volume,store)
        values=(identity,str(brand).strip(),name.strip(),concentration(conc),float(volume),store,url,status,clock().isoformat(timespec='seconds'))
        sql='INSERT OR REPLACE' if replace else 'INSERT OR IGNORE'
        with self.connect() as c:c.execute(sql+' INTO links VALUES (?,?,?,?,?,?,?,?,?)',values)
        return identity
    def links(self):
        with self.connect() as c:return [dict(x) for x in c.execute('SELECT * FROM links ORDER BY brand,name,concentration,volume,store')]
    def lookup(self,name,volume,store):
        try:identity=key(name,concentration(name),volume,store)
        except ValueError:return None
        with self.connect() as c:
            row=c.execute('SELECT * FROM links WHERE identity=?',(identity,)).fetchone()
            return dict(row) if row else None
    def bootstrap(self,root):
        root=Path(root);seeds=root/'competitor_link_seeds.json'
        if seeds.exists():
            for row in json.loads(seeds.read_text(encoding='utf-8')):
                self.save_link(row['brand'],row['name'],row['concentration'],row['volume'],row['store'],row['url'],row['verification_status'],replace=False)
        catalog=root/'products.csv'
        if catalog.exists():
            with catalog.open(encoding='utf-8-sig',newline='') as f:
                for row in csv.DictReader(f):
                    try:self.save_link(row.get('brand',''),row.get('product',''),concentration(row.get('product','')),row.get('volume_ml'),row.get('store'),row.get('url'),'imported_unverified',replace=False)
                    except (ValueError,TypeError):continue
    def attest(self,identity,*,observed_name,observed_concentration,observed_volume,price,checked_at,confirmed_name=False,confirmed_concentration=False,confirmed_volume=False,ttl_hours=None,seller='',seller_type='unknown',availability='unknown',discount_kind='regular',conditions='',at=None):
        at=aware(at or clock());dt=aware(checked_at);ttl=positive_hours(self.ttl() if ttl_hours is None else ttl_hours)
        if dt>at:raise ValueError('Дата проверки не может быть в будущем')
        # Validate timedelta overflow before persisting any data.
        try:dt+timedelta(hours=ttl)
        except OverflowError:raise ValueError('Слишком большой срок актуальности')
        if not all(x is True for x in (confirmed_name,confirmed_concentration,confirmed_volume)):raise ValueError('Подтвердите название, концентрацию и объём отдельно')
        p=money(price)
        if p is None:raise ValueError('Нужна положительная конечная цена в RUB')
        if seller_type not in ('unknown','store','third_party') or availability not in ('unknown','InStock','OutOfStock'):raise ValueError('Некорректное наличие/тип продавца')
        if discount_kind not in ('regular','loyalty','coupon'):raise ValueError('Укажите условия цены')
        if discount_kind!='regular' and not conditions.strip():raise ValueError('Опишите условия условной цены')
        with self.connect() as c:
            row=c.execute('SELECT * FROM links WHERE identity=?',(identity,)).fetchone()
            if row is None:raise ValueError('Сначала сохраните ссылку')
            observed_key=key(observed_name,observed_concentration,observed_volume,row['store'])
            if observed_key!=identity or not plain_product(observed_name):raise ValueError('Название, концентрация или объём не совпадают с зарегистрированным товаром')
            embedded=concentrations(observed_name)
            if embedded and embedded!={concentration(observed_concentration)}:raise ValueError('Концентрация в названии противоречит указанной')
            volumes=volume_values(observed_name)
            if volumes and {float(x) for x in volumes}!={float(observed_volume)}:raise ValueError('Объём в подтверждённом названии противоречит полю объёма')
            if seller_type=='store':seller=SELLERS[row['store']]
            if seller_type=='third_party' and not seller.strip():raise ValueError('Укажите стороннего продавца')
            c.execute('INSERT INTO manual_prices (identity,url,price,checked_at,ttl_hours,seller,seller_type,availability,discount_kind,conditions) VALUES (?,?,?,?,?,?,?,?,?,?)',(identity,row['url'],p,dt.isoformat(timespec='seconds'),ttl,seller,seller_type,availability,discount_kind,conditions))
            c.execute("UPDATE links SET verification_status='manual_identity_confirmed',verified_at=? WHERE identity=?",(dt.isoformat(timespec='seconds'),identity))
    def manual(self,name,volume,store,*,at=None,ttl_override=None):
        link=self.lookup(name,volume,store)
        if not link:return None
        at=aware(at or clock())
        with self.connect() as c:
            rows=c.execute('SELECT * FROM manual_prices WHERE identity=? AND url=? ORDER BY checked_at DESC,id DESC',(link['identity'],link['url'])).fetchall()
        if not rows:return None
        # ISO offsets may differ: compare actual instants, never lexicographic dates.
        entry=dict(max(rows,key=lambda x:(aware(x['checked_at']),x['id'])))
        ttl=positive_hours(entry['ttl_hours'] if ttl_override is None else ttl_override)
        age=at-aware(entry['checked_at']);entry['expires_at']=(aware(entry['checked_at'])+timedelta(hours=ttl)).isoformat(timespec='seconds')
        entry['freshness']='current' if timedelta(0)<=age<timedelta(hours=ttl) else 'expired'
        entry.update(brand=link['brand'],name=link['name'],concentration=link['concentration'],volume=link['volume'],store=store)
        return entry
    def record_automatic(self,name,volume,row):
        if not row.get('url'):return
        link=self.lookup(name,volume,row['store'])
        if row.get('confirmed'):
            code=concentration(name) or row.get('matched_concentration') or concentration(row.get('label',''))
            if not code:return
            brand=row.get('product_brand','') or (link['brand'] if link else '')
            product=row.get('product_name','') or scent_name(name)
            if brand and scent_name(product).startswith(norm(brand)+' '):product=scent_name(product)[len(norm(brand))+1:]
            if not brand and link:product=link['name']
            self.save_link(brand,product,code,volume,row['store'],row['url'],'automatic_identity_confirmed')
        elif link:
            with self.connect() as c:c.execute('UPDATE links SET verification_status=? WHERE identity=?',(row.get('status','unavailable'),link['identity']))

def hybrid_prices(name,volume,automatic,registry,*,at=None,ttl_override=None):
    at=aware(at or clock());rows=[]
    for store in STORES:
        raw=next((dict(x) for x in automatic if x['store']==store),result(store,name,volume,reason='Автоматических данных нет'))
        if raw.get('confirmed') and all(raw.get(k) for k in ('identity_verified','volume_verified','concentration_verified')):
            raw.update(source='automatically',freshness='current');registry.record_automatic(name,volume,raw);rows.append(raw);continue
        link=registry.lookup(name,volume,store);entry=registry.manual(name,volume,store,at=at,ttl_override=ttl_override)
        raw.update(auto_status=raw.get('status'),auto_reason=raw.get('reason'),source='',freshness='missing',manual_required=True)
        raw['url']=raw.get('url') or (link['url'] if link else '')
        if entry and entry['freshness']=='current' and entry['discount_kind']=='regular':
            raw.update(price_rub=entry['price'],checked_at=entry['checked_at'],expires_at=entry['expires_at'],status='ok',reason='Ручное подтверждение; автоматический сбор: '+str(raw['auto_reason']),source='manually',freshness='current',manual_required=False,confirmed=True,identity_verified=True,volume_verified=True,concentration_verified=True,found=True,price_received=True,price_source='Ручное подтверждение',seller=entry['seller'],availability=entry['availability'],discount_conditions=entry['conditions'] or 'Обычная цена, подтверждено оператором',third_party={'store':False,'third_party':True,'unknown':None}[entry['seller_type']])
        else:
            raw.update(price_rub=None,confirmed=False,price_received=False)
            if entry:
                raw['freshness']=entry['freshness'];raw['expires_at']=entry['expires_at'];raw['attempted_at']=raw.get('checked_at');raw['checked_at']=entry['checked_at']
                raw['reason']=(raw.get('reason') or '')+'; '+('Ручная цена просрочена' if entry['freshness']!='current' else 'Есть только условная ручная цена')
            raw['reason']=(raw.get('reason') or '')+'; откройте «Подтвердить цены»'
        rows.append(raw)
    return rows

def manual_snapshot_current(source,checked_at,expires_at,*,at=None):
    if source!='manually':return True
    try:return aware(checked_at)<=aware(at or clock())<aware(expires_at)
    except (ValueError,TypeError,OverflowError):return False
