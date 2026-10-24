"""Keep one target Python process alive across successive QQ input messages."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from process_control import options as process_options,terminate


def redact(value):
    return re.sub(r'\bsk[-_][A-Za-z0-9_-]{12,}','[密钥已隐藏]',value)


@dataclass
class Turn:
    text: str
    finished: bool=False
    code: int=0
    secret: bool=False
    kind: str='prompt'


class InteractiveTarget:
    def __init__(self,script,stop_event=None,timeout=300,python_executable=None,arguments=None,cwd=None,on_output=None,environment=None):
        self.script=Path(script).resolve()
        self.python_executable=str(python_executable or sys.executable)
        self.arguments=list(arguments or [])
        if not all(isinstance(value,str) for value in self.arguments):
            raise ValueError('程序参数必须为文字列表。')
        self.cwd=Path(cwd).resolve() if cwd else self.script.parent
        self.on_output=on_output
        self.environment=dict(environment or {})
        self.stop_event=stop_event or threading.Event()
        self.timeout=timeout
        self.process=None
        self.events=None
        self.waiting=False
        self.prompt=''
        self.waiting_secret=False
        self.secrets=set()

    def safe_output(self,text):
        for value in sorted(self.secrets,key=len,reverse=True):
            text=text.replace(value,'[密钥已隐藏]')
        return redact(text)

    def start(self):
        self.close()
        if self.stop_event.is_set():
            raise EOFError('已停止')
        if not self.script.is_file():
            raise FileNotFoundError('找不到目标 Python 文件：'+str(self.script))
        runner=Path(__file__).with_name('target_runner.py')
        env=os.environ.copy()
        env.update(self.environment)
        env['PYTHONIOENCODING']='utf-8'
        env['PYTHONUTF8']='1'
        self.process=subprocess.Popen([self.python_executable,'-X','utf8','-u',str(runner),str(self.script),*self.arguments],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,encoding='utf-8',errors='replace',bufsize=1,env=env,cwd=self.cwd,
            **process_options())
        self.events=queue.Queue()
        process,events=self.process,self.events
        def reader():
            try:
                for line in process.stdout:
                    try:
                        event=json.loads(line)
                        if not isinstance(event,dict) or 'type' not in event:
                            raise ValueError()
                    except ValueError:
                        event={'type':'error','text':line}
                    events.put(event)
            finally:
                events.put({'type':'eof'})
        threading.Thread(target=reader,daemon=True).start()
        return self._until_pause()

    def _until_pause(self):
        parts=[]
        deadline=time.monotonic()+self.timeout
        while not self.stop_event.is_set():
            remaining=deadline-time.monotonic()
            if remaining<=0:
                self.close()
                return Turn(self.safe_output(''.join(parts))+'\n目标程序超过等待时间，已停止本次运行。',True,124)
            try:
                event=self.events.get(timeout=min(0.2,remaining))
            except queue.Empty:
                continue
            if event['type'] in ('output','error'):
                parts.append(event.get('text',''))
                if self.on_output is not None and not self.secrets:
                    self.on_output(event.get('text',''))
            elif event['type'] in ('prompt','secret_prompt'):
                self.waiting=True
                self.waiting_secret=event['type']=='secret_prompt'
                self.prompt=event.get('text','')
                return Turn(self.safe_output(''.join(parts)+self.prompt),secret=self.waiting_secret)
            elif event['type'] in ('exit','eof'):
                self.waiting=False
                code=event.get('code')
                if code is None:
                    try:
                        code=self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        code=1
                return Turn(self.safe_output(''.join(parts)),True,code)
        raise EOFError('已停止')

    def answer(self,text):
        if not self.waiting or self.process is None or self.process.poll() is not None:
            raise RuntimeError('目标程序当前没有等待输入，请重新启动。')
        if self.waiting_secret and text:
            self.secrets.add(text)
        self.waiting_secret=False
        self.waiting=False
        self.waiting_secret=False
        self.process.stdin.write(json.dumps({'text':text},ensure_ascii=True)+'\n')
        self.process.stdin.flush()
        return self._until_pause()

    def close(self):
        process=self.process
        self.process=None
        self.waiting=False
        if process is not None:
            terminate(process)
            for stream in (process.stdin,process.stdout):
                if stream is not None:
                    stream.close()


def run_backend(bridge,script,notices,stop_event,log=print,secret_provider=None,*,prefix='/py',**target_options):
    target=InteractiveTarget(script,stop_event,**target_options)
    def complete_local_prompts(turn):
        while turn.secret and not turn.finished:
            if secret_provider is None:
                raise RuntimeError('目标程序需要在电脑本机输入密钥，请在桥接窗口填写后启动。')
            log('目标程序正在等待本机隐私输入；内容不会经过 QQ。')
            secret=secret_provider(turn.text)
            if stop_event.is_set():
                raise EOFError('已停止')
            turn=target.answer(secret)
        return turn

    try:
        initial=complete_local_prompts(target.start())
        ended=initial.finished
        notices.put(initial.text+'\n'+(('程序已结束。发送 %s /启动 可重新运行。'%prefix) if ended
            else '请在手机用 %s 内容 回答当前提示；单独 %s 表示直接回车。'%(prefix,prefix)))
        log('原程序已启动，按原有顺序等待手机输入。' if not ended else '原程序已结束。')
        while not stop_event.is_set():
            uid,text=bridge.receive()
            log('正在将一项 QQ 输入交给目标程序：'+str(uid)[:8])
            if ended:
                if text not in ('/启动','/restart'):
                    bridge.finish(uid,'原程序已经结束。发送 %s /启动 可重新运行。'%prefix)
                    continue
                turn=complete_local_prompts(target.start())
            else:
                turn=complete_local_prompts(target.answer(text))
            result=turn.text
            ended=turn.finished
            if turn.finished:
                result+='\n\n原程序已结束。发送 %s /启动 可重新运行。'%prefix
            bridge.finish(uid,result,error=bool(turn.code))
    except EOFError:
        pass
    except Exception as error:
        log('目标程序异常：'+target.safe_output(str(error)))
        # Let the transport return the error before the user restarts the session.
        if 'uid' in locals():
            bridge.finish(uid,target.safe_output(str(error)),error=True)
        else:
            notices.put('目标程序启动失败：'+target.safe_output(str(error)))
    finally:
        target.close()
