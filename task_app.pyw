"""A small desktop task catalog; the same tools can be used from a phone."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog,messagebox,scrolledtext,ttk

from bridge_core import BridgeClient,ChatIO
from onebot_adapter import relay_background
from settings import ROOT,Settings,load_connection,save_connection
from task_hub import LABELS,run_hub
from tools import BUILTINS,Catalog,Tool
from file_io import Attachment
from power_control import SystemAwake

KINDS={'普通 Python 任务':'python-task','交互式 Python':'interactive','已安装的命令行程序':'command'}


class TaskApp:
    def __init__(self,window,config):
        self.window=window
        self.config=config
        self.catalog=Catalog()
        self.events=queue.Queue()
        self.worker=None
        self.manager=None
        self.stop_event=threading.Event()
        self.pending_secrets={}
        self.ai_tests=set()
        self.ai_test_threads=[]
        self.secret=tk.StringVar()
        self.secret_job=tk.StringVar()
        self.secret_prompt=tk.StringVar(value='任务请求隐私输入时，在电脑这里填写。')
        self.status=tk.StringVar(value='先点“本机试用”体验工具，再连接手机 QQ。')
        self.keep_awake=tk.BooleanVar(value=self.config.keep_awake)
        window.title('QQ Personal Tools · 个人任务面板')
        window.geometry('960x760')
        frame=ttk.Frame(window,padding=16);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='电脑上的工具，手机上调用 · v0.9.0',font=('Microsoft YaHei UI',16,'bold')).pack(anchor='w')
        ttk.Label(frame,text='计算、文字、文件、后台任务和连续对话；可同时运行两个任务。').pack(anchor='w',pady=(4,10))
        buttons=ttk.Frame(frame);buttons.pack(fill='x')
        self.local_button=ttk.Button(buttons,text='本机试用',command=lambda:self.start(True));self.local_button.pack(side='left')
        self.phone_button=ttk.Button(buttons,text='连接手机',command=lambda:self.start(False));self.phone_button.pack(side='left',padx=8)
        ttk.Button(buttons,text='停止全部',command=self.stop).pack(side='left')
        ttk.Button(buttons,text='QQ 登录',command=self.login).pack(side='left',padx=8)
        ttk.Button(buttons,text='QQ 连接设置',command=self.connection).pack(side='left')
        ttk.Button(buttons,text='共享目录',command=self.open_shared).pack(side='left',padx=8)
        ttk.Button(buttons,text='最小化',command=window.iconify).pack(side='right')
        self.awake_choice=ttk.Checkbutton(frame,text='连接手机期间防止自动睡眠（屏幕仍可关闭；停止后恢复）',variable=self.keep_awake,command=self.save_power_choice)
        self.awake_choice.pack(anchor='w',pady=(6,0))
        ttk.Label(frame,textvariable=self.status,wraplength=900).pack(anchor='w',pady=10)
        notebook=ttk.Notebook(frame);notebook.pack(fill='both',expand=True)
        tools_frame=ttk.Frame(notebook,padding=8);jobs_frame=ttk.Frame(notebook,padding=8)
        notebook.add(tools_frame,text='可用工具');notebook.add(jobs_frame,text='任务与结果')
        self.tools=ttk.Treeview(tools_frame,columns=('id','name','kind','description'),show='headings',height=8)
        for key,name,width in (('id','手机工具 ID',110),('name','工具名称',160),('kind','类型',120),('description','用途',400)):
            self.tools.heading(key,text=name);self.tools.column(key,width=width,stretch=key=='description')
        self.tools.pack(fill='both',expand=True)
        row=ttk.Frame(tools_frame);row.pack(fill='x',pady=8)
        ttk.Label(row,text='本次内容 / 参数').pack(side='left')
        self.text=tk.StringVar();ttk.Entry(row,textvariable=self.text).pack(side='left',fill='x',expand=True,padx=8)
        ttk.Button(row,text='运行选中工具',command=self.run_selected).pack(side='right')
        row=ttk.Frame(tools_frame);row.pack(fill='x')
        ttk.Button(row,text='添加 AI',command=self.edit_ai).pack(side='left',padx=(0,8))
        ttk.Button(row,text='ChatGPT 网页通道',command=self.prepare_chatgpt).pack(side='left',padx=(0,8))
        ttk.Button(row,text='添加本机工具',command=self.edit_tool).pack(side='left')
        ttk.Button(row,text='编辑选中',command=lambda:self.edit_tool(True)).pack(side='left',padx=8)
        ttk.Button(row,text='移除自建工具',command=self.remove_tool).pack(side='left')
        ttk.Label(tools_frame,text='手机示例：/py 菜单   ·   /py 运行 calc 12*(3+4)   ·   /py 运行 demo 10').pack(anchor='w',pady=10)
        self.jobs=ttk.Treeview(jobs_frame,columns=('id','name','state'),show='headings',height=6)
        for key,label,width in (('id','任务编号',150),('name','工具',300),('state','状态',230)):
            self.jobs.heading(key,text=label);self.jobs.column(key,width=width)
        self.jobs.pack(fill='both',expand=True)
        row=ttk.Frame(jobs_frame);row.pack(fill='x',pady=8)
        ttk.Button(row,text='查看选中结果',command=self.view_result).pack(side='left')
        ttk.Button(row,text='取消选中任务',command=self.cancel_selected).pack(side='left',padx=8)
        ttk.Label(row,text='交互任务回答').pack(side='left')
        self.reply=tk.StringVar();ttk.Entry(row,textvariable=self.reply).pack(side='left',fill='x',expand=True,padx=8)
        ttk.Button(row,text='提交回答',command=self.answer_selected).pack(side='right')
        self.log=scrolledtext.ScrolledText(frame,height=6,state='disabled',wrap='word');self.log.pack(fill='x',pady=8)
        secret_frame=ttk.LabelFrame(frame,text='本机隐私输入（不经过 QQ）',padding=8);secret_frame.pack(fill='x')
        ttk.Label(secret_frame,textvariable=self.secret_prompt,wraplength=880).pack(anchor='w')
        row=ttk.Frame(secret_frame);row.pack(fill='x',pady=(6,0))
        self.secret_choice=ttk.Combobox(row,textvariable=self.secret_job,width=14,state='readonly');self.secret_choice.pack(side='left')
        self.secret_choice.bind('<<ComboboxSelected>>',lambda _:self.update_secret_prompt())
        ttk.Entry(row,textvariable=self.secret,show='*').pack(side='left',fill='x',expand=True,padx=8)
        ttk.Button(row,text='提交隐私输入',command=self.submit_secret).pack(side='right')
        self.refresh_tools()
        window.protocol('WM_DELETE_WINDOW',self.close)
        window.after(150,self.drain);window.after(800,self.refresh_jobs)

    def write(self,text):
        self.events.put(('log',str(text)))

    def drain(self):
        while True:
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='manager':self.manager=value
            elif kind=='callback':
                callback,result=value;callback(result)
            elif kind=='secret':
                uid,prompt=value;self.pending_secrets[uid]=prompt
                self.secret_choice['values']=list(self.pending_secrets)
                self.secret_job.set(uid);self.secret.set('');self.update_secret_prompt()
            elif kind=='stopped':
                self.manager=None;self.pending_secrets.clear();self.secret_choice['values']=[];self.secret.set('')
                self.local_button.config(state='normal');self.phone_button.config(state='normal');self.status.set('任务面板已停止。')
                self.awake_choice.config(state='normal')
            elif kind=='qr':
                if value is None:self.status.set('后台 QQ 已登录。')
                else:
                    dialog=tk.Toplevel(self.window);dialog.title('手机 QQ 扫码确认')
                    image=tk.PhotoImage(file=str(value)).zoom(3,3)
                    label=ttk.Label(dialog,image=image,padding=16);label.image=image;label.pack()
                    ttk.Label(dialog,text='普通电脑 QQ 需退出同账号登录；过期时关闭此页后刷新。').pack(padx=12,pady=12)
            else:
                self.log.config(state='normal');self.log.insert('end',datetime.now().strftime('%H:%M:%S')+' '+value+'\n')
                self.log.see('end');self.log.config(state='disabled')
                if '监听已就绪' in value:self.status.set('手机已连接。发送 /py 菜单 查看工具。')
                elif '本机试用已就绪' in value:self.status.set('本机试用中。选择工具并运行，无需登录 QQ。')
        self.window.after(150,self.drain)

    def refresh_tools(self):
        for item in self.tools.get_children():self.tools.delete(item)
        names={'builtin':'内置工具','python-task':'后台任务','interactive':'连续对话','command':'本机工具','ai-chat':'AI 连续对话','codex-chat':'电脑 Codex','web-chat':'ChatGPT 网页'}
        for tool in self.catalog.all():self.tools.insert('','end',iid=tool.id,values=(tool.id,tool.name,names[tool.kind],tool.description))

    def refresh_jobs(self):
        if self.manager:
            try:
                selected=self.jobs.selection()
                rows=self.manager.store.recent(30)
                for item in self.jobs.get_children():self.jobs.delete(item)
                for row in rows:self.jobs.insert('','end',iid=row['id'],values=(row['id'],row['name'],LABELS[row['state']]))
                if selected and self.jobs.exists(selected[0]):self.jobs.selection_set(selected[0])
            except Exception:pass
        self.window.after(800,self.refresh_jobs)

    def require_manager(self):
        if self.manager is None:raise ValueError('先点击“本机试用”或“连接手机”。')
        return self.manager

    def run_selected(self):
        try:
            selected=self.tools.selection()
            if not selected:raise ValueError('请先选择一个工具。')
            uid=self.require_manager().start(selected[0],self.text.get())
            self.write('已创建任务 '+uid)
        except Exception as error:messagebox.showinfo('未运行',str(error))

    def cancel_selected(self):
        try:
            selected=self.jobs.selection()
            if not selected:raise ValueError('请先选择任务。')
            self.write(self.require_manager().cancel(selected[0]))
        except Exception as error:messagebox.showinfo('未取消',str(error))

    def view_result(self):
        try:
            row=self.require_manager().store.get(self.jobs.selection()[0])
            dialog=tk.Toplevel(self.window);dialog.title('任务 '+row['id']);dialog.geometry('760x520')
            text=scrolledtext.ScrolledText(dialog,wrap='word');text.pack(fill='both',expand=True,padx=12,pady=12)
            text.insert('end',row['output'] or '暂时没有输出。');text.config(state='disabled')
        except Exception as error:messagebox.showinfo('未打开',str(error))

    def answer_selected(self):
        try:
            self.require_manager().answer(self.jobs.selection()[0],self.reply.get());self.reply.set('')
        except Exception as error:messagebox.showinfo('未提交',str(error))

    def update_secret_prompt(self):
        self.secret_prompt.set(self.pending_secrets.get(self.secret_job.get(),'请选择等待隐私输入的任务。'))

    def submit_secret(self):
        try:
            uid=self.secret_job.get();self.require_manager().answer(uid,self.secret.get(),secret=True)
            self.secret.set('');self.pending_secrets.pop(uid,None)
            self.secret_choice['values']=list(self.pending_secrets)
            self.secret_job.set(next(iter(self.pending_secrets),''));self.update_secret_prompt()
        except Exception as error:messagebox.showinfo('未提交',str(error))

    def edit_tool(self,editing=False):
        try:
            selected=self.tools.selection()
            tool=self.catalog.get(selected[0]) if editing and selected else Tool('my-tool','我的工具')
            if tool.kind=='ai-chat':self.edit_ai(tool.id);return
            if tool.kind in ('builtin','codex-chat','web-chat'):raise ValueError('内置工具可以直接使用；Codex 沿用已有登录，ChatGPT 网页通道通过浏览器扩展连接。')
        except Exception as error:messagebox.showinfo('工具设置',str(error));return
        dialog=tk.Toplevel(self.window);dialog.title('接入本机工具');dialog.geometry('760x670')
        frame=ttk.Frame(dialog,padding=16);frame.pack(fill='both',expand=True);frame.columnconfigure(1,weight=1)
        variables={key:tk.StringVar(value=str(getattr(tool,key))) for key in ('id','name','description','target','python','cwd','timeout','encoding')}
        rows=[('id','手机工具 ID'),('name','显示名称'),('description','用途说明'),('target','脚本 / 程序路径'),('python','Python 解释器'),('cwd','工作目录（可空）'),('timeout','等待上限（秒）'),('encoding','输出编码（默认 utf-8）')]
        kind=tk.StringVar(value=next(k for k,v in KINDS.items() if v==tool.kind))
        ttk.Label(frame,text='运行方式').grid(row=0,column=0,sticky='w')
        ttk.Combobox(frame,textvariable=kind,values=list(KINDS),state='readonly').grid(row=0,column=1,sticky='ew')
        for row,(key,label) in enumerate(rows,1):
            ttk.Label(frame,text=label).grid(row=row,column=0,sticky='w',padx=(0,12),pady=6)
            ttk.Entry(frame,textvariable=variables[key]).grid(row=row,column=1,sticky='ew')
            if key in ('target','python','cwd'):
                def choose(k=key):
                    value=filedialog.askdirectory(parent=dialog) if k=='cwd' else filedialog.askopenfilename(parent=dialog)
                    if value:variables[k].set(value)
                ttk.Button(frame,text='选择',command=choose).grid(row=row,column=2,padx=(8,0))
        ttk.Label(frame,text='参数每行一个：{text} 手机文字；{file} 共享文件完整路径；{output} 任务输出目录。').grid(row=9,columnspan=3,sticky='w',pady=(12,6))
        args=tk.Text(frame,height=5);args.insert('1.0','\n'.join(tool.arguments));args.grid(row=10,columnspan=3,sticky='ew')
        ttk.Label(frame,text='本机程序仅运行这里预先登记的可执行文件；无需写机器人插件。\n普通 Python 任务直接运行脚本；需要 input/getpass 时选择交互式 Python。').grid(row=11,columnspan=3,sticky='w',pady=12)
        ai_enabled=tk.BooleanVar(value=tool.ai_enabled)
        ttk.Checkbutton(frame,text='允许 AI 调用此工具（普通 Python / 本机命令行；运行权限与手动启动相同）',variable=ai_enabled).grid(row=12,columnspan=3,sticky='w',pady=6)
        def save():
            try:
                values={k:v.get().strip() for k,v in variables.items()};values['timeout']=int(values['timeout'])
                candidate=Tool(**values,kind=KINDS[kind.get()],arguments=args.get('1.0','end-1c').splitlines(),ai_enabled=ai_enabled.get())
                self.catalog.save(candidate);self.refresh_tools();dialog.destroy()
            except Exception as error:messagebox.showerror('未保存',str(error),parent=dialog)
        ttk.Button(frame,text='保存到本机工具菜单',command=save).grid(row=13,columnspan=3)

    def edit_ai(self,identifier=None):
        from ai_setup_dialog import AISetupDialog
        AISetupDialog(self,identifier)

    def remove_tool(self):
        try:self.catalog.remove(self.tools.selection()[0]);self.refresh_tools()
        except Exception as error:messagebox.showinfo('未移除',str(error))

    def prepare_chatgpt(self):
        try:
            from web_chat import prepare_extension
            from settings import STATE
            path=prepare_extension(STATE/'web-chat')
            if self.manager:self.manager.connect_web()
            if os.name=='nt':os.startfile(path)
            messagebox.showinfo('连接 ChatGPT 网页','扩展已生成。\n在 Edge 打开 edge://extensions，启用开发人员模式，选择“加载解压缩的扩展”，选中刚打开的 extension 文件夹。\n打开扩展，点“连接此浏览器”。\n浏览器 ChatGPT 需已登录；手机从菜单选择 ChatGPT 网页。',parent=self.window)
        except Exception as error:messagebox.showerror('未完成',str(error),parent=self.window)

    def open_shared(self):
        self.catalog.shared.mkdir(exist_ok=True)
        if os.name=='nt':os.startfile(self.catalog.shared)
        else:messagebox.showinfo('共享目录',str(self.catalog.shared))

    def login(self):
        def work():
            try:
                from napcat_service import refresh_login_code
                load_connection();self.events.put(('qr',refresh_login_code()))
            except Exception as error:self.write('QQ 登录未完成：'+str(error))
        threading.Thread(target=work,daemon=True).start()

    def connection(self):
        if self.worker and self.worker.is_alive():messagebox.showinfo('先停止面板','请先停止全部任务，再修改连接设置。');return
        dialog=tk.Toplevel(self.window);dialog.title('QQ 连接设置')
        frame=ttk.Frame(dialog,padding=16);frame.pack()
        cfg=load_connection();values={k:tk.StringVar(value=str(cfg[k])) for k in ('api_url','event_port','token')}
        managed=tk.BooleanVar(value=self.config.manage_backend);prefix=tk.StringVar(value=self.config.prefix)
        for row,(key,label) in enumerate((('api_url','本机 OneBot HTTP 地址'),('event_port','事件接收端口'),('token','Token（至少 24 字符）'))):
            ttk.Label(frame,text=label).grid(row=row,column=0,sticky='w',pady=6)
            ttk.Entry(frame,textvariable=values[key],width=50,show='*' if key=='token' else '').grid(row=row,column=1,padx=8)
        ttk.Label(frame,text='消息前缀').grid(row=3,column=0,sticky='w');ttk.Entry(frame,textvariable=prefix).grid(row=3,column=1,sticky='ew',padx=8)
        ttk.Checkbutton(frame,text='由本工具管理便携 QQ 后台（取消后连接已有 OneBot）',variable=managed).grid(row=4,columnspan=2,pady=10)
        def save():
            try:
                data={k:v.get().strip() for k,v in values.items()};data['event_port']=int(data['event_port']);data['prefix']=prefix.get().strip()
                save_connection(data);self.config.prefix=data['prefix'];self.config.manage_backend=managed.get();self.config.save();dialog.destroy()
            except Exception as error:messagebox.showerror('未保存',str(error),parent=dialog)
        ttk.Button(frame,text='保存到本机',command=save).grid(row=5,columnspan=2)

    def start(self,local=False):
        if self.worker and self.worker.is_alive():return
        self.stop_event=threading.Event();self.local_button.config(state='disabled');self.phone_button.config(state='disabled')
        self.awake_choice.config(state='disabled')
        self.status.set('正在启动本机任务面板…' if local else '正在连接手机 QQ…')
        self.worker=threading.Thread(target=self.run,args=(local,self.stop_event),daemon=True);self.worker.start()

    def save_power_choice(self):
        self.config.keep_awake=self.keep_awake.get();self.config.save()

    def run(self,local,stop):
        try:
            with SystemAwake(enabled=not local and self.config.keep_awake,log=self.write), ChatIO(port=0 if local else 18766,console=False) as bridge:
                if not local:
                    cfg=load_connection();cfg['prefix']=self.config.prefix;save_connection(cfg)
                    if self.config.manage_backend:
                        from napcat_service import ensure_running
                        ensure_running()
                notices=queue.Queue()
                def backend():
                    try:run_hub(bridge,self.catalog,notices,stop,self.write,
                        on_secret=lambda uid,prompt:self.events.put(('secret',(uid,prompt))),
                        on_manager=lambda manager:self.events.put(('manager',manager)),prefix=self.config.prefix)
                    except Exception as error:self.write('任务引擎停止：'+str(error));stop.set()
                worker=threading.Thread(target=backend,daemon=True);worker.start()
                try:
                    if local:
                        self.write('本机试用已就绪。选择工具，点击“运行选中工具”；无需 QQ 登录。')
                        while not stop.is_set():
                            try:
                                value=notices.get(timeout=.2)
                                self.write('本机试用附件（不发送 QQ）：'+value.path if isinstance(value,Attachment) else value)
                            except queue.Empty:pass
                    else:relay_background(BridgeClient(bridge.connection_path),stop,self.write,notices=notices,shared_dir=self.catalog.shared)
                finally:
                    stop.set();bridge.stop_event.set();worker.join(timeout=16)
        except Exception as error:self.write('面板停止：'+str(error))
        finally:stop.set();self.events.put(('stopped',''))

    def stop(self):self.stop_event.set()

    def close(self):
        for event in self.ai_tests:event.set()
        self.stop()
        if (self.worker and self.worker.is_alive()) or any(t.is_alive() for t in self.ai_test_threads):self.window.after(100,self.close)
        else:self.window.destroy()


def main():
    parser=argparse.ArgumentParser(description='Phone-controlled personal tools and tasks')
    parser.add_argument('--local',action='store_true',help='只启动本机试用，不连接 QQ')
    parser.add_argument('--run',action='store_true',help='启动后连接 QQ')
    args=parser.parse_args()
    try:config=Settings.load()
    except (OSError,ValueError,TypeError):config=Settings()
    window=tk.Tk();app=TaskApp(window,config)
    if args.local or args.run:window.after(300,lambda:app.start(args.local))
    window.mainloop()


if __name__=='__main__':main()
