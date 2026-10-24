"""An allowlist of desktop-configured tools plus useful offline built-ins."""
import ast
import csv
import codecs
from dataclasses import asdict,dataclass,field
import io
import json
import math
import operator
from pathlib import Path
import platform
import re
import shutil
import secrets
import sys
import threading
import time

from settings import ROOT,STATE
from ai_service import ProfileStore
from file_io import shared_file
from document_text import read_document

BUILTINS={
    'device':('电脑状态','查看系统、运行环境和磁盘剩余空间'),
    'calc':('计算器','例：运行 calc 12*(3+4)，支持常规算术'),
    'text':('文字统计','统计字符、汉字、英文单词和行数'),
    'files':('共享文件列表','列出共享目录内的文件'),
    'inspect':('文件概览','查看 PDF、Word、Excel、PPTX 和常见文字文件的概况'),
    'fileinfo':('文件处理演示','读取文字文件概况，生成可回传的 TXT 报告'),
    'codex':('电脑 Codex','沿用本机 Codex 登录，继续已有会话或新建独立对话；不另填 API'),
    'chatgpt':('ChatGPT 网页','沿用浏览器登录，新建或选择已打开的普通聊天；需要本机网页扩展'),
}


@dataclass
class Tool:
    id: str
    name: str
    kind: str='python-task'
    description: str=''
    target: str=''
    python: str=sys.executable
    arguments: list=field(default_factory=list)
    cwd: str=''
    timeout: int=300
    encoding: str='utf-8'
    ai_enabled: bool=False

    def validate(self):
        if not isinstance(self.id,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',self.id):
            raise ValueError('工具 ID 请用小写英文字母开头，只含字母、数字、- 或 _，最长 32 位。')
        if not isinstance(self.name,str) or not self.name.strip() or len(self.name)>80:
            raise ValueError('工具名称需为 1–80 个字符。')
        if self.kind not in ('builtin','python-task','interactive','command','ai-chat','codex-chat','web-chat'):
            raise ValueError('不支持的工具类型。')
        if type(self.ai_enabled) is not bool:raise ValueError('AI 调用权限需为开或关。')
        if not isinstance(self.arguments,list) or not all(isinstance(v,str) and '\x00' not in v for v in self.arguments):
            raise ValueError('启动参数必须是文字列表。')
        if not isinstance(self.timeout,int) or isinstance(self.timeout,bool) or not 1<=self.timeout<=86400:
            raise ValueError('工具等待上限需在 1–86400 秒之间。')
        for value in (self.description,self.target,self.python,self.cwd):
            if not isinstance(value,str) or '\x00' in value:
                raise ValueError('工具设置包含无效文字。')
        try:codecs.lookup(self.encoding)
        except (LookupError,TypeError):raise ValueError('输出编码无效，例如 utf-8 或 gb18030。') from None
        if self.kind!='builtin' and not self.target:
            raise ValueError('请选择本机脚本或程序。')
        return self

    def arguments_for(self,text,shared=None,output=None):
        def replace(match):
            key=match.group(1)
            if key=='text':return text
            if key=='file':
                if shared is None:raise ValueError('此工具需要共享文件输入。')
                return str(shared_file(shared,text))
            if output is None:raise ValueError('此工具需要任务输出目录。')
            return str(output)
        return [re.sub(r'\{(text|file|output)\}',replace,value) for value in self.arguments]

    def command(self,text,shared=None,output=None):
        if self.kind=='ai-chat':raise ValueError('AI 由内置对话入口运行。')
        args=self.arguments_for(text,shared,output)
        if self.kind in ('interactive','python-task'):
            return [self.python,'-X','utf8','-u',str(Path(self.target).resolve()),*args]
        return [self.target,*args]


class Catalog:
    def __init__(self,path=None,shared=None):
        self.path=Path(path or STATE/'tools.json')
        self.shared=Path(shared or ROOT/'shared').resolve()
        self.lock=threading.RLock()
        self.custom={}
        self.ai_store=ProfileStore(self.path.with_name('ai_profiles.json'))
        if self.path.is_file():
            values=json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(values,list):
                raise ValueError('工具配置必须为列表。')
            for value in values:
                tool=Tool(**value).validate()
                if tool.id in BUILTINS or tool.kind in ('builtin','ai-chat','codex-chat','web-chat'):
                    raise ValueError('本机工具不能覆盖内置工具。')
                self.custom[tool.id]=tool

    def all(self):
        with self.lock:
            defaults=[Tool(key,name,{'codex':'codex-chat','chatgpt':'web-chat'}.get(key,'builtin'),description,
                target=key if key in ('codex','chatgpt') else '',timeout=3600 if key in ('codex','chatgpt') else 300)
                for key,(name,description) in BUILTINS.items()]
            demo=Tool('demo','进度演示','python-task','演示进度、完成通知和取消',
                str(ROOT/'examples'/'task_demo.py'),arguments=['{text}'],timeout=60)
            ai=[Tool(p.id,p.name,'ai-chat','AI 对话；已开启共享文件与授权工具' if p.allow_tools else 'AI 文字对话、写作、翻译；可连续追问',target=p.id,timeout=p.timeout)
                for p in self.ai_store.all() if p.id not in BUILTINS and p.id!='demo' and p.id not in self.custom]
            return defaults+([demo] if 'demo' not in self.custom else [])+list(self.custom.values())+ai

    def get(self,identifier):
        for tool in self.all():
            if tool.id==identifier:
                return Tool(**asdict(tool))
        raise ValueError('没有这个工具，发送“菜单”查看可用工具。')

    def save(self,tool):
        tool.validate()
        if tool.id in BUILTINS or tool.kind in ('builtin','ai-chat','codex-chat','web-chat'):
            raise ValueError('内置工具无需编辑；可另建一个本机工具。')
        with self.lock:
            if any(p.id==tool.id for p in self.ai_store.all()):raise ValueError('此 ID 已用于 AI，请换一个工具 ID。')
            previous=self.custom.get(tool.id)
            self.custom[tool.id]=tool
            try:self._write()
            except Exception:
                if previous is None:self.custom.pop(tool.id,None)
                else:self.custom[tool.id]=previous
                raise

    def remove(self,identifier):
        with self.lock:
            if any(p.id==identifier for p in self.ai_store.all()):
                self.ai_store.remove(identifier)
                return
            if identifier not in self.custom:
                raise ValueError('只能删除自己添加的工具。')
            previous=self.custom.pop(identifier)
            try:self._write()
            except Exception:
                self.custom[identifier]=previous
                raise

    def save_ai(self,profile,key=None,remember=False):
        with self.lock:
            if profile.id in BUILTINS or profile.id=='demo' or profile.id in self.custom:
                raise ValueError('此 ID 已被工具使用，请为 AI 换一个 ID。')
            self.ai_store.save(profile,key,remember)

    def _write(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_name(self.path.stem+'-'+secrets.token_hex(6)+'.tmp')
        try:
            temp.write_text(json.dumps([asdict(v) for v in self.custom.values()],ensure_ascii=False,indent=2),encoding='utf-8')
            for attempt in range(6):
                try:
                    temp.replace(self.path)
                    break
                except PermissionError:
                    # Windows readers or scanners can briefly hold the destination open.
                    if attempt==5:raise
                    time.sleep(.03*(2**attempt))
        finally:
            if temp.exists():
                try:temp.unlink()
                except OSError:pass


def calculate(text):
    if len(text)>500:
        raise ValueError('算式最长 500 字。')
    tree=ast.parse(text,mode='eval')
    if sum(1 for _ in ast.walk(tree))>80:
        raise ValueError('算式过于复杂。')
    operations={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,
        ast.Div:operator.truediv,ast.FloorDiv:operator.floordiv,ast.Mod:operator.mod,ast.Pow:operator.pow}
    def visit(node):
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):
            value=node.value
        elif isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            value=visit(node.operand)*(1 if isinstance(node.op,ast.UAdd) else -1)
        elif isinstance(node,ast.BinOp) and type(node.op) in operations:
            left,right=visit(node.left),visit(node.right)
            if isinstance(node.op,ast.Pow) and abs(right)>100:
                raise ValueError('指数绝对值不能超过 100。')
            value=operations[type(node.op)](left,right)
        else:
            raise ValueError('仅支持数字、括号和 + - * / // % **。')
        if type(value) not in (int,float) or abs(value)>1e100 or not math.isfinite(value):
            raise ValueError('结果超过计算范围。')
        return value
    return str(visit(tree.body))


def builtin(tool,text,shared,stop=None):
    shared=Path(shared).resolve()
    if tool=='device':
        disk=shutil.disk_usage(ROOT)
        return '系统：%s %s\nPython：%s\n磁盘剩余：%.1f GB / %.1f GB'%(platform.system(),platform.machine(),
            platform.python_version(),disk.free/1024**3,disk.total/1024**3)
    if tool=='calc':
        return calculate(text)
    if tool=='text':
        chinese=sum('\u4e00'<=char<='\u9fff' for char in text)
        words=len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?",text))
        return '字符：%d\n非空白字符：%d\n汉字：%d\n英文单词：%d\n行数：%d'%(len(text),sum(not c.isspace() for c in text),
            chinese,words,len(text.splitlines()))
    if tool=='files':
        if not shared.is_dir():
            return '共享目录还没有文件。在电脑面板点击“打开共享目录”放入文件后再试。'
        names=[]
        for path in shared.rglob('*'):
            if path.is_file() and not any(p.startswith('.') for p in path.relative_to(shared).parts) and path.resolve().is_relative_to(shared):
                names.append(path.relative_to(shared).as_posix())
                if len(names)>=100:break
        return '共享文件（最多 100 个）：\n'+'\n'.join(sorted(names)) if names else '共享目录还没有文件。'
    if tool=='inspect':
        if not text.strip():
            raise ValueError('请输入共享目录内的文件名，例如：运行 inspect example.csv')
        path=shared_file(shared,text.strip())
        document=read_document(path,stop);content=document['text']
        heading='文件：%s\n大小：%d 字节\n格式：%s\n读取范围：%s\n'%(path.name,path.stat().st_size,document['format'],document['scope'])
        if document['units']:heading+='页数/工作表信息：'+json.dumps(document['units'],ensure_ascii=False)+'\n'
        if document['warnings']:heading+='读取提示：'+'；'.join(document['warnings'])+'\n'
        if path.suffix.lower()=='.json':
            value=json.loads(content)
            info=('对象，%d 个字段，前 20 个字段：%s'%(len(value),', '.join(list(value)[:20]))) if isinstance(value,dict) else (
                '列表，%d 项'%len(value) if isinstance(value,list) else '类型：'+type(value).__name__)
            return heading+info
        if path.suffix.lower() in ('.csv','.tsv'):
            reader=csv.reader(io.StringIO(content),delimiter='\t' if path.suffix.lower()=='.tsv' else ',')
            rows=[];count=0
            for row in reader:
                count+=1
                if len(rows)<4:rows.append(row)
                if count>=10000:break
            return heading+'读取行数：%d（上限 10000）\n前几行：\n'%count+'\n'.join(' | '.join(row)[:500] for row in rows)
        return heading+builtin('text',content,shared)+'\n前 2000 字：\n'+content[:2000]
    raise ValueError('未知内置工具。')
