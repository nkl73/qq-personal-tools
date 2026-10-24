from dataclasses import replace
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hashlib
import importlib.util
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ai_service import AIProfile,Conversation,ToolTurnError,call
from ai_tools import DEFINITIONS,ToolSession
from bridge_core import BridgeClient,ChatIO
from file_io import Attachment
from onebot_adapter import EventReceiver,relay_background
from task_hub import FINAL,Manager,Task,run_hub
from tools import Catalog,Tool
from test_files import QQFiles
from test_onebot import TOKEN,event,post
from test_document_text import docx
from test_common_documents import pdf,pptx

KEY='agent-fixture-key-not-real'


def tool_call(name,args,uid='call_1'):
    return {'id':uid,'type':'function','function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}}


class ModelServer:
    def __init__(self,reply):
        self.reply=reply;self.requests=[];self.lock=threading.Lock()
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with owner.lock:owner.requests.append(body);index=len(owner.requests)
                message=owner.reply(body,index)
                code=message.pop('_status',200)
                raw=json.dumps({'choices':[{'message':message,'finish_reason':message.pop('_finish','stop')}]},ensure_ascii=False).encode()
                try:
                    self.send_response(code);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        self.url='http://127.0.0.1:'+str(self.server.server_port)+'/v1'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(2)


class AIToolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.catalog=Catalog(self.root/'tools.json',self.root/'shared');self.catalog.shared.mkdir()
        self.notices=queue.Queue();self.manager=Manager(self.catalog,self.notices,self.root/'state');self.model=None

    def tearDown(self):
        self.manager.close()
        if self.model:self.model.close()
        self.temp.cleanup()

    def register(self,reply):
        self.model=ModelServer(reply)
        self.profile=AIProfile('ai','工具助手',self.model.url,'fixture',timeout=5,allow_tools=True)
        self.catalog.save_ai(self.profile,KEY)

    def wait(self,predicate,timeout=8):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.02)
        self.fail('工作没有在预期时间内完成')

    def session(self):
        parent=Task('parent',self.catalog.get('ai'),'')
        self.manager.store.add(parent.id,parent.tool);self.manager.tasks[parent.id]=parent
        return ToolSession(self.manager,parent,lambda text:text.replace(KEY,'[密钥已隐藏]'))

    def test_file_read_model_analysis_write_and_receipts_over_real_http(self):
        original='待办：完成文件通道测试。\n'
        (self.catalog.shared/'input.txt').write_bytes(original.encode('utf-8'))
        def reply(body,index):
            if index==1:return {'content':None,'tool_calls':[tool_call('read_file',{'path':'input.txt','offset':0})],'reasoning_content':'provider continuation'}
            if index==2:
                self.assertIn(original,json.loads(body['messages'][-1]['content'])['content'])
                self.assertEqual(body['messages'][-2]['reasoning_content'],'provider continuation')
                return {'content':None,'tool_calls':[tool_call('write_text_file',{'name':'总结.txt','content':'已整理：完成测试。'},'call_2')]}
            return {'content':'总结已生成，附件正在回传。'}
        self.register(reply)
        uid=self.manager.start('ai','读取 input.txt，整理后保存并回传。')
        self.wait(lambda:self.manager.store.get(uid)['state']=='waiting_input')
        self.assertIn('总结已生成',self.manager.store.get(uid)['output'])
        attachments=[x for x in list(self.notices.queue) if isinstance(x,Attachment)]
        self.assertEqual(len(attachments),1)
        self.assertEqual((self.catalog.shared/attachments[0].path).read_text(encoding='utf-8'),'已整理：完成测试。')
        self.assertTrue(all('tools' in body for body in self.model.requests))
        self.manager.end_chat(uid);self.wait(lambda:self.manager.store.get(uid)['state']=='completed')
        self.assertEqual(len([x for x in list(self.notices.queue) if isinstance(x,Attachment)]),1)

    def test_natural_docx_request_after_old_session_ends_reaches_model_and_pages(self):
        from task_hub import Dispatcher
        original='文档验收：共享文件正文。'
        docx(self.catalog.shared/'input.docx','<w:p><w:r><w:t>'+original+'</w:t></w:r></w:p>')
        def reply(body,index):
            if body['messages'][-1]['role']=='user':
                return {'content':None,'tool_calls':[tool_call('read_file',{'path':'input.docx','offset':0})]}
            receipt=json.loads(body['messages'][-1]['content'])
            self.assertEqual(receipt['content'],original)
            self.assertIn('不含图片',receipt['scope'])
            return {'content':'文档描述了共享文件正文验收。'}
        self.register(reply);dispatcher=Dispatcher(self.manager)
        dispatcher.handle('运行 ai');old=dispatcher.selected
        self.wait(lambda:self.manager.store.get(old)['state']=='waiting_input')
        self.manager.end_chat(old);self.wait(lambda:self.manager.store.get(old)['state']=='completed')
        self.assertIn('已新建 AI 会话',dispatcher.handle('阅读本文档，概述内容'))
        uid=dispatcher.selected
        self.wait(lambda:self.manager.store.get(uid)['state']=='waiting_input')
        self.assertIn('文档描述了',self.manager.store.get(uid)['output'])
        session=ToolSession(self.manager,self.manager.tasks[uid]);stop=threading.Event()
        docx(self.catalog.shared/'long.docx','<w:p><w:r><w:t>'+'甲'*12500+'</w:t></w:r></w:p>')
        first=session.execute('read_file',{'path':'long.docx','offset':0},stop)
        second=session.execute('read_file',{'path':'long.docx','offset':first['next_offset']},stop)
        self.assertEqual(len(first['content']),12000);self.assertEqual(len(second['content']),500)
        self.assertIsNone(second['next_offset'])

    @unittest.skipUnless(importlib.util.find_spec('pypdf') and importlib.util.find_spec('openpyxl'),'install document readers')
    def test_common_file_metadata_and_omission_warnings_reach_model(self):
        from openpyxl import Workbook
        pdf(self.catalog.shared/'sample.pdf',chinese=True,extra_blank=True)
        pptx(self.catalog.shared/'sample.pptx')
        book=Workbook();book.active.append(['金额',100]);book.active['B2']='=SUM(B1:B1)';book.save(self.catalog.shared/'sample.xlsx');book.close()
        seen=[]
        def reply(body,index):
            if body['messages'][-1]['role']=='user':
                return {'content':None,'tool_calls':[tool_call('read_file',{'path':body['messages'][-1]['content'],'offset':0},'read_'+str(index))]}
            receipt=json.loads(body['messages'][-1]['content']);seen.append(receipt)
            return {'content':'已读取 '+receipt['format']+'；读取提示：'+str(receipt['warnings'])}
        self.register(reply)
        uid=self.manager.start('ai','sample.pdf')
        self.wait(lambda:self.manager.store.get(uid)['state']=='waiting_input')
        for name in ('sample.xlsx','sample.pptx'):
            self.manager.answer(uid,name);self.wait(lambda:self.manager.store.get(uid)['state']=='waiting_input')
        self.assertEqual([row['format'] for row in seen],['PDF','XLSX','PPTX'])
        self.assertIn('中文',seen[0]['content']);self.assertIn('未完成识别',str(seen[0]['warnings']))
        self.assertIn('公式无缓存',seen[1]['content']);self.assertTrue(seen[1]['warnings'])
        self.assertIn('第一页内容',seen[2]['content']);self.assertEqual(seen[2]['units']['slides'],2)

    def test_read_pages_reuse_parse_and_changed_files_are_read_again(self):
        from document_text import read_document
        self.register(lambda *_:{'content':'ok'});session=self.session();stop=threading.Event()
        path=self.catalog.shared/'pages.txt';path.write_text('甲'*12500,encoding='utf-8')
        with patch('ai_tools.read_document',wraps=read_document) as reader:
            session.execute('read_file',{'path':'pages.txt','offset':0},stop)
            page=session.execute('read_file',{'path':'pages.txt','offset':12000},stop)
            self.assertEqual(page['content'],'甲'*500);self.assertEqual(reader.call_count,1)
            path.write_text('更新后的内容',encoding='utf-8')
            page=session.execute('read_file',{'path':'pages.txt','offset':0},stop)
            self.assertEqual(page['content'],'更新后的内容');self.assertEqual(reader.call_count,2)

    def test_registered_python_uses_parent_slot_with_two_ai_sessions(self):
        script=self.root/'convert.py'
        script.write_text('import os,sys\nfrom pathlib import Path\nx=Path(sys.argv[1]).read_text(encoding="utf-8")\nPath(os.environ["QQ_BRIDGE_OUTPUT_DIR"],"result.txt").write_text(x.upper(),encoding="utf-8")\nprint("converted",flush=True)\n',encoding='utf-8')
        self.catalog.save(Tool('convert','转换','python-task',target=str(script),arguments=['{file}'],ai_enabled=True))
        (self.catalog.shared/'input.txt').write_text('hello',encoding='utf-8')
        barrier=threading.Barrier(2)
        def reply(body,index):
            if body['messages'][-1]['role']=='user':
                barrier.wait(5)
                return {'content':None,'tool_calls':[tool_call('run_tool',{'tool_id':'convert','text':'input.txt'})]}
            receipt=json.loads(body['messages'][-1]['content'])
            return {'content':'程序结果：'+receipt['state']+' '+receipt['output']}
        self.register(reply)
        a=self.manager.start('ai','第一份转换');b=self.manager.start('ai','第二份转换')
        self.wait(lambda:all(self.manager.store.get(uid)['state']=='waiting_input' for uid in (a,b)))
        children=[t for t in self.manager.tasks.values() if t.parent_id]
        self.assertEqual(len(children),2)
        for child in children:
            self.assertEqual(child.state,'completed')
            self.assertEqual((self.catalog.shared/'results'/child.id/'result.txt').read_text(encoding='utf-8'),'HELLO')
        self.assertTrue(Catalog(self.catalog.path,self.catalog.shared).get('convert').ai_enabled)

    def test_boundaries_permissions_ownership_and_revocation(self):
        self.register(lambda *_:{'content':'ok'})
        self.catalog.save(Tool('blocked','未授权',target='private-script.py'))
        self.catalog.save(Tool('allowed','已授权',target='private-script.py',arguments=['private-fixed'],ai_enabled=True))
        session=self.session();stop=threading.Event()
        available=session.execute('list_tools',{},stop)
        self.assertIn('allowed',str(available));self.assertNotIn('blocked',str(available));self.assertNotIn('private-',str(available))
        (self.root/'secret.txt').write_text('secret',encoding='utf-8')
        (self.catalog.shared/'.secret.txt').write_text('hidden',encoding='utf-8')
        cases=[('read_file',{'path':'../secret.txt','offset':0}),('read_file',{'path':'input.txt','offset':True}),
            ('run_tool',{'tool_id':'blocked','text':''}),('run_tool',{'tool_id':'ai','text':''}),
            ('run_tool',{'tool_id':'fileinfo','text':'.secret.txt'}),
            ('write_text_file',{'name':'../overwrite.txt','content':'bad'}),('task_status',{'task_id':'other'}),
            ('cancel_task',{'task_id':'parent'}),('shell',{'command':'arbitrary'}),('list_files',{'extra':1})]
        for name,args in cases:self.assertIn('error',session.execute(name,args,stop),(name,args))
        self.catalog.save_ai(replace(self.profile,allow_tools=False),KEY)
        with self.assertRaisesRegex(ValueError,'权限已关闭'):session.execute('list_files',{},stop)
        self.assertFalse(AIProfile(base_url=self.model.url,model='x').allow_tools)
        self.assertFalse(Tool('old','旧工具',target='old.py').ai_enabled)

    def test_agent_intermediate_content_arguments_and_results_redact_key(self):
        def reply(body,index):
            if index==1:return {'content':'intermediate '+KEY,'reasoning_content':KEY,'tool_calls':[
                tool_call('write_text_file',{'name':'result.txt','content':'secret='+KEY})]}
            return {'content':'final '+KEY}
        self.register(reply)
        uid=self.manager.start('ai','生成测试文件')
        self.wait(lambda:self.manager.store.get(uid)['state']=='waiting_input')
        self.assertNotIn(KEY,str(self.model.requests[-1]['messages']))
        self.assertNotIn(KEY,self.manager.store.get(uid)['output'])
        attachment=next(x for x in list(self.notices.queue) if isinstance(x,Attachment))
        self.assertNotIn(KEY,(self.catalog.shared/attachment.path).read_text(encoding='utf-8'))

    def test_per_turn_operation_budget_is_bounded_even_with_many_calls(self):
        def reply(body,index):return {'content':None,'tool_calls':[
            tool_call('read_file',{'path':'missing_%d_%d.txt'%(index,i),'offset':0},'call_%d_%d'%(index,i)) for i in range(4)]}
        self.register(reply);session=self.session();chat=Conversation(self.profile,KEY,session)
        with patch.object(session,'execute',wraps=session.execute) as execute:
            with self.assertRaises(ToolTurnError):chat.ask('检查文件',threading.Event())
            self.assertEqual(execute.call_count,12)
        self.assertEqual(len(self.model.requests),8)

    def test_paged_file_reads_and_key_redaction(self):
        self.register(lambda *_:{'content':'ok'})
        (self.catalog.shared/'long.txt').write_text('甲'*12500+KEY,encoding='utf-8')
        session=self.session();stop=threading.Event()
        page=session.execute('read_file',{'path':'long.txt','offset':0},stop)
        self.assertEqual(len(page['content']),12000);self.assertEqual(page['next_offset'],12000)
        page=session.execute('read_file',{'path':'long.txt','offset':12000},stop)
        self.assertNotIn(KEY,page['content']);self.assertIsNone(page['next_offset'])
        (self.catalog.shared/'big.txt').write_bytes(b'x'*(1024*1024+1))
        self.assertIn('error',session.execute('read_file',{'path':'big.txt','offset':0},stop))

    def test_duplicate_calls_are_not_executed_twice_and_failure_preserves_receipts(self):
        def reply(body,index):
            if index==1:return {'content':None,'tool_calls':[tool_call('write_text_file',{'name':'result.txt','content':'once'},'first'),tool_call('write_text_file',{'name':'result.txt','content':'once'},'second')]}
            return {'_status':500,'content':KEY}
        self.register(reply);session=self.session();chat=Conversation(self.profile,KEY,session)
        with self.assertRaises(ToolTurnError):chat.ask('写一次结果',threading.Event())
        self.assertEqual(len(list((self.catalog.shared/'results'/'parent').iterdir())),1)
        self.assertEqual(len(self.model.requests),2)
        self.assertEqual(len([x for x in chat.messages if x['role']=='tool']),2)
        self.assertNotIn(KEY,str(chat.messages))
        self.assertIn('已执行的操作',chat.messages[-1]['content'])

    def test_malformed_tool_calls_are_rejected_before_side_effects(self):
        self.register(lambda *_:{'content':None,'tool_calls':[tool_call('write_text_file',{'name':'x.txt','content':'bad'}),tool_call('list_files',{})]})
        with self.assertRaisesRegex(ValueError,'格式无效'):call(self.profile,KEY,[{'role':'user','content':'test'}],operation='agent',tools=DEFINITIONS)
        self.assertFalse((self.catalog.shared/'results').exists())
        self.model.reply=lambda *_:{'content':None,'tool_calls':[tool_call('list_files',{})],'_finish':'length'}
        with self.assertRaisesRegex(ValueError,'截断'):call(self.profile,KEY,[],operation='agent',tools=DEFINITIONS)

    def test_parent_cancellation_stops_running_child(self):
        script=self.root/'slow.py';script.write_text('import time\nprint("started",flush=True)\ntime.sleep(30)\n',encoding='utf-8')
        self.catalog.save(Tool('slow','慢任务',target=str(script),ai_enabled=True))
        self.register(lambda *_:{'content':None,'tool_calls':[tool_call('run_tool',{'tool_id':'slow','text':''})]})
        uid=self.manager.start('ai','启动慢任务')
        self.wait(lambda:any(t.parent_id==uid and t.state=='running' for t in self.manager.tasks.values()))
        self.manager.cancel(uid)
        self.wait(lambda:self.manager.store.get(uid)['state']=='cancelled')
        children=[t for t in self.manager.tasks.values() if t.parent_id==uid]
        self.assertTrue(all(t.state=='cancelled' for t in children))
        self.assertEqual(len(self.model.requests),1)

    def test_bounded_loop_and_unknown_requests_do_not_run_arbitrary_programs(self):
        self.register(lambda body,index:{'content':None,'tool_calls':[tool_call('shell',{'command':'bad'},'call_'+str(index))]})
        session=self.session();chat=Conversation(self.profile,KEY,session)
        with self.assertRaisesRegex(ToolTurnError,'8 次模型请求'):chat.ask('工作',threading.Event())
        self.assertEqual(len(self.model.requests),8)
        self.assertEqual(len(self.manager.tasks),1)

    def test_full_signed_qq_ai_tool_and_file_upload_flow(self):
        (self.catalog.shared/'input.txt').write_text('QQ agent test',encoding='utf-8')
        def reply(body,index):
            if index==1:return {'content':None,'tool_calls':[tool_call('read_file',{'path':'input.txt','offset':0})]}
            if index==2:return {'content':None,'tool_calls':[tool_call('write_text_file',{'name':'report.txt','content':'File read: QQ agent test'},'write')]}
            return {'content':'报告已完成。'}
        self.register(reply)
        qq=QQFiles();stop=threading.Event();ready=threading.Event();receivers=[]
        notices=queue.Queue();config=self.root/'connection.json'
        config.write_text(json.dumps({'api_url':qq.url,'event_port':0,'token':TOKEN}),encoding='utf-8')
        def receiver(*args,**kwargs):
            value=EventReceiver(*args,**kwargs);receivers.append(value);ready.set();return value
        try:
            with ChatIO(self.root/'wire',port=0,console=False) as bridge:
                hub=threading.Thread(target=run_hub,args=(bridge,self.catalog,notices,stop),kwargs={'directory':self.root/'wire-state'})
                transport=threading.Thread(target=relay_background,args=(BridgeClient(bridge.connection_path),stop),kwargs={
                    'notices':notices,'directory':self.root/'wire-transport','config_path':config,'log':lambda _:None,'shared_dir':self.catalog.shared})
                with patch('onebot_adapter.EventReceiver',side_effect=receiver):
                    hub.start();transport.start()
                    try:
                        self.assertTrue(ready.wait(4));post(receivers[0],event('/py 运行 ai 读取 input.txt，生成报告文件'))
                        self.wait(lambda:len(qq.uploads)==1)
                        self.assertEqual(qq.uploads[0]['bytes'],b'File read: QQ agent test')
                        self.wait(lambda:any('报告已完成。' in x['message'][0]['data'].get('text','') for x in qq.sent))
                        texts='\n'.join(x['message'][0]['data']['text'] for x in qq.sent)
                        self.assertIn('报告已完成。',texts);self.assertNotIn('read_file',texts);self.assertNotIn('write_text_file',texts);self.assertNotIn(KEY,texts)
                    finally:stop.set();bridge.stop_event.set();hub.join(7);transport.join(7)
        finally:qq.close()
