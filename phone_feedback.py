"""Phone display preference. Detailed execution records always stay on the PC."""
import json
import os
from pathlib import Path
import secrets


class Feedback:
    def __init__(self,folder):
        self.path=Path(folder)/'phone-feedback.json'

    @property
    def detailed(self):
        try:return json.loads(self.path.read_text(encoding='utf-8')).get('mode')=='detailed'
        except (OSError,ValueError,AttributeError):return False

    def set(self,mode):
        if mode not in ('brief','detailed'):raise ValueError('请选择简洁或详细消息。')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_name('phone-feedback-'+secrets.token_hex(6)+'.tmp')
        try:
            temp.write_text(json.dumps({'mode':mode}),encoding='utf-8')
            os.replace(temp,self.path)
        finally:temp.unlink(missing_ok=True)
