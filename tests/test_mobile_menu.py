from pathlib import Path
import queue
import tempfile
import time
import unittest

import mobile_menu
from task_hub import Dispatcher,Manager
from tools import Catalog,Tool


class MobileMenuTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.root=Path(self.folder.name)
        self.catalog=Catalog(self.root/'tools.json',self.root/'shared')
        self.notices=queue.Queue()
        self.manager=Manager(self.catalog,self.notices,self.root/'state')
        self.dispatcher=Dispatcher(self.manager)

    def tearDown(self):
        self.manager.close();self.folder.cleanup()

    def test_startup_is_brief_and_numbered_home_is_available_on_request(self):
        self.dispatcher.startup()
        self.assertEqual(self.notices.get_nowait(),'已连接。发送 /py 菜单 选择功能。')
        home=self.dispatcher.handle('菜单')
        self.assertIn('手机使用菜单',home)
        self.assertIn('1. 与 AI 对话',home)
        self.assertIn('/py 1',home)
        self.assertTrue(self.notices.empty())
        for tool in self.catalog.all():self.assertIn(tool.name,'\n'.join(mobile_menu.tool_pages(self.catalog)))
        self.assertEqual(self.manager.store.recent(),[])

    def test_menu_commands_match_current_features_and_known_limits(self):
        for topic in ('工具','任务','对话','文件','问题','1','2','3','4','5'):
            result=self.dispatcher.handle('菜单 '+topic)
            self.assertTrue(result)
        for tool in self.catalog.all():
            detail=self.dispatcher.handle('帮助 '+tool.id)
            self.assertIn('/py 运行 '+tool.id,detail)
            self.assertIn(tool.name,detail)
        files=self.dispatcher.handle('菜单 文件')
        self.assertIn('附件已保存',files)
        self.assertIn('附件',files)
        self.assertIn('共享目录',files)
        self.assertIn('1 MB',self.dispatcher.handle('帮助 inspect'))
        formats=self.dispatcher.handle('格式')
        for name in ('PDF','XLSX','PPTX','RTF','OCR'):self.assertIn(name,formats)
        with self.assertRaises(ValueError):self.dispatcher.handle('菜单 工具 999')
        self.assertIn('选择任务',self.dispatcher.handle('结果'))

    def test_custom_tool_help_is_live_and_does_not_expose_paths_or_fixed_args(self):
        tool=Tool('report','报表生成','python-task','生成本机工作报表',target='private-path-script.py',
            arguments=['fixture-private-fixed-argument','{text}'])
        self.catalog.save(tool)
        text=self.dispatcher.handle('帮助 report')
        self.assertIn('/py 运行 report 需要交给工具的内容',text)
        self.assertNotIn('private-path-script',text)
        self.assertNotIn('fixture-private-fixed-argument',text)
        self.assertIn('报表生成','\n'.join(mobile_menu.tool_pages(self.catalog)))
        self.catalog.remove('report')
        self.assertNotIn('报表生成','\n'.join(mobile_menu.tool_pages(self.catalog)))
        self.catalog.save(Tool('fixed','固定工具','command',target='private-program.exe'))
        text=self.dispatcher.handle('帮助 fixed')
        self.assertIn('不用另加内容',text)
        self.assertNotIn('运行 fixed 需要交给',text)
        self.catalog.save(Tool('demo','替换后的工具','command',target='private-program.exe'))
        changed=self.dispatcher.handle('帮助 demo')
        self.assertNotIn('约 10 秒',changed)
        self.assertNotIn('/py 运行 demo 10',changed)

    def test_menu_paging_and_custom_prefix_fit_qq_message_size(self):
        for index in range(25):
            self.catalog.save(Tool('task%d'%index,'工作工具'*15,'python-task','说明'*100,
                target='private-tool.py',arguments=['{text}']))
        prefix='/abcdefghijklmn'
        pages=mobile_menu.all_pages(self.catalog,prefix)
        self.assertGreater(len(mobile_menu.tool_pages(self.catalog,prefix)),1)
        for page in pages:
            self.assertLessEqual(len(page)+len('【使用说明 99/99】\n'),1200)
            self.assertNotIn('/py ',page)
        dispatch=Dispatcher(self.manager,prefix)
        second=dispatch.handle('菜单 工具 2')
        self.assertIn('2/',second)
        self.assertIn(prefix+' 帮助',second)

    def test_all_help_pages_are_queued_individually_without_creating_tasks(self):
        reply=self.dispatcher.handle('菜单 全部')
        expected=mobile_menu.all_pages(self.catalog)
        self.assertIn(str(len(expected)),reply)
        delivered=[self.notices.get_nowait() for _ in expected]
        self.assertTrue(self.notices.empty())
        for index,(actual,body) in enumerate(zip(delivered,expected),1):
            self.assertTrue(actual.startswith('【使用说明 %d/%d】'%(index,len(expected))))
            self.assertIn(body,actual)
        self.assertEqual(self.manager.store.recent(),[])

    def test_help_does_not_consume_answer_of_selected_interactive_task(self):
        path=self.root/'chat.py';path.write_text('x=input("你的回答：")\nprint("收到="+x)\n',encoding='utf-8')
        self.catalog.save(Tool('chat','工作会话','interactive',target=str(path)))
        self.dispatcher.handle('运行 chat')
        uid=self.dispatcher.selected
        deadline=time.monotonic()+4
        while self.manager.store.get(uid)['state']!='waiting_input' and time.monotonic()<deadline:time.sleep(.02)
        for command in ('菜单','菜单 对话','菜单 工具','帮助 chat','工具'):
            self.dispatcher.handle(command)
        self.assertEqual(self.manager.store.get(uid)['state'],'waiting_input')
        self.assertEqual(self.dispatcher.selected,uid)
        self.dispatcher.handle('回复 '+uid+' 菜单')
        deadline=time.monotonic()+4
        while self.manager.store.get(uid)['state']!='completed' and time.monotonic()<deadline:time.sleep(.02)
        self.assertIn('收到=菜单',self.manager.store.get(uid)['output'])


if __name__=='__main__':unittest.main()
