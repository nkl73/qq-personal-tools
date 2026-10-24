from dataclasses import replace
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ai_service import AIProfile,Conversation,MAX_HISTORY,ProfileStore,call,endpoint
from bridge_core import BridgeClient,ChatIO
import mobile_menu
from onebot_adapter import EventReceiver,relay_background
from task_hub import Dispatcher,FINAL,Manager,run_hub
from tools import Catalog,Tool
from test_onebot import FakeServer,TOKEN,event,post

KEY='fixture-ai-key-do-not-use-for-real-services'


class MockAI:
    def __init__(self):
        self.received=[];self.mode='ok';self.entered=threading.Event()
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                outer.received.append((self.path,self.headers.get('Authorization'),{'operation':'models'}))
                value={'data':[{'id':'fixture-chat-a'},{'id':'fixture-chat-b'},{'id':'fixture-chat-a'},
                    {'id':KEY},{'id':'invalid\nvalue'}]}
                if outer.mode=='malformed':value={'wrong':'format'}
                raw=json.dumps(value).encode('utf-8')
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.received.append((self.path,self.headers.get('Authorization'),data))
                outer.entered.set()
                code=200;body={}
                if outer.mode=='slow':time.sleep(10)
                if outer.mode=='error':code=401;body={'error':KEY}
                elif outer.mode=='redirect':
                    self.send_response(307);self.send_header('Location',outer.url+'/redirected');self.send_header('Content-Length','0');self.end_headers();return
                elif outer.mode=='malformed':body={'wrong':'format'}
                elif outer.mode=='tools':body={'choices':[{'message':{'tool_calls':[{'type':'function'}]}}]}
                else:
                    text='模拟回复：'+data['messages'][-1]['content']
                    if outer.mode=='echo-key':text+=' '+KEY
                    if outer.mode=='parts':text=[{'type':'text','text':'分段文字'},{'type':'image','image':'ignored'}]
                    body={'choices':[{'message':{'content':text},'finish_reason':'stop'}]}
                raw=json.dumps(body,ensure_ascii=False).encode('utf-8')
                try:
                    self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)+'/v1'

    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)


class AITests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name)
        self.server=MockAI()
        self.profile=AIProfile('ai','测试文字 AI',self.server.url,'fixture-model','请使用中文',5)
        self.catalog=Catalog(self.root/'tools.json',self.root/'shared')
        self.notices=queue.Queue();self.manager=Manager(self.catalog,self.notices,self.root/'state')
        self.dispatcher=Dispatcher(self.manager)

    def tearDown(self):
        self.manager.close();self.server.close();self.folder.cleanup()

    def wait(self,uid,states,timeout=6):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            row=self.manager.store.get(uid)
            if row['state'] in states:return row
            time.sleep(.02)
        self.fail('AI 任务未到达预期状态')

    def register(self):self.catalog.save_ai(self.profile,KEY)

    def test_endpoint_accepts_service_paths_and_rejects_unsafe_key_placement(self):
        self.assertEqual(endpoint('https://example.com/api/v3/'),'https://example.com/api/v3/chat/completions')
        self.assertEqual(endpoint('https://example.com/v1/chat/completions'),'https://example.com/v1/chat/completions')
        self.assertIn('[::1]',endpoint('http://[::1]:9000/v1'))
        for value in ('http://example.com/v1','https://key@example.com/v1','https://example.com/v1?key=x',
            'file:///tmp/a','https://example.com/v1#secret','https://example.com/a\nb','https://example.com:bad/v1'):
            with self.assertRaises(ValueError):endpoint(value)

    def test_profile_is_a_dynamic_tool_without_persisting_plaintext_key(self):
        self.register()
        self.assertEqual(self.catalog.get('ai').kind,'ai-chat')
        self.assertNotIn(KEY,self.catalog.ai_store.path.read_text(encoding='utf-8'))
        self.assertEqual(ProfileStore(self.catalog.ai_store.path).key('ai'),'')
        self.assertEqual(self.catalog.ai_store.key('ai'),KEY)
        with self.assertRaises(ValueError):self.catalog.save(Tool('ai','重名',target='x.py'))
        with self.assertRaises(ValueError):self.catalog.save_ai(replace(self.profile,id='calc'),KEY)
        for content in (self.dispatcher.handle('帮助 ai'),self.dispatcher.handle('菜单 AI'),self.dispatcher.handle('菜单 6')):
            self.assertIn('AI',content);self.assertNotIn(KEY,content);self.assertNotIn(self.server.url,content)
        self.assertIn('运行 ai','\n'.join(mobile_menu.tool_pages(self.catalog)))
        self.catalog.remove('ai')
        with self.assertRaises(ValueError):self.catalog.get('ai')
        self.assertEqual(self.catalog.ai_store.key('ai'),'')

    @unittest.skipUnless(os.name=='nt','Windows DPAPI only')
    def test_windows_saved_key_roundtrips_encrypted_and_can_be_forgotten(self):
        self.catalog.save_ai(self.profile,KEY,True)
        store=ProfileStore(self.catalog.ai_store.path)
        self.assertEqual(store.key('ai'),KEY)
        self.assertTrue(store.remembered('ai'))
        self.assertNotIn(KEY,store.path.read_text(encoding='utf-8'))
        store.save(self.profile,None,False)
        self.assertEqual(store.key('ai'),KEY)
        self.assertEqual(ProfileStore(store.path).key('ai'),'')
        self.assertEqual(json.loads(store.path.read_text(encoding='utf-8'))[0]['encrypted_key'],'')

    def test_http_request_uses_model_history_and_bearer_with_no_sdk(self):
        chat=Conversation(self.profile,KEY)
        first=chat.ask('第一问',threading.Event())
        chat.ask('接着说',threading.Event())
        path,authorization,body=self.server.received[-1]
        self.assertEqual(path,'/v1/chat/completions');self.assertEqual(authorization,'Bearer '+KEY)
        self.assertEqual(body['model'],'fixture-model');self.assertFalse(body['stream'])
        self.assertEqual([m['role'] for m in body['messages']],['system','user','assistant','user'])
        self.assertIn(first,str(body['messages']))
        self.assertNotIn('tools',body)
        call(self.profile,'',[{'role':'user','content':'本机无需密钥'}])
        self.assertIsNone(self.server.received[-1][1])
        with self.assertRaisesRegex(ValueError,'补填'):
            call(replace(self.profile,base_url='https://example.com/v1'),'',[])

    def test_provider_errors_do_not_echo_keys_or_retry_or_poison_history(self):
        chat=Conversation(self.profile,KEY);self.server.mode='error'
        with self.assertRaisesRegex(ValueError,'HTTP 401') as result:chat.ask('失败的问题',threading.Event())
        self.assertNotIn(KEY,str(result.exception));self.assertEqual(len(self.server.received),1)
        self.assertEqual(chat.messages,[])
        self.server.mode='ok';chat.ask('重提问题',threading.Event())
        self.assertNotIn('失败的问题',str(self.server.received[-1][2]))

    def test_redirects_and_malformed_or_tool_only_responses_fail_clearly(self):
        for mode,pattern in (('redirect','重定向'),('malformed','格式'),('tools','仅支持文字')):
            self.server.mode=mode
            with self.assertRaisesRegex(ValueError,pattern):call(self.profile,KEY,[{'role':'user','content':'测试'}])
        self.assertEqual(len(self.server.received),3)
        self.server.mode='parts'
        self.assertEqual(call(self.profile,KEY,[{'role':'user','content':'文字'}]),'分段文字')

    def test_model_echo_of_configured_key_is_redacted_before_task_storage(self):
        self.register();self.server.mode='echo-key'
        uid=self.manager.start('ai','普通问题')
        row=self.wait(uid,{'waiting_input'})
        self.assertNotIn(KEY,row['output']);self.assertIn('密钥已隐藏',row['output'])
        self.assertNotIn(KEY,'\n'.join(list(self.notices.queue)))
        self.assertNotIn(KEY.encode(),(self.root/'state/tasks.sqlite3').read_bytes())

    def test_context_is_bounded_without_silently_truncating_a_new_question(self):
        chat=Conversation(self.profile,KEY)
        chat.messages=[{'role':role,'content':'x'*2000} for _ in range(25) for role in ('user','assistant')]
        with patch('ai_service.call',return_value='回答') as invoked:
            chat.ask('新问题',threading.Event())
            sent=invoked.call_args.args[2]
            self.assertLessEqual(len(sent),41)
            self.assertLessEqual(sum(len(m['content']) for m in sent),MAX_HISTORY+8000)
            self.assertEqual(sent[-1]['content'],'新问题')
        with self.assertRaises(ValueError):chat.ask('x'*12001,threading.Event())

    def test_qq_commands_keep_ai_sessions_separate_and_can_finish(self):
        self.register()
        self.dispatcher.handle('运行 ai 会话一')
        a=self.dispatcher.selected;self.wait(a,{'waiting_input'})
        self.dispatcher.handle('运行 ai 会话二')
        b=self.dispatcher.selected;self.wait(b,{'waiting_input'})
        self.dispatcher.handle('回复 '+a+' 继续一');self.wait(a,{'waiting_input'})
        messages=self.server.received[-1][2]['messages']
        self.assertIn('会话一',str(messages));self.assertNotIn('会话二',str(messages))
        self.assertIn('AI',self.dispatcher.handle('菜单 AI'))
        self.assertEqual(self.manager.store.get(a)['state'],'waiting_input')
        self.dispatcher.handle('结束 '+a)
        self.assertEqual(self.wait(a,FINAL)['state'],'completed')
        self.assertEqual(self.manager.store.get(b)['state'],'waiting_input')
        self.manager.cancel(b);self.assertEqual(self.wait(b,FINAL)['state'],'cancelled')

    def test_natural_request_starts_ai_continues_and_reopens_after_finished_selection(self):
        self.register()
        reply=self.dispatcher.handle('阅读本文档，概述内容')
        first=self.dispatcher.selected
        self.assertIn('已新建 AI 会话',reply)
        self.assertIn('模拟回复：阅读本文档，概述内容',self.wait(first,{'waiting_input'})['output'])
        self.dispatcher.handle('改得简短一些');self.wait(first,{'waiting_input'})
        self.assertEqual(len(self.manager.tasks),1)
        self.assertIn('阅读本文档',str(self.server.received[-1][2]['messages']))
        # Ending from the desktop leaves a stale phone selection, as in the reported bug.
        self.manager.end_chat(first);self.wait(first,{'completed'})
        reply=self.dispatcher.handle('阅读本文档，概述内容')
        second=self.dispatcher.selected
        self.assertNotEqual(first,second);self.assertIn('已新建 AI 会话',reply)
        self.wait(second,{'waiting_input'})
        self.assertNotIn('改得简短一些',str(self.server.received[-1][2]['messages']))

    def test_natural_request_during_generation_is_not_sent_or_started_twice(self):
        self.register();self.server.mode='slow'
        self.dispatcher.handle('生成中')
        self.assertTrue(self.server.entered.wait(3))
        uid=self.dispatcher.selected
        reply=self.dispatcher.handle('不要重复创建')
        self.assertIn('这条内容尚未提交',reply)
        self.assertEqual(len(self.manager.tasks),1);self.assertEqual(len(self.server.received),1)
        self.manager.cancel(uid);self.wait(uid,FINAL)

    def test_natural_request_reports_missing_key_and_ambiguous_ai_without_running(self):
        self.catalog.save_ai(self.profile,'')
        self.assertIn('补填',self.dispatcher.handle('概述文档'))
        self.assertEqual(self.manager.tasks,{})
        self.catalog.save_ai(replace(self.profile,id='ai2',name='第二个 AI'),KEY)
        reply=self.dispatcher.handle('概述文档')
        self.assertIn('1. 测试文字 AI',reply)
        self.assertIn('2. 第二个 AI',reply)
        self.assertIn('/py 1',reply)
        self.assertEqual(self.manager.tasks,{})

    def test_cancellation_stops_inflight_request_and_frees_worker_promptly(self):
        self.register();self.server.mode='slow'
        uid=self.manager.start('ai','慢请求')
        self.assertTrue(self.server.entered.wait(3))
        started=time.monotonic();self.manager.cancel(uid)
        self.assertEqual(self.wait(uid,FINAL)['state'],'cancelled')
        self.assertLess(time.monotonic()-started,4)
        self.assertEqual(len(self.server.received),1)
        fast=self.manager.start('calc','6*7')
        self.assertEqual(self.wait(fast,FINAL)['output'],'42')

    def test_timeout_reports_once_and_allows_a_new_question(self):
        self.register();self.server.mode='slow'
        uid=self.manager.start('ai','慢请求')
        row=self.wait(uid,{'waiting_input'},timeout=7)
        self.assertIn('超时',row['output']);self.assertEqual(len(self.server.received),1)
        self.server.mode='ok';self.manager.answer(uid,'重提')
        self.assertIn('模拟回复：重提',self.wait(uid,{'waiting_input'})['output'])

    def test_ai_answers_travel_through_full_onebot_callback_and_send_path(self):
        self.register()
        qq=FakeServer();stop=threading.Event();ready=threading.Event();receivers=[]
        notices=queue.Queue();config=self.root/'connection.json'
        config.write_text(json.dumps({'api_url':qq.url,'event_port':0,'token':TOKEN}),encoding='utf-8')
        def receiver(*args,**kwargs):
            value=EventReceiver(*args,**kwargs);receivers.append(value);ready.set();return value
        try:
            with ChatIO(self.root/'wire',port=0,console=False) as bridge:
                hub=threading.Thread(target=run_hub,args=(bridge,self.catalog,notices,stop),kwargs={'directory':self.root/'wire-state'})
                transport=threading.Thread(target=relay_background,args=(BridgeClient(bridge.connection_path),stop),
                    kwargs={'notices':notices,'directory':self.root/'wire-transport','config_path':config,'log':lambda _:None})
                with patch('onebot_adapter.EventReceiver',side_effect=receiver):
                    hub.start();transport.start()
                    try:
                        self.assertTrue(ready.wait(4));post(receivers[0],event('/py 运行 ai 手机提问'))
                        deadline=time.monotonic()+6
                        while time.monotonic()<deadline:
                            texts=[v['message'][0]['data']['text'] for v in qq.sent]
                            if any('模拟回复：手机提问' in text for text in texts):break
                            time.sleep(.03)
                        self.assertTrue(any('模拟回复：手机提问' in text for text in texts))
                        self.assertFalse(any('任务已创建' in text or '等待提问' in text for text in texts))
                        self.assertNotIn(KEY,'\n'.join(texts))
                    finally:
                        stop.set();bridge.stop_event.set();hub.join(timeout=6);transport.join(timeout=6)
                    self.assertFalse(hub.is_alive());self.assertFalse(transport.is_alive())
        finally:qq.close()


if __name__=='__main__':unittest.main()
