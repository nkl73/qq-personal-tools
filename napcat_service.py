"""Provision and launch the portable background QQ component without a console."""
import json
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from urllib.parse import urlsplit

from bridge_core import ROOT


def owned_process_running(runtime,state,terminate=False):
    """Check our saved process without touching unrelated QQ or Node processes."""
    pid_file=state/'service-pid.json'
    if not pid_file.exists() or os.name!='nt':
        return False
    import ctypes
    from ctypes import wintypes
    try:
        info=json.loads(pid_file.read_text(encoding='utf-8'))
        if Path(info['executable']).resolve()!=runtime/'node.exe':
            return False
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        handle=kernel.OpenProcess(0x1000 | (0x100001 if terminate else 0),False,int(info['pid']))
        if not handle:
            return False
        try:
            size=wintypes.DWORD(32768)
            path=ctypes.create_unicode_buffer(size.value)
            exit_code=wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(exit_code)) or exit_code.value!=259:
                return False
            matched=bool(kernel.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(size))) and Path(path.value).resolve()==runtime/'node.exe'
            if matched and terminate:
                if not kernel.TerminateProcess(handle,0):
                    raise OSError('无法停止本桥接启动的后台 QQ 进程。')
                kernel.WaitForSingleObject(handle,2000)
            return matched
        finally:
            kernel.CloseHandle(handle)
    except (OSError,ValueError,KeyError,TypeError):
        return False


def prepare():
    folder=ROOT/'.runtime'
    folder.mkdir(exist_ok=True)
    runtime_file=folder/'napcat-runtime.json'
    bundled=ROOT/'.napcat'
    if (bundled/'node.exe').is_file():
        runtime=bundled
    elif runtime_file.exists():
        runtime=Path(json.loads(runtime_file.read_text(encoding='utf-8'))['runtime_dir'])
    else:
        runtime=bundled
    if not (runtime/'node.exe').is_file() or not (runtime/'index.js').is_file():
        raise FileNotFoundError('后台 QQ 组件尚未安装，请先运行“安装后台组件.cmd”。')
    runtime=runtime.resolve()
    state=folder/'napcat-state'
    config=state/'config'
    config.mkdir(parents=True,exist_ok=True)
    connection=folder/'onebot.json'
    if connection.exists():
        settings=json.loads(connection.read_text(encoding='utf-8'))
    else:
        settings={'api_url':'http://127.0.0.1:18768','event_port':18767,'token':secrets.token_urlsafe(36)}
        connection.write_text(json.dumps(settings,indent=2),encoding='utf-8')
    from onebot_adapter import OneBotClient
    OneBotClient(settings['api_url'],settings['token'])
    parts=urlsplit(settings['api_url'])
    if parts.hostname not in ('127.0.0.1','localhost'):
        raise ValueError('托管后台 QQ 请使用 127.0.0.1 或 localhost。')
    event_port=int(settings['event_port'])
    if not 1<=event_port<=65535:
        raise ValueError('QQ 回调端口无效。')
    webui_file=config/'webui.json'
    if not webui_file.exists():
        webui_file.write_text(json.dumps({'host':'127.0.0.1','port':18769,'token':secrets.token_urlsafe(36),'loginRate':3}),encoding='utf-8')
    onebot={'network':{
        'httpServers':[{'name':'qq-python-local-api','enable':True,'host':'127.0.0.1','port':parts.port,
            'enableCors':False,'enableWebsocket':False,'messagePostFormat':'array','token':settings['token'],'debug':False}],
        'httpClients':[{'name':'qq-python-local-events','enable':True,'url':'http://127.0.0.1:%d/events'%event_port,
            'messagePostFormat':'array','reportSelfMessage':True,'token':settings['token'],'debug':False}],
        'httpSseServers':[],'websocketServers':[],'websocketClients':[],'plugins':[]},
        'musicSignUrl':'','enableLocalFile2Url':False,'parseMultMsg':False}
    (config/'onebot11.json').write_text(json.dumps(onebot,indent=2),encoding='utf-8')
    account_file=folder/'onebot-account.json'
    if account_file.is_file():
        account=str(json.loads(account_file.read_text(encoding='utf-8')).get('user_id',''))
        if account.isdigit() and 5<=len(account)<=16:
            (config/('onebot11_'+account+'.json')).write_text(json.dumps(onebot,indent=2),encoding='utf-8')
    (config/'napcat.json').write_text(json.dumps({'fileLog':False,'consoleLog':True,
        'fileLogLevel':'warn','consoleLogLevel':'warn','packetBackend':'auto','packetServer':''}),encoding='utf-8')
    runtime_file.write_text(json.dumps({'runtime_dir':str(runtime)},indent=2),encoding='utf-8')
    return runtime,state


def ensure_running():
    runtime,state=prepare()
    settings=json.loads((ROOT/'.runtime'/'onebot.json').read_text(encoding='utf-8'))
    digest=hashlib.sha256(json.dumps({k:settings[k] for k in ('api_url','event_port','token')},sort_keys=True).encode()).hexdigest()
    if owned_process_running(runtime,state):
        previous=json.loads((state/'service-pid.json').read_text(encoding='utf-8'))
        if previous.get('connection_digest')==digest:
            return state
        owned_process_running(runtime,state,terminate=True)
    for port in (urlsplit(settings['api_url']).port,18769):
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=0.4):
                raise RuntimeError('后台 QQ 的本机端口已被其他程序占用：'+str(port))
        except OSError:
            pass
    env=os.environ.copy()
    env['NAPCAT_WORKDIR']=str(state)
    env['NAPCAT_DISABLE_MULTI_PROCESS']='1'
    env['NAPCAT_FORCE_NODE_PROCESS']='1'
    args=[str(runtime/'node.exe'),str(runtime/'index.js')]
    account_file=ROOT/'.runtime'/'onebot-account.json'
    if account_file.is_file():
        account=str(json.loads(account_file.read_text(encoding='utf-8')).get('user_id',''))
        if account.isdigit() and 5<=len(account)<=16:
            args+=['-q',account]
    with (state/'background-service.log').open('ab',buffering=0) as output:
        process=subprocess.Popen(args,cwd=runtime,
            env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
    (state/'service-pid.json').write_text(json.dumps({'pid':process.pid,'executable':str(runtime/'node.exe'),
        'connection_digest':digest}),encoding='utf-8')
    return state


def refresh_login_code():
    """User-triggered login refresh; never restart an already logged-in account."""
    from onebot_adapter import OneBotClient
    runtime,state=prepare()
    settings=json.loads((ROOT/'.runtime'/'onebot.json').read_text(encoding='utf-8'))
    try:
        account=OneBotClient(settings['api_url'],settings['token'],timeout=1).call('get_login_info')
        if isinstance(account,dict) and account.get('user_id'):
            return None
    except (OSError,ValueError):
        pass
    owned_process_running(runtime,state,terminate=True)
    stamp=time.time()
    ensure_running()
    image=state/'cache'/'qrcode.png'
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        try:
            account=OneBotClient(settings['api_url'],settings['token'],timeout=.5).call('get_login_info')
            if isinstance(account,dict) and account.get('user_id'):
                return None
        except (OSError,ValueError):
            pass
        if image.is_file() and image.stat().st_mtime>=stamp:
            return image
        time.sleep(.2)
    raise RuntimeError('暂未生成新二维码，请稍后重试“QQ 登录 / 刷新二维码”。')


if __name__=='__main__':
    state=ensure_running()
    print('后台 QQ 已启动。首次登录请查看扫码图片。')
    print('扫码图片位置：'+str(state/'cache'/'qrcode.png'))
