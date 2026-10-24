"""A short AI setup page: provider, credential, model, connect."""
import os
from dataclasses import replace
import threading
import tkinter as tk
from tkinter import ttk

from ai_service import AIProfile,call,list_models
from ai_setup import PROVIDERS,credential_for,profile_from_form,provider_for,suggested_id


class AISetupDialog:
    def __init__(self,app,identifier=None):
        self.app=app;self.identifier=identifier
        self.old=app.catalog.ai_store.get(identifier) if identifier else None
        self.uid=identifier or suggested_id(app.catalog)
        initial=self.old or AIProfile()
        provider=provider_for(initial.base_url) if self.old else PROVIDERS[0]
        self.window=tk.Toplevel(app.window);self.window.title('添加 AI · 简单设置')
        self.frame=ttk.Frame(self.window,padding=16);self.frame.pack(fill='both',expand=True)
        self.controls=[];self.operation=None;self.expanded=False
        ttk.Label(self.frame,text='选平台、填密钥、选模型，就可以开始。',font=('Microsoft YaHei UI',13,'bold')).pack(anchor='w',pady=(0,12))
        self.provider=tk.StringVar(value=provider.label)
        row=self.row('AI 平台')
        self.platform=ttk.Combobox(row,textvariable=self.provider,values=[p.label for p in PROVIDERS],state='readonly')
        self.platform.pack(side='left',fill='x',expand=True);self.controls.append((self.platform,'readonly'))
        self.platform.bind('<<ComboboxSelected>>',self.changed_provider)
        self.hint=tk.StringVar(value=provider.hint)
        ttk.Label(self.frame,textvariable=self.hint,wraplength=600).pack(anchor='w',pady=(2,8))
        self.custom=ttk.Frame(self.frame)
        ttk.Label(self.custom,text='服务地址',width=12).pack(side='left')
        self.base=tk.StringVar(value=initial.base_url if self.old else provider.base_url)
        self.base_entry=ttk.Entry(self.custom,textvariable=self.base);self.base_entry.pack(side='left',fill='x',expand=True)
        self.controls.append((self.base_entry,'normal'))
        self.key=tk.StringVar()
        self.keyrow=self.row('密钥 API Key')
        entry=ttk.Entry(self.keyrow,textvariable=self.key,show='*');entry.pack(side='left',fill='x',expand=True);self.controls.append((entry,'normal'))
        saved=bool(identifier and app.catalog.ai_store.key(identifier))
        ttk.Label(self.frame,text='已有密钥，留空沿用；换平台时须重新填写。' if saved else '只需粘贴所选平台的 API 密钥，不要发到 QQ。',wraplength=600).pack(anchor='w',pady=(0,8))
        self.model=tk.StringVar(value=initial.model)
        row=self.row('使用的模型')
        self.models=ttk.Combobox(row,textvariable=self.model)
        self.models.pack(side='left',fill='x',expand=True);self.controls.append((self.models,'normal'))
        self.fetch=ttk.Button(row,text='读取模型',command=self.discover);self.fetch.pack(side='right',padx=(8,0))
        self.remember=tk.BooleanVar(value=bool(identifier and app.catalog.ai_store.remembered(identifier)))
        remember=ttk.Checkbutton(self.frame,text='在这台电脑加密记住密钥，下次不用重新填',variable=self.remember)
        remember.pack(anchor='w',pady=(6,4))
        if os.name=='nt':self.controls.append((remember,'normal'))
        else:remember.config(state='disabled')
        self.allow_tools=tk.BooleanVar(value=initial.allow_tools)
        tools=ttk.Checkbutton(self.frame,text='允许 AI 使用共享文件和电脑已授权工具',variable=self.allow_tools)
        tools.pack(anchor='w',pady=(4,0));self.controls.append((tools,'normal'))
        ttk.Label(self.frame,text='开启后，读取的文件内容和工具结果会发送给所选模型；生成文件会回传 QQ。',wraplength=600).pack(anchor='w',pady=(0,4))
        self.more=ttk.Button(self.frame,text='更多设置 ▸',command=self.toggle);self.more.pack(anchor='w',pady=(4,4))
        self.advanced=ttk.Frame(self.frame)
        self.name=tk.StringVar(value=initial.name if self.old else '')
        self.timeout=tk.StringVar(value=str(initial.timeout))
        for label,var in (('显示名称（可空）',self.name),('等待上限（秒）',self.timeout)):
            row=ttk.Frame(self.advanced);row.pack(fill='x',pady=3)
            ttk.Label(row,text=label,width=18).pack(side='left')
            entry=ttk.Entry(row,textvariable=var);entry.pack(side='left',fill='x',expand=True);self.controls.append((entry,'normal'))
        ttk.Label(self.advanced,text='固定要求（可空）').pack(anchor='w')
        self.prompt=tk.Text(self.advanced,height=3,wrap='word');self.prompt.insert('1.0',initial.system_prompt)
        self.prompt.pack(fill='x');self.controls.append((self.prompt,'normal'))
        ttk.Label(self.advanced,text='手机工具编号已自动设置：'+self.uid).pack(anchor='w',pady=3)
        self.status=tk.StringVar(value='点下方按钮会测试一条短问题，成功后自动加入 QQ 菜单。')
        self.status_label=ttk.Label(self.frame,textvariable=self.status,wraplength=600)
        self.status_label.pack(anchor='w',pady=(10,8))
        bottom=ttk.Frame(self.frame);bottom.pack(fill='x')
        ttk.Button(bottom,text='取消',command=self.close).pack(side='left')
        self.submit=ttk.Button(bottom,text='验证并保存' if identifier else '连接并添加',command=self.connect);self.submit.pack(side='right')
        ttk.Label(self.frame,text='密钥仅在电脑配置。问题会发送给所选 AI；测试按平台规则可能计费。\n未勾选“记住”时，重开面板需要补填密钥。',wraplength=600).pack(anchor='w',pady=(10,0))
        self.layout();self.window.protocol('WM_DELETE_WINDOW',self.close)

    def row(self,label):
        row=ttk.Frame(self.frame);row.pack(fill='x',pady=4)
        ttk.Label(row,text=label,width=12).pack(side='left')
        return row

    def selected(self):return next(p for p in PROVIDERS if p.label==self.provider.get())

    def layout(self):
        custom=not self.selected().base_url
        if custom:self.custom.pack(before=self.keyrow,fill='x',pady=4)
        else:self.custom.pack_forget()
        self.fetch.config(state='normal' if self.selected().can_list and self.operation is None else 'disabled')
        self.window.geometry('650x%d'%(520+(55 if custom else 0)+(190 if self.expanded else 0)))

    def changed_provider(self,_=None):
        provider=self.selected();self.hint.set(provider.hint)
        self.base.set(provider.base_url);self.key.set('');self.model.set('');self.models['values']=[]
        self.status.set('平台已切换，请填写此平台自己的密钥和模型。');self.layout()

    def toggle(self):
        self.expanded=not self.expanded
        if self.expanded:self.advanced.pack(before=self.status_label,fill='x',pady=4)
        else:self.advanced.pack_forget()
        self.more.config(text='收起设置 ▾' if self.expanded else '更多设置 ▸');self.layout()

    def candidate(self,discover=False):
        profile=profile_from_form(self.selected(),'model-discovery' if discover else self.model.get(),self.base.get(),
            self.name.get(),self.prompt.get('1.0','end-1c'),self.timeout.get(),self.uid)
        profile=replace(profile,allow_tools=self.allow_tools.get()).validate()
        if self.identifier is None and any(t.id==self.uid for t in self.app.catalog.all()):
            raise ValueError('另一页已添加了这个 AI，请重新打开“添加 AI”。')
        old_key=self.app.catalog.ai_store.key(self.identifier) if self.identifier else ''
        return profile,credential_for(profile,self.key.get(),self.old,old_key)

    def busy(self,value):
        for widget,state in self.controls:widget.config(state='disabled' if value else state)
        self.more.config(state='disabled' if value else 'normal')
        self.submit.config(state='disabled' if value else 'normal')
        self.fetch.config(state='disabled' if value or not self.selected().can_list else 'normal')

    def launch(self,function,done):
        stop=threading.Event();self.operation=stop;self.app.ai_tests.add(stop);self.busy(True)
        def finish(result):
            self.app.ai_tests.discard(stop)
            if not self.window.winfo_exists():return
            self.operation=None;self.busy(False);done(result)
        def work():
            try:result=(True,function(stop))
            except EOFError:return
            except Exception as error:result=(False,str(error))
            self.app.events.put(('callback',(finish,result)))
        worker=threading.Thread(target=work,daemon=True)
        self.app.ai_test_threads=[t for t in self.app.ai_test_threads if t.is_alive()]+[worker];worker.start()

    def discover(self):
        try:profile,key=self.candidate(True)
        except Exception as error:self.status.set(str(error));return
        self.status.set('正在读取模型列表…')
        def done(result):
            ok,value=result
            if not ok:self.status.set('未能读取列表，可直接填写模型名称。'+value);return
            self.models['values']=value
            if len(value)==1:self.model.set(value[0])
            self.status.set('读到 %d 个模型，请选择支持文字对话的一个；仍需连接测试。'%len(value))
        self.launch(lambda stop:list_models(profile.base_url,key,stop),done)

    def connect(self):
        try:profile,key=self.candidate()
        except Exception as error:self.status.set(str(error));return
        remember=self.remember.get();self.status.set('正在连接测试，成功后自动添加…')
        def done(result):
            ok,value=result
            if not ok:self.status.set('尚未添加：'+value);return
            try:
                if self.identifier is None and any(t.id==self.uid for t in self.app.catalog.all()):
                    raise ValueError('工具编号已被另一页使用，请重新打开添加页。')
                self.app.catalog.save_ai(profile,key,remember);self.app.refresh_tools()
                self.app.write('AI 连接成功，已加入菜单。手机发送：'+self.app.config.prefix+' 运行 '+profile.id+' 你的问题')
                self.close()
            except Exception as error:self.status.set('测试成功，但未保存：'+str(error))
        def test(stop):
            if profile.allow_tools:
                from ai_tools import DEFINITIONS
                return call(profile,key,[{'role':'user','content':'这是连通测试，请只回复：连接成功。不要调用工具。'}],stop,
                    operation='agent',tools=DEFINITIONS)
            return call(profile,key,[{'role':'user','content':'请只回复：连接成功'}],stop)
        self.launch(test,done)

    def close(self):
        if self.operation:self.operation.set();self.app.ai_tests.discard(self.operation)
        self.key.set('');self.window.destroy()
