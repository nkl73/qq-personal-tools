"""Provider-independent desktop control panel for a trusted Python script."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog,messagebox,scrolledtext,ttk

from bridge_core import BridgeClient,ChatIO
from onebot_adapter import relay_background
from script_backend import run_backend
from settings import ROOT,STATE,Settings,load_connection,save_connection


class App:
    def __init__(self,window,settings):
        self.window=window
        window.title('QQ Python Bridge · 通用预览版')
        window.geometry('850x760')
        self.events=queue.Queue()
        self.stop_event=threading.Event()
        self.worker=None
        self.secret_inputs=queue.Queue()
        self.secret_needed=False
        self.settings=settings
        self.target=tk.StringVar(value=settings.target)
        self.python=tk.StringVar(value=settings.python_executable)
        self.cwd=tk.StringVar(value=settings.working_directory)
        self.prefix=tk.StringVar(value=settings.prefix)
        self.timeout=tk.StringVar(value=str(settings.timeout))
        self.managed=tk.BooleanVar(value=settings.manage_backend)
        self.secret=tk.StringVar()
        self.secret_prompt=tk.StringVar(value='目标程序使用 getpass() 时，在此输入隐私内容。')
        self.status=tk.StringVar(value='未启动。默认示例只返回普通文字，不调用模型。')
        frame=ttk.Frame(window,padding=16)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='手机 QQ ↔ 本机 Python',font=('Microsoft YaHei UI',15,'bold')).pack(anchor='w')
        ttk.Label(frame,text='选择你信任的脚本；QQ 消息只作为输入内容，不作为终端命令执行。').pack(anchor='w',pady=(5,12))
        form=ttk.Frame(frame)
        form.pack(fill='x')
        form.columnconfigure(1,weight=1)
        for row,(label,var,kind) in enumerate((('Python 脚本',self.target,'script'),
                ('Python 解释器 / 虚拟环境',self.python,'python'),('工作目录（可留空）',self.cwd,'directory'))):
            ttk.Label(form,text=label).grid(row=row,column=0,sticky='w',padx=(0,12),pady=4)
            ttk.Entry(form,textvariable=var).grid(row=row,column=1,sticky='ew',pady=4)
            ttk.Button(form,text='选择',command=lambda v=var,k=kind:self.choose(v,k)).grid(row=row,column=2,padx=(8,0))
        ttk.Label(form,text='程序参数（每行一项）').grid(row=3,column=0,sticky='nw',padx=(0,12),pady=4)
        self.arguments=tk.Text(form,height=3,wrap='none')
        self.arguments.insert('1.0','\n'.join(settings.arguments))
        self.arguments.grid(row=3,column=1,columnspan=2,sticky='ew')
        row=ttk.Frame(frame)
        row.pack(fill='x',pady=8)
        ttk.Label(row,text='消息前缀').pack(side='left')
        ttk.Entry(row,textvariable=self.prefix,width=12).pack(side='left',padx=(8,20))
        ttk.Label(row,text='单轮等待（秒）').pack(side='left')
        ttk.Entry(row,textvariable=self.timeout,width=9).pack(side='left',padx=8)
        ttk.Checkbutton(frame,text='由桥接启动便携后台 QQ（首次需安装组件；取消后连接现有 OneBot）',variable=self.managed).pack(anchor='w')
        controls=ttk.Frame(frame)
        controls.pack(fill='x',pady=10)
        self.start_button=ttk.Button(controls,text='启动脚本桥接',command=self.start)
        self.start_button.pack(side='left')
        ttk.Button(controls,text='停止',command=self.stop).pack(side='left',padx=8)
        ttk.Button(controls,text='QQ 登录 / 刷新二维码',command=self.open_login).pack(side='left')
        ttk.Button(controls,text='QQ 连接设置',command=self.connection_dialog).pack(side='left',padx=8)
        ttk.Button(controls,text='最小化',command=window.iconify).pack(side='right')
        secret_frame=ttk.LabelFrame(frame,text='仅在本机填写的隐私输入',padding=8)
        secret_frame.pack(fill='x')
        ttk.Label(secret_frame,textvariable=self.secret_prompt,wraplength=780).pack(anchor='w')
        secret_row=ttk.Frame(secret_frame)
        secret_row.pack(fill='x',pady=(6,0))
        ttk.Entry(secret_row,textvariable=self.secret,show='*').pack(side='left',fill='x',expand=True)
        ttk.Button(secret_row,text='提交当前隐私输入',command=self.submit_secret).pack(side='right',padx=(8,0))
        ttk.Label(frame,textvariable=self.status,wraplength=800).pack(anchor='w',pady=10)
        self.output=scrolledtext.ScrolledText(frame,height=10,wrap='word',state='disabled')
        self.output.pack(fill='both',expand=True)
        window.protocol('WM_DELETE_WINDOW',self.close)
        window.after(150,self.drain)

    def choose(self,var,kind):
        value=filedialog.askdirectory() if kind=='directory' else filedialog.askopenfilename(
            filetypes=[('Python 脚本','*.py'),('所有文件','*.*')] if kind=='script' else [('Python 解释器','*.exe'),('所有文件','*.*')])
        if value:
            var.set(value)

    def connection_dialog(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo('先停止桥接','请先停止桥接，再修改连接设置。')
            return
        cfg=load_connection()
        dialog=tk.Toplevel(self.window)
        dialog.title('本机 QQ 连接设置')
        frame=ttk.Frame(dialog,padding=16)
        frame.pack()
        values={key:tk.StringVar(value=str(cfg[key])) for key in ('api_url','event_port','token')}
        for row,(key,label) in enumerate((('api_url','OneBot HTTP 地址'),('event_port','消息回调本机端口'),('token','OneBot Token'))):
            ttk.Label(frame,text=label).grid(row=row,column=0,sticky='w',pady=5)
            ttk.Entry(frame,textvariable=values[key],width=55,show='*' if key=='token' else '').grid(row=row,column=1,padx=(10,0),pady=5)
        ttk.Label(frame,text='现有 OneBot 的 HTTP 客户端回调地址设为 http://127.0.0.1:端口/events，\n开启自身消息上报，并使用同一个 Token。Token 至少 24 字符。').grid(row=3,columnspan=2,pady=10)
        def save():
            try:
                data={key:var.get().strip() for key,var in values.items()}
                data['event_port']=int(data['event_port'])
                data['prefix']=self.prefix.get().strip()
                save_connection(data)
            except Exception as error:
                messagebox.showerror('设置未保存',str(error),parent=dialog)
                return
            dialog.destroy()
        ttk.Button(frame,text='保存到本机',command=save).grid(row=4,columnspan=2)

    def log(self,message):
        # Diagnostics never contain target output, received text or local secret values.
        self.events.put(('log',datetime.now().strftime('%H:%M:%S')+' '+str(message)))

    def drain(self):
        while True:
            try:
                kind,value=self.events.get_nowait()
            except queue.Empty:
                break
            if kind=='secret':
                self.secret.set('')
                self.secret_needed=True
                self.secret_prompt.set(value or '请输入目标程序当前要求的隐私内容。')
                self.status.set('等待本机隐私输入；请从任务栏打开此窗口填写。')
            elif kind=='qr':
                if value is None:
                    self.status.set('后台 QQ 已登录，无需重新扫码。')
                else:
                    dialog=tk.Toplevel(self.window)
                    dialog.title('请用手机 QQ 扫码确认')
                    pic=tk.PhotoImage(file=str(value)).zoom(3,3)
                    label=ttk.Label(dialog,image=pic,padding=16)
                    label.image=pic
                    label.pack()
                    ttk.Label(dialog,text='普通电脑 QQ 需退出登录。过期时关闭此页并刷新。').pack(padx=12,pady=12)
            elif kind=='stopped':
                self.secret_needed=False
                self.secret.set('')
                self.start_button.config(state='normal')
                self.status.set('桥接已停止。')
            else:
                self.output.config(state='normal')
                self.output.insert('end',value+'\n')
                self.output.see('end')
                self.output.config(state='disabled')
                if '监听已就绪' in value:
                    self.status.set('后台监听已就绪。等待手机输入。')
        self.window.after(150,self.drain)

    def submit_secret(self):
        if not self.secret_needed:
            self.status.set('目标程序当前没有请求隐私输入。')
            return
        self.secret_inputs.put(self.secret.get())
        self.secret.set('')
        self.secret_needed=False
        self.status.set('当前隐私输入已通过本机通道提交。')

    def open_login(self):
        if not self.managed.get():
            messagebox.showinfo('现有 OneBot 模式','请在现有 QQ 后台组件中完成登录。')
            return
        def prepare():
            try:
                load_connection()
                from napcat_service import refresh_login_code
                self.events.put(('qr',refresh_login_code()))
            except Exception as error:
                self.log('登录准备未完成：'+str(error))
        threading.Thread(target=prepare,daemon=True).start()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            cfg=Settings(target=self.target.get().strip().strip('"'),python_executable=self.python.get().strip().strip('"'),
                working_directory=self.cwd.get().strip().strip('"'),arguments=self.arguments.get('1.0','end-1c').splitlines(),
                prefix=self.prefix.get().strip(),timeout=int(self.timeout.get()),manage_backend=self.managed.get()).validate()
            if not Path(cfg.target).is_file():
                raise ValueError('请选择存在的 Python 脚本。')
            cfg.save()
            connection=load_connection()
            connection['prefix']=cfg.prefix
            save_connection(connection)
        except Exception as error:
            messagebox.showerror('无法启动',str(error))
            return
        self.settings=cfg
        self.stop_event=threading.Event()
        self.secret_inputs=queue.Queue()
        self.start_button.config(state='disabled')
        self.status.set('正在启动后台桥接…')
        self.worker=threading.Thread(target=self.run,args=(cfg,self.stop_event,self.secret_inputs),daemon=True)
        self.worker.start()

    def run(self,cfg,stop,secret_inputs):
        try:
            with ChatIO(console=False) as bridge:
                if cfg.manage_backend:
                    from napcat_service import ensure_running
                    ensure_running()
                notices=queue.Queue()
                def local_secret(prompt):
                    self.events.put(('secret',prompt))
                    notices.put('目标程序正在等待本机隐私输入，请在电脑桥接窗口填写。不要通过 QQ 发送。')
                    while not stop.is_set():
                        try:
                            return secret_inputs.get(timeout=.2)
                        except queue.Empty:
                            pass
                    raise EOFError('桥接已停止')
                worker=threading.Thread(target=run_backend,args=(bridge,cfg.target,notices,stop,self.log),
                    kwargs={'secret_provider':local_secret,'prefix':cfg.prefix,'python_executable':cfg.python_executable,
                        'arguments':cfg.arguments,'cwd':cfg.working_directory or None,'timeout':cfg.timeout},daemon=True)
                worker.start()
                try:
                    relay_background(BridgeClient(bridge.connection_path),stop,self.log,notices=notices)
                finally:
                    stop.set()
                    bridge.stop_event.set()
                    worker.join(timeout=6)
        except Exception as error:
            self.log('桥接停止：'+str(error))
        finally:
            stop.set()
            self.events.put(('stopped',''))

    def stop(self):
        self.stop_event.set()

    def close(self):
        self.stop()
        if self.worker and self.worker.is_alive():
            self.window.after(100,self.close)
        else:
            self.window.destroy()


def main():
    parser=argparse.ArgumentParser(description='QQ ↔ interactive Python scripts (preview)')
    parser.add_argument('--run',action='store_true',help='打开面板后启动已选脚本')
    parser.add_argument('--target',help='Python 脚本路径')
    parser.add_argument('--python',dest='python_executable',help='目标脚本的 Python 解释器')
    parser.add_argument('--external-onebot',action='store_true',help='使用已运行的本机 OneBot')
    args=parser.parse_args()
    try:
        cfg=Settings.load()
    except (OSError,ValueError,TypeError):
        cfg=Settings()
    if args.target:
        cfg.target=args.target
    if args.python_executable:
        cfg.python_executable=args.python_executable
    if args.external_onebot:
        cfg.manage_backend=False
    window=tk.Tk()
    app=App(window,cfg)
    if args.run:
        window.after(300,app.start)
    window.mainloop()


if __name__=='__main__':
    main()
