"""TLS-verified public browser transport with robots and challenge checks.
Does not use or modify the supplier's persistent profile.
"""
import json,time
from pathlib import Path
from urllib.parse import urlsplit,urljoin
import requests
from protego import Protego
from bs4 import BeautifulSoup

class AccessError(RuntimeError):
    def __init__(self, status, reason, code=""):
        self.status = status
        self.code = code
        super().__init__(reason)

def protection(text):
    soup = BeautifulSoup(text, 'html.parser')
    title = " ".join(soup.title.get_text(" ", strip=True).casefold().split()) if soup.title else ""
    if title in {"checking device", "checking your device"} or (
        title.startswith("gold apple") and title.endswith(("checking device", "checking your device"))
    ):
        return "Защита сайта: проверка устройства"
    visible = soup.get_text(' ', strip=True).casefold()
    if '__jhash_' in text and '__js_p_' in text:return 'JavaScript cookie challenge'
    if any(x in visible for x in ('доступ к сайту временно ограничен','проверка вашего браузера','access denied','verify you are human','just a moment')):return 'Защита сайта / проверка браузера'
    if soup.select('iframe[src*="captcha"], .g-recaptcha, .h-captcha'):return 'CAPTCHA'
    return ''

class PublicAccess:
    agent='PerfumeBrowserAgent'
    service_workers = 'block'
    def __init__(self,folder=None,verified_transport=False):
        self.session=requests.Session();self.session.headers['User-Agent']='PerfumeBrowserAgent/1.0 (user-requested monitoring)'
        self.verified_transport=verified_transport;self.store_failures={};self.asset_cache={};self.rules={};self.blocked={};self.last={};self.events=[];self.folder=Path(folder) if folder else None
        if self.folder:self.folder.mkdir(parents=True,exist_ok=True)
    def origin(self,url):
        u=urlsplit(url)
        if u.scheme!='https' or u.username or u.password or u.port not in (None,443):raise AccessError('unavailable','Разрешён только HTTPS без учётных данных', 'unsafe_url')
        return f'https://{u.netloc}'
    def request(self,url):
        origin=self.origin(url)
        if origin in self.blocked:raise self.blocked[origin]
        if url in self.asset_cache:return self.asset_cache[url]
        delay=max(0,self.last.get(origin,0)+1-time.monotonic())
        time.sleep(delay);self.last[origin]=time.monotonic()
        try:r=self.session.get(url,allow_redirects=False,timeout=15)
        except requests.RequestException as e:raise AccessError('unavailable','Сетевая ошибка '+type(e).__name__) from e
        self._record_event(host=urlsplit(url).hostname, method="GET", http_status=r.status_code)
        why=protection(r.text) if 'html' in r.headers.get('Content-Type', '').lower() else ''
        if r.status_code in (401,403,429) or why:
            err=AccessError('blocked',f'HTTP {r.status_code}: '+(why or 'ограничение доступа'), 'access_restricted')
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
            if r.status_code in (404,410):
                self._record_event(host=urlsplit(robot_url).hostname, http_status=r.status_code, robots='absent')
                self.rules[origin]=Protego.parse('')
            elif r.status_code!=200 or r.headers.get('Content-Type','').split(';')[0].strip().lower()!='text/plain':raise AccessError('unavailable',f'robots.txt не подтверждён: HTTP {r.status_code}, {r.headers.get("Content-Type","")}', 'robots_unavailable')
            else:self.rules[origin]=Protego.parse(r.text)
        rp=self.rules[origin]
        if not rp.can_fetch(url,self.agent):raise AccessError('blocked','Запрещено robots.txt', 'robots_disallowed')
        delay=max(1,float(rp.crawl_delay(self.agent) or 0));rate=rp.request_rate(self.agent)
        if rate:delay=max(delay,rate.seconds/rate.requests)
        if delay>30:raise AccessError('unavailable','robots.txt требует длительную паузу; запрос отложен', 'robots_delay')
        time.sleep(max(0,self.last.get(origin,0)+delay-time.monotonic()))
    def _record_event(self, **event):
        self.events.append(event)
        if self.folder:
            (self.folder/'requests.json').write_text(json.dumps(self.events, ensure_ascii=False, indent=2), encoding='utf-8')

    def _record_route(self, request, **updates):
        self._record_event(host=urlsplit(request.url).hostname,
                           method=getattr(request, 'method', 'unknown'),
                           resource_type=getattr(request, 'resource_type', 'unknown'), **updates)

    def _handle_route(self,route,host,deadline,errors):
        """Prepare a decision, then complete the route exactly once (including failures)."""
        req=route.request;u=urlsplit(req.url)
        document=getattr(req,'resource_type','document')=='document'
        action='abort';kwargs={}
        def record_error(error):
            code = error.code or ("access_restricted" if error.status == "blocked" else "transport_unavailable")
            self._record_route(req, status=error.status, skipped=code)
            if document:errors.append(error)
        try:
            if time.monotonic()>deadline:
                raise AccessError('unavailable','Лимит времени отображения карточки', 'render_deadline')
            outside_store = not (u.hostname == host or (u.hostname or '').endswith('.' + host))
            reasons = (['external_host'] if outside_store else []) + (['method_not_get'] if req.method != 'GET' else [])
            if reasons:
                self._record_route(req, skipped='+'.join(reasons))
            else:
                self.policy(req.url)
                if self.verified_transport:
                    r=self.request(req.url)
                    if r.status_code>=400:raise AccessError('unavailable',f'HTTP {r.status_code}', 'http_error')
                    kwargs={'status':r.status_code,'headers':{k:v for k,v in r.headers.items() if k.lower() not in ('content-length','content-encoding','transfer-encoding','connection')},'body':r.content}
                else:
                    response=route.fetch(max_redirects=0,timeout=15000)
                    self._record_route(req,http_status=response.status)
                    text=response.text() if 'html' in response.headers.get('content-type','').lower() else ''
                    why=protection(text) if text else ''
                    if response.status in (401,403,429) or why:
                        error=AccessError('blocked',f'HTTP {response.status}: '+(why or 'Ограничение доступа'))
                        self.blocked[self.origin(req.url)]=error;raise error
                    if response.status>=400:raise AccessError('unavailable',f'HTTP {response.status}', 'http_error')
                    kwargs={'response':response}
                action='fulfill'
        except AccessError as exc:record_error(exc)
        except Exception as exc:
            # Fetch can race context/page closure. If it says handled, do not attempt another terminal operation.
            record_error(AccessError('unavailable','Ошибка браузерного транспорта: '+type(exc).__name__))
            if 'already handled' in str(exc).lower() or 'has been closed' in str(exc).lower():return
        # Terminal calls MUST remain outside the preparation try/except. A failed fulfill may already
        # have completed the request; aborting from its exception handler caused the Windows regression.
        try:
            getattr(route,action)(**kwargs)
        except Exception as exc:
            self._record_route(req, skipped='route_completion_failed')
            if document:errors.append(AccessError('unavailable','Не удалось завершить маршрут '+action+': '+type(exc).__name__))
            # No abort/continue/fulfill retry, even if completion itself raised.

    def context(self,browser,host,budget=90):
        context=browser.new_context(locale='ru-RU',user_agent=self.session.headers['User-Agent'],service_workers=self.service_workers)
        deadline=time.monotonic()+budget;errors=[]
        context.route('**/*',lambda route:self._handle_route(route,host,deadline,errors))
        return context,errors
