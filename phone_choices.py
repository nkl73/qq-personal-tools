"""Numbered phone navigation; selections refer to snapshots, never list offsets."""
import time

from file_io import shared_file


class PhoneChoices:
    def __init__(self,dispatcher):
        self.dispatcher=dispatcher;self.manager=dispatcher.manager;self.active=None

    def clear(self):self.active=None

    def show(self,title,entries,note='',back=None,page=0):
        entries=list(entries);pages=max(1,(len(entries)+5)//6);page=min(max(page,0),pages-1)
        current=entries[page*6:page*6+6];actions={str(i):action for i,(_,action) in enumerate(current,1)}
        lines=['【'+title+(' %d/%d'%(page+1,pages) if pages>1 else '')+'】']
        lines += ['%d. %s'%(i,' '.join(str(label).split())[:110]) for i,(label,_) in enumerate(current,1)]
        if page:actions['7']=lambda:self.show(title,entries,note,back,page-1);lines.append('7. 上一页')
        if page+1<pages:actions['8']=lambda:self.show(title,entries,note,back,page+1);lines.append('8. 下一页')
        actions['0']=back or self.home;lines.append('0. 返回')
        if note:lines.append(note)
        lines.append('发送 '+self.dispatcher.prefix+' 序号 选择，例如 '+self.dispatcher.prefix+' 1。选择有效期 5 分钟。')
        self.active={'actions':actions,'created':time.monotonic(),'title':title}
        return '\n'.join(lines)

    def ask(self,title,callback,back):
        self.active={'input':callback,'created':time.monotonic(),'title':title,'back':back}
        return '【'+title+'】\n请发送 '+self.dispatcher.prefix+' 内容。发送 '+self.dispatcher.prefix+' 返回 放弃本次输入。'

    def consume(self,text):
        state=self.active
        if state is None:return None
        if time.monotonic()-state['created']>300:
            self.clear();return '刚才的选择已过期，没有执行。\n'+self.home()
        if text in ('返回','返回菜单'):
            self.clear();return state.get('back',self.home)()
        if 'input' in state:
            self.clear();return state['input'](text)
        if not text.isdigit():return '当前正在选择“'+state['title']+'”，请回复显示的序号；返回首页发送 '+self.dispatcher.prefix+' 菜单。'
        action=state['actions'].get(text)
        if action is None:return '没有这个选项，请回复当前菜单显示的序号；返回发送 '+self.dispatcher.prefix+' 0。'
        self.clear();return action()

    def home(self):
        return self.show('手机使用菜单',[
            ('与 AI 对话',self.ai),('继续电脑 Codex',self.codex),
            ('运行电脑工具',self.tools),('选择文件进行处理或取回',self.files),
            ('查看任务、结果或继续对话',self.tasks),('使用说明与支持格式',self.help),
        ],'只需按序号选择；工作要求仍用文字描述。电脑须保持开机联网。',back=self.resume_current)

    def start(self,identifier,value=''):
        return self.dispatcher._handle('运行 '+identifier+(' '+value if value else ''))

    def codex(self):
        return self.show('Codex 对话',[
            ('继续默认或已选的会话',lambda:self.dispatcher._handle('codex')),
            ('选择其他已有会话',lambda:self.start('codex','@list')),
            ('新建一个独立对话',lambda:self.start('codex','@new')),
        ],'桌面应用可以保持打开。选择已有会话后可建立保留上下文的手机分支；独立新对话不继承旧记录。')

    def ai(self,question=None):
        if question is None:
            return self.show('选择 AI 通道',[
                ('ChatGPT 网页（浏览器登录）',lambda:self.start('chatgpt')),
                ('通过 API 新建 AI 对话',self.api_profiles),
                ('继续本次打开的 AI 对话',self.ai_tasks),
            ],'ChatGPT 网页与 API 是独立通道，各自保留上下文。')
        return self.api_profiles(question)

    def api_profiles(self,question=None):
        entries=[(t.name,lambda uid=t.id:self.start(uid,question or ''))
            for t in self.manager.catalog.all() if t.kind=='ai-chat']
        return self.show('选择 API 模型并新建对话',entries,'选择后开启独立会话。没有配置时，请先在电脑“添加 AI”。',back=self.ai)

    def ai_tasks(self):
        from task_hub import FINAL,LABELS
        rows=[task for task in self.manager.tasks.values() if task.tool.kind in ('ai-chat','web-chat') and task.state not in FINAL]
        return self.show('继续 AI 对话',[(t.tool.name+' · '+LABELS[t.state]+' · '+t.id,lambda uid=t.id:self.resume_current(uid)) for t in rows],back=self.ai)

    def tools(self):
        entries=[(t.name+' · '+t.id,lambda uid=t.id:self.tool(uid))
            for t in self.manager.catalog.all() if t.kind not in ('ai-chat','codex-chat','web-chat')]
        return self.show('选择要运行的工具',entries,'这里只列出电脑上已接入的工具。')

    def tool(self,identifier):
        tool=self.manager.catalog.get(identifier)
        if tool.kind=='builtin' and identifier=='files':return self.files()
        if identifier in ('inspect','fileinfo') or any('{file}' in arg for arg in tool.arguments):
            return self.files(lambda path:self.start(identifier,path),self.tools)
        if identifier in ('calc','text') or any('{text}' in arg for arg in tool.arguments):
            return self.ask(tool.name+'｜填写本次内容',lambda value:self.start(identifier,value),self.tools)
        return self.start(identifier)

    def files(self,callback=None,back=None):
        root=self.manager.catalog.shared;rows=[]
        if root.is_dir():
            for path in root.rglob('*'):
                try:
                    relative=path.relative_to(root).as_posix()
                    if any(p.startswith('.') for p in path.relative_to(root).parts):continue
                    checked=shared_file(root,relative)
                    rows.append((checked.stat().st_mtime,relative))
                except (OSError,ValueError):continue
                if len(rows)>=100:break
        rows.sort(reverse=True)
        entries=[(relative,lambda path=relative:(callback(path) if callback else self.file_actions(path))) for _,relative in rows]
        return self.show('选择共享文件',entries,'最多显示 100 个；手机直接发送附件，收到保存提示后再打开本菜单。',back=back)

    def file_actions(self,path):
        shared_file(self.manager.catalog.shared,path)
        return self.show('文件操作',[
            ('查看文件概览',lambda:self.start('inspect',path)),
            ('生成并回传 TXT 概览报告',lambda:self.start('fileinfo',path)),
            ('选择 AI 处理这个文件',lambda:self.file_ai(path)),
            ('把原文件发回手机',lambda:self.dispatcher._handle('发文件 '+path)),
        ],'文件：'+path,back=self.files)

    def file_ai(self,path):
        entries=[]
        for tool in self.manager.catalog.all():
            if tool.kind=='ai-chat' and self.manager.catalog.ai_store.get(tool.target).allow_tools:
                entries.append((tool.name,lambda uid=tool.id:self.ask('描述希望怎样处理文件',
                    lambda value:self.start(uid,'请读取共享文件 '+repr(path)+'，然后按以下要求处理：\n'+value),lambda:self.file_actions(path))))
        return self.show('选择能读取文件的 AI',entries,'仅列出电脑已开启文件和工具权限的 AI。',back=lambda:self.file_actions(path))

    def tasks(self):
        from task_hub import LABELS
        entries=[(row['name']+' · '+LABELS[row['state']]+' · '+row['id'],lambda uid=row['id']:self.task_actions(uid))
            for row in self.manager.store.recent(30)]
        return self.show('选择任务',entries,'选好后可看结果、继续对话或停止；无需复制任务编号。')

    def task_actions(self,uid):
        from task_hub import FINAL,LABELS
        row=self.manager.store.get(uid);task=self.manager.tasks.get(uid)
        entries=[('查看文字结果',lambda:self.dispatcher._handle('结果 '+uid)),
            ('查看执行详情',lambda:self.dispatcher._handle('详情 '+uid))]
        if task is not None and row['state'] not in FINAL:
            if task.tool.kind in ('interactive','ai-chat','codex-chat','web-chat'):entries.append(('选中并继续回答这个任务',lambda:self.resume_current(uid)))
            if task.tool.kind in ('ai-chat','codex-chat','web-chat'):
                entries.append(('结束这次会话',lambda:self.dispatcher._handle('结束 '+uid)))
            entries.append(('停止这个任务',lambda:self.show('确认停止任务',[
                ('确认停止 '+row['name'],lambda:self.dispatcher._handle('取消 '+uid))
            ],'已发生的改动不会自动撤销。',back=lambda:self.task_actions(uid))))
        return self.show('任务操作',entries,row['name']+' · '+LABELS[row['state']],back=self.tasks)

    def resume_current(self,uid=None):
        from task_hub import FINAL
        uid=uid or self.dispatcher.selected;task=self.manager.tasks.get(uid) if uid else None
        if task is None or task.state in FINAL:return '当前没有可继续的任务。发送 '+self.dispatcher.prefix+' 菜单 选择功能。'
        self.dispatcher.selected=uid
        if task.state=='waiting_secret':return '这个任务等待电脑本机隐私输入，请在电脑填写，不要发送到 QQ。'
        if task.state!='waiting_input':return '这个任务正在处理，请等待新的问题或结果。'
        if not self.manager.feedback.detailed:return '已选中 '+task.tool.name+'，用 '+self.dispatcher.prefix+' 内容 继续。'
        return '已返回 '+task.tool.name+' 的对话。\n最近提示：\n'+task.output[-2500:]

    def feedback(self):
        def choose(mode):
            self.manager.feedback.set(mode)
            return '已切换为'+('详细消息，会显示进度和执行记录。' if mode=='detailed' else '简洁消息，只发结果、必要提问和错误。')
        return self.show('QQ 消息显示',[
            ('简洁（默认）：结果、必要提问和错误',lambda:choose('brief')),
            ('详细：同时查看任务与工具进度',lambda:choose('detailed')),
        ],'当前：'+('详细' if self.manager.feedback.detailed else '简洁')+'。完整执行记录始终保存在电脑，可在任务菜单按需查看。',back=self.help)

    def help(self):
        import mobile_menu
        entries=[('QQ 消息显示：简洁 / 详细',self.feedback)]
        entries += [(name,lambda topic=name:mobile_menu.section(self.manager.catalog,topic,self.dispatcher.prefix))
            for name in ('任务','对话','文件','AI','问题')]
        entries.append(('支持格式和组件状态',lambda:self.dispatcher._handle('格式')))
        return self.show('使用说明',entries)
