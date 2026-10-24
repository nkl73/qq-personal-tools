import json
from pathlib import Path
import queue
import tempfile
import time
import unittest
import zipfile

from file_io import Attachment
from project_tool import ProjectTool,digest
from task_hub import FINAL,Manager
from tools import Catalog,Tool


class ProjectToolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.project=self.base/'project';self.project.mkdir()
        (self.project/'quote.py').write_text('def total(values):\n    return sum(values)\n',encoding='utf-8')
        (self.project/'README.md').write_text('Quote project\n',encoding='utf-8')
        self.tests=self.project/'check.py'
        self.tests.write_text('from quote import total\nassert total([10,20], shipping=5)==35\nprint("fixed tests passed")\n',encoding='utf-8')
        self.config=self.base/'project.json'
        self.config.write_text(json.dumps({'root':str(self.project),'editable':['quote.py','README.md'],'test_script':'check.py','test_timeout':3}),encoding='utf-8')
        self.output=self.base/'output';self.tool=ProjectTool(self.config,self.output);self.tool.snapshot()

    def tearDown(self):self.temp.cleanup()

    def replace(self,content):
        current=self.tool.execute({'action':'read','path':'quote.py','offset':0})
        return self.tool.execute({'action':'replace','path':'quote.py','sha256':current['sha256'],'content':content})

    def fix(self):return self.replace('def total(values, shipping=0):\n    return sum(values)+shipping\n')

    def test_edit_existing_project_test_and_export_verified_source(self):
        before=digest(self.tests.read_bytes())
        self.assertFalse(self.tool.execute({'action':'test'})['passed'])
        self.fix();self.assertTrue(self.tool.execute({'action':'test'})['passed'])
        changes=self.tool.execute({'action':'diff'})
        self.assertEqual(changes['changed_files'],['quote.py']);self.assertIn('+def total',changes['diff'])
        report=self.tool.execute({'action':'export'});self.assertTrue(report['passed'])
        self.assertEqual(digest(self.tests.read_bytes()),before)
        with zipfile.ZipFile(self.output/'project-source.zip') as archive:
            self.assertEqual(set(archive.namelist()),{'quote.py','README.md','check.py'})
            self.assertEqual(archive.read('quote.py'),(self.project/'quote.py').read_bytes())
        self.assertIn('fixed tests passed',(self.output/'project-validation.txt').read_text(encoding='utf-8'))

    def test_paths_readonly_tests_and_unknown_actions_cannot_change_scope(self):
        (self.base/'outside.py').write_text('outside',encoding='utf-8')
        for path in ('../outside.py',str(self.base/'outside.py'),'C:\\outside.py','.secret','quote.py:stream'):
            with self.assertRaises(ValueError):self.tool.execute({'action':'read','path':path,'offset':0})
        with self.assertRaises(ValueError):self.tool.execute({'action':'replace','path':'check.py','sha256':digest(self.tests.read_bytes()),'content':'print("skip")'})
        for request in ({'action':'shell','command':'anything'},{'action':'test','command':'other'}, {'action':'snapshot'}):
            with self.assertRaises(ValueError):self.tool.execute(request)
        self.assertFalse((self.output/'project-source.zip').exists())

    def test_stale_hash_does_not_overwrite_and_original_backup_is_preserved(self):
        before=self.tool.originals()['quote.py']
        old=self.tool.execute({'action':'read','path':'quote.py','offset':0})
        self.fix()
        with self.assertRaisesRegex(ValueError,'已经变化'):
            self.tool.execute({'action':'replace','path':'quote.py','sha256':old['sha256'],'content':'wrong'})
        self.assertEqual(self.tool.originals()['quote.py'],before)
        self.assertIn('shipping=0',(self.project/'quote.py').read_text(encoding='utf-8'))

    def test_export_requires_current_successful_test_not_an_old_pass(self):
        with self.assertRaisesRegex(ValueError,'尚未通过'):self.tool.execute({'action':'export'})
        self.fix();self.assertTrue(self.tool.test()['passed'])
        self.replace('def total(values, shipping=0):\n    return 0\n')
        with self.assertRaisesRegex(ValueError,'尚未通过'):self.tool.execute({'action':'export'})

    def test_large_text_and_editable_test_configuration_are_rejected(self):
        (self.project/'quote.py').write_bytes(b'x'*64001)
        with self.assertRaisesRegex(ValueError,'64 KB'):self.tool.execute({'action':'read','path':'quote.py','offset':0})
        config=json.loads(self.config.read_text(encoding='utf-8'));config['editable'].append('check.py')
        self.config.write_text(json.dumps(config),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'只读'):ProjectTool(self.config)

    def test_test_process_timeout_is_bounded(self):
        self.tests.write_text('import time\ntime.sleep(30)\n',encoding='utf-8')
        config=json.loads(self.config.read_text(encoding='utf-8'));config['test_timeout']=1
        self.config.write_text(json.dumps(config),encoding='utf-8');tool=ProjectTool(self.config)
        started=time.monotonic();result=tool.test()
        self.assertFalse(result['passed']);self.assertTrue(result['stopped_by_limit']);self.assertLess(time.monotonic()-started,6)

    def test_registered_program_exports_three_qq_attachments(self):
        self.fix();self.assertTrue(self.tool.test()['passed'])
        catalog=Catalog(self.base/'tools.json',self.base/'shared');notices=queue.Queue()
        target=Path(__file__).resolve().parents[1]/'project_tool.py'
        catalog.save(Tool('project','项目工具',target=str(target),arguments=['--config',str(self.config),'--request','{text}'],ai_enabled=True))
        manager=Manager(catalog,notices,self.base/'state')
        try:
            uid=manager.start('project',json.dumps({'action':'export'}))
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and manager.store.get(uid)['state'] not in FINAL:time.sleep(.02)
            self.assertEqual(manager.store.get(uid)['state'],'completed')
            deadline=time.monotonic()+2
            while time.monotonic()<deadline and len([x for x in list(notices.queue) if isinstance(x,Attachment)])<3:time.sleep(.02)
            attachments=[x for x in list(notices.queue) if isinstance(x,Attachment)]
            self.assertEqual(len(attachments),3)
            self.assertEqual({Path(x.path).name for x in attachments},{'project-changes.diff','project-validation.txt','project-source.zip'})
        finally:manager.close()
