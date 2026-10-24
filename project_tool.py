"""An explicitly configured project adapter: inspect, edit, test and export."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path,PureWindowsPath
import secrets
import subprocess
import sys
import time
import zipfile

from process_control import options,terminate

MAX_FILE=64000
MAX_OUTPUT=12000


def digest(raw):return hashlib.sha256(raw).hexdigest()


class ProjectTool:
    def __init__(self,config_path,output=None):
        self.config_path=Path(config_path).resolve()
        config=json.loads(self.config_path.read_text(encoding='utf-8'))
        self.root=Path(config['root']).resolve()
        if not self.root.is_dir():raise ValueError('项目目录不存在。')
        self.editable=set(config['editable']);self.readable=set(config.get('readable',[]))|self.editable
        self.test_script=config['test_script'];self.readable.add(self.test_script)
        if self.test_script in self.editable:raise ValueError('固定测试脚本必须保持只读。')
        if not self.editable or len(self.readable)>100:raise ValueError('项目文件清单无效，最多 100 项。')
        for name in self.readable:self.path(name)
        self.state=self.config_path.parent/('project-state-'+digest(str(self.root).encode())[:12])
        self.state.mkdir(parents=True,exist_ok=True)
        self.output=Path(output).resolve() if output else None
        self.timeout=config.get('test_timeout',45)
        if type(self.timeout) is not int or not 1<=self.timeout<=120:raise ValueError('项目测试等待上限需为 1–120 秒。')

    def path(self,name,edit=False):
        if not isinstance(name,str) or not name or len(name)>240:raise ValueError('文件名无效。')
        # Treat Windows path spellings consistently even in non-Windows tests.
        if '\\' in name or ':' in name or PureWindowsPath(name).drive or name.startswith('/'):
            raise ValueError('只接受清单内的项目相对路径。')
        parts=name.split('/')
        if any(not p or p in ('.','..') or p.startswith('.') for p in parts):raise ValueError('项目路径无效。')
        allowed=self.editable if edit else self.readable
        if name not in allowed:raise ValueError('该文件未列入可编辑清单。' if edit else '该文件未列入可读清单。')
        path=self.root.joinpath(*parts)
        current=self.root
        for part in parts:
            current=current/part
            if current.is_symlink() or (hasattr(current,'is_junction') and current.is_junction()):raise ValueError('项目路径不能包含链接。')
        if not path.resolve().is_relative_to(self.root):raise ValueError('项目路径越界。')
        if not path.is_file():raise ValueError('清单文件不存在；本工具只修改已有文件。')
        return path

    def raw(self,name):
        with self.path(name).open('rb') as stream:raw=stream.read(MAX_FILE+1)
        if len(raw)>MAX_FILE:raise ValueError('项目文本文件最多 64 KB。')
        try:raw.decode('utf-8-sig')
        except UnicodeError:raise ValueError('项目文件需为 UTF-8 文本。') from None
        return raw

    def hashes(self):return {name:digest(self.raw(name)) for name in sorted(self.readable)}

    def originals(self):
        path=self.state/'originals.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def snapshot(self):
        """Desktop setup captures original bytes before handing the tool to AI."""
        path=self.state/'originals.json'
        if not path.exists():
            originals={name:self.raw(name).decode('utf-8') for name in sorted(self.editable)}
            with path.open('x',encoding='utf-8') as stream:json.dump(originals,stream,ensure_ascii=False,indent=2)
        return {'baseline_saved':True,'files':list(self.originals())}

    def changes(self):
        originals=self.originals()
        if not originals:raise ValueError('请先在电脑执行 snapshot 保存项目基线。')
        parts=[];changed=[]
        for name,before in sorted(originals.items()):
            after=self.raw(name).decode('utf-8')
            if before==after:continue
            changed.append(name)
            parts.extend(difflib.unified_diff(before.splitlines(keepends=True),after.splitlines(keepends=True),fromfile='a/'+name,tofile='b/'+name))
        return changed,''.join(parts)

    def execute(self,request):
        if not isinstance(request,dict) or not isinstance(request.get('action'),str):raise ValueError('请传入包含 action 的 JSON 对象。')
        action=request['action']
        fields={'inspect':{'action'},'read':{'action','path','offset'},'replace':{'action','path','sha256','content'},
            'diff':{'action'},'test':{'action'},'export':{'action'}}
        if action not in fields or set(request)!=fields[action]:raise ValueError('操作或参数无效；支持 inspect/read/replace/diff/test/export。')
        if action=='inspect':
            return {'files':[{'path':name,'editable':name in self.editable,'sha256':value} for name,value in self.hashes().items()],
                'test_script':self.test_script,'actions':{
                'read':{'action':'read','path':'清单相对路径','offset':0},
                'replace':{'action':'replace','path':'可编辑相对路径','sha256':'读取时的 SHA256','content':'完整新内容'},
                'diff':{'action':'diff'},'test':{'action':'test'},'export':{'action':'export'}},
                'note':'只能修改 editable 文件。测试脚本只读，test 固定执行该脚本；以 passed 为准。修改后必须重新测试，再 export。'}
        if action=='read':
            offset=request['offset']
            if type(offset) is not int or not 0<=offset<=MAX_FILE:raise ValueError('offset 无效。')
            raw=self.raw(request['path']);text=raw.decode('utf-8-sig');page=text[offset:offset+6000]
            return {'path':request['path'],'sha256':digest(raw),'content':page,
                'next_offset':offset+len(page) if offset+len(page)<len(text) else None,'total_chars':len(text)}
        if action=='replace':
            name=request['path'];path=self.path(name,edit=True);raw=self.raw(name)
            if request['sha256']!=digest(raw):raise ValueError('文件已经变化，请重新读取后再修改；未覆盖。')
            content=request['content']
            if not isinstance(content,str) or '\x00' in content or len(content.encode('utf-8'))>MAX_FILE:raise ValueError('新内容无效或超过 64 KB。')
            self.snapshot()
            temporary=path.with_name('.'+path.name+'.'+secrets.token_hex(6)+'.tmp')
            try:
                temporary.write_bytes(content.encode('utf-8'))
                # Re-check immediately before replacing an existing project file.
                self.path(name,edit=True)
                if digest(self.raw(name))!=request['sha256']:raise ValueError('文件已经变化，未覆盖。')
                temporary.replace(path)
            finally:temporary.unlink(missing_ok=True)
            return {'path':name,'changed':raw!=content.encode('utf-8'),'sha256':digest(content.encode('utf-8')),'backup_saved':True}
        if action=='diff':
            changed,diff=self.changes()
            return {'changed_files':changed,'diff':diff[:8000],'truncated':len(diff)>8000}
        if action=='test':return self.test()
        if self.output is None:raise ValueError('导出需要由桥接提供任务输出目录。')
        changed,diff=self.changes();test_path=self.state/'last-test.json'
        tested=json.loads(test_path.read_text(encoding='utf-8')) if test_path.exists() else {}
        sources={name:self.raw(name) for name in sorted(self.readable)}
        current={name:digest(raw) for name,raw in sources.items()};stale=tested.get('hashes')!=current
        if not tested.get('passed') or stale:raise ValueError('当前文件尚未通过固定测试，不能导出成功交付包；请先 test。')
        self.output.mkdir(parents=True,exist_ok=True)
        report={'passed':True,'changed_files':changed,'test_script':self.test_script,'test_output':tested['output'],
            'source_hashes':current,'note':'项目修改权限和测试由电脑配置；本结果不表示执行沙箱或所有场景均已覆盖。'}
        (self.output/'project-changes.diff').write_text(diff,encoding='utf-8')
        (self.output/'project-validation.txt').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        with zipfile.ZipFile(self.output/'project-source.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for name,raw in sources.items():archive.writestr(name,raw)
        return {'passed':True,'changed_files':changed,'artifacts':['project-changes.diff','project-validation.txt','project-source.zip']}

    def test(self):
        hashes=self.hashes();path=self.path(self.test_script)
        env=os.environ.copy();env.update(PYTHONIOENCODING='utf-8',PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
        # The desktop fixes the test script. The model cannot supply a shell,
        # interpreter, command arguments, environment variables or working dir.
        log=self.state/('test-'+secrets.token_hex(5)+'.log')
        timed_out=False
        with log.open('wb') as stream:
            process=subprocess.Popen([sys.executable,'-X','utf8','-u',str(path)],cwd=self.root,env=env,
                stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,**options())
            deadline=time.monotonic()+self.timeout
            try:
                while process.poll() is None:
                    if time.monotonic()>=deadline or log.stat().st_size>128000:
                        timed_out=True;break
                    time.sleep(.05)
            finally:terminate(process)
        with log.open('rb') as stream:output=stream.read(MAX_OUTPUT).decode('utf-8',errors='replace')
        passed=not timed_out and process.returncode==0 and hashes==self.hashes()
        report={'passed':passed,'exit_code':process.returncode,'stopped_by_limit':timed_out,'output':output,
            'hashes':hashes,'checked_at':time.time()}
        (self.state/'last-test.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return {key:value for key,value in report.items() if key!='hashes'}


def main():
    parser=argparse.ArgumentParser(description='Operate only explicitly configured project files')
    parser.add_argument('--config',required=True)
    parser.add_argument('--request',required=True)
    parser.add_argument('--snapshot',action='store_true',help='Desktop baseline setup, not an AI action')
    args=parser.parse_args()
    try:
        tool=ProjectTool(args.config,os.environ.get('QQ_BRIDGE_OUTPUT_DIR'))
        value=tool.snapshot() if args.snapshot else tool.execute(json.loads(args.request))
        print(json.dumps(value,ensure_ascii=False))
    except (ValueError,OSError,KeyError,TypeError):
        # Do not expose absolute paths or arbitrary exception text to the model.
        print(json.dumps({'error':'项目操作未完成：请检查操作参数、文件清单、读取时的 SHA256、固定测试和导出状态。'},ensure_ascii=False))


if __name__=='__main__':main()
