from pathlib import Path
import queue
import tempfile
import time
import unittest
from unittest.mock import patch

from task_hub import Dispatcher,Manager,FINAL
from tools import Catalog,Tool


class PhoneChoiceTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name)
        self.catalog=Catalog(self.root/'tools.json',self.root/'shared');self.catalog.shared.mkdir()
        self.manager=Manager(self.catalog,queue.Queue(),self.root/'state');self.dispatch=Dispatcher(self.manager)

    def tearDown(self):self.manager.close();self.folder.cleanup()

    def wait(self,uid,state):
        end=time.monotonic()+5
        while time.monotonic()<end:
            row=self.manager.store.get(uid)
            if row['state'] in state:return row
            time.sleep(.02)
        self.fail(str(row))

    def test_numbered_tool_and_content_create_a_real_calculation(self):
        self.assertIn('手机使用菜单',self.dispatch.handle('菜单'))
        self.assertIn('计算器',self.dispatch.handle('3'))
        self.assertIn('填写本次内容',self.dispatch.handle('2'))
        self.dispatch.handle('12*(3+4)')
        row=self.wait(self.manager.store.recent()[0]['id'],FINAL)
        self.assertIn('84',row['output']);self.assertEqual(row['state'],'completed')

    def test_file_choice_keeps_snapshot_and_returns_file_without_copying_path(self):
        (self.catalog.shared/'old name.txt').write_text('fixture',encoding='utf-8')
        self.dispatch.handle('菜单');self.assertIn('old name.txt',self.dispatch.handle('4'))
        (self.catalog.shared/'new.txt').write_text('new',encoding='utf-8')
        self.assertIn('old name.txt',self.dispatch.handle('1'))
        self.dispatch.handle('4')
        attachment=self.manager.notices.get_nowait()
        self.assertEqual(attachment.path,'old name.txt')
        self.assertIsNone(self.dispatch.choices.active)

    def test_removed_file_is_rechecked_and_never_sent(self):
        path=self.catalog.shared/'file.txt';path.write_text('fixture',encoding='utf-8')
        self.dispatch.handle('文件');path.unlink()
        with self.assertRaises(ValueError):self.dispatch.handle('1')
        self.assertTrue(self.manager.notices.empty())

    def test_expired_choice_and_invalid_number_do_not_execute(self):
        self.dispatch.handle('菜单');self.dispatch.handle('3')
        self.assertIn('没有这个选项',self.dispatch.handle('999'))
        self.dispatch.choices.active['created']-=301
        self.assertIn('已过期',self.dispatch.handle('1'))
        self.assertEqual(self.manager.store.recent(),[])

    def test_menu_is_not_a_default_answer_and_does_not_consume_numeric_program_input(self):
        path=self.root/'chat.py';path.write_text('x=input("数字：")\nprint("收到="+x)\n',encoding='utf-8')
        self.catalog.save(Tool('chat','数字任务','interactive',target=str(path)))
        self.dispatch.handle('运行 chat');uid=self.dispatch.selected;self.wait(uid,{'waiting_input'})
        self.dispatch.handle('菜单');self.assertIn('请回复显示的序号',self.dispatch.handle('修改要求'))
        self.assertEqual(self.manager.store.get(uid)['state'],'waiting_input')
        self.dispatch.handle('0');self.dispatch.handle('1')
        self.assertIn('收到=1',self.wait(uid,FINAL)['output'])

    def test_task_can_be_selected_and_stopped_without_typing_identifier(self):
        path=self.root/'chat.py';path.write_text('input("等待：")\n',encoding='utf-8')
        self.catalog.save(Tool('chat','等待任务','interactive',target=str(path)))
        self.dispatch.handle('运行 chat');uid=self.dispatch.selected;self.wait(uid,{'waiting_input'})
        self.dispatch.handle('任务');self.assertIn('停止这个任务',self.dispatch.handle('1'))
        self.assertIn('确认停止',self.dispatch.handle('4'))
        self.assertEqual(self.manager.store.get(uid)['state'],'waiting_input')
        self.dispatch.handle('1');self.assertEqual(self.wait(uid,FINAL)['state'],'cancelled')

    def test_multiple_ai_selection_preserves_entire_question(self):
        from types import SimpleNamespace
        question='这是一段完整问题'*100
        tools=[SimpleNamespace(kind='ai-chat',id='ai-a',name='模型 A'),SimpleNamespace(kind='ai-chat',id='ai-b',name='模型 B')]
        with patch.object(self.catalog,'all',return_value=tools),patch.object(self.dispatch.choices,'start',return_value='started') as start:
            self.assertIn('模型 B',self.dispatch.converse(question))
            self.assertEqual(self.dispatch.handle('2'),'started')
            start.assert_called_once_with('ai-b',question)

    def test_tools_page_navigation_and_custom_prefix(self):
        self.dispatch=Dispatcher(self.manager,'/work')
        for i in range(10):self.catalog.save(Tool('tool%d'%i,'工具%d'%i,'command',target='program.exe'))
        first=self.dispatch.handle('工具');self.assertIn('/work 1',first)
        second=self.dispatch.handle('8');self.assertIn('2/',second)
        self.assertIn('1/',self.dispatch.handle('7'))
        self.assertEqual(self.manager.store.recent(),[])


if __name__=='__main__':unittest.main()
