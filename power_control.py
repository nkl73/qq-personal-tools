"""A thread-scoped Windows idle-sleep request; the display may still turn off."""
import ctypes
import sys

ES_CONTINUOUS=0x80000000
ES_SYSTEM_REQUIRED=0x00000001


class SystemAwake:
    def __init__(self,enabled=True,log=print,setter=None):
        self.enabled=enabled;self.log=log;self.setter=setter;self.active=False

    def __enter__(self):
        if not self.enabled:return self
        if self.setter is None:
            if sys.platform!='win32':
                self.log('当前系统未启用防自动睡眠，请自行保持电脑唤醒。');return self
            try:
                self.setter=ctypes.WinDLL('kernel32',use_last_error=True).SetThreadExecutionState
                self.setter.argtypes=[ctypes.c_uint32];self.setter.restype=ctypes.c_uint32
            except (OSError,AttributeError):
                self.log('防自动睡眠未能启用，请检查电脑电源设置。');return self
        self.active=bool(self.setter(ES_CONTINUOUS|ES_SYSTEM_REQUIRED))
        self.log('已防止自动睡眠：连接手机期间保持主机唤醒，屏幕仍可关闭；手动睡眠或合盖仍可能中断。' if self.active
            else '防自动睡眠未能启用，请检查电脑电源设置。')
        return self

    def __exit__(self,*args):
        if self.active:
            # Called on the same bridge worker thread that acquired the request.
            if self.setter(ES_CONTINUOUS):self.log('已解除防自动睡眠，恢复原有电源行为。')
            else:self.log('防自动睡眠释放未确认；当前工作线程结束时系统会清理其请求。')
            self.active=False
