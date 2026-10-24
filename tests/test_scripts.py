import contextlib
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest

from bridge_core import BridgeClient,ChatIO
from script_backend import InteractiveTarget,run_backend
from settings import Settings,load_connection,save_connection,validate_prefix
from onebot_adapter import incoming_text


class ScriptTests(unittest.TestCase):
    def test_arguments_cwd_and_literal_input_use_selected_python(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            script=root/'script.py'
            script.write_text('import sys,os\nprint(sys.argv[1:])\nprint(os.getcwd())\nx=input("输入：")\nprint(repr(x))\n',encoding='utf-8')
            cwd=root/'working folder';cwd.mkdir()
            target=InteractiveTarget(script,python_executable=sys.executable,arguments=['a b','$(literal)'],cwd=cwd)
            try:
                first=target.start()
                self.assertIn("['a b', '$(literal)']",first.text)
                self.assertIn(str(cwd),first.text)
                turn=target.answer('  [CQ:at,qq=1] {Enter} & $(literal)  ')
                self.assertTrue(turn.finished)
                self.assertIn("'  [CQ:at,qq=1] {Enter} & $(literal)  '",turn.text)
            finally:
                target.close()

    def test_each_local_secret_prompt_gets_its_own_value_and_both_are_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'script.py'
            script.write_text('from getpass import getpass\na=getpass("第一个口令：")\nb=getpass("第二个口令：")\nprint(a,b)\nx=input("普通输入：")\nprint(x)\n',encoding='utf-8')
            target=InteractiveTarget(script)
            try:
                self.assertTrue(target.start().secret)
                turn=target.answer('fixture-secret-one')
                self.assertTrue(turn.secret)
                self.assertIn('第二个口令',turn.text)
                turn=target.answer('fixture-secret-two')
                self.assertFalse(turn.secret)
                self.assertNotIn('fixture-secret-one',turn.text)
                self.assertNotIn('fixture-secret-two',turn.text)
                self.assertIn('普通输入',turn.text)
                self.assertTrue(target.answer('').finished)
            finally:
                target.close()

    def test_backend_prompts_secrets_and_restart_without_model_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            script=root/'script.py'
            script.write_text('from getpass import getpass\na=getpass("本机口令A：")\nb=getpass("本机口令B：")\nx=input("你的名字：")\nprint("你好，"+x)\n',encoding='utf-8')
            prompts=[]
            def provide(prompt):
                prompts.append(prompt)
                return 'private-fixture-'+str(len(prompts))
            notices=queue.Queue();stop=threading.Event();logs=[]
            with ChatIO(root/'state',port=0,console=False) as bridge:
                worker=threading.Thread(target=run_backend,args=(bridge,script,notices,stop,logs.append),
                    kwargs={'secret_provider':provide,'prefix':'/run'})
                worker.start()
                try:
                    first=notices.get(timeout=4)
                    self.assertIn('你的名字',first)
                    self.assertIn('/run',first)
                    client=BridgeClient(bridge.connection_path)
                    def answer(uid,text):
                        client.request('/jobs',{'id':uid,'text':text})
                        until=time.monotonic()+4
                        while time.monotonic()<until:
                            item=client.request('/jobs/'+uid)
                            if item['status'] in ('done','failed'):
                                return item['result']
                            time.sleep(.02)
                        self.fail('目标程序未回复')
                    self.assertIn('你好，小明',answer('name','小明'))
                    self.assertIn('你的名字',answer('restart','/restart'))
                    self.assertEqual(len(prompts),4)
                    self.assertIn('口令A',prompts[0]);self.assertIn('口令B',prompts[1])
                finally:
                    stop.set();bridge.stop_event.set();worker.join(timeout=4)
                self.assertFalse(worker.is_alive())
            self.assertNotIn('private-fixture-','\n'.join(logs))
            self.assertNotIn(b'private-fixture-',(root/'state/jobs.sqlite3').read_bytes())

    def test_timeout_and_nonzero_exit_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'script.py'
            script.write_text('import time\ntime.sleep(10)\n',encoding='utf-8')
            target=InteractiveTarget(script,timeout=.2)
            try:
                turn=target.start()
                self.assertTrue(turn.finished);self.assertEqual(turn.code,124)
                self.assertIsNone(target.process)
            finally:
                target.close()
            script.write_text('raise RuntimeError("example failure")\n',encoding='utf-8')
            target=InteractiveTarget(script)
            try:
                turn=target.start()
                self.assertNotEqual(turn.code,0)
                self.assertIn('example failure',turn.text)
            finally:
                target.close()

    def test_settings_roundtrip_and_prefix_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'settings.json'
            value=Settings(target='example.py',arguments=['a b'],prefix='/run',manage_backend=False)
            value.save(path)
            loaded=Settings.load(path)
            self.assertEqual(loaded,value)
            self.assertNotIn('token',json.loads(path.read_text(encoding='utf-8')))
            connection=Path(folder)/'onebot.json'
            cfg=load_connection(connection)
            self.assertGreaterEqual(len(cfg['token']),24)
            self.assertEqual(load_connection(connection),cfg)
            with self.assertRaises(ValueError):
                save_connection({**cfg,'api_url':'http://example.org:1234'},connection)
            self.assertEqual(load_connection(connection),cfg)
        for prefix in ('','x','/has space','/','/\n','/'+('x'*16)):
            with self.assertRaises(ValueError):validate_prefix(prefix)
        with self.assertRaises(ValueError):Settings(timeout='300').validate()

    def test_custom_prefix_is_used_by_callback(self):
        now=time.time()
        value={'self_id':12345,'user_id':12345,'message_id':1,'time':now,
            'post_type':'message','message_type':'private','message':'/run hello'}
        self.assertIsNone(incoming_text(value,'12345',now))
        self.assertEqual(incoming_text(value,'12345',now,prefix='/run')[1],'hello')


if __name__=='__main__':
    unittest.main()
