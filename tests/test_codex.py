from collections import deque
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_locator import resolve_codex
from codex_session import CodexConversation,CodexRPC,codex_environment
from task_hub import Dispatcher,Manager
from tools import Catalog

THREAD='11111111-2222-4333-8444-555555555555'


class CodexProxyTests(unittest.TestCase):
    def test_windows_registry_proxy_is_inherited_without_changing_parent(self):
        import os
        with patch.dict(os.environ,{'NO_PROXY':'internal.example'},clear=True),patch('codex_session.sys.platform','win32'),patch('codex_session.urllib.request.getproxies_registry',create=True,return_value={'http':'http://127.0.0.1:7897','https':'http://127.0.0.1:7897','ftp':'http://unused:1'}):
            env=codex_environment()
            self.assertEqual(env['HTTPS_PROXY'],'http://127.0.0.1:7897')
            self.assertEqual(env['HTTP_PROXY'],'http://127.0.0.1:7897')
            self.assertNotIn('FTP_PROXY',env)
            for host in ('internal.example','localhost','127.0.0.1','::1'):self.assertIn(host,env['NO_PROXY'].split(','))
            self.assertNotIn('HTTP_PROXY',os.environ)

    def test_explicit_proxy_even_empty_is_preserved(self):
        import os
        for name,value in (('http_proxy','http://explicit:1234'),('HTTPS_PROXY',''),('ALL_PROXY','socks5://explicit:1')):
            with patch.dict(os.environ,{name:value},clear=True),patch('codex_session.sys.platform','win32'),patch('codex_session.urllib.request.getproxies_registry',create=True) as reader:
                self.assertEqual(codex_environment(),dict(os.environ));reader.assert_not_called()

    def test_other_platform_or_registry_failure_does_not_change_environment(self):
        import os
        with patch.dict(os.environ,{'KEEP':'value'},clear=True),patch('codex_session.sys.platform','linux'):
            self.assertEqual(codex_environment(),{'KEEP':'value'})
        with patch.dict(os.environ,{'KEEP':'value'},clear=True),patch('codex_session.sys.platform','win32'),patch('codex_session.urllib.request.getproxies_registry',create=True,side_effect=OSError):
            self.assertEqual(codex_environment(),{'KEEP':'value'})


class CodexLocatorTests(unittest.TestCase):
    def test_command_install_is_preferred(self):
        with patch('codex_locator.shutil.which',return_value='installed-codex'):
            self.assertEqual(resolve_codex(),'installed-codex')

    def test_desktop_install_survives_empty_path_and_an_app_update(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)/'OpenAI'/'Codex'/'bin'
            old=base/'old-build'/'codex.exe';old.parent.mkdir(parents=True);old.write_bytes(b'old')
            os.utime(old,ns=(1_000_000_000,1_000_000_000))
            with patch.dict(os.environ,{'LOCALAPPDATA':folder,'PATH':''}),patch('codex_locator.shutil.which',return_value=None):
                self.assertEqual(resolve_codex('codex'),str(old))
                new=base/'new-build'/'codex.exe';new.parent.mkdir();new.write_bytes(b'new')
                self.assertEqual(resolve_codex('codex'),str(new))
                old.unlink()
                self.assertEqual(resolve_codex('codex.exe'),str(new))

    def test_no_install_and_invalid_explicit_program_fail_clearly(self):
        import os
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'LOCALAPPDATA':folder}),patch('codex_locator.shutil.which',return_value=None):
            with self.assertRaisesRegex(ValueError,'自动检查'):resolve_codex()
            with self.assertRaisesRegex(ValueError,'指定'):resolve_codex(str(Path(folder)/'custom.exe'))

    def test_rpc_resolves_at_start_and_catalog_does_not_pin_a_build(self):
        rpc=CodexRPC('codex')
        with patch('codex_session.resolve_codex',side_effect=ValueError('resolver called')) as resolver:
            with self.assertRaisesRegex(ValueError,'resolver called'):rpc.start()
            resolver.assert_called_once_with('codex')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);catalog=Catalog(root/'tools.json',root/'shared')
            self.assertEqual(catalog.get('codex').target,'codex')


class FakeRPC:
    def __init__(self):
        self.calls=[];self.replies=[];self.events=deque();self.state='completed';self.closed=False
        self.account={'type':'chatgpt'};self.started=False;self.scenario='answer';self.cwd='fixture-project';self.thread=THREAD
    def start(self):self.started=True
    def call(self,method,params,timeout=20):
        self.calls.append((method,params))
        if method=='account/read':return {'account':self.account}
        if method=='thread/list':return {'data':[{'id':THREAD,'name':'隔离测试项目','cwd':self.cwd}]}
        if method=='model/list':return {'data':[{'id':'fixture-model','displayName':'可选模型'}]}
        if method=='thread/start':
            self.thread='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';return {'thread':{'id':self.thread,'cwd':params['cwd']}}
        if method=='thread/fork':
            self.thread='cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee';self.state='completed'
            return {'thread':{'id':self.thread,'cwd':self.cwd,'forkedFromId':params['threadId']}}
        if method=='thread/read':return {'thread':{'id':THREAD,'name':'隔离测试项目','cwd':'fixture-project'}}
        if method=='thread/turns/list':return {'data':[{'status':self.state,'items':[]}]}
        if method=='thread/resume':return {'thread':{'id':THREAD},'model':'inherited','approvalPolicy':'on-request'}
        if method=='turn/start':
            if self.scenario=='answer':self.complete()
            elif self.scenario=='approval':self.request('item/commandExecution/requestApproval',{'command':'python -m unittest','cwd':'fixture-project'})
            elif self.scenario=='permissions':self.request('item/permissions/requestApproval',{'permissions':{'fileSystem':{'write':['fixture-project']}}})
            elif self.scenario=='questions':self.request('item/tool/requestUserInput',{'questions':[{'id':'mode','question':'处理方式','options':[{'label':'简要'},{'label':'详细'}]},{'id':'private','question':'本机隐私输入','isSecret':True}]})
            return {'turn':{'id':'owned-turn','status':'inProgress'}}
        if method=='turn/interrupt':return {}
        raise AssertionError(method)
    def complete(self):
        self.events.extend([{'method':'item/started','params':{'threadId':self.thread,'turnId':'owned-turn','item':{'id':'cmd','type':'commandExecution'}}},
            {'method':'item/completed','params':{'threadId':self.thread,'turnId':'owned-turn','item':{'type':'agentMessage','text':'完成了指定项目工作'}}},
            {'method':'turn/completed','params':{'threadId':self.thread,'turn':{'id':'owned-turn','status':'completed'}}}])
    def request(self,method,extra):self.events.append({'id':'request-1','method':method,'params':{'threadId':THREAD,'turnId':'owned-turn',**extra}})
    def next(self,timeout=.2):
        if self.events:return self.events.popleft()
        time.sleep(.01);return None
    def reply(self,uid,result):self.replies.append((uid,result));self.complete()
    def send(self,message):self.replies.append(message);self.complete()
    def close(self):self.closed=True


class CodexTests(unittest.TestCase):
    def test_new_unmaterialized_history_does_not_block_first_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            rpc=FakeRPC();rpc.cwd=folder;chat=CodexConversation(rpc=rpc,initial_thread='@new',timeout=2);self.addCleanup(chat.close)
            original=rpc.call;materialized=False
            def call(method,params,timeout=20):
                nonlocal materialized
                if method=='thread/turns/list' and not materialized:raise ValueError('thread fixture is not materialized yet; thread/turns/list is unavailable before first user message')
                if method=='turn/start':materialized=True
                return original(method,params,timeout)
            rpc.call=call;chat.start();chat.answer('1');chat.answer('1');chat.answer('1')
            self.assertIn('完成了',chat.answer('第一条实际要求').text)
            self.assertTrue(materialized)
            self.assertEqual(sum(m=='turn/start' for m,_ in rpc.calls),1)
            self.assertFalse(any(m=='thread/resume' for m,_ in rpc.calls))

    def test_other_history_errors_are_not_treated_as_an_empty_conversation(self):
        chat,rpc=self.connect();original=rpc.call
        def call(method,params,timeout=20):
            if method=='thread/turns/list':raise ValueError('history permission denied')
            return original(method,params,timeout)
        rpc.call=call
        with self.assertRaisesRegex(ValueError,'permission denied'):chat.answer('读取文件')
        self.assertFalse(any(m=='turn/start' for m,_ in rpc.calls))

    def test_new_chat_uses_selected_project_and_model_only_after_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            rpc=FakeRPC();rpc.cwd=folder;chat=CodexConversation(rpc=rpc,initial_thread='@new',timeout=2);self.addCleanup(chat.close)
            self.assertIn('选择项目',chat.start().text)
            self.assertIn('选择模型',chat.answer('1').text)
            self.assertIn('确认新建',chat.answer('2').text)
            self.assertFalse(any(method=='thread/start' for method,_ in rpc.calls))
            self.assertIn('已建立',chat.answer('1').text)
            self.assertEqual(next(p for m,p in rpc.calls if m=='thread/start'),{'cwd':str(Path(folder).resolve()),'model':'fixture-model'})
            self.assertFalse(any(method=='turn/start' for method,_ in rpc.calls))
            self.assertIn('完成了',chat.answer('隔离的新要求').text)
            self.assertFalse(any(method=='thread/resume' for method,_ in rpc.calls))
            self.assertEqual(next(p for m,p in rpc.calls if m=='turn/start')['threadId'],rpc.thread)

    def test_new_chat_back_and_default_model_do_not_copy_history_or_take_an_old_writer(self):
        with tempfile.TemporaryDirectory() as folder:
            rpc=FakeRPC();rpc.cwd=folder;chat=CodexConversation(rpc=rpc,initial_thread='@new');self.addCleanup(chat.close)
            chat.start();self.assertIn('不会',chat.answer('../unapproved-folder').text)
            chat.answer('1');chat.answer('1');chat.answer('0')
            self.assertFalse(any(m=='thread/start' for m,_ in rpc.calls))
            chat.answer('1');chat.answer('1')
            self.assertEqual(next(p for m,p in rpc.calls if m=='thread/start'),{'cwd':str(Path(folder).resolve())})
            self.assertFalse(any(m in ('thread/resume','turn/start') for m,_ in rpc.calls))

    def connect(self,scenario='answer'):
        rpc=FakeRPC();rpc.scenario=scenario;chat=CodexConversation(rpc=rpc,initial_thread=THREAD,timeout=2)
        first=chat.start();self.assertIn('接管',first.text)
        self.assertFalse(any(method in ('thread/resume','turn/start') for method,_ in rpc.calls))
        chat.answer('接管')
        self.addCleanup(chat.close)
        return chat,rpc

    def test_existing_login_and_explicit_handoff_preserve_thread_settings(self):
        rpc=FakeRPC();chat=CodexConversation(rpc=rpc);self.addCleanup(chat.close)
        self.assertIn('隔离测试项目',chat.start().text)
        self.assertIn('接管',chat.answer('1').text)
        self.assertIn('尚未',chat.answer('直接修改').text)
        self.assertFalse(any(method=='turn/start' for method,_ in rpc.calls))
        chat.answer('接管');result=chat.answer('处理当前项目')
        self.assertIn('完成了指定项目工作',result.text)
        resume=next(params for method,params in rpc.calls if method=='thread/resume')
        start=next(params for method,params in rpc.calls if method=='turn/start')
        self.assertEqual(resume,{'threadId':THREAD,'excludeTurns':True})
        self.assertEqual(start,{'threadId':THREAD,'input':[{'type':'text','text':'处理当前项目'}]})
        self.assertIsNone(chat.turn_id)

    def test_connection_retry_is_visible_without_leaking_raw_error_or_resubmitting(self):
        chat,rpc=self.connect()
        rpc.events.append({'method':'error','params':{'threadId':THREAD,'turnId':'owned-turn','willRetry':True,'error':{'message':'PRIVATE_SERVICE_ERROR_BODY'}}})
        result=chat.answer('连接验收')
        self.assertIn('正在重连',chat.output)
        self.assertNotIn('正在重连',result.text)
        self.assertNotIn('PRIVATE_SERVICE_ERROR_BODY',result.text)
        self.assertEqual(sum(method=='turn/start' for method,_ in rpc.calls),1)

    def test_recorded_inflight_turn_is_not_resumed_or_duplicated(self):
        chat,rpc=self.connect();rpc.state='inProgress'
        self.assertIn('尚未提交',chat.answer('改动文件').text)
        self.assertFalse(any(method=='thread/resume' for method,_ in rpc.calls))

    def test_numbered_handoff_and_approval_only_accept_the_displayed_scope(self):
        rpc=FakeRPC();rpc.scenario='approval';chat=CodexConversation(rpc=rpc,initial_thread=THREAD,timeout=2)
        self.addCleanup(chat.close)
        self.assertIn('1. 继续',chat.start().text)
        self.assertIn('已确认',chat.answer('1').text)
        self.assertIn('1. 同意',chat.answer('运行测试').text)
        chat.answer('2');self.assertEqual(rpc.replies,[('request-1',{'decision':'decline'})])

    def test_desktop_writer_conflict_retains_unsent_request_until_explicit_retry(self):
        chat,rpc=self.connect();original=rpc.call;blocked=True
        def call(method,params,timeout=20):
            if method=='thread/resume' and blocked:raise ValueError('thread '+THREAD+' already has an active writer')
            return original(method,params,timeout)
        rpc.call=call
        prompt=chat.answer('只回复连接成功')
        self.assertIn('尚未提交',prompt.text);self.assertIn('退出',prompt.text)
        self.assertFalse(any(method=='turn/start' for method,_ in rpc.calls))
        self.assertIn('尚未提交',chat.answer('重试').text)
        self.assertFalse(any(method=='turn/start' for method,_ in rpc.calls))
        blocked=False
        self.assertIn('完成了',chat.answer('重试').text)
        starts=[p for m,p in rpc.calls if m=='turn/start']
        self.assertEqual(len(starts),1);self.assertEqual(starts[0]['input'][0]['text'],'只回复连接成功')
        self.assertIsNone(chat.retry_text)

    def test_desktop_writer_conflict_can_be_cancelled_without_running_it(self):
        chat,rpc=self.connect();original=rpc.call
        def call(method,params,timeout=20):
            if method=='thread/resume':raise ValueError('already has an active writer')
            return original(method,params,timeout)
        rpc.call=call;chat.answer('修改文件')
        self.assertIn('丢弃',chat.answer('取消请求').text)
        self.assertIsNone(chat.retry_text)
        self.assertFalse(any(method=='turn/start' for method,_ in rpc.calls))

    def test_single_command_confirmation_and_decline_do_not_amend_policy(self):
        for choice,decision in (('同意','accept'),('拒绝','decline')):
            chat,rpc=self.connect('approval')
            prompt=chat.answer('运行测试')
            self.assertIn('python -m unittest',prompt.text)
            self.assertIn('明确选择',chat.answer('随便').text);self.assertEqual(rpc.replies,[])
            self.assertIn('完成了',chat.answer(choice).text)
            self.assertEqual(rpc.replies,[('request-1',{'decision':decision})])

    def test_granted_permission_is_only_the_requested_turn_scope(self):
        chat,rpc=self.connect('permissions');self.assertIn('fixture-project',chat.answer('写文件').text)
        chat.answer('同意')
        self.assertEqual(rpc.replies[0][1],{'permissions':{'fileSystem':{'write':['fixture-project']}},'scope':'turn'})

    def test_questions_and_secrets_keep_local_input_private(self):
        chat,rpc=self.connect('questions')
        self.assertIn('处理方式',chat.answer('开始').text)
        prompt=chat.answer('2');self.assertTrue(prompt.secret)
        result=chat.answer('fixture-private-value')
        self.assertEqual(rpc.replies[0][1]['answers']['mode'],{'answers':['详细']})
        self.assertNotIn('fixture-private-value',result.text)
        self.assertEqual(chat.safe_output('echo fixture-private-value'),'echo [隐私值已隐藏]')

    def test_interrupt_only_targets_the_turn_started_by_this_bridge(self):
        chat,rpc=self.connect('approval');chat.answer('开始任务')
        result=chat.answer('停止');self.assertIn('已请求停止',result.text)
        interrupts=[params for method,params in rpc.calls if method=='turn/interrupt']
        self.assertEqual(interrupts,[{'threadId':THREAD,'turnId':'owned-turn'}])

    def test_long_confirmation_is_declined_without_truncating_the_action(self):
        chat,rpc=self.connect('approval')
        original=rpc.request
        rpc.request=lambda method,extra:original(method,{'command':'hidden-tail-command '+'x'*2500})
        result=chat.answer('开始')
        self.assertIn('无法在手机完整审阅',result.text)
        self.assertEqual(rpc.replies[0][1],{'decision':'decline'})

    def test_gui_manager_routes_codex_without_an_ai_profile_or_api_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);catalog=Catalog(root/'tools.json',root/'shared')
            (root/'codex.json').write_text(json.dumps({'thread_id':THREAD}),encoding='utf-8')
            notices=queue.Queue();manager=Manager(catalog,notices,root/'state');dispatcher=Dispatcher(manager)
            rpc=FakeRPC()
            def factory(*args,**kwargs):return CodexConversation(rpc=rpc,stop_event=args[1],timeout=2,on_output=kwargs['on_output'],initial_thread=kwargs['initial_thread'])
            def waiting(uid):
                end=time.monotonic()+4
                while time.monotonic()<end:
                    if manager.store.get(uid)['state']=='waiting_input':return
                    time.sleep(.01)
                self.fail(str(manager.store.get(uid)))
            try:
                with patch('codex_session.CodexConversation',side_effect=factory):
                    dispatcher.handle('codex');uid=dispatcher.selected;waiting(uid)
                    dispatcher.handle('接管');waiting(uid)
                    dispatcher.handle('修改项目并测试');waiting(uid)
                    self.assertIn('完成了指定项目工作',manager.store.get(uid)['output'])
                    self.assertEqual(catalog.ai_store.all(),[])
                    dispatcher.handle('codex 退出')
                    end=time.monotonic()+4
                    while time.monotonic()<end and manager.store.get(uid)['state']!='completed':time.sleep(.01)
                    self.assertEqual(manager.store.get(uid)['state'],'completed')
            finally:manager.close()

    def test_rpc_keeps_notifications_in_order_when_the_response_arrives_later(self):
        rpc=CodexRPC();rpc.send=lambda message:None
        rpc.events.put({'method':'item/completed','params':{'text':'first'}})
        rpc.events.put({'id':1,'result':{'ok':True}})
        rpc.events.put({'method':'turn/completed','params':{}})
        self.assertTrue(rpc.call('fixture',{})['ok'])
        self.assertEqual(rpc.next()['method'],'item/completed')
        self.assertEqual(rpc.next()['method'],'turn/completed')


if __name__=='__main__':unittest.main()
