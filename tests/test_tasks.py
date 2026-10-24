import contextlib
import io
import os
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bridge_core import BridgeClient,ChatIO
from task_hub import Dispatcher,FINAL,Manager,TaskStore,run_hub
from tools import Catalog,Tool,builtin,calculate
from onebot_adapter import EventReceiver,relay_background
from test_onebot import FakeServer,TOKEN,event,post


class HubTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.root=Path(self.folder.name)
        self.catalog=Catalog(self.root/'tools.json',self.root/'shared')
        self.notices=queue.Queue()
        self.secret_requests=queue.Queue()
        self.manager=Manager(self.catalog,self.notices,self.root/'state',
            on_secret=lambda uid,prompt:self.secret_requests.put((uid,prompt)),progress_interval=.05)
        self.dispatcher=Dispatcher(self.manager)

    def tearDown(self):
        self.manager.close()
        self.folder.cleanup()

    def script(self,name,source,kind='python-task',**kwargs):
        path=self.root/(name+'.py');path.write_text(source,encoding='utf-8')
        tool=Tool(name,name,kind,target=str(path),**kwargs)
        self.catalog.save(tool)
        return tool

    def wait(self,uid,states=FINAL,timeout=6):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            row=self.manager.store.get(uid)
            if row['state'] in states:return row
            time.sleep(.02)
        self.fail('任务未到达预期状态：'+str(self.manager.store.get(uid)))

    def test_builtin_tools_work_without_installing_a_script_or_api(self):
        self.assertIn('运行电脑工具',self.dispatcher.handle('菜单'))
        uid=self.manager.start('calc','12*(3+4)')
        self.assertEqual(self.wait(uid)['output'],'84')
        uid=self.manager.start('text','你好 hello\nworld')
        self.assertIn('汉字：2',self.wait(uid)['output'])
        self.assertIn('Python',self.dispatcher.handle('设备'))
        self.assertIn(uid,self.dispatcher.handle('状态'))
        self.assertIn('汉字：2',self.dispatcher.handle('结果 '+uid))

    def test_calculator_rejects_code_and_excessive_expressions(self):
        for text in ('__import__("os")','open("x")','2**1000000','[1]*1000','True','9**99**99'):
            with self.assertRaises((ValueError,OverflowError)):calculate(text)
        self.assertEqual(calculate('-2 + 5 / 2'),'0.5')

    def test_shared_file_inspection_stays_inside_its_folder(self):
        shared=self.catalog.shared;shared.mkdir()
        (shared/'example.csv').write_text('名称,值\n测试,12\n',encoding='utf-8')
        (shared/'example.json').write_text('{"items":[1,2]}',encoding='utf-8')
        (self.root/'outside.txt').write_text('private',encoding='utf-8')
        self.assertIn('example.csv',builtin('files','',shared))
        self.assertIn('读取行数：2',builtin('inspect','example.csv',shared))
        self.assertIn('items',builtin('inspect','example.json',shared))
        with self.assertRaises(ValueError):builtin('inspect','../outside.txt',shared)
        with self.assertRaises(ValueError):builtin('inspect',str(self.root/'outside.txt'),shared)

    def test_plain_python_task_uses_literal_arguments_cwd_and_progress(self):
        self.manager.feedback.set('detailed')
        work=self.root/'work';work.mkdir()
        tool=self.script('plain','import sys,os,time\nprint(sys.argv[1],flush=True)\nprint(os.getcwd(),flush=True)\ntime.sleep(.15)\nprint("progress",flush=True)\ntime.sleep(.15)\nprint("done",flush=True)\n',
            arguments=['{text}'],cwd=str(work))
        uid=self.manager.start(tool.id,'$(literal) & {Enter}')
        row=self.wait(uid)
        self.assertEqual(row['state'],'completed')
        self.assertIn('$(literal) & {Enter}',row['output'])
        self.assertIn(str(work),row['output'])
        messages=[]
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            try:message=self.notices.get(timeout=.1)
            except queue.Empty:continue
            messages.append(message)
            if uid in message and '已完成' in message:break
        self.assertTrue(any('进度' in m for m in messages))
        self.assertTrue(any('已完成' in m for m in messages))

    def test_preconfigured_executable_without_python_input_protocol(self):
        tool=Tool('external','外部程序','command',target=sys.executable,
            arguments=['-u','-c','import sys;print(sys.argv[1])','{text}'])
        self.catalog.save(tool)
        uid=self.manager.start('external','one argument with spaces & symbols')
        self.assertIn('one argument with spaces & symbols',self.wait(uid)['output'])
        with self.assertRaises(ValueError):self.manager.start('unregistered','anything')

    def test_non_utf8_output_can_be_configured_per_tool(self):
        self.script('encoded','import sys\nsys.stdout.buffer.write("中文输出".encode("gb18030"))\n',encoding='gb18030')
        uid=self.manager.start('encoded')
        self.assertEqual(self.wait(uid)['output'],'中文输出')

    def test_cancellation_stops_a_spawned_child_process(self):
        marker=self.root/'child-pid.txt'
        source='import subprocess,sys,time\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,"-c","import time;time.sleep(15)"])\nPath('+repr(str(marker))+').write_text(str(p.pid))\ntime.sleep(20)\n'
        self.script('parent',source)
        uid=self.manager.start('parent')
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(marker.exists())
        pid=int(marker.read_text())
        self.manager.cancel(uid)
        self.assertEqual(self.wait(uid)['state'],'cancelled')
        def alive():
            if os.name=='nt':
                import ctypes
                from ctypes import wintypes
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
                kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)];kernel.CloseHandle.argtypes=[wintypes.HANDLE]
                handle=kernel.OpenProcess(0x1000,False,pid)
                if not handle:return False
                try:
                    code=wintypes.DWORD();return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
                finally:kernel.CloseHandle(handle)
            try:
                stat=Path('/proc')/str(pid)/'stat'
                if stat.exists() and stat.read_text().split(') ',1)[1].startswith('Z'):return False
                os.kill(pid,0);return True
            except ProcessLookupError:return False
        deadline=time.monotonic()+2
        while alive() and time.monotonic()<deadline:time.sleep(.03)
        self.assertFalse(alive(),'取消后任务子进程仍在运行')

    def test_long_task_does_not_block_builtin_tasks_or_status(self):
        self.script('slow','import time\ntime.sleep(.6)\nprint("finished")\n')
        slow=self.manager.start('slow')
        fast=self.manager.start('calc','6*7')
        self.assertEqual(self.wait(fast)['output'],'42')
        self.assertIn(slow,self.dispatcher.handle('状态'))
        self.assertEqual(self.wait(slow)['state'],'completed')

    def test_cancel_running_task_and_timeout_do_not_leave_parent_alive(self):
        self.script('slow','import time\nprint("started",flush=True)\ntime.sleep(20)\n')
        uid=self.manager.start('slow');self.wait(uid,{'running'})
        self.assertIn(uid,self.dispatcher.handle('取消 '+uid))
        self.assertEqual(self.wait(uid)['state'],'cancelled')
        self.script('timeout','import time\ntime.sleep(20)\n',timeout=1)
        uid=self.manager.start('timeout')
        self.assertEqual(self.wait(uid)['state'],'failed')
        self.assertIn('超过等待',self.manager.store.get(uid)['output'])

    def test_multiple_interactive_tasks_keep_replies_and_secrets_separate(self):
        self.script('chat','from getpass import getpass\nx=input("名字：")\nk=getpass("本机演示口令：")\nprint(x,k)\n',kind='interactive')
        a=self.manager.start('chat');b=self.manager.start('chat')
        self.wait(a,{'waiting_input'});self.wait(b,{'waiting_input'})
        self.manager.answer(a,'Alice');self.manager.answer(b,'Bob')
        self.wait(a,{'waiting_secret'});self.wait(b,{'waiting_secret'})
        with self.assertRaises(ValueError):self.manager.answer(a,'phone-cannot-answer-secret')
        self.manager.answer(a,'fixture-secret-A',secret=True)
        self.manager.answer(b,'fixture-secret-B',secret=True)
        out_a=self.wait(a)['output'];out_b=self.wait(b)['output']
        self.assertIn('Alice',out_a);self.assertNotIn('Bob',out_a)
        self.assertIn('Bob',out_b);self.assertNotIn('Alice',out_b)
        self.assertNotIn('fixture-secret',out_a+out_b)
        self.assertNotIn(b'fixture-secret',(self.root/'state/tasks.sqlite3').read_bytes())

    def test_dispatcher_selected_task_empty_answer_and_other_commands(self):
        self.script('chat','x=input("optional：")\nprint(repr(x))\n',kind='interactive')
        response=self.dispatcher.handle('运行 chat')
        uid=self.dispatcher.selected
        self.assertIn(uid,response)
        self.wait(uid,{'waiting_input'})
        self.assertIn('运行电脑工具',self.dispatcher.handle('菜单'))
        self.assertEqual(self.manager.store.get(uid)['state'],'waiting_input')
        self.dispatcher.handle('0')
        self.dispatcher.handle('')
        self.assertIn("''",self.wait(uid)['output'])

    def test_natural_request_keeps_secret_input_on_desktop_and_explains_missing_ai(self):
        self.assertIn('未接入 AI',self.dispatcher.handle('阅读本文档，概述内容'))
        self.script('secretchat','import getpass\nx=getpass.getpass("key:")\nprint("done")\n',kind='interactive')
        self.dispatcher.handle('运行 secretchat')
        uid=self.dispatcher.selected;self.wait(uid,{'waiting_secret'})
        self.assertIn('电脑上的隐私输入',self.dispatcher.handle('这条不应进入隐私提示'))
        self.assertTrue(self.manager.tasks[uid].secrets.empty())
        self.assertEqual(self.manager.store.get(uid)['state'],'waiting_secret')

    def test_tool_catalog_roundtrip_reserved_ids_and_fixed_parameters(self):
        tool=self.script('saved','print("hi")\n',arguments=['fixed','{text}'])
        catalog=Catalog(self.root/'tools.json',self.catalog.shared)
        self.assertEqual(catalog.get('saved').arguments,['fixed','{text}'])
        self.assertEqual(tool.command('a b')[-2:],['fixed','a b'])
        with self.assertRaises(ValueError):catalog.save(Tool('calc','bad',target='x.py'))
        catalog.remove('saved')
        with self.assertRaises(ValueError):catalog.get('saved')

    def test_task_history_marks_unfinished_work_as_interrupted_without_replay(self):
        folder=self.root/'recovery'
        store=TaskStore(folder)
        store.add('history',Tool('old','old',target='old.py'))
        store.close()
        store=TaskStore(folder)
        try:self.assertEqual(store.get('history')['state'],'interrupted')
        finally:store.close()

    def test_actual_queue_dispatch_and_async_notice_flow(self):
        stop=threading.Event();ready=threading.Event();managers=[];notices=queue.Queue()
        with ChatIO(self.root/'wire',port=0,console=False) as bridge:
            def created(manager):managers.append(manager);ready.set()
            worker=threading.Thread(target=run_hub,args=(bridge,self.catalog,notices,stop),
                kwargs={'on_manager':created,'directory':self.root/'hub'})
            worker.start()
            try:
                self.assertTrue(ready.wait(3))
                self.assertIn('已连接',notices.get(timeout=3))
                client=BridgeClient(bridge.connection_path)
                client.request('/jobs',{'id':'run-calc','text':'运行 calc 7*8'})
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    row=client.request('/jobs/run-calc')
                    if row['status']=='done':break
                    time.sleep(.02)
                self.assertIn('任务已创建',row['result'])
                self.assertTrue(row['silent'])
                deadline=time.monotonic()+3
                while True:
                    answer=notices.get(timeout=max(.01,deadline-time.monotonic()))
                    if answer=='56':break
                self.assertIn('56',answer)
                self.assertEqual(answer,'56')
            finally:
                stop.set();bridge.stop_event.set();worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_full_onebot_callback_task_and_async_result_without_real_qq(self):
        server=FakeServer();stop=threading.Event();ready=threading.Event();receivers=[]
        notices=queue.Queue()
        config=self.root/'connection.json'
        config.write_text(json.dumps({'api_url':server.url,'event_port':0,'token':TOKEN}),encoding='utf-8')
        def create_receiver(*args,**kwargs):
            receiver=EventReceiver(*args,**kwargs);receivers.append(receiver);ready.set();return receiver
        try:
            with ChatIO(self.root/'queue',port=0,console=False) as bridge:
                hub=threading.Thread(target=run_hub,args=(bridge,self.catalog,notices,stop),kwargs={'directory':self.root/'hub'})
                transport=threading.Thread(target=relay_background,args=(BridgeClient(bridge.connection_path),stop),
                    kwargs={'notices':notices,'directory':self.root/'transport','config_path':config,'log':lambda _:None})
                with patch('onebot_adapter.EventReceiver',side_effect=create_receiver):
                    hub.start();transport.start()
                    try:
                        self.assertTrue(ready.wait(4))
                        for message,uid,expected in (('/py 菜单',122,'手机使用菜单'),('/py 3',123,'选择要运行的工具'),('/py 2',124,'填写本次内容')):
                            post(receivers[0],event(message,message_id=uid))
                            deadline=time.monotonic()+4
                            while time.monotonic()<deadline:
                                texts=[value['message'][0]['data']['text'] for value in server.sent]
                                if any(expected in value for value in texts):break
                                time.sleep(.03)
                            self.assertTrue(any(expected in value for value in texts),texts)
                        post(receivers[0],event('/py 8*9',message_id=125))
                        deadline=time.monotonic()+5
                        while time.monotonic()<deadline:
                            texts=[value['message'][0]['data']['text'] for value in server.sent]
                            if '↳ 72' in texts:break
                            time.sleep(.03)
                        self.assertIn('↳ 72',texts)
                        self.assertFalse(any('任务已创建' in value for value in texts),texts)
                        self.assertTrue(any('手机使用菜单' in value for value in texts),texts)
                        self.assertTrue(any('选择要运行的工具' in value for value in texts),texts)
                        post(receivers[0],event('/py 菜单 文件',message_id=126))
                        deadline=time.monotonic()+4
                        while time.monotonic()<deadline:
                            texts=[value['message'][0]['data']['text'] for value in server.sent]
                            if any('怎样通过 QQ 继续工作｜文件' in value for value in texts):break
                            time.sleep(.03)
                        self.assertTrue(any('附件已保存' in value and '发文件' in value for value in texts),texts)
                    finally:
                        stop.set();bridge.stop_event.set();hub.join(timeout=5);transport.join(timeout=5)
                    self.assertFalse(hub.is_alive());self.assertFalse(transport.is_alive())
        finally:server.close()


if __name__=='__main__':unittest.main()
