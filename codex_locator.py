"""Find an installed Codex without depending on the desktop app's PATH."""
import os
from pathlib import Path
import shutil


def resolve_codex(executable=None):
    requested=os.fspath(executable) if executable else 'codex'
    found=shutil.which(requested)
    if found:return found
    # A custom explicit program must never silently select a different program.
    if requested.lower() not in ('codex','codex.exe'):
        raise ValueError('指定的 Codex 程序已不存在，请检查本机安装位置。')
    local=os.environ.get('LOCALAPPDATA')
    if local:
        base=Path(local)/'OpenAI'/'Codex'/'bin';candidates=[]
        try:
            for path in base.glob('*/codex.exe'):
                try:
                    if path.is_file():candidates.append((path.stat().st_mtime_ns,str(path)))
                except OSError:continue
        except OSError:pass
        if candidates:return max(candidates)[1]
    raise ValueError('找不到 Codex。请先安装并登录官方 Codex 桌面版，或安装官方 Codex CLI。已自动检查命令路径和 Windows 桌面版安装目录；无需另外填写 API Key。')
