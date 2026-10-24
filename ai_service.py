"""Desktop-configured text AI profiles and cancellable, isolated HTTP calls."""
import base64
import ctypes
from dataclasses import asdict,dataclass,replace
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit,urlunsplit

from process_control import options,terminate

MAX_INPUT=12000
MAX_REPLY=24000
MAX_HISTORY=48000


def endpoint(value):
    if not isinstance(value,str) or any(c.isspace() or ord(c)<32 for c in value):
        raise ValueError('服务地址不能包含空格或换行。')
    try:
        parts=urlsplit(value)
        host=parts.hostname
        port=parts.port
    except ValueError:raise ValueError('服务地址格式无效。') from None
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('请填写服务基础地址，不要在地址里放密钥、查询参数或账号。')
    try:local=ipaddress.ip_address(host).is_loopback
    except ValueError:local=host.lower()=='localhost'
    if parts.scheme!='https' and not (parts.scheme=='http' and local):
        raise ValueError('远程服务须使用 HTTPS；HTTP 只允许本机地址。')
    path=parts.path.rstrip('/')
    if not path.endswith('/chat/completions'):path+='/chat/completions'
    return urlunsplit((parts.scheme,parts.netloc,path,'',''))


def models_endpoint(value):
    parts=urlsplit(endpoint(value))
    return urlunsplit((parts.scheme,parts.netloc,parts.path[:-len('/chat/completions')]+'/models','',''))


@dataclass
class AIProfile:
    id: str='ai'
    name: str='我的 AI'
    base_url: str=''
    model: str=''
    system_prompt: str=''
    timeout: int=120
    allow_tools: bool=False

    def validate(self):
        if not isinstance(self.id,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',self.id):
            raise ValueError('手机工具 ID 请使用小写英文字母开头，最长 32 位。')
        if not isinstance(self.name,str) or not 1<=len(self.name.strip())<=80:
            raise ValueError('AI 名称需为 1–80 个字符。')
        if not isinstance(self.model,str) or not 1<=len(self.model.strip())<=200 or any(ord(c)<32 for c in self.model):
            raise ValueError('请填写服务商提供的模型名称或接入点 ID。')
        if not isinstance(self.system_prompt,str) or len(self.system_prompt)>8000:
            raise ValueError('固定要求最多 8000 字。')
        if type(self.timeout) is not int or not 5<=self.timeout<=600:
            raise ValueError('单次等待上限需为 5–600 秒。')
        if type(self.allow_tools) is not bool:raise ValueError('电脑工具开关需为开或关。')
        endpoint(self.base_url)
        return self


def protect(value,decrypt=False):
    """DPAPI binds an optional saved key to this Windows user's credentials."""
    if os.name!='nt':raise ValueError('此系统仅支持本次打开期间使用密钥，不支持加密记住。')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    raw=base64.b64decode(value,validate=True) if decrypt else value.encode('utf-8')
    buffer=ctypes.create_string_buffer(raw)
    source=Blob(len(raw),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    output=Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[wintypes.HLOCAL];kernel.LocalFree.restype=wintypes.HLOCAL
    function=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    function.restype=wintypes.BOOL
    if not function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output)):
        raise ValueError('本机密钥加密或解密失败，请重新填写。')
    try:
        data=ctypes.string_at(output.data,output.size)
        return data.decode('utf-8') if decrypt else base64.b64encode(data).decode('ascii')
    finally:kernel.LocalFree(output.data)


def validate_key(key):
    if not isinstance(key,str) or len(key)>4096 or any(ord(c)<33 or ord(c)>126 for c in key):
        raise ValueError('API Key 格式无效，请勿包含空格或换行。')
    return key


class ProfileStore:
    def __init__(self,path):
        self.path=Path(path)
        self.lock=threading.RLock()
        self.profiles={};self._keys={};self._encrypted={}
        if self.path.exists():
            rows=json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(rows,list):raise ValueError('AI 配置格式无效。')
            for row in rows:
                profile=AIProfile(**row['profile']).validate()
                self.profiles[profile.id]=profile
                encrypted=row.get('encrypted_key','')
                if encrypted:
                    self._encrypted[profile.id]=encrypted
                    try:self._keys[profile.id]=validate_key(protect(encrypted,True))
                    except (ValueError,OSError):pass

    def all(self):
        with self.lock:return [replace(profile) for profile in self.profiles.values()]

    def get(self,identifier):
        with self.lock:
            if identifier not in self.profiles:raise ValueError('AI 配置不存在，请在电脑重新添加。')
            return replace(self.profiles[identifier])

    def key(self,identifier):
        with self.lock:return self._keys.get(identifier,'')

    def remembered(self,identifier):
        with self.lock:return identifier in self._encrypted

    def save(self,profile,key=None,remember=False):
        profile=replace(profile).validate()
        with self.lock:
            key=validate_key(self.key(profile.id) if key is None else key)
            previous=(self.profiles.copy(),self._keys.copy(),self._encrypted.copy())
            encrypted=protect(key) if remember and key else ''
            self.profiles[profile.id]=profile;self._keys[profile.id]=key
            if encrypted:self._encrypted[profile.id]=encrypted
            else:self._encrypted.pop(profile.id,None)
            try:self._write()
            except Exception:
                self.profiles,self._keys,self._encrypted=previous
                raise

    def remove(self,identifier):
        with self.lock:
            previous=(self.profiles.copy(),self._keys.copy(),self._encrypted.copy())
            self.profiles.pop(identifier,None);self._keys.pop(identifier,None);self._encrypted.pop(identifier,None)
            try:self._write()
            except Exception:
                self.profiles,self._keys,self._encrypted=previous
                raise

    def _write(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_name(self.path.stem+'-'+secrets.token_hex(6)+'.tmp')
        rows=[{'profile':asdict(p),'encrypted_key':self._encrypted.get(p.id,'')} for p in self.profiles.values()]
        try:
            temp.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
            for attempt in range(6):
                try:temp.replace(self.path);break
                except PermissionError:
                    if attempt==5:raise
                    time.sleep(.03*2**attempt)
        finally:
            if temp.exists():
                try:temp.unlink()
                except OSError:pass


def call(profile,key,messages,stop=None,*,operation='chat',tools=None):
    """Secrets go to the owned child over stdin, never command arguments or disk."""
    profile.validate();validate_key(key)
    host=urlsplit(profile.base_url).hostname
    try:local=ipaddress.ip_address(host).is_loopback
    except ValueError:local=host.lower()=='localhost'
    if not key and not local:
        raise ValueError('此 AI 尚未填写可用密钥，请在电脑“编辑选中”中补填 API Key。')
    stop=stop if stop is not None else threading.Event()
    if stop.is_set():raise EOFError('已停止 AI 请求。')
    if operation not in ('chat','models','agent'):raise ValueError('不支持此 AI 操作。')
    if operation=='agent' and not profile.allow_tools:raise ValueError('此 AI 尚未开启电脑工具。')
    payload=json.dumps({'profile':asdict(profile),'key':key,'messages':messages,'operation':operation,'tools':tools},ensure_ascii=True)
    if len(payload)>2*1024*1024:raise ValueError('本次对话过长，请新建会话。')
    process=subprocess.Popen([sys.executable,'-X','utf8','-u',str(Path(__file__).with_name('ai_worker.py'))],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',**options())
    deadline=time.monotonic()+profile.timeout
    try:
        first=True
        while True:
            if stop.is_set():raise EOFError('已停止 AI 请求。')
            if time.monotonic()>=deadline:raise ValueError('AI 请求超时；不会自动重发，请稍后重新提问。')
            try:
                output,_=process.communicate(input=payload if first else None,timeout=.1)
                break
            except subprocess.TimeoutExpired:first=False
        try:result=json.loads(output)
        except (ValueError,TypeError):raise ValueError('AI 请求进程未返回有效结果，请检查 Python 环境。') from None
        if not result.get('ok'):raise ValueError(result.get('error','AI 请求失败。'))
        if operation=='models':return result['models']
        if operation=='agent':return result['message']
        text=result['text']
        if key:text=text.replace(key,'[密钥已隐藏]')
        return text
    finally:
        terminate(process)
        for stream in (process.stdin,process.stdout,process.stderr):
            if stream is not None:stream.close()


def list_models(base_url,key,stop=None):
    profile=AIProfile(base_url=base_url,model='model-discovery',timeout=20)
    return call(profile,key,[],stop,operation='models')


class ToolTurnError(ValueError):
    """Tool receipts remain in context even if a subsequent model call fails."""


class Conversation:
    def __init__(self,profile,key,tool_session=None):
        self.profile=replace(profile).validate();self.key=key;self.messages=[];self.tool_session=tool_session

    def safe(self,text):return text.replace(self.key,'[密钥已隐藏]') if self.key else text

    def ask(self,text,stop):
        if not text.strip():raise ValueError('请输入问题；结束对话请使用“结束 任务编号”。')
        if len(text)>MAX_INPUT:raise ValueError('每次提问最多 12000 字，请拆分发送。')
        history=list(self.messages)
        while history and (len(history)>=40 or len(json.dumps(history,ensure_ascii=False))+len(text)>MAX_HISTORY):
            # Drop complete user turns so tool calls and their receipts stay paired.
            boundary=next((i for i in range(1,len(history)) if history[i]['role']=='user'),len(history))
            history=history[boundary:]
        if self.profile.allow_tools and self.tool_session is not None:
            return self.ask_tools(text,history,stop)
        messages=([{'role':'system','content':self.profile.system_prompt}] if self.profile.system_prompt else [])
        messages+=history+[{'role':'user','content':text}]
        answer=call(self.profile,self.key,messages,stop)
        self.messages=history+[{'role':'user','content':text},{'role':'assistant','content':answer}]
        return answer

    def ask_tools(self,text,history,stop):
        from ai_tools import SYSTEM_GUIDANCE
        turn=[{'role':'user','content':text}];cache={};count=0;acted=False
        deadline=time.monotonic()+min(600,max(30,self.profile.timeout*3))
        self.tool_session.deadline=deadline
        system=SYSTEM_GUIDANCE+('\n用户在电脑配置的固定要求：\n'+self.profile.system_prompt if self.profile.system_prompt else '')
        try:
            for _ in range(8):
                if stop.is_set():raise EOFError('AI 会话已停止')
                remaining=int(deadline-time.monotonic())
                if remaining<5:raise ValueError('本轮工具工作达到等待上限，已停止；不会自动重试。')
                messages=[{'role':'system','content':system}]+history+turn
                if len(json.dumps(messages,ensure_ascii=False))>120000:raise ValueError('本轮工具上下文过长，请缩小文件范围。')
                response=call(replace(self.profile,timeout=min(self.profile.timeout,remaining)),self.key,messages,stop,
                    operation='agent',tools=self.tool_session.definitions())
                calls=response.get('tool_calls',[])
                if not calls:
                    answer=self.safe(response['content']);turn.append({'role':'assistant','content':answer})
                    self.messages=history+turn
                    return answer
                turn.append(response)
                for item in calls:
                    function=item['function'];name=function['name'];arguments=self.safe(function['arguments'])
                    result=None
                    try:
                        args=json.loads(arguments)
                        signature=(name,json.dumps(args,sort_keys=True,ensure_ascii=False))
                        if signature in cache:result=cache[signature]
                        elif count>=12:result={'error':'本轮最多执行 12 次工具操作，请根据已有结果回答。'}
                        else:
                            count+=1;acted=True
                            result=self.tool_session.execute(name,args,stop)
                            cache[signature]=result
                    except (ValueError,TypeError,json.JSONDecodeError):result={'error':'工具参数无效，请按工具定义修正。'}
                    content=self.safe(json.dumps(result,ensure_ascii=False))
                    turn.append({'role':'tool','tool_call_id':item['id'],'content':content})
            raise ValueError('本轮达到 8 次模型请求上限，已停止；不会自动继续。')
        except ValueError as error:
            if acted:
                message=self.safe(str(error))+' 已执行的操作和结果已保留，请先查看任务通知；不要重复提交同一操作。'
                # Every assistant call has a tool receipt before the next HTTP call.
                turn.append({'role':'assistant','content':message});self.messages=history+turn
                raise ToolTurnError(message) from None
            raise
