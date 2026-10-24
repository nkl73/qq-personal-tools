"""Use the local Codex app-server protocol and its existing cached login."""
import json
import os
from collections import deque
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
from urllib.parse import urlsplit

from process_control import options,terminate
from script_backend import Turn,redact
from codex_locator import resolve_codex


def answer_text(items):
    """Prefer final answers, keeping planning commentary out of the phone reply."""
    final=[item.get('text','') for item in items if item.get('phase')=='final_answer']
    if final:return '\n'.join(final)
    candidates=[item.get('text','') for item in items if item.get('phase') in (None,'partial_answer')]
    return candidates[-1] if candidates else ''


def codex_environment():
    """Honor explicit proxy env; otherwise inherit the existing Windows proxy per child."""
    env=os.environ.copy()
    if sys.platform!='win32' or any(name.lower() in ('http_proxy','https_proxy','all_proxy') for name in env):return env
    # Registry lookup also works when NO_PROXY alone is set in the environment.
    reader=getattr(urllib.request,'getproxies_registry',urllib.request.getproxies)
    try:proxies=reader()
    except OSError:return env
    inherited=False
    for scheme in ('http','https'):
        value=proxies.get(scheme)
        if not isinstance(value,str):continue
        try:parsed=urlsplit(value)
        except ValueError:continue
        if parsed.scheme not in ('http','https') or not parsed.hostname:continue
        env[scheme.upper()+'_PROXY']=value;inherited=True
    if inherited:
        bypass=','.join(filter(None,(env.get('NO_PROXY',''),env.get('no_proxy','')))).split(',')
        bypass=list(dict.fromkeys([item for item in bypass if item]+['localhost','127.0.0.1','::1']))
        env['NO_PROXY']=','.join(bypass)
        if 'no_proxy' in env:env['no_proxy']=env['NO_PROXY']
    return env


class CodexRPC:
    def __init__(self,executable=None,stop=None):
        self.executable=executable;self.stop=stop or threading.Event()
        self.process=None;self.events=queue.Queue(maxsize=4096);self.backlog=deque();self.counter=0;self.write_lock=threading.Lock()

    def start(self):
        self.executable=resolve_codex(self.executable)
        self.process=subprocess.Popen([self.executable,'app-server','--stdio'],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',errors='replace',bufsize=1,env=codex_environment(),**options())
        process=self.process
        def reader():
            try:
                while True:
                    line=process.stdout.readline(2*1024*1024+1)
                    if not line:break
                    if len(line)>2*1024*1024:break
                    try:value=json.loads(line)
                    except ValueError:continue
                    if not isinstance(value,dict):continue
                    try:self.events.put(value,timeout=2)
                    except queue.Full:break
            finally:
                try:self.events.put_nowait({'method':'bridge/eof'})
                except queue.Full:pass
        self.reader=threading.Thread(target=reader,daemon=True);self.reader.start()
        self.call('initialize',{'clientInfo':{'name':'qq_personal_tools','version':'0.9.0'},'capabilities':{'experimentalApi':True}})
        self.send({'method':'initialized'})

    def send(self,message):
        with self.write_lock:
            if self.process is None or self.process.poll() is not None:raise EOFError('Codex 本地连接已断开；不会自动重发任务。')
            self.process.stdin.write(json.dumps(message,ensure_ascii=True)+'\n');self.process.stdin.flush()

    def next(self,timeout=.2):
        if self.stop.is_set():raise EOFError('Codex 桥接已停止')
        try:value=self.backlog.popleft() if self.backlog else self.events.get(timeout=timeout)
        except queue.Empty:return None
        if value.get('method')=='bridge/eof':raise EOFError('Codex 本地连接已断开；请先查看原会话，避免重复执行。')
        return value

    def call(self,method,params,timeout=20):
        self.counter+=1;uid=self.counter
        self.send({'id':uid,'method':method,'params':params})
        deadline=time.monotonic()+timeout;deferred=[]
        try:
            while time.monotonic()<deadline:
                if self.stop.is_set():raise EOFError('Codex 桥接已停止')
                try:value=self.events.get(timeout=.2)
                except queue.Empty:continue
                if value.get('method')=='bridge/eof':raise EOFError('Codex 本地连接已断开；不会自动重发任务。')
                if value.get('id')==uid and 'method' not in value:
                    if 'error' in value:
                        error=value['error'];message=str(error.get('message','Codex 请求失败'))
                        raise ValueError(redact(message)[:600])
                    return value.get('result',{})
                deferred.append(value)
            raise ValueError('Codex 接口等待超时；不会自动重发，请先检查会话状态。')
        finally:
            self.backlog.extend(deferred)

    def reply(self,identifier,result):self.send({'id':identifier,'result':result})

    def close(self):
        if self.process is None:return
        terminate(self.process)
        if hasattr(self,'reader'):self.reader.join(timeout=2)
        for stream in (self.process.stdin,self.process.stdout):
            if stream:stream.close()
        self.process=None


class CodexConversation:
    def __init__(self,executable=None,stop_event=None,timeout=3600,on_output=None,rpc=None,initial_thread=None):
        self.stop=stop_event or threading.Event();self.rpc=rpc or CodexRPC(executable,self.stop)
        self.timeout=timeout;self.on_output=on_output;self.choices=[];self.cursor=None;self.selected=None
        self.pending=None;self.turn_id=None;self.output='';self.mode='select';self.secrets=set();self.finished=False
        self.initial_thread=initial_thread;self.items={};self.search='';self.retry_text=None
        self.projects=[];self.models=[];self.new_cwd=None;self.new_model=None;self.ready_thread=None
        self.answers=[];self.alerts=[]

    def safe_output(self,text):
        text=str(text)
        for value in sorted(self.secrets,key=len,reverse=True):text=text.replace(value,'[隐私值已隐藏]')
        text=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}',r'\1[凭据已隐藏]',text)
        return redact(text)

    def emit(self,text):
        text=self.safe_output(text);self.output=(self.output+text)[-24000:]
        if self.on_output:self.on_output(text)

    def prompt(self,text,secret=False):return Turn(self.safe_output(text),secret=secret)

    def result(self,text):return Turn(self.safe_output(text),kind='result')

    def alert(self,text):
        self.alerts.append(text.strip());self.emit(text)

    def handoff(self):
        name=self.selected.get('name') or self.selected.get('title') or '未命名会话'
        return self.prompt('已选择：'+name+'\n1. 继续原会话（接管）\n2. 选择其他会话\n4. 建立手机分支\n0. 结束桥接\n桌面应用可以保持打开。原会话被占用时可选手机分支：保留已保存的上下文，聊天记录不自动合并，项目文件仍共用。尚未提交工作要求。')

    def fork_selected(self):
        original=self.selected['id']
        saved=self.metadata(original)
        params={'threadId':original,'excludeTurns':True}
        # Fork otherwise uses the server's default model, unlike resume.
        for key in ('model','modelProvider'):
            if isinstance(saved.get(key),str) and saved[key]:params[key]=saved[key]
        if saved.get('reasoningEffort'):params['config']={'model_reasoning_effort':saved['reasoningEffort']}
        thread=self.rpc.call('thread/fork',params)['thread']
        if thread['id']==original:raise ValueError('未取得独立手机分支，未提交工作要求。')
        self.selected=thread;self.ready_thread=thread['id'];self.mode='chat'
        self.emit('已建立手机分支：'+thread['id']+'，来源：'+original+'\n')

    def busy(self,text,running=False):
        self.mode='writer_busy';self.retry_text=text
        reason='原会话仍在处理任务' if running else '原会话正被桌面连接占用'
        return self.prompt(reason+'，本条要求尚未提交。\n4. 开手机分支并处理刚才要求\n1. 重试原会话\n2. 取消请求\n3. 选择其他会话\n0. 结束桥接\n分支保留已保存上下文，聊天记录不合并，项目文件共用。继续原会话需先让它完成并释放连接；必要时退出桌面应用。不会自动重发。')

    def start(self):
        self.rpc.start()
        account=self.rpc.call('account/read',{'refreshToken':False}).get('account')
        if not account:raise ValueError('Codex 尚未登录，请先在电脑的官方 Codex 完成登录；不要把账号凭据发到 QQ。')
        self.emit('已连接电脑 Codex，使用本机已有登录。不会读取或回传登录令牌。\n')
        if self.initial_thread in ('@new','新建','new'):return self.new_projects()
        if self.initial_thread=='@list':return self.list_threads()
        if self.initial_thread:
            try:identifier=str(uuid.UUID(self.initial_thread))
            except ValueError:raise ValueError('Codex 默认会话编号无效，请重新选择。') from None
            return self.answer(identifier)
        return self.list_threads()

    def new_projects(self):
        rows=self.rpc.call('thread/list',{'limit':40,'sortKey':'updated_at','archived':False,'useStateDbOnly':True}).get('data',[])
        self.projects=[];seen=set()
        for row in rows:
            value=row.get('cwd')
            if not isinstance(value,str):continue
            path=Path(value)
            if not path.is_absolute() or not path.is_dir():continue
            key=str(path.resolve())
            if key in seen:continue
            seen.add(key);self.projects.append(key)
            if len(self.projects)>=8:break
        self.mode='new_project'
        lines=['【新建 Codex 对话｜选择项目】']
        lines+=['%d. %s'%(i,value) for i,value in enumerate(self.projects,1)]
        if not self.projects:lines.append('没有找到现存项目目录。请先在电脑 Codex 打开一个项目会话。')
        lines+=['0. 返回已有会话列表','按序号选择已用过的项目；不复制旧聊天记录，尚未创建会话。']
        return self.prompt('\n'.join(lines))

    def new_models(self):
        result=self.rpc.call('model/list',{'limit':20})
        self.models=[row for row in result.get('data',[]) if isinstance(row.get('id'),str) and not row.get('hidden')][:8]
        self.mode='new_model'
        return self.prompt('【新建 Codex 对话｜选择模型】\n1. 使用电脑默认模型\n'+'\n'.join('%d. %s'%(i,row.get('displayName') or row['id']) for i,row in enumerate(self.models,2))+'\n0. 返回选择项目\n目录：'+self.new_cwd)

    def create_confirm(self):
        self.mode='new_confirm'
        return self.prompt('【确认新建 Codex 对话】\n项目：'+self.new_cwd+'\n模型：'+(self.new_model or '电脑默认模型')+'\n权限与项目指令使用本机配置，不继承旧聊天记录。\n1. 确认新建\n0. 返回选择模型\n只有确认后才创建；创建本身不发送模型要求。')

    def list_threads(self,search='',more=False):
        params={'limit':8,'sortKey':'updated_at','archived':False,'useStateDbOnly':True}
        if not more:self.search=search
        if self.search:params['searchTerm']=self.search
        if more and self.cursor:params['cursor']=self.cursor
        response=self.rpc.call('thread/list',params)
        self.choices=response.get('data',[]);self.cursor=response.get('nextCursor')
        lines=['【电脑 Codex｜选择已有会话】']
        for index,row in enumerate(self.choices,1):
            name=row.get('name') or row.get('title') or '未命名会话'
            lines.append('%d. %s'%(index,name.replace('\n',' ')[:72]))
        if not self.choices:lines.append('未找到会话。请先在电脑 Codex 打开要继续的项目会话。')
        lines+=['回复序号或完整会话编号选择；“查找 关键词”搜索，“下一页”翻页。','这里只读取列表；选好后再发送工作要求，才会启动 Codex。']
        lines.append('0. 结束桥接')
        self.mode='select'
        return self.prompt('\n'.join(lines))

    def metadata(self,identifier):
        return self.rpc.call('thread/read',{'threadId':identifier,'includeTurns':False})['thread']

    def latest(self,identifier,items='notLoaded'):
        try:result=self.rpc.call('thread/turns/list',{'threadId':identifier,'limit':1,'sortDirection':'desc','itemsView':items})
        except ValueError as error:
            message=str(error)
            if 'not materialized yet' in message and 'before first user message' in message:return None
            raise
        rows=result.get('data',[])
        return rows[0] if rows else None

    def answer(self,text):
        text=text.strip()
        if self.pending:return self.answer_request(text)
        if text in ('退出','结束','exit'):
            self.finished=True;return Turn('Codex 桥接会话已结束；电脑原有会话仍保留。',True)
        if text in ('新建','new'):return self.new_projects()
        if text in ('会话','列表','切换','menu'):return self.list_threads()
        if text.startswith('查找 '):return self.list_threads(text[3:].strip())
        if text=='下一页':
            if not self.cursor:return self.prompt('已经是最后一页。回复“列表”重新查看，或发送完整会话编号。')
            return self.list_threads(more=True)
        if self.mode=='new_project':
            if text=='0':return self.list_threads()
            if text.isdigit() and 1<=int(text)<=len(self.projects):
                self.new_cwd=self.projects[int(text)-1];return self.new_models()
            return self.prompt('请选择显示的项目序号，0 返回；不会把任意手机文字当作电脑目录。')
        if self.mode=='new_model':
            if text=='0':return self.new_projects()
            if text=='1':self.new_model=None;return self.create_confirm()
            if text.isdigit() and 2<=int(text)<=len(self.models)+1:
                self.new_model=self.models[int(text)-2]['id'];return self.create_confirm()
            return self.prompt('请选择模型序号，1 使用电脑默认模型，0 返回。')
        if self.mode=='new_confirm':
            if text=='0':return self.new_models()
            if text!='1':return self.create_confirm()
            if not Path(self.new_cwd).is_dir():raise ValueError('所选项目目录已不存在，未创建会话。')
            params={'cwd':self.new_cwd}
            if self.new_model:params['model']=self.new_model
            self.selected=self.rpc.call('thread/start',params)['thread'];self.ready_thread=self.selected['id'];self.retry_text=None;self.mode='chat'
            return self.prompt('新 Codex 对话已建立，请发送工作要求。桌面应用可以保持打开；此对话不继承旧聊天上下文。')
        if self.mode=='select':
            if text=='0':return self.answer('退出')
            if text.isdigit() and 1<=int(text)<=len(self.choices):identifier=self.choices[int(text)-1]['id']
            else:
                try:identifier=str(uuid.UUID(text))
                except ValueError:return self.prompt('请选择列表中的序号，或发送完整会话编号；“查找 关键词”可搜索。')
            self.selected=self.metadata(identifier);self.mode='handoff';self.retry_text=None
            return self.handoff()
        if self.mode=='handoff':
            if text=='0':return self.answer('退出')
            if text=='2':return self.list_threads()
            if text=='4':
                self.fork_selected();self.retry_text=None
                return self.prompt('已建立手机分支，请发送工作要求。桌面原聊天可以继续使用；避免两边同时修改同一文件。')
            if text not in ('1','接管'):return self.handoff()
            self.mode='chat'
            return self.prompt('已确认继续原会话，请发送工作要求。')
        if text=='状态':
            latest=self.latest(self.selected['id'])
            return self.prompt('原会话最近一轮状态：'+str(latest.get('status') if latest else '尚无记录')+'。发送工作要求可继续。')
        if text=='结果':
            latest=self.latest(self.selected['id'],'full')
            items=[item for item in (latest or {}).get('items',[]) if item.get('type')=='agentMessage']
            return self.result(answer_text(items)[-12000:] or '暂无已保存的文字答复。')
        if self.mode=='writer_busy':
            if text=='4':
                value=self.retry_text;self.fork_selected();self.retry_text=None
                return self.run(value) if value else self.prompt('已建立手机分支，请发送工作要求。')
            text={'1':'重试','2':'取消请求','3':'列表','0':'退出'}.get(text,text)
            if text in ('列表','退出'):return self.answer(text)
            if text=='取消请求':
                self.retry_text=None;self.mode='chat'
                return self.prompt('已丢弃尚未提交的要求。可以发送新要求，或“退出”结束桥接。')
            if text=='重试':
                value=self.retry_text
                if not value:return self.prompt('没有待重试的要求，请发送新内容。')
                return self.run(value)
        if not text:return self.prompt('请发送工作要求；退出请回复“退出”。')
        if len(text)>12000:raise ValueError('每次工作要求最多 12000 字。')
        return self.run(text)

    def run(self,text):
        identifier=self.selected['id'];latest=self.latest(identifier)
        if latest and latest.get('status')=='inProgress':
            return self.busy(text,True)
        self.output='';self.answers=[];self.alerts=[]
        # Preserve the saved model, project, permissions, instructions and history.
        try:
            if self.ready_thread!=identifier:
                self.rpc.call('thread/resume',{'threadId':identifier,'excludeTurns':True});self.ready_thread=identifier
        except ValueError as error:
            if 'active writer' not in str(error).lower():raise
            return self.busy(text)
        self.retry_text=None;self.mode='chat'
        result=self.rpc.call('turn/start',{'threadId':identifier,'input':[{'type':'text','text':text}]})
        self.turn_id=result['turn']['id'];self.emit('Codex 已开始处理，原会话：'+identifier+'\n')
        return self.wait_turn()

    def wait_turn(self):
        deadline=time.monotonic()+self.timeout
        while not self.stop.is_set():
            if time.monotonic()>=deadline:
                self.interrupt();return self.prompt('本轮等待超过上限，已请求停止；已有改动仍保留。请先检查原会话。')
            event=self.rpc.next()
            if event is None:continue
            params=event.get('params') or {};method=event.get('method','')
            if params.get('threadId')!=self.selected['id']:
                if 'id' in event and method:
                    self.rpc.send({'id':event['id'],'error':{'code':-32601,'message':'Request is outside the QQ selected thread; no action approved.'}})
                continue
            event_turn=params.get('turnId') or (params.get('turn') or {}).get('id')
            if event_turn and event_turn!=self.turn_id:continue
            if 'id' in event and method:
                return self.request(event)
            if method=='error':
                # Surface connection state without forwarding raw service error bodies.
                self.emit('Codex 连接暂时中断，接口正在重连；本桥接没有重新提交要求。\n' if params.get('willRetry') else 'Codex 接口报告错误，请检查本轮状态；本桥接不会自动重发。\n')
            if method=='item/completed':
                item=params.get('item',{})
                if item.get('type')=='agentMessage':
                    self.answers.append(item);self.emit(item.get('text','')+'\n')
            elif method=='item/started':
                item=params.get('item',{});kind=item.get('type')
                if kind=='fileChange':self.items={item.get('id'):item}
                if kind in ('commandExecution','fileChange','mcpToolCall'):self.emit('[Codex 进度] '+{'commandExecution':'正在运行电脑命令','fileChange':'正在修改项目文件','mcpToolCall':'正在调用已接入工具'}[kind]+'\n')
            elif method=='turn/completed':
                turn=params['turn'];self.turn_id=None
                text='\n'.join(self.alerts+[answer_text(self.answers)]).strip()
                if turn.get('status')=='failed':text+='\nCodex 本轮失败：'+str((turn.get('error') or {}).get('message','请在电脑查看原因'))
                elif turn.get('status')=='interrupted':text+='\nCodex 本轮已停止；已有改动不会自动撤销。'
                self.mode='chat'
                return self.result(text.strip() or '本轮已完成，未返回文字结果。')
        raise EOFError('Codex 桥接已停止')

    def request(self,event):
        method=event['method'];params=event['params'];uid=event['id']
        if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval','item/permissions/requestApproval'):
            self.pending={'kind':'approval','event':event}
            description=params.get('command') or params.get('reason') or 'Codex 请求确认一项文件操作'
            if params.get('cwd'):description+='\n目录：'+params['cwd']
            if method=='item/fileChange/requestApproval':
                changes=self.items.get(params.get('itemId'),{}).get('changes',[])
                if changes:description+='\n'+json.dumps(changes,ensure_ascii=False)
                elif params.get('grantRoot'):description+='\n写入目录：'+params['grantRoot']
            if method=='item/permissions/requestApproval':description+='\n申请范围：'+json.dumps(params.get('permissions'),ensure_ascii=False)
            if len(str(description))>2400:
                self.pending=None
                self.rpc.reply(uid,{'permissions':{},'scope':'turn'} if method=='item/permissions/requestApproval' else {'decision':'decline'})
                self.alert('本次操作说明过长，无法在手机完整审阅，已拒绝授权；需要时请回电脑处理。\n')
                return self.wait_turn()
            return self.prompt('【Codex 等待确认】\n'+str(description)+'\n1. 同意（仅这一次）\n2. 拒绝\n3. 停止本轮\n回复序号或对应文字。')
        if method=='item/tool/requestUserInput':
            questions=params.get('questions',[])
            self.pending={'kind':'questions','event':event,'questions':questions,'answers':{},'index':0}
            return self.question()
        if method=='item/tool/call':
            self.rpc.reply(uid,{'success':False,'contentItems':[{'type':'inputText','text':'QQ 本地接口无法代执行此桌面客户端专属工具。请使用现有本地文件/命令工具，或明确告知需要回电脑处理。'}]})
            self.alert('此桌面专属工具无法通过当前连接执行，已告知 Codex。\n')
            return self.wait_turn()
        self.rpc.send({'id':uid,'error':{'code':-32601,'message':'QQ bridge does not implement this client request; no action approved.'}})
        self.alert('Codex 请求了暂不支持的客户端操作，未授权执行。\n')
        return self.wait_turn()

    def question(self):
        pending=self.pending;questions=pending['questions']
        if pending['index']>=len(questions):
            self.rpc.reply(pending['event']['id'],{'answers':pending['answers']});self.pending=None
            return self.wait_turn()
        question=questions[pending['index']]
        lines=['【Codex 提问】',question['question']]
        for index,option in enumerate(question.get('options') or [],1):lines.append('%d. %s'%(index,option['label']))
        lines.append('回复选项序号或直接填写回答；“停止”结束本轮。')
        return self.prompt('\n'.join(lines),secret=bool(question.get('isSecret')))

    def answer_request(self,text):
        pending=self.pending;event=pending['event']
        if pending['kind']=='approval':text={'1':'同意','2':'拒绝','3':'停止'}.get(text,text)
        if text=='停止':
            self.interrupt();self.pending=None;return self.prompt('已请求停止 Codex 本轮。请检查原会话；可继续发送新要求。')
        if pending['kind']=='approval':
            if text not in ('同意','拒绝'):return self.prompt('等待明确选择：“同意”仅确认本次操作；“拒绝”不授权；“停止”结束本轮。')
            if event['method']=='item/permissions/requestApproval':
                result={'permissions':event['params'].get('permissions',{}) if text=='同意' else {},'scope':'turn'}
            else:result={'decision':'accept' if text=='同意' else 'decline'}
            self.rpc.reply(event['id'],result)
        else:
            question=pending['questions'][pending['index']];options=question.get('options') or []
            if question.get('isSecret') and text:self.secrets.add(text)
            if text.isdigit() and 1<=int(text)<=len(options):text=options[int(text)-1]['label']
            pending['answers'][question['id']]={'answers':[text]};pending['index']+=1
            return self.question()
        self.pending=None
        return self.wait_turn()

    def interrupt(self):
        if self.turn_id and self.selected:
            try:self.rpc.call('turn/interrupt',{'threadId':self.selected['id'],'turnId':self.turn_id},timeout=5)
            except (ValueError,EOFError,OSError):pass
            self.turn_id=None

    def close(self):
        self.interrupt();self.rpc.close()
