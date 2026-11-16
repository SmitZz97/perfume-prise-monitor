"""TLS-verified public browser transport with robots and challenge checks.
Does not use or modify the supplier's persistent profile.
"""
import hashlib,json,time
from pathlib import Path
from urllib.parse import urlsplit,urljoin
import requests
from protego import Protego
from bs4 import BeautifulSoup

class AccessError(RuntimeError):
    def __init__(self,status,reason):self.status=status;super().__init__(reason)

def protection(text):
    visible=BeautifulSoup(text,'html.parser').get_text(' ',strip=True).casefold()
    if '__jhash_' in text and '__js_p_' in text:return 'JavaScript cookie challenge'
    if any(x in visible for x in ('доступ к сайту временно ограничен','проверка вашего браузера','access denied','verify you are human','just a moment')):return 'Защита сайта / проверка браузера'
    if BeautifulSoup(text,'html.parser').select('iframe[src*="captcha"], .g-recaptcha, .h-captcha'):return 'CAPTCHA'
    return ''

class PublicAccess:
    agent='PerfumeBrowserAgent'
    def __init__(self,folder=None,verified_transport=False):
        self.session=requests.Session();self.session.headers['User-Agent']='PerfumeBrowserAgent/1.0 (user-requested monitoring)'
        self.verified_transport=verified_transport;self.store_failures={};self.asset_cache={};self.rules={};self.blocked={};self.last={};self.events=[];self.folder=Path(folder) if folder else None
        if self.folder:self.folder.mkdir(parents=True,exist_ok=True)
    def origin(self,url):
        u=urlsplit(url)
        if u.scheme!='https' or u.username or u.password or u.port not in (None,443):raise AccessError('unavailable','Разрешён только HTTPS без учётных данных')
        return f'https://{u.netloc}'
    def request(self,url):
        origin=self.origin(url)
        if origin in self.blocked:raise self.blocked[origin]
        if url in self.asset_cache:return self.asset_cache[url]
        delay=max(0,self.last.get(origin,0)+1-time.monotonic())
        time.sleep(delay);self.last[origin]=time.monotonic()
        try:r=self.session.get(url,allow_redirects=False,timeout=15)
        except requests.RequestException as e:raise AccessError('unavailable','Сетевая ошибка '+type(e).__name__) from e
        record={'url':url,'http_status':r.status_code,'content_type':r.headers.get('Content-Type',''),'location':r.headers.get('Location','')}
        self.events.append(record)
        if self.folder:
            (self.folder/'requests.json').write_text(json.dumps(self.events,ensure_ascii=False,indent=2),encoding='utf-8')
            if 'html' in record['content_type'] or urlsplit(url).path=='/robots.txt':
                (self.folder/(hashlib.sha256(url.encode()).hexdigest()[:16]+'.html')).write_text(r.text,encoding='utf-8')
        why=protection(r.text) if 'html' in record['content_type'].lower() else ''
        if r.status_code in (401,403,429) or why:
            err=AccessError('blocked',f'HTTP {r.status_code}: '+(why or 'ограничение доступа'))
            self.blocked[origin]=err;raise err
        if r.status_code==200 and any(x in r.headers.get('Content-Type','') for x in ('javascript','text/css')):self.asset_cache[url]=r
        return r
    def policy(self,url):
        origin=self.origin(url)
        if origin in self.blocked:raise self.blocked[origin]
        if origin not in self.rules:
            robot_url=origin+'/robots.txt';seen=set()
            for _ in range(6):
                if robot_url in seen:raise AccessError('unavailable','Цикл редиректов robots.txt')
                seen.add(robot_url);r=self.request(robot_url)
                if r.status_code in (301,302,303,307,308):
                    destination=urljoin(robot_url,r.headers.get('Location',''))
                    if (urlsplit(destination).hostname or '').removeprefix('www.')!=(urlsplit(origin).hostname or '').removeprefix('www.') or urlsplit(destination).path!='/robots.txt':raise AccessError('unavailable','Неоднозначный редирект robots.txt')
                    self.origin(destination);robot_url=destination;continue
                break
            else:raise AccessError('unavailable','Лимит редиректов robots.txt')
            if r.status_code!=200 or r.headers.get('Content-Type','').split(';')[0].strip().lower()!='text/plain':raise AccessError('unavailable',f'robots.txt не подтверждён: HTTP {r.status_code}, {r.headers.get("Content-Type","")}')
            self.rules[origin]=Protego.parse(r.text)
        rp=self.rules[origin]
        if not rp.can_fetch(url,self.agent):raise AccessError('blocked','Запрещено robots.txt: '+urlsplit(url).path)
        delay=max(1,float(rp.crawl_delay(self.agent) or 0));rate=rp.request_rate(self.agent)
        if rate:delay=max(delay,rate.seconds/rate.requests)
        if delay>30:raise AccessError('unavailable','robots.txt требует длительную паузу; запрос отложен')
        time.sleep(max(0,self.last.get(origin,0)+delay-time.monotonic()))
    def context(self,browser,host,budget=45):
        context=browser.new_context(locale='ru-RU',user_agent=self.session.headers['User-Agent'],service_workers='block')
        deadline=time.monotonic()+budget;errors=[]
        def route_handler(route):
            req=route.request;u=urlsplit(req.url)
            if time.monotonic()>deadline:errors.append(AccessError('unavailable','Лимит времени отображения карточки'));route.abort();return
            if req.method!='GET' or not (u.hostname==host or (u.hostname or '').endswith('.'+host)):
                self.events.append({'url':req.url,'skipped':'Внешний домен или запрос не GET; ресурс не запрошен'})
                route.abort();return
            try:
                self.policy(req.url)
                if self.verified_transport:
                    r=self.request(req.url)
                    if r.status_code>=400:raise AccessError('unavailable','HTTP '+str(r.status_code))
                    headers={k:v for k,v in r.headers.items() if k.lower() not in ('content-length','content-encoding','transfer-encoding','connection')}
                    route.fulfill(status=r.status_code,headers=headers,body=r.content)
                else:
                    response=route.fetch(max_redirects=0,timeout=15000)
                    text=response.text() if 'html' in response.headers.get('content-type','') else ''
                    why=protection(text) if text else ''
                    if response.status in (401,403,429) or why:
                        error=AccessError('blocked',f'HTTP {response.status}: '+(why or 'Ограничение доступа'))
                        self.blocked[self.origin(req.url)]=error;raise error
                    if response.status>=400:raise AccessError('unavailable',f'HTTP {response.status}')
                    route.fulfill(response=response)
            except AccessError as e:errors.append(e);route.abort()
            except Exception as exc:errors.append(AccessError('unavailable','Ошибка браузерного транспорта: '+type(exc).__name__+': '+str(exc)[:180]));route.abort()
        context.route('**/*',route_handler)
        return context,errors
