"""Authenticated loopback queue for an explicitly paired ChatGPT browser extension."""
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from script_backend import Turn,redact


def web_settings(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);path=folder/'connection.json'
    if path.exists():
        value=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value.get('token'),str) or len(value['token'])<32:raise ValueError('浏览器通道配置无效。')
        return value
    value={'token':secrets.token_urlsafe(32)}
    try:
        with path.open('x',encoding='utf-8') as stream:json.dump(value,stream)
    except FileExistsError:return web_settings(folder)
    return value


def prepare_extension(folder,port=18770):
    folder=Path(folder);cfg=web_settings(folder);target=folder/'extension';target.mkdir(exist_ok=True)
    source=Path(__file__).parent/'extensions'/'chatgpt'
    for name in ('manifest.json','worker.js','content.js','popup.html','popup.js'):
        shutil.copyfile(source/name,target/name)
    (target/'config.js').write_text('const QQ_BRIDGE = '+json.dumps({'url':'http://127.0.0.1:'+str(port),'token':cfg['token']})+';\n',encoding='utf-8')
    return target


def chat_url(value):
    if not isinstance(value,str):return False
    parts=urlsplit(value)
    return parts.scheme=='https' and parts.netloc=='chatgpt.com' and (parts.path=='/' or re.fullmatch(r'/c/[A-Za-z0-9-]+',parts.path) is not None) and not parts.query and not parts.fragment


class WebBridge:
    def __init__(self,folder,port=18770):
        self.folder=Path(folder);self.token=web_settings(folder)['token'];self.lock=threading.RLock()
        self.closed=threading.Event();self.last_seen=0;self.instance=None;self.owners={}
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def authorized(self):
                origin=self.headers.get('Origin','')
                host=self.headers.get('Host','')
                expected='127.0.0.1:'+str(owner.server.server_port)
                return host==expected and (not origin or re.fullmatch(r'chrome-extension://[a-p]{32}',origin) is not None) and hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+owner.token)
            def respond(self,code,value):
                data=json.dumps(value,ensure_ascii=False).encode('utf-8');self.send_response(code)
                self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers()
                try:self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):pass
            def do_GET(self):self.respond(405,{'error':'POST only'})
            def do_POST(self):
                if not self.authorized():self.respond(403,{'error':'not authorized'});return
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=160000:raise ValueError('invalid body size')
                    data=json.loads(self.rfile.read(size));instance=data.get('instance')
                    if not isinstance(instance,str) or not re.fullmatch(r'[A-Za-z0-9-]{16,80}',instance):raise ValueError('invalid browser instance')
                    with owner.lock:
                        if owner.instance and owner.instance!=instance and time.monotonic()-owner.last_seen<60:
                            self.respond(409,{'error':'another browser is connected'});return
                        owner.instance=instance;owner.last_seen=time.monotonic()
                    if self.path=='/next':self.respond(200,{'job':owner.lease()})
                    elif self.path=='/result':
                        owner.finish(data.get('id'),data.get('result'));self.respond(200,{'ok':True})
                    elif self.path=='/status':
                        with owner.lock:
                            row=owner.db.execute('SELECT state FROM jobs WHERE id=?',(data.get('id'),)).fetchone()
                            progress=data.get('progress')
                            if isinstance(progress,dict) and len(json.dumps(progress))<=8000 and row and row['state']=='leased':
                                owner.db.execute('UPDATE jobs SET result=? WHERE id=?',(json.dumps({'progress':progress}),data['id']));owner.db.commit()
                        self.respond(200,{'active':bool(row and row['state']=='leased')})
                    else:self.respond(404,{'error':'unknown endpoint'})
                except (ValueError,TypeError,KeyError):self.respond(400,{'error':'invalid request'})
        # A duplicate panel must fail before touching the running panel's queue.
        self.server=ThreadingHTTPServer(('127.0.0.1',port),Handler,bind_and_activate=False);self.server.daemon_threads=True
        self.server.allow_reuse_address=False
        try:
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
                self.server.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            self.server.server_bind();self.server.server_activate()
        except Exception:
            self.server.server_close()
            raise
        try:
            self.db=sqlite3.connect(self.folder/'jobs.sqlite3',check_same_thread=False);self.db.row_factory=sqlite3.Row
            self.db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, action TEXT,payload TEXT,state TEXT,result TEXT)')
            self.db.execute("UPDATE jobs SET state='uncertain' WHERE state IN ('queued','leased')");self.db.commit()
        except Exception:
            self.server.server_close()
            if hasattr(self,'db'):self.db.close()
            raise
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def lease(self):
        with self.lock:
            row=self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:return None
            self.db.execute("UPDATE jobs SET state='leased' WHERE id=?",(row['id'],));self.db.commit()
            return {'id':row['id'],'action':row['action'],'payload':json.loads(row['payload'])}

    def finish(self,uid,result):
        if not isinstance(uid,str) or not isinstance(result,dict) or len(json.dumps(result))>150000:raise ValueError('invalid result')
        with self.lock:
            self.db.execute("UPDATE jobs SET state='done',result=? WHERE id=? AND state='leased'",(json.dumps(result,ensure_ascii=False),uid));self.db.commit()

    def call(self,action,payload,stop,timeout=120):
        if action not in ('list','new','send','inspect'):raise ValueError('不支持的网页操作。')
        ready_deadline=time.monotonic()+min(5,timeout)
        while not self.last_seen and time.monotonic()<ready_deadline and not stop.is_set():time.sleep(.1)
        if not self.last_seen or time.monotonic()-self.last_seen>45:raise ValueError('ChatGPT 网页扩展尚未连接。电脑点击“ChatGPT 网页通道”生成扩展，在浏览器安装并点“连接”。网页需已登录；无需 API Key。')
        uid=secrets.token_hex(16);deadline=time.monotonic()+timeout
        with self.lock:
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(uid,action,json.dumps(payload,ensure_ascii=False),'queued',''));self.db.commit()
        try:
            while not stop.is_set() and not self.closed.is_set():
                with self.lock:row=self.db.execute('SELECT state,result FROM jobs WHERE id=?',(uid,)).fetchone()
                if row['state']=='done':
                    result=json.loads(row['result'])
                    if not result.get('ok'):raise ValueError(redact(str(result.get('error','网页操作失败')))[:500])
                    return result
                if time.monotonic()>=deadline:raise ValueError('网页等待超时；可能已发送，请先查看网页。不会自动重发。')
                time.sleep(.1)
            raise EOFError('网页桥接已停止；网页可能仍在生成，请到浏览器确认。')
        finally:
            with self.lock:self.db.execute("UPDATE jobs SET state='uncertain' WHERE id=? AND state IN ('queued','leased')",(uid,));self.db.commit()

    def claim(self,tab,owner):
        with self.lock:
            if tab in self.owners and self.owners[tab]!=owner:raise ValueError('此 ChatGPT 标签页已由另一个桥接会话使用，请选其他页面或新建。')
            self.owners[tab]=owner

    def release(self,owner):
        with self.lock:self.owners={tab:value for tab,value in self.owners.items() if value!=owner}

    def close(self):
        if self.closed.is_set():return
        self.closed.set();self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        with self.lock:self.db.close()


class BrowserConversation:
    def __init__(self,bridge,stop=None,timeout=600,initial_new=False):
        self.bridge=bridge;self.stop=stop or threading.Event();self.timeout=timeout;self.owner=secrets.token_hex(12)
        self.mode='home';self.selected=None;self.rows=[];self.output='';self.finished=False;self.initial_new=initial_new
    def safe_output(self,value):return redact(str(value))
    def prompt(self,value):return Turn(self.safe_output(value))
    def start(self):
        result=self.bridge.call('list',{},self.stop,20);self.rows=result.get('tabs',[])
        if self.initial_new:return self.answer('1')
        return self.home()
    def home(self):
        self.mode='home'
        return self.prompt('【ChatGPT 网页】\n1. 新建普通聊天\n2. 选择浏览器里已打开的聊天\n0. 结束桥接\n沿用浏览器登录；只发送你接下来提交的文字。')
    def answer(self,text):
        text=text.strip()
        if self.mode=='select' and text=='0':return self.home()
        if text in ('新建','new') and self.mode=='chat':return self.answer_home_new()
        if text in ('退出','结束','exit') or (self.mode!='chat' and text=='0'):
            self.finished=True;return Turn('ChatGPT 网页桥接已结束，网页和聊天记录保留。',True)
        if self.mode=='home':
            if text=='1':
                result=self.bridge.call('new',{},self.stop,20);return self.choose(result['tab'])
            if text=='2':
                result=self.bridge.call('list',{},self.stop,20);self.rows=[r for r in result.get('tabs',[]) if type(r.get('id')) is int and chat_url(r.get('url'))][:8];self.mode='select'
                return self.prompt('【选择已打开的 ChatGPT 聊天】\n'+'\n'.join('%d. %s'%(i,' '.join(str(r.get('title','ChatGPT')).split())[:80]) for i,r in enumerate(self.rows,1))+'\n0. 返回\n这里只列出已打开的标签页；历史聊天请先在浏览器打开。')
            return self.home()
        if self.mode=='select':
            if text.isdigit() and 1<=int(text)<=len(self.rows):return self.choose(self.rows[int(text)-1])
            return self.prompt('请回复列表里的序号，或“退出”结束。')
        if text in ('切换','列表'):return self.home()
        if text.startswith('网页诊断 '):
            result=self.bridge.call('inspect',{'tab_id':self.selected['id'],'probe':text[5:][:80]},self.stop,20)
            return self.prompt('网页结构诊断（仅标签与属性，不读取其他消息正文）：\n'+json.dumps(result.get('shape',{}),ensure_ascii=False))
        if not text or len(text)>12000:return self.prompt('请发送 1–12000 字的要求；“退出”结束，“切换”选择其他聊天。')
        result=self.bridge.call('send',{'tab_id':self.selected['id'],'expected_url':self.selected['url'],'text':text},self.stop,self.timeout)
        if not chat_url(result.get('url')):raise ValueError('网页已跳转，未确认答复归属；请检查浏览器，不会自动重发。')
        self.selected['url']=result['url'];self.output=str(result.get('text',''))[-24000:]
        return Turn(self.safe_output(self.output),kind='result')
    def answer_home_new(self):
        self.mode='home';return self.answer('1')
    def choose(self,row):
        if type(row.get('id')) is not int or not chat_url(row.get('url')):raise ValueError('只能选择普通 ChatGPT 聊天页。')
        self.bridge.claim(row['id'],self.owner);self.selected=dict(row);self.mode='chat'
        return self.prompt('已选择 ChatGPT 网页聊天。现在发送你的要求；尚未向网页提交任何文字。请保持浏览器打开，避免同时在此页面输入。')
    def close(self):self.bridge.release(self.owner)
