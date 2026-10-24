"""Asynchronous, allowlisted local tasks controlled through short chat messages."""
from dataclasses import dataclass,field,replace
import json
import os
from pathlib import Path
import queue
import secrets
import sqlite3
import subprocess
import threading
import time

from process_control import options as process_options,terminate
from script_backend import InteractiveTarget
from settings import STATE
from tools import builtin
from ai_service import Conversation,ToolTurnError
from file_io import Attachment,shared_file
from phone_feedback import Feedback
import mobile_menu

FINAL={'completed','failed','cancelled','interrupted'}
LABELS={'queued':'排队中','running':'运行中','waiting_input':'等待手机输入','waiting_secret':'等待电脑隐私输入',
    'completed':'已完成','failed':'失败','cancelled':'已取消','interrupted':'上次运行已中断'}


class TaskStore:
    def __init__(self,folder):
        Path(folder).mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.db=sqlite3.connect(Path(folder)/'tasks.sqlite3',check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY,tool TEXT,name TEXT,state TEXT,output TEXT,created REAL,updated REAL)')
            if 'answer' not in {row['name'] for row in self.db.execute('PRAGMA table_info(tasks)')}:
                self.db.execute("ALTER TABLE tasks ADD COLUMN answer TEXT NOT NULL DEFAULT ''")
            self.db.execute("UPDATE tasks SET state='interrupted' WHERE state IN ('queued','running','waiting_input','waiting_secret')")

    def add(self,uid,tool):
        with self.lock,self.db:
            now=time.time()
            self.db.execute('INSERT INTO tasks (id,tool,name,state,output,created,updated) VALUES (?,?,?,?,?,?,?)',(uid,tool.id,tool.name,'queued','',now,now))

    def set_answer(self,uid,text):
        with self.lock,self.db:
            self.db.execute('UPDATE tasks SET answer=? WHERE id=?',(str(text)[-24000:],uid))

    def set(self,uid,state,output):
        with self.lock,self.db:
            self.db.execute('UPDATE tasks SET state=?,output=?,updated=? WHERE id=?',(state,output[-24000:],time.time(),uid))

    def get(self,uid):
        with self.lock:
            row=self.db.execute('SELECT * FROM tasks WHERE id=?',(uid,)).fetchone()
            if row is None:raise ValueError('找不到这个任务编号。')
            return dict(row)

    def recent(self,limit=20):
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM tasks ORDER BY created DESC LIMIT ?',(limit,))]

    def close(self):
        with self.lock:self.db.close()


@dataclass
class Task:
    id: str
    tool: object
    text: str
    stop: object=field(default_factory=threading.Event)
    end: object=field(default_factory=threading.Event)
    inputs: object=field(default_factory=queue.Queue)
    secrets: object=field(default_factory=queue.Queue)
    state: str='queued'
    output: str=''
    pending: str=''
    last_progress: float=field(default_factory=time.monotonic)
    lock: object=field(default_factory=threading.RLock)
    attached: set=field(default_factory=set)
    parent_id: str=''
    error: str=''
    result: object=None
    final_notified: bool=False


class LinkedEvent:
    """A child can stop itself without stopping its parent; parent stop cascades."""
    def __init__(self,parent):self.parent=parent;self.local=threading.Event()
    def is_set(self):return self.local.is_set() or self.parent.is_set()
    def set(self):self.local.set()
    def wait(self,timeout=None):
        deadline=float('inf') if timeout is None else time.monotonic()+timeout
        while not self.is_set():
            remaining=deadline-time.monotonic()
            if remaining<=0:return False
            self.local.wait(min(.05,remaining))
        return True


class Manager:
    def __init__(self,catalog,notices=None,folder=None,workers=2,on_secret=None,progress_interval=3,prefix='/py'):
        self.catalog=catalog
        self.notices=notices if notices is not None else queue.Queue()
        self.store=TaskStore(folder or STATE)
        self.feedback=Feedback(folder or STATE)
        self.on_secret=on_secret
        self.progress_interval=progress_interval
        self.prefix=prefix
        self.tasks={}
        self.lock=threading.RLock()
        self.pending=queue.Queue(maxsize=20)
        self.stop=threading.Event()
        self.web_bridge=None
        self.workers=[threading.Thread(target=self._worker,daemon=True) for _ in range(workers)]
        for worker in self.workers:worker.start()

    def start(self,identifier,text=''):
        tool=self.catalog.get(identifier).validate()
        with self.lock:
            if self.stop.is_set():raise ValueError('任务面板已停止。')
            if self.pending.full():raise ValueError('等待任务过多，请稍后再试。')
            uid=secrets.token_hex(4)
            while uid in self.tasks:uid=secrets.token_hex(4)
            task=Task(uid,tool,text)
            self.store.add(uid,tool)
            self.tasks[uid]=task
            self.pending.put_nowait(task)
        return uid

    def connect_web(self):
        from web_chat import WebBridge
        with self.lock:
            if self.web_bridge is None:self.web_bridge=WebBridge(self.catalog.path.parent/'web-chat')
            return self.web_bridge

    def _set(self,task,state):
        with task.lock:
            task.state=state
            if state in FINAL and task.tool.kind not in ('ai-chat','codex-chat','web-chat'):
                self.store.set_answer(task.id,task.error or (task.result if task.result is not None else task.output[-12000:]) or LABELS[state])
            self.store.set(task.id,state,task.output)

    def _feed(self,task,text,announce=True):
        if not text:return
        with task.lock:
            task.output=(task.output+text)[-24000:]
            task.pending=(task.pending+text)[-1200:]
            if announce and time.monotonic()-task.last_progress>=self.progress_interval:
                self.store.set(task.id,task.state,task.output)
                if self.feedback.detailed:
                    self.notices.put('任务 %s · %s · 进度\n%s'%(task.id,task.tool.name,task.pending[-1000:]))
                task.pending=''
                task.last_progress=time.monotonic()

    def notify(self,task,text,kind='result'):
        if task.parent_id and not self.feedback.detailed:return
        if kind=='result':self.store.set_answer(task.id,text)
        if self.feedback.detailed:
            text='任务 %s · %s\n%s'%(task.id,task.tool.name,text)
        else:
            # Disambiguate only when there really are several live user tasks.
            with self.lock:
                multiple=sum(not t.parent_id and t.state not in FINAL for t in self.tasks.values())>1
            if multiple:text=task.tool.name+' · '+task.id+'\n'+text
        self.notices.put(text)

    def _worker(self):
        while not self.stop.is_set():
            try:task=self.pending.get(timeout=.1)
            except queue.Empty:continue
            try:self._execute(task)
            finally:self.pending.task_done()

    def _execute(self,task):
        try:
            if task.stop.is_set():self._set(task,'cancelled');return
            self._set(task,'running')
            if task.tool.kind=='builtin':
                if task.tool.id=='fileinfo':
                    result=builtin('inspect',task.text,self.catalog.shared,task.stop)
                    (self.output_dir(task)/'文件概览.txt').write_text(result,encoding='utf-8')
                    result+='\n已生成 TXT 报告，附件将单独回传。'
                else:result=builtin(task.tool.id,task.text,self.catalog.shared,task.stop)
                self._feed(task,result,False);state='completed'
            elif task.tool.kind in ('interactive','codex-chat','web-chat'):state=self._interactive(task)
            elif task.tool.kind=='ai-chat':state=self._ai_chat(task)
            else:state=self._command(task)
            self._set(task,'cancelled' if task.stop.is_set() else state)
            if task.state=='completed':self.collect_files(task)
        except EOFError:
            task.error='连接已中断，未自动重发；请检查后再继续。'
            self._set(task,'cancelled' if task.stop.is_set() else 'failed')
        except Exception as error:
            task.error='任务错误：'+str(error)
            self._feed(task,'\n'+task.error,False)
            self._set(task,'cancelled' if task.stop.is_set() else 'failed')
        finally:
            if not self.stop.is_set():
                if self.feedback.detailed:
                    self.notices.put('任务 %s · %s · %s\n%s'%(task.id,task.tool.name,LABELS[task.state],task.output[-6000:] or '（没有文字输出）'))
                elif not task.parent_id and not task.final_notified:
                    if task.state=='cancelled':message='已停止。已发生的改动仍保留。'
                    elif task.state=='failed':message=task.error or task.output[-6000:] or '任务未完成，请查看电脑日志。'
                    elif task.tool.kind in ('ai-chat','codex-chat','web-chat'):message='对话已结束。'
                    else:message=(task.result if task.result is not None else task.output[-12000:]) or '已完成。'
                    if task.tool.id=='fileinfo' and task.state=='completed':message='已生成文件概览.txt。'
                    self.notify(task,message,'notice' if task.tool.kind in ('ai-chat','codex-chat','web-chat') and task.state!='failed' else 'result')

    def run_ai_tool(self,parent,tool,text,children,deadline):
        # Execute inside the parent's worker slot, avoiding deadlock when both
        # worker slots are occupied by AI conversations. Existing process control,
        # task records, progress, cancellation and attachment collection are reused.
        tool=self.catalog.get(tool.id).validate()
        if tool.kind!='builtin' and (tool.kind not in ('python-task','command') or not tool.ai_enabled):
            raise ValueError('此工具未授权给 AI。')
        remaining=int(deadline-time.monotonic())
        if remaining<1:raise ValueError('本轮工具工作达到等待上限。')
        with self.lock:
            if self.stop.is_set() or parent.stop.is_set():raise EOFError('AI 工作已停止')
            uid=secrets.token_hex(4)
            while uid in self.tasks:uid=secrets.token_hex(4)
            task=Task(uid,replace(tool,timeout=min(tool.timeout,remaining)),text,
                stop=LinkedEvent(parent.stop),parent_id=parent.id)
            self.store.add(uid,tool);self.tasks[uid]=task;children.add(uid)
        if self.feedback.detailed:self.notices.put('AI 会话 %s · 启动电脑工具 %s · 子任务 %s'%(parent.id,tool.id,uid))
        self._execute(task)
        return uid

    def output_dir(self,task):
        root=(self.catalog.shared/'results'/task.id).resolve()
        if not root.is_relative_to(self.catalog.shared):raise ValueError('任务输出目录必须在共享目录中。')
        root.mkdir(parents=True,exist_ok=True)
        return root

    def environment(self,task):
        return {'QQ_BRIDGE_SHARED_DIR':str(self.catalog.shared),'QQ_BRIDGE_OUTPUT_DIR':str(self.output_dir(task))}

    def collect_files(self,task):
        root=self.catalog.shared/'results'/task.id
        if not root.is_dir() or not root.resolve().is_relative_to(self.catalog.shared):return
        count=0
        for path in root.rglob('*'):
            if not path.is_file() or any(p.startswith('.') for p in path.relative_to(root).parts):continue
            try:
                relative=path.relative_to(self.catalog.shared).as_posix();shared_file(self.catalog.shared,relative)
                with task.lock:
                    if relative in task.attached:continue
                    self.notices.put(Attachment(relative,task_id=task.id));task.attached.add(relative);count+=1
            except ValueError:self.notices.put('任务 '+task.id+' 有文件超出共享边界或 50 MB，未回传。')
            if count>=10:
                self.notices.put('任务 '+task.id+' 最多自动回传 10 个附件；其他文件可用“发文件 文件名”取回。')
                break

    def mark_attached(self,task,relative):
        with task.lock:task.attached.add(relative)

    def _command(self,task):
        if task.stop.is_set() or self.stop.is_set():return 'cancelled'
        tool=task.tool
        cwd=tool.cwd or (str(Path(tool.target).resolve().parent) if Path(tool.target).is_file() else None)
        env=os.environ.copy();env.update(PYTHONIOENCODING='utf-8',PYTHONUTF8='1')
        env.update(self.environment(task))
        process=subprocess.Popen(tool.command(task.text,self.catalog.shared,self.output_dir(task)),cwd=cwd,env=env,stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding=tool.encoding,errors='replace',**process_options())
        output=queue.Queue(maxsize=128)
        def read():
            try:
                while True:
                    chunk=process.stdout.readline(4096)
                    if not chunk:break
                    while not task.stop.is_set() and not self.stop.is_set():
                        try:output.put(chunk,timeout=.1);break
                        except queue.Full:pass
            finally:
                while not task.stop.is_set() and not self.stop.is_set():
                    try:output.put(None,timeout=.1);break
                    except queue.Full:pass
        reader=threading.Thread(target=read,daemon=True);reader.start()
        deadline=time.monotonic()+tool.timeout
        eof=False
        try:
            while not eof:
                if task.stop.is_set() or self.stop.is_set():return 'cancelled'
                if time.monotonic()>=deadline:
                    self._feed(task,'\n任务超过等待上限，已停止。',False)
                    return 'failed'
                try:
                    value=output.get(timeout=.1)
                    if value is None:eof=True
                    else:self._feed(task,value)
                except queue.Empty:pass
            while process.poll() is None:
                if task.stop.is_set() or self.stop.is_set():return 'cancelled'
                if time.monotonic()>=deadline:
                    self._feed(task,'\n任务超过等待上限，已停止。',False)
                    return 'failed'
                time.sleep(.05)
            if process.returncode:
                self._feed(task,'\n程序退出码：'+str(process.returncode),False)
                return 'failed'
            return 'completed'
        finally:
            terminate(process)
            reader.join(timeout=2)
            process.stdout.close()

    def _interactive(self,task):
        tool=task.tool
        # Progress is returned at prompt boundaries after secrets have been entered.
        if tool.kind=='codex-chat':
            from codex_session import CodexConversation
            config_path=self.catalog.path.parent/'codex.json'
            config=json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
            initial=task.text.strip() or config.get('thread_id')
            target=CodexConversation(tool.target,task.stop,tool.timeout,on_output=lambda value:self._feed(task,value),initial_thread=initial)
        elif tool.kind=='web-chat':
            from web_chat import BrowserConversation
            if task.text.strip() not in ('','new','新建','@new'):raise ValueError('请先选择网页聊天，再发送要求。')
            target=BrowserConversation(self.connect_web(),task.stop,initial_new=bool(task.text.strip()))
        else:
            target=InteractiveTarget(tool.target,task.stop,tool.timeout,tool.python,
                tool.arguments_for(task.text,self.catalog.shared,self.output_dir(task)),tool.cwd or None,
                environment=self.environment(task))
        try:
            turn=target.start()
            while True:
                self._feed(task,turn.text+'\n',False)
                if turn.finished:
                    task.result=turn.text
                    return 'failed' if turn.code else 'completed'
                if turn.secret:
                    self._set(task,'waiting_secret')
                    if self.on_secret:self.on_secret(task.id,turn.text)
                    self.notify(task,'请在电脑面板填写本机隐私输入，不要发到 QQ。','prompt')
                    source=task.secrets
                else:
                    if turn.kind=='result':self.store.set_answer(task.id,turn.text)
                    self._set(task,'waiting_input')
                    text=turn.text
                    if self.feedback.detailed:text+='\n用 %s 内容 回答；切换用 %s 任务。'%(self.prefix,self.prefix)
                    self.notify(task,text,turn.kind)
                    source=task.inputs
                while not task.stop.is_set() and not self.stop.is_set():
                    if tool.kind in ('codex-chat','web-chat') and task.end.is_set():return 'completed'
                    try:value=source.get(timeout=.1);break
                    except queue.Empty:pass
                else:raise EOFError('任务已停止')
                self._set(task,'running')
                turn=target.answer(value)
        except Exception as error:
            task.error=target.safe_output(str(error))
            self._feed(task,'\n'+task.error,False)
            if isinstance(error,EOFError):return 'cancelled'
            return 'failed'
        finally:target.close()

    def _ai_chat(self,task):
        profile=self.catalog.ai_store.get(task.tool.target)
        from ai_tools import ToolSession
        key=self.catalog.ai_store.key(profile.id)
        safe=lambda text:text.replace(key,'[密钥已隐藏]') if key else text
        conversation=Conversation(profile,key,ToolSession(self,task,safe) if profile.allow_tools else None)
        value=task.text.strip()
        first=True
        while not task.stop.is_set() and not self.stop.is_set():
            if task.end.is_set():return 'completed'
            if value:
                try:
                    answer=conversation.ask(value,task.stop)
                    self._feed(task,'\n你：'+value+'\nAI：'+answer+'\n',False)
                    self.notify(task,answer)
                except ValueError as error:
                    message=str(error)
                    self._feed(task,'\n'+message+'\n',False)
                    suffix='本轮工具记录已保留；请先检查任务状态，再继续提问。' if isinstance(error,ToolTurnError) else '本次问题未加入上下文；可重新发送问题，或结束会话。'
                    self.notify(task,message+'\n'+suffix,'notice')
            if task.end.is_set():return 'completed'
            self._set(task,'waiting_input')
            if self.feedback.detailed:
                self.notices.put('任务 %s · %s · 等待提问\n发送 %s 回复 %s 问题 继续；发送 %s 结束 %s 完成会话。'%(task.id,task.tool.name,self.prefix,task.id,self.prefix,task.id))
            elif first and not value:self.notify(task,'请用 '+self.prefix+' 内容 发送你的要求。','prompt')
            first=False
            while not task.stop.is_set() and not self.stop.is_set():
                if task.end.is_set():return 'completed'
                try:value=task.inputs.get(timeout=.1);break
                except queue.Empty:pass
            else:raise EOFError('AI 会话已停止')
            self._set(task,'running')
            if not value.strip():
                self.notices.put('任务 %s：问题不能为空；结束请发送 %s 结束 %s。'%(task.id,self.prefix,task.id))
        return 'cancelled'

    def end_chat(self,uid):
        with self.lock:task=self.tasks.get(uid)
        if task is None or task.tool.kind not in ('ai-chat','codex-chat','web-chat'):raise ValueError('“结束”用于本次运行的 AI/Codex/ChatGPT 会话；其他任务请使用“取消”。')
        if task.state in FINAL:return '会话已经结束。'
        task.end.set()
        return '已请求结束会话 '+uid+'；正在进行的回答完成后退出。'

    def answer(self,uid,text,secret=False):
        with self.lock:
            task=self.tasks.get(uid)
        if task is None:raise ValueError('这个任务不在本次运行中。')
        expected='waiting_secret' if secret else 'waiting_input'
        with task.lock:
            if task.state!=expected:raise ValueError('该任务当前没有等待这种输入。')
            # Reserve this prompt immediately so repeated clicks cannot queue another answer.
            self._set(task,'running')
            (task.secrets if secret else task.inputs).put(text)

    def cancel(self,uid):
        row=self.store.get(uid)
        if row['state'] in FINAL:return '任务已经结束：'+LABELS[row['state']]
        with self.lock:task=self.tasks.get(uid)
        if task is None:raise ValueError('这个任务来自上次运行，不能继续操作。')
        task.stop.set()
        if task.state=='queued':
            self._set(task,'cancelled')
            if not self.feedback.detailed:
                task.final_notified=True;self.notify(task,'已取消排队任务。','notice')
        return '已请求取消任务 '+uid

    def close(self):
        self.stop.set()
        for task in list(self.tasks.values()):task.stop.set()
        for worker in self.workers:worker.join(timeout=7)
        if any(worker.is_alive() for worker in self.workers):
            raise RuntimeError('仍有任务未能停止，请检查本机任务进程。')
        for task in self.tasks.values():
            if task.state not in FINAL:self._set(task,'cancelled')
        if self.web_bridge is not None:self.web_bridge.close()
        self.store.close()


class Dispatcher:
    def __init__(self,manager,prefix='/py'):
        self.manager=manager;self.prefix=prefix;self.selected=None
        from phone_choices import PhoneChoices
        self.choices=PhoneChoices(self)
        self.silent=False

    def menu(self):
        return self.choices.home()

    def startup(self):
        self.manager.notices.put(self.menu() if self.manager.feedback.detailed else '已连接。发送 '+self.prefix+' 菜单 选择功能。')

    def handle(self,text):
        self.silent=False
        text=text.strip()
        command,_,rest=text.partition(' ')
        # Explicit commands remain available and leave any numbered menu.
        commands={'菜单','menu','帮助','help','工具','tools','文件','files','格式','formats','发文件','发图片','sendfile','sendimage',
            '运行','run','状态','任务','status','tasks','结果','result','详情','details','消息','取消','cancel','结束','end','选择','use','回复','reply','设备','device','codex','chatgpt','chat'}
        if command.lower() in commands:
            self.choices.clear()
            if command in ('菜单','menu') and not rest.strip():return self.menu()
            if command in ('工具','tools') and not rest.strip():return self.choices.tools()
            if command in ('文件','files') and not rest.strip():return self.choices.files()
            if command=='消息' and not rest.strip():return self.choices.feedback()
            if command in ('任务','tasks','状态','status','结果','result','取消','cancel','结束','end','选择','use') and not rest.strip():return self.choices.tasks()
        else:
            result=self.choices.consume(text)
            if result is not None:return result
        return self._handle(text)

    def receipt(self,text):
        self.silent=not self.manager.feedback.detailed
        return text

    def _handle(self,text):
        command,_,rest=text.partition(' ')
        rest=rest.strip()
        if command.lower() in ('chatgpt','chat'):
            task=self.manager.tasks.get(self.selected) if self.selected else None
            if task and task.tool.kind=='web-chat' and task.state not in FINAL:
                if rest=='停止':return self.manager.cancel(task.id)
                return self.converse(rest)
            command='运行';rest='chatgpt'+(' '+rest if rest else '')
        if command.lower()=='codex':
            if rest in ('菜单','帮助','help'):return mobile_menu.tool_help(self.manager.catalog,'codex',self.prefix)
            task=self.manager.tasks.get(self.selected) if self.selected else None
            if task and task.tool.kind=='codex-chat' and task.state not in FINAL:
                if rest=='停止':return self.manager.cancel(task.id)
                return self.converse(rest)
            command='运行';rest='codex'+(' '+rest if rest else '')
        if command in ('菜单','menu'):
            if rest in ('全部','all'):
                pages=mobile_menu.all_pages(self.manager.catalog,self.prefix)
                for index,page in enumerate(pages,1):
                    self.manager.notices.put('【使用说明 %d/%d】\n'%(index,len(pages))+page)
                return '已发送全部 %d 页使用说明；每页都是独立消息，可在本会话保存和回看。'%len(pages)
            return mobile_menu.section(self.manager.catalog,rest,self.prefix)
        if command in ('帮助','help'):
            return mobile_menu.tool_help(self.manager.catalog,rest,self.prefix) if rest else self.menu()
        if command in ('工具','tools'):return mobile_menu.section(self.manager.catalog,'工具 '+rest,self.prefix)
        if command in ('文件','files'):return builtin('files','',self.manager.catalog.shared)
        if command in ('格式','formats'):
            from document_text import format_help
            return format_help()
        if command in ('发文件','发图片','sendfile','sendimage'):
            path=shared_file(self.manager.catalog.shared,rest)
            relative=path.relative_to(self.manager.catalog.shared).as_posix()
            self.manager.notices.put(Attachment(relative,'image' if command in ('发图片','sendimage') else 'file'))
            return self.receipt('已加入附件回传队列：'+relative+'。本机试用仅显示文件。')
        if command in ('运行','run'):
            identifier,_,value=rest.partition(' ')
            if not identifier:raise ValueError('用法：运行 工具ID 内容。发送“菜单”查看工具。')
            uid=self.manager.start(identifier,value)
            if self.manager.tasks[uid].tool.kind in ('interactive','ai-chat','codex-chat','web-chat'):self.selected=uid
            return self.receipt('任务已创建：%s（%s）。结果会自动返回。\n查看结果或停止：发送 %s 任务，按编号选择。'%(uid,identifier,self.prefix))
        if command in ('状态','任务','status','tasks'):
            rows=[self.manager.store.get(rest)] if rest else self.manager.store.recent(10)
            return '\n'.join('%s · %s · %s'%(r['id'],r['name'],LABELS[r['state']]) for r in rows) or '还没有任务。发送“菜单”开始。'
        if command in ('结果','result'):
            if not rest:raise ValueError('请发送 %s 结果 真实任务编号；可先用 %s 状态 复制编号。'%(self.prefix,self.prefix))
            row=self.manager.store.get(rest)
            return row['answer'] or '暂时没有结果；可在任务菜单查看执行详情。'
        if command in ('详情','details'):
            row=self.manager.store.get(rest)
            return '任务 %s · %s\n%s'%(row['id'],LABELS[row['state']],row['output'][-12000:] or '暂无执行记录。')
        if command in ('取消','cancel'):
            if not rest:raise ValueError('请发送 %s 取消 真实任务编号；可先用 %s 状态 查看。'%(self.prefix,self.prefix))
            if self.manager.store.get(rest)['state'] in FINAL:return self.manager.cancel(rest)
            return self.receipt(self.manager.cancel(rest))
        if command in ('结束','end'):
            if not rest:raise ValueError('请发送 %s 结束 AI会话编号；可用“状态”查看编号。'%self.prefix)
            if self.manager.store.get(rest)['state'] in FINAL:return self.manager.end_chat(rest)
            return self.receipt(self.manager.end_chat(rest))
        if command in ('选择','use'):
            row=self.manager.store.get(rest)
            if row['state'] in FINAL:raise ValueError('任务已经结束，请重新运行工具。')
            self.selected=rest
            return '已选择任务 '+rest+'。接下来的普通文字将回答它的输入提示。'
        if command in ('回复','reply'):
            uid,_,value=rest.partition(' ')
            if not uid:raise ValueError('请发送 %s 回复 真实任务编号 内容；编号后不写内容表示空输入。'%self.prefix)
            self.manager.answer(uid,value)
            return self.receipt('已交给任务 '+uid+'。处理结果会自动返回。')
        if command in ('设备','device'):return builtin('device','',self.manager.catalog.shared)
        return self.converse(text)

    def converse(self,text):
        """Reply to a live prompt, or start the sole configured AI for a new request."""
        if self.selected:
            with self.manager.lock:task=self.manager.tasks.get(self.selected)
            if task is not None:
                with task.lock:
                    if task.state not in FINAL:
                        if task.stop.is_set() or task.end.is_set():
                            return '任务 '+task.id+' 正在结束，请等结束通知后再发送新要求。'
                        if task.state=='waiting_input':
                            self.manager.answer(task.id,text)
                            return self.receipt('已交给任务 '+task.id+'。处理结果会自动返回。')
                        if task.state=='waiting_secret':
                            return '任务 '+task.id+' 正在等待电脑上的隐私输入，请在电脑面板填写；不要把密钥发到 QQ。'
                        return '任务 '+task.id+' '+LABELS[task.state]+'，这条内容尚未提交。请等下一条回答或提问后再发送；查看进度：'+self.prefix+' 状态 '+task.id+'。'
            self.selected=None
        if not text:return self.menu()
        candidates=[tool for tool in self.manager.catalog.all() if tool.kind=='ai-chat']
        if not candidates:
            return '当前没有选中的待回答任务，也未接入 AI。请先在电脑添加 AI，或发送 '+self.prefix+' 菜单 工具 按示例运行已有工具。'
        if len(candidates)>1:
            return self.choices.ai(text)
        tool=candidates[0]
        if not self.manager.catalog.ai_store.key(tool.target):
            return '已找到 AI“'+tool.name+'”，但本次打开尚未填写密钥。请在电脑编辑此 AI 补填；密钥不要发到 QQ。'
        uid=self.manager.start(tool.id,text)
        self.selected=uid
        return self.receipt('已新建 AI 会话：'+uid+'（'+tool.name+'）。正在处理你的要求，结果会自动返回；之后可直接发 '+self.prefix+' 内容 继续。')


def run_hub(bridge,catalog,notices,stop,log=print,on_secret=None,on_manager=None,prefix='/py',directory=None):
    manager=Manager(catalog,notices,directory,on_secret=on_secret,prefix=prefix)
    if (catalog.path.parent/'web-chat'/'extension'/'manifest.json').is_file():
        try:manager.connect_web()
        except OSError:log('ChatGPT 网页通道端口未就绪；其他工具继续运行，请关闭重复的桥接面板。')
    if on_manager:on_manager(manager)
    dispatcher=Dispatcher(manager,prefix)
    dispatcher.startup()
    try:
        while not stop.is_set():
            uid,text=bridge.receive()
            try:result=dispatcher.handle(text)
            except Exception as error:
                dispatcher.silent=False;result='未执行：'+str(error)
            bridge.finish(uid,result,silent=dispatcher.silent)
    except EOFError:pass
    finally:manager.close()
