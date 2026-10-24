"""Local-only configuration. No model provider or user-specific defaults."""
from dataclasses import asdict,dataclass,field
import json
from pathlib import Path
import re
import secrets
import sys

ROOT=Path(__file__).resolve().parent
STATE=ROOT/'.runtime'


def validate_prefix(value):
    if not isinstance(value,str) or not re.fullmatch(r'/[^\s\x00-\x1f]{1,15}',value):
        raise ValueError('消息前缀需以 / 开头，长度 2–16，不能包含空格。')
    return value


@dataclass
class Settings:
    target: str=str(ROOT/'examples'/'echo.py')
    python_executable: str=sys.executable
    working_directory: str=''
    arguments: list=field(default_factory=list)
    prefix: str='/py'
    timeout: int=300
    manage_backend: bool=True
    keep_awake: bool=True

    def validate(self):
        validate_prefix(self.prefix)
        if not isinstance(self.arguments,list) or not all(isinstance(v,str) for v in self.arguments):
            raise ValueError('程序参数必须是文字列表。')
        if not isinstance(self.timeout,int) or isinstance(self.timeout,bool) or not 1<=self.timeout<=86400:
            raise ValueError('单轮等待时间需在 1–86400 秒之间。')
        if not isinstance(self.manage_backend,bool):
            raise ValueError('后台组件设置需为开关。')
        if not isinstance(self.keep_awake,bool):raise ValueError('防自动睡眠设置需为开关。')
        for value in (self.target,self.python_executable,self.working_directory):
            if not isinstance(value,str) or '\x00' in value:
                raise ValueError('程序路径格式无效。')
        return self

    def save(self,path=None):
        self.validate()
        path=Path(path or STATE/'settings.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(asdict(self),ensure_ascii=False,indent=2),encoding='utf-8')

    @classmethod
    def load(cls,path=None):
        path=Path(path or STATE/'settings.json')
        if not path.is_file():
            return cls()
        data=json.loads(path.read_text(encoding='utf-8'))
        return cls(**data).validate()


def load_connection(path=None):
    path=Path(path or STATE/'onebot.json')
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    value={'api_url':'http://127.0.0.1:18768','event_port':18767,'token':secrets.token_urlsafe(36)}
    save_connection(value,path)
    return value


def save_connection(value,path=None):
    from onebot_adapter import OneBotClient
    OneBotClient(value['api_url'],value['token'])
    if isinstance(value['event_port'],bool) or not 1<=int(value['event_port'])<=65535:
        raise ValueError('消息接收端口需在 1–65535 之间。')
    validate_prefix(value.get('prefix','/py'))
    path=Path(path or STATE/'onebot.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2),encoding='utf-8')
