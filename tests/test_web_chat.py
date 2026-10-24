import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from unittest.mock import patch

from web_chat import BrowserConversation,WebBridge,chat_url,prepare_extension
from task_hub import Dispatcher,Manager
from tools import Catalog


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.bridge=WebBridge(self.root/'web',port=0);self.stop=threading.Event();self.calls=[]
        self.instance='fixture-browser-12345678'
    def tearDown(self):self.stop.set();self.bridge.close();self.temp.cleanup()
    def post(self,path,data=None,token=None,origin=None):
        payload={'instance':self.instance,**(data or {})};headers={'Content-Type':'application/json','Authorization':'Bearer '+(token or self.bridge.token)}
        if origin:headers['Origin']=origin
        req=Request('http://127.0.0.1:'+str(self.bridge.server.server_port)+path,data=json.dumps(payload).encode(),headers=headers,method='POST')
        with urlopen(req,timeout=3) as response:return json.load(response)
    def worker(self):
        def run():
            while not self.stop.is_set():
                try:job=self.post('/next')['job']
                except OSError:return
                if job:
                    self.calls.append(job)
                    result={'ok':True}
                    if job['action']=='list':result['tabs']=[{'id':7,'url':'https://chatgpt.com/c/existing','title':'已有聊天'}]
                    elif job['action']=='new':result['tab']={'id':8,'url':'https://chatgpt.com/','title':'新聊天'}
                    elif job['action']=='send':result.update(text='网页模拟结果：'+job['payload']['text'],url='https://chatgpt.com/c/new-chat')
                    self.post('/result',{'id':job['id'],'result':result})
                self.stop.wait(.02)
        thread=threading.Thread(target=run,daemon=True);thread.start();self.addCleanup(lambda:(self.stop.set(),thread.join(timeout=3)))
    def wait(self,manager,uid,state):
        end=time.monotonic()+4
        while time.monotonic()<end:
            row=manager.store.get(uid)
            if row['state']==state:return row
            time.sleep(.02)
        self.fail(str(row))
    def test_auth_origin_and_single_browser_are_enforced(self):
        with self.assertRaises(HTTPError) as caught:self.post('/next',token='wrong')
        self.assertEqual(caught.exception.code,403);caught.exception.close()
        with self.assertRaises(HTTPError) as caught:self.post('/next',origin='https://outside.example')
        self.assertEqual(caught.exception.code,403);caught.exception.close()
        self.post('/next');self.instance='another-browser-123456'
        with self.assertRaises(HTTPError) as caught:self.post('/next')
        self.assertEqual(caught.exception.code,409);caught.exception.close()
    def test_new_select_send_and_context_url_are_preserved(self):
        self.worker();chat=BrowserConversation(self.bridge);self.addCleanup(chat.close)
        self.assertIn('新建普通聊天',chat.start().text)
        self.assertIn('尚未',chat.answer('1').text)
        self.assertFalse(any(row['action']=='send' for row in self.calls))
        self.assertIn('第一问',chat.answer('第一问').text)
        self.assertIn('第二问',chat.answer('第二问').text)
        sent=[row for row in self.calls if row['action']=='send']
        self.assertEqual(sent[0]['payload']['expected_url'],'https://chatgpt.com/')
        self.assertEqual(sent[1]['payload']['expected_url'],'https://chatgpt.com/c/new-chat')
        self.assertTrue(chat.answer('退出').finished)
    def test_existing_tabs_and_back_do_not_create_or_send(self):
        self.worker();chat=BrowserConversation(self.bridge);self.addCleanup(chat.close);chat.start()
        self.assertIn('已有聊天',chat.answer('2').text)
        self.assertIn('新建普通聊天',chat.answer('0').text)
        chat.answer('2');self.assertIn('已选择',chat.answer('1').text)
        self.assertFalse(any(row['action'] in ('new','send') for row in self.calls))
        with self.assertRaises(ValueError):self.bridge.claim(7,'another-owner')
    def test_lease_is_not_replayed_after_cancellation_and_late_result(self):
        self.post('/next');cancel=threading.Event();errors=[]
        def run():
            try:self.bridge.call('send',{'text':'fixture'},cancel,timeout=2)
            except EOFError:errors.append('cancelled')
        thread=threading.Thread(target=run);thread.start()
        end=time.monotonic()+2;job=None
        while not job and time.monotonic()<end:job=self.post('/next')['job'];time.sleep(.01)
        self.assertIsNotNone(job);self.assertIsNone(self.post('/next')['job'])
        cancel.set();thread.join(timeout=2);self.assertEqual(errors,['cancelled'])
        self.post('/result',{'id':job['id'],'result':{'ok':True,'text':'late'}})
        with self.bridge.lock:state=self.bridge.db.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()[0]
        self.assertEqual(state,'uncertain');self.assertIsNone(self.post('/next')['job'])
    def test_url_scope_and_generated_config_stay_local(self):
        for url in ('https://chatgpt.com/c/abc','https://chatgpt.com/'):self.assertTrue(chat_url(url))
        for url in ('https://chatgpt.com.evil/c/a','http://chatgpt.com/','https://chatgpt.com/?token=x','https://chatgpt.com/g/private'):self.assertFalse(chat_url(url))
        target=prepare_extension(self.root/'generated',self.bridge.server.server_port)
        self.assertTrue((target/'manifest.json').is_file());self.assertIn('127.0.0.1',(target/'config.js').read_text())
        source=Path(__file__).resolve().parents[1]/'extensions'/'chatgpt'
        self.assertFalse((source/'config.js').exists())
        manifest=json.loads((target/'manifest.json').read_text(encoding='utf-8'));self.assertNotIn('cookies',manifest['permissions'])
    def test_duplicate_panel_does_not_interrupt_current_browser_request(self):
        with self.bridge.lock:
            self.bridge.db.execute("INSERT INTO jobs VALUES ('running-fixture','send','{}','leased','')");self.bridge.db.commit()
        with self.assertRaises(OSError):WebBridge(self.bridge.folder,port=self.bridge.server.server_port)
        with self.bridge.lock:
            self.assertEqual(self.bridge.db.execute("SELECT state FROM jobs WHERE id='running-fixture'").fetchone()[0],'leased')
    def test_manager_routes_web_chat_without_api_key_or_codex(self):
        self.worker();catalog=Catalog(self.root/'tools.json',self.root/'shared');manager=Manager(catalog,queue.Queue(),self.root/'tasks');dispatcher=Dispatcher(manager)
        try:
            with patch('web_chat.WebBridge',return_value=self.bridge):
                dispatcher.handle('chatgpt');uid=dispatcher.selected;self.wait(manager,uid,'waiting_input')
                dispatcher.handle('1');self.wait(manager,uid,'waiting_input')
                dispatcher.handle('网页问题');row=self.wait(manager,uid,'waiting_input');self.assertIn('网页模拟结果：网页问题',row['output'])
                dispatcher.handle('结束 '+uid);self.wait(manager,uid,'completed')
            self.assertEqual(catalog.ai_store.all(),[])
        finally:manager.close()
    def test_browser_dom_adapter_handles_drafts_and_uncertain_completion(self):
        node=shutil.which('node') or os.environ.get('QQ_BRIDGE_NODE')
        if not node:self.skipTest('Node.js unavailable')
        for name in ('browser_content.test.js','browser_worker.test.js'):
            result=subprocess.run([node,str(Path(__file__).with_name(name))],capture_output=True,text=True,encoding='utf-8',timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
