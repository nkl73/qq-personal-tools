"""Bounded shared files; messages never become executable paths or commands."""
from dataclasses import dataclass
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler,ProxyHandler,Request,build_opener

MAX_FILE=50*1024*1024
MAX_IMAGE=10*1024*1024
MAX_INBOX=500*1024*1024


@dataclass(frozen=True)
class Attachment:
    path: str
    mode: str='auto'
    task_id: str=''


def shared_file(shared,value):
    root=Path(shared).resolve()
    if not isinstance(value,str) or not value.strip() or '\x00' in value:
        raise ValueError('请填写共享目录里的文件名。')
    relative=Path(value.replace('\\','/'))
    if relative.is_absolute() or relative.drive or ':' in value or any(p in ('.','..') or p.startswith('.') for p in relative.parts):
        raise ValueError('只能使用共享目录内的相对文件名。')
    path=(root/relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():raise ValueError('共享目录中找不到这个文件。')
    if path.stat().st_size>MAX_FILE:raise ValueError('文件通道单个文件最多 50 MB。')
    return path


def safe_name(value):
    name=re.split(r'[/\\]',str(value))[-1]
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]','_',name).strip(' .')[:120].rstrip(' .')
    if not name:name='file.bin'
    if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:name='_'+name
    return name


def image_suffix(head):
    if head.startswith(b'\x89PNG\r\n\x1a\n'):return '.png'
    if head.startswith(b'\xff\xd8\xff'):return '.jpg'
    if head.startswith((b'GIF87a',b'GIF89a')):return '.gif'
    if head.startswith(b'RIFF') and head[8:12]==b'WEBP':return '.webp'
    return ''


def copy_bounded(source,destination,stop,expected=None):
    total=0;sha=hashlib.sha256();md5=hashlib.md5()
    try:
        with open(source,'rb') as src,open(destination,'xb') as dst:
            while True:
                if stop.is_set():raise EOFError('文件传输已停止。')
                block=src.read(65536)
                if not block:break
                total+=len(block)
                if total>MAX_FILE:raise ValueError('文件超过 50 MB。')
                sha.update(block);md5.update(block);dst.write(block)
        if expected is not None and total!=expected:raise ValueError('文件大小不匹配，未保存不完整文件。')
        return {'size':total,'sha256':sha.hexdigest(),'md5':md5.hexdigest()}
    except Exception:
        Path(destination).unlink(missing_ok=True)
        raise


def validate_download_url(url):
    parts=urlsplit(url)
    if parts.scheme not in ('https','http') or not parts.hostname or parts.username or parts.password or parts.port not in (None,80,443):
        raise ValueError('后台提供的下载地址无效。')
    host=parts.hostname.lower()
    try:
        address=ipaddress.ip_address(host)
        allowed=address.is_global
    except ValueError:
        allowed=any(host==domain or host.endswith('.'+domain) for domain in
            ('qq.com','qq.com.cn','qpic.cn','qlogo.cn','gtimg.cn','myqcloud.com','qqmail.com','weiyun.com'))
    if not allowed:raise ValueError('仅接收 QQ 文件下载地址，不读取消息里的任意链接。')
    try:addresses=socket.getaddrinfo(host,parts.port or (443 if parts.scheme=='https' else 80),type=socket.SOCK_STREAM)
    except OSError:raise ValueError('QQ 文件下载地址暂时无法解析，请重新发送。') from None
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('不允许从本机或内网地址下载消息附件。')
    return url


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_download_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download(url,destination,stop,expected=None):
    validate_download_url(url)
    opener=build_opener(ProxyHandler({}),SafeRedirect())
    deadline=time.monotonic()+120;total=0
    try:
        with opener.open(Request(url,headers={'User-Agent':'QQ-Personal-Tools/0.4'}),timeout=5) as response,open(destination,'xb') as dst:
            declared=response.headers.get('Content-Length')
            if declared is not None and int(declared)>MAX_FILE:raise ValueError('附件超过 50 MB。')
            while True:
                if stop.is_set():raise EOFError('接收已停止。')
                if time.monotonic()>deadline:raise ValueError('文件接收超时，请重新发送。')
                block=response.read(65536)
                if not block:break
                total+=len(block)
                if total>MAX_FILE:raise ValueError('附件超过 50 MB。')
                dst.write(block)
        if expected is not None and total!=expected:raise ValueError('接收大小不匹配，请重新发送附件。')
        if declared is not None and total!=int(declared):raise ValueError('下载不完整，请重新发送附件。')
        return total
    except Exception:
        Path(destination).unlink(missing_ok=True)
        raise


def cache_roots(state):
    home=Path.home()
    roots=[Path(state)/'napcat-state',home/'Documents'/'Tencent Files']
    for key in ('APPDATA','LOCALAPPDATA'):
        value=os.environ.get(key)
        if value:roots += [Path(value)/'Tencent'/'QQ',Path(value)/'Tencent'/'QQNT']
    return [p.resolve() for p in roots]
