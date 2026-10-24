import ctypes
import importlib.machinery
import importlib.util
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from power_control import ES_CONTINUOUS,ES_SYSTEM_REQUIRED,SystemAwake
from settings import Settings


class PowerTests(unittest.TestCase):
    def test_request_only_prevents_idle_sleep_and_clears_on_exception_on_same_thread(self):
        calls=[]
        def setter(flags):calls.append((threading.get_ident(),flags));return ES_CONTINUOUS
        with self.assertRaisesRegex(ValueError,'test failure'):
            with SystemAwake(setter=setter,log=lambda _:None) as guard:
                self.assertTrue(guard.active);raise ValueError('test failure')
        self.assertEqual([flags for _,flags in calls],[ES_CONTINUOUS|ES_SYSTEM_REQUIRED,ES_CONTINUOUS])
        self.assertEqual(len({tid for tid,_ in calls}),1)
        self.assertFalse(guard.active)

    def test_disabled_unavailable_and_failed_requests_do_not_claim_to_be_active(self):
        with patch('power_control.ctypes.WinDLL',create=True) as dll:
            with SystemAwake(False,log=lambda _:None) as guard:self.assertFalse(guard.active)
            dll.assert_not_called()
            with patch('power_control.sys.platform','linux'):
                with SystemAwake(log=lambda _:None) as guard:self.assertFalse(guard.active)
            dll.assert_not_called()
        calls=[];messages=[]
        with SystemAwake(setter=lambda flags:calls.append(flags) or 0,log=messages.append) as guard:self.assertFalse(guard.active)
        self.assertEqual(calls,[ES_CONTINUOUS|ES_SYSTEM_REQUIRED]);self.assertIn('未能启用',messages[0])

    def test_existing_settings_default_to_awake_and_switch_is_saved_and_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'settings.json';path.write_text('{}',encoding='utf-8')
            self.assertTrue(Settings.load(path).keep_awake)
            config=Settings.load(path);config.keep_awake=False;config.save(path)
            self.assertFalse(Settings.load(path).keep_awake)
            with self.assertRaisesRegex(ValueError,'防自动睡眠'):Settings(keep_awake='yes').validate()

    def test_phone_worker_holds_request_for_session_and_releases_on_stop_or_error(self):
        loader=importlib.machinery.SourceFileLoader('power_test_task_app',str(Path(__file__).resolve().parents[1]/'task_app.pyw'))
        spec=importlib.util.spec_from_loader(loader.name,loader);app=importlib.util.module_from_spec(spec);loader.exec_module(app)
        class FakeChat:
            connection_path='unused';stop_event=None
            def __init__(self,**kwargs):self.stop_event=threading.Event()
            def __enter__(self):return self
            def __exit__(self,*args):pass
        for local,enabled,fail in ((False,True,False),(False,True,True),(True,True,False),(False,False,False)):
            with self.subTest(local=local,enabled=enabled,fail=fail):
                calls=[];stop=threading.Event();entered=threading.Event()
                owner=app.TaskApp.__new__(app.TaskApp);owner.config=SimpleNamespace(keep_awake=enabled,manage_backend=False,prefix='/py')
                owner.catalog=object();owner.events=queue.Queue();owner.write=lambda _:None
                def setter(flags):calls.append(flags);return ES_CONTINUOUS
                def awake(**kwargs):return SystemAwake(**kwargs,setter=setter)
                def hub(*args,**kwargs):entered.set();stop.wait(2)
                def relay(*args,**kwargs):
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(calls,[ES_CONTINUOUS|ES_SYSTEM_REQUIRED] if enabled else [])
                    if fail:raise OSError('test transport error')
                    stop.set()
                def local_log(text):
                    if '本机试用已就绪' in text:stop.set()
                if local:owner.write=local_log
                with patch.object(app,'SystemAwake',side_effect=awake),patch.object(app,'ChatIO',FakeChat),patch.object(app,'run_hub',side_effect=hub),patch.object(app,'relay_background',side_effect=relay),patch.object(app,'BridgeClient'),patch.object(app,'load_connection',return_value={}),patch.object(app,'save_connection'):
                    owner.run(local,stop)
                self.assertEqual(calls,[ES_CONTINUOUS|ES_SYSTEM_REQUIRED,ES_CONTINUOUS] if enabled and not local else [])
                self.assertTrue(stop.is_set())

    @unittest.skipUnless(sys.platform=='win32','Windows execution-state API')
    def test_real_windows_request_is_registered_and_released(self):
        setter=ctypes.WinDLL('kernel32',use_last_error=True).SetThreadExecutionState
        setter.argtypes=[ctypes.c_uint32];setter.restype=ctypes.c_uint32
        try:
            with SystemAwake(log=lambda _:None) as guard:
                self.assertTrue(guard.active)
                self.assertEqual(setter(ES_CONTINUOUS|ES_SYSTEM_REQUIRED),ES_CONTINUOUS|ES_SYSTEM_REQUIRED)
            self.assertEqual(setter(ES_CONTINUOUS),ES_CONTINUOUS)
        finally:setter(ES_CONTINUOUS)


if __name__=='__main__':unittest.main()
