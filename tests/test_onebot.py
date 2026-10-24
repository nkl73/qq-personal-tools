import contextlib
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from bridge_core import BridgeClient,ChatIO
from onebot_adapter import BackgroundStore,EventReceiver,OneBotClient,incoming_text,read_event_body,relay_background,send_background_result

ACCOUNT='123456789'
TOKEN='fixture-private-token-'+'a'*32


def event(text='/py 普通文字',**changes):
    value={'post_type':'message','message_type':'private','self_id':int(ACCOUNT),'user_id':int(ACCOUNT),
        'target_id':int(ACCOUNT),'message_id':123,'time':int(time.time()),
        'message':[{'type':'text','data':{'text':text}}]}
    value.update(changes)
    return value


def post(receiver,value,token=TOKEN,origin=None):
    body=json.dumps(value,ensure_ascii=False).encode('utf-8')
    headers={'Content-Type':'application/json','x-self-id':ACCOUNT,
        'x-signature':'sha1='+hmac.new(token.encode(),body,hashlib.sha1).hexdigest()}
    if origin:
        headers['Origin']=origin
    with urlopen(Request('http://127.0.0.1:%d/events'%receiver.port,data=body,headers=headers),timeout=3) as response:
        return response.status


class FakeServer:
    def __init__(self):
        self.sent=[]
        self.headers=[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_POST(self):
                owner.headers.append(self.headers.get('Authorization'))
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path=='/get_login_info':
                    data={'user_id':int(ACCOUNT)}
                elif self.path=='/send_private_msg':
                    owner.sent.append(body)
                    data={'message_id':1000+len(owner.sent)}
                else:
                    self.send_error(404)
                    return
                encoded=json.dumps({'status':'ok','retcode':0,'data':data}).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url='http://127.0.0.1:'+str(self.server.server_address[1])
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class BackgroundTests(unittest.TestCase):
    def test_chunked_native_callback_and_limits(self):
        body=json.dumps(event(),ensure_ascii=False).encode('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            store=BackgroundStore(folder)
            receiver=EventReceiver(store,TOKEN,ACCOUNT,port=0,log=lambda _:None)
            connection=http.client.HTTPConnection('127.0.0.1',receiver.port,timeout=3)
            try:
                headers={'Content-Type':'application/json','x-self-id':ACCOUNT,
                    'x-signature':'sha1='+hmac.new(TOKEN.encode(),body,hashlib.sha1).hexdigest()}
                connection.request('POST','/events',body=iter([body[:7],body[7:]]),headers=headers,encode_chunked=True)
                response=connection.getresponse()
                self.assertEqual(response.status,200)
                response.read()
                self.assertEqual(len(store.active()),1)
            finally:
                connection.close()
                receiver.close()
                store.close()
        with self.assertRaises(OverflowError):
            read_event_body(io.BytesIO(b'FFFF\r\n'),{'Transfer-Encoding':'chunked'},limit=100)
        with self.assertRaises(ValueError):
            read_event_body(io.BytesIO(b'3\r\nabcXY'),{'Transfer-Encoding':'chunked'})
        with self.assertRaises(ValueError):
            read_event_body(io.BytesIO(),{'Transfer-Encoding':'chunked','Content-Length':'3'})

    def test_filter_own_private_text_and_ignore_history_replies_and_other_chats(self):
        now=time.time()
        self.assertEqual(incoming_text(event(),ACCOUNT,now)[1],'普通文字')
        self.assertEqual(incoming_text(event('/py',post_type='message_sent'),ACCOUNT,now)[1],'')
        self.assertEqual(incoming_text(event('/py原样 [CQ:at,qq=1] {Enter}'),ACCOUNT,now)[1],'原样 [CQ:at,qq=1] {Enter}')
        for changed in ({'post_type':'notice'},{'message_type':'group'},{'self_id':42},
                {'user_id':42},{'target_id':42},{'time':now-20},{'message_id':None},
                {'message':[{'type':'text','data':None}]},{'message':[{'type':'image','data':{}}]},
                {'message':'普通消息'},{'message':'[PY结果 a 1/1]\n/py 不再处理'}):
            self.assertIsNone(incoming_text(event(**changed),ACCOUNT,now),changed)

    def test_signature_and_origin_validation_and_duplicate_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            store=BackgroundStore(folder)
            receiver=EventReceiver(store,TOKEN,ACCOUNT,port=0,log=lambda _:None)
            try:
                self.assertEqual(post(receiver,event()),200)
                self.assertEqual(post(receiver,event()),200)
                self.assertEqual(len(store.active()),1)
                with self.assertRaises(HTTPError) as caught:
                    post(receiver,event(message_id=124),token='wrong')
                self.assertEqual(caught.exception.code,401)
                with self.assertRaises(HTTPError) as caught:
                    post(receiver,event(message_id=124),origin='http://outside.example')
                self.assertEqual(caught.exception.code,403)
                self.assertEqual(len(store.active()),1)
            finally:
                receiver.close()
                store.close()

    def test_api_literal_text_and_message_receipts(self):
        server=FakeServer()
        try:
            api=OneBotClient(server.url,TOKEN)
            with tempfile.TemporaryDirectory() as folder:
                store=BackgroundStore(folder)
                store.add('result','',result='[CQ:at,qq=1] {Enter} & $(literal)\n第二行')
                send_background_result(api,store,ACCOUNT,'result','[CQ:at,qq=1] {Enter} & $(literal)\n第二行',lambda _:None)
                row=store.db.execute('SELECT * FROM jobs').fetchone()
                self.assertEqual(row['status'],'sent')
                self.assertEqual(json.loads(row['receipts']),['1001'])
                self.assertEqual(server.sent[0]['user_id'],ACCOUNT)
                self.assertEqual(server.sent[0]['message'][0]['type'],'text')
                self.assertIn('[CQ:at,qq=1] {Enter} & $(literal)',server.sent[0]['message'][0]['data']['text'])
                self.assertTrue(all(value=='Bearer '+TOKEN for value in server.headers))
                store.close()
        finally:
            server.close()

    def test_uncertain_send_is_saved_and_never_automatically_retried(self):
        class LostReply:
            def require_account(self,_):
                pass
            def call(self,*args):
                raise TimeoutError('发送后未收到回执')
        with tempfile.TemporaryDirectory() as folder:
            store=BackgroundStore(folder)
            store.add('ambiguous','',result='完整结果')
            with self.assertRaises(TimeoutError):
                send_background_result(LostReply(),store,ACCOUNT,'ambiguous','完整结果',lambda _:None)
            self.assertEqual(store.active(),[])
            self.assertEqual(store.db.execute('SELECT status FROM jobs').fetchone()[0],'uncertain')
            self.assertEqual((Path(folder)/'未确认后台回传-ambiguous.txt').read_text(encoding='utf-8'),'完整结果')
            store.close()

    def test_account_change_prevents_sending_and_does_not_mark_result_as_sent(self):
        class OtherAccount:
            calls=0
            def require_account(self,_):
                raise RuntimeError('账号变化')
            def call(self,*args):
                self.calls+=1
        with tempfile.TemporaryDirectory() as folder:
            store=BackgroundStore(folder)
            store.add('ready','',result='普通回复')
            api=OtherAccount()
            with self.assertRaises(RuntimeError):
                send_background_result(api,store,ACCOUNT,'ready','普通回复')
            self.assertEqual(api.calls,0)
            self.assertEqual(store.active()[0]['status'],'ready')
            store.close()

    def test_restart_does_not_replay_previous_prompts_or_pending_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            store=BackgroundStore(folder)
            store.add('old-input','旧输入')
            store.add('old-prompt','',result='旧提示')
            store.add('old-send','',result='已尝试发送')
            store.set('old-send','sending','已尝试发送',['99'])
            store.close()
            store=BackgroundStore(folder)
            self.assertEqual(store.active(),[])
            states=dict(store.db.execute('SELECT id,status FROM jobs'))
            self.assertEqual(states,{'old-input':'stale','old-prompt':'stale','old-send':'uncertain'})
            store.close()

    def test_full_callback_queue_python_reply_flow_without_ui(self):
        server=FakeServer()
        stop=threading.Event()
        receivers=[]
        created=threading.Event()
        real_receiver=EventReceiver
        def create_receiver(*args,**kwargs):
            receiver=real_receiver(*args,**kwargs)
            receivers.append(receiver)
            created.set()
            return receiver
        try:
            with tempfile.TemporaryDirectory() as folder,contextlib.redirect_stdout(io.StringIO()):
                config=Path(folder)/'onebot.json'
                config.write_text(json.dumps({'api_url':server.url,'event_port':0,'token':TOKEN}),encoding='utf-8')
                with ChatIO(Path(folder)/'queue',port=0,console=False) as bridge:
                    def backend():
                        try:
                            while not stop.is_set():
                                uid,text=bridge.receive()
                                bridge.finish(uid,'普通文本测试：'+text)
                        except EOFError:
                            pass
                    worker=threading.Thread(target=backend)
                    worker.start()
                    with patch('onebot_adapter.EventReceiver',side_effect=create_receiver):
                        transport=threading.Thread(target=relay_background,args=(BridgeClient(bridge.connection_path),stop),
                            kwargs={'log':lambda _:None,'notices':queue.Queue(),'directory':Path(folder)/'transport','config_path':config})
                        transport.start()
                        try:
                            self.assertTrue(created.wait(3))
                            command=event('/py 后台测试 {Enter}')
                            post(receivers[0],command)
                            post(receivers[0],command)
                            deadline=time.monotonic()+5
                            while not server.sent and time.monotonic()<deadline:
                                time.sleep(.03)
                            self.assertEqual(len(server.sent),1)
                            self.assertIn('普通文本测试：后台测试 {Enter}',server.sent[0]['message'][0]['data']['text'])
                        finally:
                            stop.set()
                            bridge.stop_event.set()
                            transport.join(timeout=3)
                            worker.join(timeout=3)
                        self.assertFalse(transport.is_alive())
                        self.assertFalse(worker.is_alive())
        finally:
            server.close()

    def test_only_local_authenticated_api_urls_are_accepted(self):
        for url in ('https://127.0.0.1:80','http://example.org:80','http://127.0.0.1:80/path','http://user:pass@127.0.0.1:80'):
            with self.assertRaises(ValueError):
                OneBotClient(url,TOKEN)
        with self.assertRaises(ValueError):
            OneBotClient('http://127.0.0.1:1234','short')


if __name__=='__main__':
    unittest.main()
