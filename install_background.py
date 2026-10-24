"""Install the pinned portable QQ runtime beside this bridge, without changing QQ."""
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import sys
from urllib.request import urlopen
import zipfile

ROOT=Path(__file__).resolve().parent
VERSION='4.18.41'
URL='https://github.com/NapNeko/NapCatQQ/releases/download/v'+VERSION+'/NapCat.Shell.Windows.Node.zip'
SHA256='51fbb10e7bcd56b0c2a6de86c37811e7518330e48a137ac689ab1e4d8627d3f0'


def install():
    if sys.platform!='win32' or platform.machine().lower() not in ('amd64','x86_64'):
        raise RuntimeError('此安装包需要 64 位 Windows。')
    destination=ROOT/'.napcat'
    if destination.exists():
        if (destination/'node.exe').is_file() and (destination/'index.js').is_file():
            print('后台组件已安装。')
            return destination
        raise RuntimeError('.napcat 已存在但不完整，请先将该文件夹改名后重试。')
    state=ROOT/'.runtime'
    state.mkdir(exist_ok=True)
    staging=state/('napcat-install-'+secrets.token_hex(8))
    staging.mkdir()
    archive=staging/'runtime.zip'
    payload=staging/'payload'
    print('正在下载后台 QQ 组件 '+VERSION+'（约 124 MB）…',flush=True)
    digest=hashlib.sha256()
    with urlopen(URL,timeout=60) as response,archive.open('wb') as output:
        while True:
            block=response.read(1024*1024)
            if not block:
                break
            digest.update(block)
            output.write(block)
    if digest.hexdigest()!=SHA256:
        raise RuntimeError('下载文件的校验值不匹配，已停止安装。')
    with zipfile.ZipFile(archive) as source:
        root=payload.resolve()
        for item in source.infolist():
            target=(root/item.filename).resolve()
            if not target.is_relative_to(root) or item.filename.startswith(('/','\\')) or ':' in item.filename:
                raise RuntimeError('压缩包含无效路径，已停止安装。')
        source.extractall(payload)
    if not (payload/'node.exe').is_file() or not (payload/'index.js').is_file():
        raise RuntimeError('后台组件文件不完整，已停止安装。')
    os.replace(payload,destination)
    (state/'napcat-runtime.json').write_text(json.dumps({'runtime_dir':str(destination)},indent=2),encoding='utf-8')
    # Only remove the fresh directory created by this installer under its own runtime folder.
    if staging.resolve().parent==state.resolve() and staging.name.startswith('napcat-install-'):
        shutil.rmtree(staging)
    print('后台组件已安装，可启动桥接。')
    return destination


if __name__=='__main__':
    try:
        install()
    except Exception as error:
        print('安装未完成：'+str(error),file=sys.stderr)
        sys.exit(1)
