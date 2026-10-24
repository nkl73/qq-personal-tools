"""Background-only QQ transport. No window, keyboard, mouse or UIA dependencies."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler,ProxyHandler, Request, build_opener

from bridge_core import ROOT


def read_event_body(stream,headers,limit=262144):
    """Node's HTTP callback streams JSON using chunked transfer encoding."""
    transfer=headers.get('Transfer-Encoding','').lower()
    if transfer:
        if transfer!='chunked' or headers.get('Content-Length') is not None:
            raise ValueError('无效的消息传输编码')
        parts=[]
        total=0
        while True:
            line=stream.readline(256)
            if not line.endswith(b'\r\n'):
                raise ValueError('分块长度格式错误')
            raw=line[:-2].split(b';',1)[0]
            if not re.fullmatch(b'[0-9a-fA-F]+',raw):
                raise ValueError('分块长度格式错误')
            size=int(raw,16)
            if total+size>limit:
                raise OverflowError('消息超过长度限制')
            if size==0:
                trailer_size=0
                while True:
                    trailer=stream.readline(4096)
                    trailer_size+=len(trailer)
                    if trailer_size>4096 or not trailer.endswith(b'\r\n'):
                        raise ValueError('分块尾部格式错误')
                    if trailer==b'\r\n':
                        return b''.join(parts)
            block=stream.read(size)
            if len(block)!=size or stream.read(2)!=b'\r\n':
                raise ValueError('分块消息不完整')
            parts.append(block)
            total+=size
    length=int(headers.get('Content-Length','0'))
    if not 0<length<=limit:
        raise OverflowError('消息长度无效')
    body=stream.read(length)
    if len(body)!=length:
        raise ValueError('消息不完整')
    return body


class NoAPIRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class OneBotClient:
    def __init__(self, url, token, timeout=20):
        parts=urlsplit(url)
        if (parts.scheme!='http' or parts.hostname not in ('127.0.0.1','localhost','::1')
                or not parts.port or parts.username or parts.password or parts.query or parts.fragment
                or parts.path not in ('','/')):
            raise ValueError('后台 QQ 接口必须是本机 HTTP 地址。')
        if not isinstance(token,str) or len(token)<24:
            raise ValueError('后台 QQ 连接令牌未配置。')
        self.url=url.rstrip('/')
        self.token=token
        self.timeout=timeout
        self.opener=build_opener(ProxyHandler({}),NoAPIRedirect())

    def call(self, action, body=None):
        if not re.fullmatch(r'[a-z_]+',action):
            raise ValueError('后台接口名称无效')
        data=json.dumps(body or {},ensure_ascii=False).encode('utf-8')
        request=Request(self.url+'/'+action,data=data,
                        headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},method='POST')
        try:
            with self.opener.open(request,timeout=self.timeout) as response:
                value=json.loads(response.read(2_000_000).decode('utf-8'))
        except HTTPError as error:
            raise OSError('后台 QQ 接口返回 HTTP '+str(error.code)) from None
        if not isinstance(value,dict) or value.get('status')!='ok' or value.get('retcode',0)!=0:
            message=str(value.get('message',value.get('wording','后台接口调用失败'))) if isinstance(value,dict) else '响应格式不正确'
            raise ValueError(message.replace(self.token,'[连接令牌已隐藏]'))
        return value.get('data')

    def require_account(self, self_id):
        account=self.call('get_login_info')
        if not isinstance(account,dict) or str(account.get('user_id'))!=str(self_id):
            raise RuntimeError('后台 QQ 登录账号已变化，已暂停桥接。请确认账号后重新启动桥接。')


def incoming_text(event, self_id, started, prefix='/py'):
    """Accept this account's own private commands; do not rely on a chat title."""
    if not isinstance(event,dict) or event.get('post_type') not in ('message','message_sent'):
        return None
    if event.get('message_type')!='private':
        return None
    if str(event.get('self_id',''))!=self_id or str(event.get('user_id',''))!=self_id:
        return None
    if event.get('target_id') is not None and str(event['target_id'])!=self_id:
        return None
    try:
        if float(event.get('time',0))<started-2:
            return None
    except (TypeError,ValueError):
        return None
    message=event.get('message')
    if isinstance(message,list):
        if not all(isinstance(item,dict) and item.get('type')=='text'
                   and isinstance(item.get('data'),dict)
                   and isinstance(item['data'].get('text'),str) for item in message):
            return None
        text=''.join(item['data']['text'] for item in message)
    elif isinstance(message,str):
        text=message
    else:
        return None
    if not text.startswith(prefix):
        return None
    text=text[len(prefix):].strip()
    if len(text)>16000 or any(ord(c)<32 and c not in '\n\r\t' for c in text):
        return None
    mid=event.get('message_id')
    if not isinstance(mid,(str,int)) or isinstance(mid,bool) or not str(mid) or len(str(mid))>160:
        return None
    uid=hashlib.sha256(('onebot:'+self_id+':'+str(mid)).encode()).hexdigest()
    return uid,text


class BackgroundStore:
    def __init__(self, folder):
        self.folder=Path(folder)
        self.folder.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.db=sqlite3.connect(self.folder/'onebot-outbox.sqlite3',check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,text TEXT,status TEXT,result TEXT,receipts TEXT)')
            self.db.execute("UPDATE jobs SET status='stale' WHERE status IN ('pending','submitted','ready')")
            self.db.execute("UPDATE jobs SET status='uncertain' WHERE status='sending'")

    def add(self, uid, text, result=None):
        with self.lock,self.db:
            changed=self.db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?)',
                         (uid,text,'ready' if result is not None else 'pending',result or '','[]')).rowcount
        return bool(changed)

    def active(self):
        with self.lock:
            return self.db.execute("SELECT * FROM jobs WHERE status IN ('pending','submitted','ready') ORDER BY rowid").fetchall()

    def set(self, uid, status, result='', receipts=None):
        with self.lock,self.db:
            if receipts is None:
                self.db.execute('UPDATE jobs SET status=?,result=? WHERE id=?',(status,result,uid))
            else:
                self.db.execute('UPDATE jobs SET status=?,result=?,receipts=? WHERE id=?',
                                (status,result,json.dumps(receipts),uid))

    def close(self):
        self.db.close()


class EventReceiver:
    def __init__(self, store, token, self_id, port=18767, log=print,prefix='/py',on_files=None):
        self.started=time.time()
        self.self_id=str(self_id)
        self.store=store
        self.token=token
        receiver=self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass

            def reply(self,code):
                body=b'{}'
                self.send_response(code)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.connection.settimeout(5)
                self.close_connection=True
                if (self.path!='/events' or self.headers.get('Origin') is not None
                        or self.headers.get('Host') not in ('127.0.0.1:'+str(receiver.port),'localhost:'+str(receiver.port))):
                    self.reply(403)
                    return
                try:
                    body=read_event_body(self.rfile,self.headers)
                    expected='sha1='+hmac.new(token.encode(),body,hashlib.sha1).hexdigest()
                    if (self.headers.get('x-self-id')!=receiver.self_id
                            or not hmac.compare_digest(self.headers.get('x-signature',''),expected)):
                        self.reply(401)
                        return
                    event=json.loads(body.decode('utf-8'))
                    value=incoming_text(event,receiver.self_id,receiver.started,prefix)
                    if value is not None and store.add(*value):
                        log('收到后台 /py 指令：'+value[0][:8])
                    if on_files is not None:on_files(event,receiver.started)
                    self.reply(200)
                except OverflowError:
                    self.reply(413)
                except (ValueError,UnicodeError,OSError):
                    self.reply(400)

        self.server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        self.server.daemon_threads=True
        self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def send_background_result(api,store,self_id,uid,result,log=print):
    parts=[result[i:i+1200] for i in range(0,len(result),1200)] or ['（没有文字输出）']
    receipts=[]
    # Account and availability failures happen before a send is attempted and are safe to retry.
    api.require_account(self_id)
    store.set(uid,'sending',result,receipts)
    try:
        for index,part in enumerate(parts,1):
            data=api.call('send_private_msg',{'user_id':self_id,'message':[
                # Always prefix replies so model text starting with /py cannot loop back as a command.
                {'type':'text','data':{'text':'↳ '+('[%d/%d] '%(index,len(parts)) if len(parts)>1 else '')+part}}],
                'auto_escape':True})
            if not isinstance(data,dict) or data.get('message_id') is None:
                raise ValueError('后台 QQ 没有返回发送消息编号，需核实发送结果。')
            receipts.append(str(data['message_id']))
            store.set(uid,'sending',result,receipts)
        store.set(uid,'sent',result,receipts)
        log('已通过后台接口回传 QQ：'+uid[:8])
    except Exception:
        store.set(uid,'uncertain',result,receipts)
        (store.folder/('未确认后台回传-'+uid+'.txt')).write_text(result,encoding='utf-8')
        raise


def relay_background(client,stop_event,log=print,notices=None,directory=None,config_path=None,shared_dir=None):
    folder=Path(directory or ROOT/'.runtime')
    settings=json.loads(Path(config_path or ROOT/'.runtime'/'onebot.json').read_text(encoding='utf-8'))
    from settings import validate_prefix
    validate_prefix(settings.get('prefix','/py'))
    api=OneBotClient(settings['api_url'],settings['token'])
    waiting=False
    while not stop_event.is_set():
        try:
            account=api.call('get_login_info')
            self_id=str(account['user_id'])
            if not re.fullmatch(r'[1-9][0-9]{4,15}',self_id):
                raise ValueError('后台 QQ 返回的账号格式无效')
            break
        except (OSError,ValueError,KeyError,TypeError):
            if not waiting:
                log('等待后台 QQ 扫码登录；登录完成后自动接通。')
            waiting=True
            stop_event.wait(2)
    else:
        return
    store=BackgroundStore(folder)
    prefix=settings.get('prefix','/py')
    from file_io import Attachment
    files=None
    if shared_dir is not None:
        import queue
        from file_channel import FileChannel
        if notices is None:notices=queue.Queue()
        files=FileChannel(OneBotClient(settings['api_url'],settings['token'],timeout=8),self_id,shared_dir,
            folder/'file-channel',notices,stop_event,prefix=prefix)
    try:
        receiver=EventReceiver(store,settings['token'],self_id,port=int(settings['event_port']),log=log,prefix=prefix,
            on_files=files.accept if files else None)
    except Exception:
        if files:files.close()
        store.close()
        raise
    (folder/'onebot-account.json').write_text(json.dumps({'user_id':self_id}),encoding='utf-8')
    log('后台 QQ 监听已就绪：无需打开聊天页，发送 %s 内容 即可。'%prefix)
    try:
        while not stop_event.is_set():
            if notices is not None:
                import queue
                while True:
                    try:
                        value=notices.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(value,Attachment):
                        if files:
                            try:files.enqueue(value)
                            except (ValueError,OSError):store.add(secrets.token_hex(16),'','附件未发送，请检查共享文件是否存在、大小是否超过限制。')
                        else:log('当前兼容入口没有启用文件通道。')
                    else:store.add(secrets.token_hex(16),'',value)
            for item in store.active():
                if stop_event.is_set():
                    break
                uid=item['id']
                try:
                    if item['status']=='ready':
                        send_background_result(api,store,self_id,uid,item['result'],log)
                        continue
                    if item['status']=='pending':
                        job=client.request('/jobs',{'id':uid,'text':item['text']})
                        store.set(uid,'submitted')
                    else:
                        job=client.request('/jobs/'+uid)
                    if job['status'] in ('done','failed'):
                        if job['status']=='done' and job.get('silent'):
                            store.set(uid,'sent',job['result'],[])
                            continue
                        result=('处理失败：\n' if job['status']=='failed' else '')+job['result']
                        store.set(uid,'ready',result)
                        send_background_result(api,store,self_id,uid,result,log)
                except (OSError,ValueError) as error:
                    log('后台传输暂未完成，结果保留在本机：'+str(error).replace(settings['token'],'[连接令牌已隐藏]'))
                    stop_event.wait(1)
                    break
            stop_event.wait(0.3)
    finally:
        receiver.close()
        if files:files.close()
        store.close()
