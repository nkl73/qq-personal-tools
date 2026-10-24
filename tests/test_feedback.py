import json
from pathlib import Path
import queue
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_service import AIProfile
from bridge_core import JobStore
from codex_session import CodexConversation
from onebot_adapter import BackgroundStore,OneBotClient,incoming_text,send_background_result
from phone_feedback import Feedback
from task_hub import Dispatcher,Manager,TaskStore
from tools import Catalog,Tool
from test_ai_tools import ModelServer,tool_call
from test_codex import FakeRPC,THREAD
from test_onebot import ACCOUNT,FakeServer,TOKEN,event


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.catalog=Catalog(self.root/'tools.json',self.root/'shared')
        self.notices=queue.Queue();self.manager=Manager(self.catalog,self.notices,self.root/'state',progress_interval=0)
        self.dispatch=Dispatcher(self.manager)

    def tearDown(self):self.manager.close();self.temp.cleanup()

    def wait(self,uid,state='waiting_input'):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if self.manager.store.get(uid)['state']==state:return
            time.sleep(.01)
        self.fail('任务未到达预期状态')

    def messages(self):
        values=[]
        while not self.notices.empty():values.append(self.notices.get_nowait())
        return values

    def test_numbered_display_choice_persists_and_does_not_start_work(self):
        self.assertFalse(self.manager.feedback.detailed)
        self.assertIn('QQ 消息显示',self.dispatch.handle('消息'))
        self.assertIn('已切换为详细',self.dispatch.handle('2'))
        self.assertTrue(Feedback(self.root/'state').detailed)
        self.dispatch.startup();self.assertIn('手机使用菜单',self.messages()[0])
        self.dispatch.handle('消息');self.dispatch.handle('1')
        self.assertFalse(Feedback(self.root/'state').detailed)
        self.assertFalse(self.manager.store.recent())

    def test_tool_steps_are_retained_on_pc_but_only_final_ai_answer_is_sent(self):
        model=ModelServer(lambda body,index: {'content':None,'tool_calls':[tool_call('run_tool',{'tool_id':'calc','text':'12*(3+4)'})]} if index==1 else {'content':'84'})
        try:
            self.catalog.save_ai(AIProfile('ai','工具助手',model.url,'fixture',timeout=5,allow_tools=True),'fixture-key-not-real')
            self.dispatch.handle('运行 ai 只给算式结果');uid=self.dispatch.selected
            self.assertTrue(self.dispatch.silent)
            self.wait(uid)
            self.assertEqual(self.messages(),['84'])
            row=self.manager.store.get(uid)
            self.assertIn('[电脑工具]',row['output'])
            self.assertEqual(self.dispatch.handle('结果 '+uid),'84')
            self.assertIn('[电脑工具]',self.dispatch.handle('详情 '+uid))
            self.dispatch.handle('结束 '+uid);self.wait(uid,'completed')
            self.assertEqual(self.notices.get(timeout=3),'对话已结束。');self.assertEqual(self.messages(),[])
        finally:model.close()

    def test_codex_approval_is_sent_but_progress_and_end_replay_are_not(self):
        rpc=FakeRPC();rpc.scenario='approval'
        def factory(*args,**kw):return CodexConversation(rpc=rpc,stop_event=args[1],timeout=2,on_output=kw['on_output'],initial_thread=THREAD)
        with patch('codex_session.CodexConversation',side_effect=factory):
            self.dispatch.handle('codex');uid=self.dispatch.selected;self.wait(uid);self.notices.get(timeout=3);self.assertEqual(self.messages(),[])
            self.dispatch.handle('1');self.wait(uid);self.notices.get(timeout=3);self.assertEqual(self.messages(),[])
            self.dispatch.handle('运行测试');self.wait(uid)
            prompt=self.notices.get(timeout=3)
            self.assertEqual(self.messages(),[]);self.assertIn('python -m unittest',prompt);self.assertIn('1. 同意',prompt)
            self.dispatch.handle('1');self.wait(uid)
            self.assertEqual(self.notices.get(timeout=3),'完成了指定项目工作');self.assertEqual(self.messages(),[])
            self.assertEqual(self.dispatch.handle('结果 '+uid),'完成了指定项目工作')
            self.dispatch.handle('codex 退出');self.wait(uid,'completed')
            self.assertEqual(self.notices.get(timeout=3),'对话已结束。');self.assertEqual(self.messages(),[])

    def test_interactive_script_sends_new_output_without_replaying_old_prompts(self):
        path=self.root/'ask.py';path.write_text('value=input("请输入内容：")\nprint("收到="+value)\n',encoding='utf-8')
        self.catalog.save(Tool('ask','交互程序','interactive',target=str(path)))
        self.dispatch.handle('运行 ask');uid=self.dispatch.selected;self.wait(uid)
        self.assertIn('请输入内容',self.notices.get(timeout=3));self.assertEqual(self.messages(),[])
        self.dispatch.handle('测试');self.wait(uid,'completed')
        result=self.notices.get(timeout=3)
        self.assertIn('收到=测试',result);self.assertNotIn('请输入内容',result);self.assertEqual(self.messages(),[])
        self.assertNotIn('请输入内容',self.dispatch.handle('结果 '+uid))
        self.assertIn('请输入内容',self.dispatch.handle('详情 '+uid))

    def test_old_databases_migrate_and_silent_receipts_survive_reopen(self):
        path=self.root/'old-jobs.sqlite3'
        db=sqlite3.connect(path);db.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY,text TEXT,status TEXT,result TEXT,created REAL)')
        db.execute("INSERT INTO jobs VALUES ('old','x','done','old result',0)");db.commit();db.close()
        store=JobStore(path)
        self.assertEqual(store.get('old')['result'],'old result');self.assertFalse(store.get('old')['silent'])
        store.submit('new','run');store.claim();store.finish('new','internal acknowledgement',silent=True);store.db.close()
        store=JobStore(path);self.assertTrue(store.get('new')['silent']);store.db.close()
        folder=self.root/'old-tasks';folder.mkdir()
        db=sqlite3.connect(folder/'tasks.sqlite3');db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY,tool TEXT,name TEXT,state TEXT,output TEXT,created REAL,updated REAL)')
        db.execute("INSERT INTO tasks VALUES ('old','calc','计算','completed','84',0,0)");db.commit();db.close()
        store=TaskStore(folder)
        self.assertEqual(store.get('old')['output'],'84');self.assertEqual(store.get('old')['answer'],'')
        store.add('new',self.catalog.get('calc'));store.set_answer('new','42');self.assertEqual(store.get('new')['answer'],'42');store.close()

    def test_model_output_starting_with_command_cannot_trigger_a_reply_loop(self):
        server=FakeServer()
        try:
            api=OneBotClient(server.url,TOKEN);store=BackgroundStore(self.root/'transport')
            try:
                text='/py 运行 calc 1+1\n'+('内容'*1300)
                store.add('result','',result=text);send_background_result(api,store,ACCOUNT,'result',text,lambda _:None)
                parts=[value['message'][0]['data']['text'] for value in server.sent]
                self.assertGreater(len(parts),1)
                self.assertTrue(all(p.startswith('↳ [') for p in parts))
                for i,part in enumerate(parts):self.assertIsNone(incoming_text(event(part,message_id=800+i),ACCOUNT,time.time()-1))
                self.assertFalse(any('[PY结果' in p for p in parts))
            finally:store.close()
        finally:server.close()


class CodexBranchTests(unittest.TestCase):
    def chat(self):
        rpc=FakeRPC();chat=CodexConversation(rpc=rpc,initial_thread=THREAD,timeout=2)
        self.addCleanup(chat.close);return chat,rpc

    def test_branch_requires_explicit_choice_and_keeps_original_thread_untouched(self):
        chat,rpc=self.chat();prompt=chat.start().text
        self.assertIn('桌面应用可以保持打开',prompt)
        self.assertFalse(any(m=='thread/fork' for m,_ in rpc.calls))
        self.assertIn('已建立手机分支',chat.answer('4').text)
        self.assertEqual(next(p for m,p in rpc.calls if m=='thread/fork'),{'threadId':THREAD,'excludeTurns':True})
        self.assertFalse(any(m in ('turn/start','thread/resume','thread/start') for m,_ in rpc.calls))
        result=chat.answer('新要求');self.assertEqual(result.text,'完成了指定项目工作')
        self.assertNotEqual(next(p for m,p in rpc.calls if m=='turn/start')['threadId'],THREAD)

    def test_busy_branch_submits_pending_request_once_to_new_thread(self):
        chat,rpc=self.chat();chat.start();chat.answer('1');original=rpc.call
        def call(method,params,timeout=20):
            if method=='thread/resume':raise ValueError('active writer')
            return original(method,params,timeout)
        rpc.call=call;chat.answer('只返回用户需要的结论')
        self.assertFalse(any(m in ('thread/fork','turn/start') for m,_ in rpc.calls))
        chat.answer('4')
        starts=[p for m,p in rpc.calls if m=='turn/start']
        self.assertEqual(len(starts),1);self.assertNotEqual(starts[0]['threadId'],THREAD)
        self.assertEqual(starts[0]['input'][0]['text'],'只返回用户需要的结论');self.assertIsNone(chat.retry_text)

    def test_branch_inherits_saved_model_instead_of_server_default(self):
        chat,rpc=self.chat();original=rpc.call
        def call(method,params,timeout=20):
            result=original(method,params,timeout)
            if method=='thread/read':result['thread'].update(model='saved-model',modelProvider='saved-provider',reasoningEffort='high')
            return result
        rpc.call=call;chat.start();chat.answer('4')
        self.assertEqual(next(p for m,p in rpc.calls if m=='thread/fork'),{
            'threadId':THREAD,'excludeTurns':True,'model':'saved-model','modelProvider':'saved-provider','config':{'model_reasoning_effort':'high'}})

    def test_running_original_offers_branch_without_stopping_its_turn(self):
        chat,rpc=self.chat();chat.start();chat.answer('1');rpc.state='inProgress'
        self.assertIn('4. 开手机分支',chat.answer('独立检查文件').text)
        chat.answer('4')
        self.assertFalse(any(m=='turn/interrupt' for m,_ in rpc.calls))
        self.assertEqual(sum(m=='turn/start' for m,_ in rpc.calls),1)

    def test_only_final_phase_is_returned_and_commentary_remains_in_pc_log(self):
        chat,rpc=self.chat();chat.start();chat.answer('1');rpc.scenario='manual'
        for phase,text in [('commentary','我先查看项目'),('partial_answer','中间草稿'),('final_answer','这是用户需要的结论')]:
            rpc.events.append({'method':'item/completed','params':{'threadId':THREAD,'turnId':'owned-turn','item':{'type':'agentMessage','phase':phase,'text':text}}})
        rpc.events.append({'method':'turn/completed','params':{'threadId':THREAD,'turn':{'id':'owned-turn','status':'completed'}}})
        result=chat.answer('给我结论')
        self.assertEqual(result.text,'这是用户需要的结论');self.assertEqual(result.kind,'result')
        self.assertIn('我先查看项目',chat.output);self.assertNotIn('中间草稿',result.text)


if __name__=='__main__':unittest.main()
