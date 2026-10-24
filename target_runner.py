"""Run the user's unchanged program and forward input/print over a JSON line channel."""
import builtins
import getpass
import io
import json
import os
from pathlib import Path
import runpy
import sys
import traceback


def main():
    script=Path(sys.argv[1]).resolve()
    arguments=sys.argv[2:]
    wire_out=sys.stdout
    wire_in=sys.stdin

    def emit(kind,**fields):
        wire_out.write(json.dumps({'type':kind,**fields},ensure_ascii=True)+'\n')
        wire_out.flush()

    class Output(io.TextIOBase):
        def __init__(self,kind):
            self.kind=kind

        @property
        def encoding(self):
            return 'utf-8'

        def write(self,value):
            if value:
                emit(self.kind,text=str(value))
            return len(value)

        def flush(self):
            wire_out.flush()

        def isatty(self):
            return False

    def receive_input(prompt='', secret=False):
        emit('secret_prompt' if secret else 'prompt',text=str(prompt))
        line=wire_in.readline()
        if not line:
            raise EOFError('QQ input channel closed')
        value=json.loads(line)
        if not isinstance(value,dict) or not isinstance(value.get('text'),str):
            raise ValueError('Invalid input channel message')
        return value['text']

    def remote_input(prompt=''):
        return receive_input(prompt)

    def local_secret(prompt='Password: ',stream=None,**kwargs):
        return receive_input(prompt,secret=True)

    sys.stdout=Output('output')
    sys.stderr=Output('error')
    builtins.input=remote_input
    getpass.getpass=local_secret
    sys.argv=[str(script),*arguments]
    sys.path.insert(0,str(script.parent))
    code=0
    try:
        runpy.run_path(str(script),run_name='__main__')
    except SystemExit as error:
        code=error.code if isinstance(error.code,int) else (0 if error.code is None else 1)
        if code:
            print(str(error),file=sys.stderr)
    except BaseException:
        code=1
        traceback.print_exc()
    finally:
        emit('exit',code=code)


if __name__=='__main__':
    main()
