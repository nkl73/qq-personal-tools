"""Phone-first, truthful help generated from the tools actually registered here."""
from pathlib import Path
from settings import ROOT

KINDS={'builtin':'内置功能','python-task':'后台任务','interactive':'连续对话','command':'本机工具','ai-chat':'AI 连续对话','codex-chat':'电脑 Codex','web-chat':'ChatGPT 网页'}
SAMPLES={'device':'','calc':'12*(3+4)','text':'这是一段需要统计的文字','files':'','inspect':'example.csv','fileinfo':'example.csv','demo':'10'}
RETURNS={
    'device':'返回系统、Python 版本、磁盘剩余空间。',
    'calc':'返回算式结果；上例为 84。',
    'text':'返回字符、汉字、英文单词和行数。',
    'files':'返回电脑共享目录内的文件名。',
    'inspect':'返回文件概况和少量预览，不发送文件附件。',
    'fileinfo':'生成并回传 TXT 概览报告；文件名请换成实际共享文件名。',
    'demo':'约 10 秒的演示；返回结果，可取消；详细模式显示进度。',
}


def sample_key(tool):
    if tool.kind=='builtin':return tool.id
    if (tool.id=='demo' and tool.kind=='python-task' and tool.arguments==['{text}']
            and Path(tool.target).resolve()==ROOT/'examples'/'task_demo.py'):
        return 'demo'
    return None


def example(tool,prefix):
    if tool.kind=='codex-chat':return prefix+' codex'
    if tool.kind=='web-chat':return prefix+' 运行 chatgpt'
    if tool.kind=='ai-chat':return '%s 运行 %s 帮我润色下面这段文字'%(prefix,tool.id)
    key=sample_key(tool)
    value=SAMPLES.get(key,'')
    if key not in SAMPLES and any('{text}' in arg for arg in tool.arguments):
        value='需要交给工具的内容'
    if any('{file}' in arg for arg in tool.arguments):value='inbox/实际文件名'
    return '%s 运行 %s%s'%(prefix,tool.id,' '+value if value else '')


def home(catalog,prefix='/py'):
    return '\n'.join([
        '【个人工具面板｜手机使用菜单】',
        '离开电脑后，在 QQ 中当前已连接的个人账号会话继续已接入的工作；结果仍发回本会话。',
        '电脑须提前启动面板并保持联网。可锁屏；连接手机时默认防自动睡眠，屏幕可关闭。手动睡眠/休眠/合盖仍会中断。离开前完成登录和必要密钥输入。',
        '',
        '从这里开始：',
        '现在可发 '+prefix+' 菜单，按编号选择 AI、工具、文件和任务，无需复制工具 ID 或任务编号。',
        '① 看能做什么：'+prefix+' 菜单 工具',
        '② 照工具条目里的“发送”指令运行。',
        '③ 等待回答、必要提问或文件；不用一直停留聊天页。默认简洁消息，过程记录留在电脑。',
        '',
        '直接体验：'+prefix+' 运行 calc 12*(3+4)',
        '继续电脑 Codex：'+prefix+' codex（沿用本机登录，按提示选择会话）',
        'ChatGPT 网页：'+prefix+' chatgpt（电脑先连接扩展；按编号新建或选择已打开的聊天）',
        '已接入一个 AI 时，可直接发：'+prefix+' 阅读本文档，概述内容；先发文档并等保存提示。',
        '查看正在做什么：'+prefix+' 状态',
        '选择简洁或详细消息：'+prefix+' 消息',
        '',
        '详细用法（数字也可以）：',
        prefix+' 菜单 1  → 可用工具和运行示例',
        prefix+' 菜单 2  → 启动、进度、结果、取消',
        prefix+' 菜单 3  → 多轮对话、回复与任务编号',
        prefix+' 菜单 4  → 文件工作怎么进行',
        prefix+' 格式  → 可读的文件格式和组件状态',
        prefix+' 菜单 5  → 卡住、报错和当前限制',
        prefix+' 菜单 6  → 接入 AI、提问、追问和结束',
        prefix+' 帮助 工具ID  → 某项工具的具体用法',
        prefix+' 菜单 全部  → 一次发送全部说明（多条消息）',
        '',
        '指令都要带 '+prefix+' 前缀；任务可在“任务”菜单按编号选择。',
        '支持文字、文件回传和图片显示。直接发文件到本会话即可接收；详细用法见“菜单 文件”。',
    ])


def tool_pages(catalog,prefix='/py'):
    entries=[]
    for tool in catalog.all():
        result=RETURNS.get(sample_key(tool),'启动后按提示逐项回答。' if tool.kind=='interactive' else '返回该工具的文字输出、进度和完成状态。')
        if tool.kind=='ai-chat':result='返回 AI 文字回答，可继续追问；每次新运行会新建独立会话。'
        if tool.kind=='codex-chat':result='继续原会话、建立手机分支或新建独立对话，返回答复与必要提问；不另填 API。'
        if tool.kind=='web-chat':result='选择或新建浏览器 ChatGPT 普通聊天，返回文字答复；需要安装本机网页扩展。'
        if tool.id=='inspect' and tool.kind=='builtin':result+=' 文件名请换成 files 列表中的实际名字。'
        description=(tool.description.strip() or result).replace('\n',' ')[:140]
        entries.append('%s｜%s（%s）\n用途：%s\n发送：%s\n反馈：%s'%(tool.id,tool.name,KINDS[tool.kind],description,example(tool,prefix),result))
    groups=[];current=[];size=0
    for entry in entries:
        if current and size+len(entry)+2>850:
            groups.append(current);current=[];size=0
        current.append(entry);size+=len(entry)+2
    if current:groups.append(current)
    pages=[]
    for index,group in enumerate(groups,1):
        footer='详细说明：%s 帮助 工具ID\n返回首页：%s 菜单'%(prefix,prefix)
        if index<len(groups):footer='下一页：%s 菜单 工具 %d\n'%(prefix,index+1)+footer
        pages.append('【当前可用工具 %d/%d】\n只列出这台电脑已提供的功能；额外工作工具须先在电脑登记。\n\n'%(index,len(groups))+'\n\n'.join(group)+'\n\n'+footer)
    return pages


def tool_help(catalog,identifier,prefix='/py'):
    tool=catalog.get(identifier)
    key=sample_key(tool)
    messages=['【工具用法｜%s】'%tool.name,'工具 ID：'+tool.id,'类型：'+KINDS[tool.kind],
        '用途：'+(tool.description or '运行电脑已登记的这项工具。')[:300],'','手机发送：',example(tool,prefix),'']
    if key in RETURNS:
        messages.append('你会收到：'+RETURNS[key])
        if key in ('files','inspect','fileinfo'):
            messages += ['可从手机直接发送附件；收到保存提示后复制共享文件名。也可提前放在电脑“共享目录”。',
                '先发送 %s 运行 files 查看实际文件名。'%prefix,
                '可读 PDF、DOCX、Excel、PPTX、RTF、HTML 和常见文本。文字最多 1 MB，其他格式通常 20 MB；发送 '+prefix+' 格式 查看详细范围。上例文件名需换成实际文件名。']
    elif tool.kind=='web-chat':
        messages += ['电脑先点击“ChatGPT 网页通道”生成扩展，在 Edge/Chrome 手动安装，点击扩展“连接此浏览器”。浏览器里的 ChatGPT 需已登录。',
            '手机按编号新建聊天，或选择浏览器已打开的聊天；之后直接发送要求。无需 API Key；不会读取 Cookie、密码和登录令牌。',
            '浏览器须保持打开；不要同时在所选聊天页输入。遇到登录验证、输入草稿、页面变化或发送不确定会报错，不自动重发。',
            '目前只桥接普通聊天的文字输入和答复，不自动上传附件或执行网页工具确认。电脑关闭此桥接不保证网页生成也停止。']
    elif tool.kind=='codex-chat':
        messages += ['也可发送 '+prefix+' 运行 codex。使用电脑官方 Codex 已有登录；先在电脑安装并登录 Codex，不需要在这里添加 AI 或 API Key。',
            '桌面应用可以保持打开。选已有会话后，选“继续原会话”或“建立手机分支”，再发送工作要求；也可从首页选择新建独立对话。',
            '有本机默认会话时会直接选中它；回复“列表”切换，“查找 关键词”搜索，“下一页”翻页。',
            '回复“状态”查原会话，“结果”看最近答复；“同意/拒绝”处理明确的本次确认；“退出”结束桥接。',
            '正在处理时用 '+prefix+' codex 停止 请求停止本轮，已有改动保留。',
            '同一原会话被占用时，选手机分支可带着已保存上下文继续，聊天记录不自动合并，项目文件共用。若坚持继续原会话，需释放其连接后明确重试。不会自动分支或重发。桌面专属工具可能需回电脑处理。']
    elif tool.kind=='ai-chat':
        enabled=catalog.ai_store.get(tool.target).allow_tools
        messages += ['将示例中的问题换成你的内容；也可先不写问题，启动后再提问。',
            '每次“运行”新建一个会话；继续追问请用下面的“回复”，这样才保留上下文。',
            ('此 AI 已开启电脑工具：可读共享文字文件、运行已授权的普通程序、生成并回传文字文件。' if enabled else
             '此 AI 当前为文字对话模式；在电脑编辑 AI，勾选“允许 AI 使用共享文件和电脑已授权工具”可开启工具。'),
            '问题和最近上下文发送给电脑配置的 AI 服务，密钥不经过 QQ。']
        if enabled:messages += ['文件例子：'+prefix+' 运行 '+tool.id+' 把最新收到的 TXT 整理成待办清单，保存为文件发给我。',
            '默认只回结果、文件和必要问题；工具过程在任务详情，详细消息模式可显示进度。自建工具需在电脑单独勾选“允许 AI 调用”。格式范围：'+prefix+' 格式。']
    elif any('{file}' in arg for arg in tool.arguments):
        messages += ['将示例文件名换成附件保存提示或“文件”列表中的实际名字；程序会收到电脑上的完整文件路径。',
            '成功任务写入专用输出目录的文件会自动回传，每任务最多 10 个；其他共享文件可用“发文件 文件名”取回。']
    elif any('{text}' in arg for arg in tool.arguments):
        messages += ['将示例最后一段换成你的实际内容，整段文字会交给这项工具。',
            '它处理什么内容、生成什么结果，由电脑上接入的工具决定。']
    else:
        messages += ['此工具按电脑预设参数启动，指令后不用另加内容。']
    if tool.kind in ('interactive','ai-chat','codex-chat','web-chat'):
        messages += ['启动后按提示回答；普通 '+prefix+' 内容 继续当前对话，任务菜单可切换。',
            '回复格式：%s 回复 任务编号 内容'%prefix,
            '密码/密钥只在电脑配置，不要发到 QQ。']
        if tool.kind=='ai-chat':messages += ['问题不能为空。结束会话：%s 结束 任务编号；立即停止：%s 取消 任务编号。'%(prefix,prefix)]
        else:messages += ['空输入：%s 回复 任务编号（编号后不写内容）'%prefix]
    else:
        messages += ['结果或失败会自动返回；任务菜单可查看状态和执行详情。',
            '简洁模式不发过程；详细模式显示程序主动输出的进度。']
    messages += ['','查看：%s 状态 任务编号'%prefix,'结果：%s 结果 任务编号'%prefix,
        '停止：%s 取消 任务编号'%prefix,'返回工具列表：%s 菜单 工具'%prefix]
    return '\n'.join(messages)


def topics(prefix='/py'):
    return {
        'AI':'\n'.join([
            '【怎样通过 QQ 使用 AI】',
            '先发 '+prefix+' 菜单，选“与 AI 对话”：可选 ChatGPT 网页、通过 API 新建对话，或继续本次打开的 AI 对话。',
            'ChatGPT 网页沿用浏览器登录，无需 API Key。电脑先点“ChatGPT 网页通道”连接扩展；QQ 按编号新建聊天或选择已打开的聊天，再发送工作要求。',
            '浏览器和桥接程序保持运行即可，不用把网页放在前台。当前网页通道仅传文字，不自动上传 QQ 附件，也不接入电脑工具。',
            'Codex 在首页单列：可继续已有会话，或按项目、模型新建独立对话。',
            '',
            '以下为通过 API 配置其他 AI 的方法：',
            '电脑点“添加 AI”，选平台、填密钥、选模型，点“连接并添加”。地址和编号自动设置；其他兼容服务才需自己填地址。',
            '手机先发 '+prefix+' 菜单 工具，找到自动分配的 AI 工具 ID。',
            '只有一个 AI 且未选中活跃任务时，可直接发 '+prefix+' 你的要求 自动开始；旧任务结束后同样适用。多个 AI 时按下面格式选择。',
            '假设 ID 是 ai（须换成真实 ID）：',
            prefix+' 运行 ai 帮我起草一封请假邮件',
            '收到回答和任务编号后，继续同一会话：',
            prefix+' 回复 任务编号 改得简短一些',
            '也可先“选择 编号”，之后用 '+prefix+' 你的问题 继续。',
            '完成工作：'+prefix+' 结束 任务编号',
            '立即停止：'+prefix+' 取消 任务编号',
            '',
            '新发“运行”就新建会话；不同会话相互独立。每个会话保留最近最多 20 轮、约 48000 字的上下文。每次提问最多 12000 字。',
            '接口失败不会自动重发；看提示修正后再提交问题。等待提问也占用一个活跃任务名额，闲置时请结束。',
            '电脑编辑 AI，可勾选“允许 AI 使用共享文件和电脑已授权工具”。模型须支持工具调用；旧配置默认只做文字对话。',
            '开启后可读取 PDF、DOCX、Excel、PPTX、RTF、HTML 和常见文字文件，运行已授权普通程序并回传结果；扫描件需 OCR。格式及限制：'+prefix+' 格式。',
            '自建程序要在电脑另勾选“允许 AI 调用”；交互程序仍由你直接操作。每轮最多 8 次模型请求、12 次工具操作。默认只回最终结果和必要错误；过程可在任务详情查看。',
            '项目改动：先在电脑登记项目工具、可编辑文件和固定测试，再用自然语言让 AI 改源码并验证，回传差异、测试报告和源码包。',
            '密钥在电脑填写；问题、读取的文件和工具结果会发给所选模型。图片识别、网页搜索、任意电脑命令尚未接入。',
            '返回首页：'+prefix+' 菜单',
        ]),
        '任务':'\n'.join([
            '【怎样通过 QQ 继续工作｜任务】',
            '1. 先看工具：'+prefix+' 菜单 工具',
            '2. 照条目发送运行指令，例如：'+prefix+' 运行 demo 10',
            '3. 电脑回复“任务已创建：编号”。编号只是接收凭据，不代表工作已经完成。',
            '4. 之后自动收到结果、必要提问或失败；选择详细消息可看进度。',
            '',
            '常用指令（把“任务编号”替换为实际编号）：',
            prefix+' 状态  → 最近任务和状态',
            prefix+' 状态 任务编号  → 查这一项',
            prefix+' 结果 任务编号  → 查已保存的文字结果',
            prefix+' 取消 任务编号  → 请求停止这一项',
            '',
            '状态含义：排队中→运行中→已完成；也可能等待输入、失败、超时或已取消。',
            '最多同时活跃 2 个任务，其他排队；等待回答的会话也占一个名额。',
            '进度取决于工具主动输出；没有进度时先查状态，不要反复运行同一任务。',
            '取消不能撤销已经发生的文件写入等操作。重启后未完成任务会记为“中断”，不会自动重跑。',
            '返回首页：'+prefix+' 菜单',
        ]),
        '对话':'\n'.join([
            '【怎样通过 QQ 继续工作｜连续对话】',
            '工具列表里标为“连续对话”的工具，会把程序的问题逐项发回 QQ。',
            '先照该工具的启动示例运行，之后看它返回的真实任务编号。',
            '',
            '指定任务回答：'+prefix+' 回复 任务编号 你的回答',
            '直接回车/空输入：'+prefix+' 回复 任务编号',
            '选定一个会话：'+prefix+' 选择 任务编号',
            '选定后可直接发：'+prefix+' 你的回答',
            '等待新的问题或结果后，再回答下一项。',
            '正在处理时新发的普通内容不会提交，会提示等待；旧任务结束后，有且仅有一个 AI 时会自动开启新会话。',
            '',
            '运行新的连续对话时会自动选中它；有多个会话时优先用带任务编号的回复。',
            '若想把“菜单”“状态”等词当作回答，也用“回复 编号 内容”，避免被识别成操作指令。',
            '密码或密钥由 getpass 请求时，QQ 只提示“等待电脑隐私输入”，请在电脑面板选中对应任务填写。',
            '人在外面不能代填这类隐私输入；离开前应先完成必要设置。不要把秘密发到 QQ。',
            '当前只操作通过新版面板启动的会话，不能接管已经开着的 VS Code 终端。',
            '返回首页：'+prefix+' 菜单',
        ]),
        '文件':'\n'.join([
            '【怎样通过 QQ 继续工作｜文件】',
            '直接在当前已连接的 QQ 个人账号会话发送文件或图片，不用加文字前缀。收到“附件已保存”后再进行处理。',
            '',
            '1. 文件以独立名称保存在共享目录 inbox/ 下，同名文件不会互相覆盖。',
            '2. 查看文件：'+prefix+' 文件',
            '3. 文字概况：'+prefix+' 运行 inspect 实际共享文件名',
            '4. 生成 TXT 报告并自动回传：'+prefix+' 运行 fileinfo 实际共享文件名',
            '5. 取回任意共享文件：'+prefix+' 发文件 实际共享文件名',
            '6. 图片直接显示：'+prefix+' 发图片 实际共享图片名',
            '',
            '文件通道单个文件最多 50 MB，直接显示的 PNG/JPEG/GIF/WebP 图片最多 10 MB；接收目录总量最多 500 MB。',
            '内置概览、TXT 报告和已开启工具的 AI 共用读取器：PDF、DOCX、Excel、PPTX、RTF、HTML 及常见文本。支持范围、组件状态：'+prefix+' 格式。',
            '收到保存提示后发 '+prefix+' 阅读本文档，概述内容（只有一个 AI 时）；也可说“分析表格”或“提炼这份 PPT 的要点”。',
            '扫描 PDF/图片需要 OCR；旧 DOC/PPT 请另存为 DOCX/PPTX 或 PDF。未读到的页和公式结果会明确提示。',
            '成功任务写入专用输出目录的结果会自动回传，每任务最多 10 个。原有程序需按工具说明设置输出位置。',
            'QQ 的接受回执不等于手机已下载；发送未确认时先查手机，附件不会自动重发。',
            '图片可传输不代表 AI 能看图；直接 AI 入口仍只支持文字。文件不会自动执行或解压。',
            '返回首页：'+prefix+' 菜单',
        ]),
        '问题':'\n'.join([
            '【使用问题与当前限制】',
            '没有收到结果：先发 '+prefix+' 状态，再用“结果 任务编号”查看；可能正在排队或等待输入。',
            '没有进度：程序须主动输出并刷新，桥接才能转发；请勿因此重复启动。',
            '等待手机输入：用 '+prefix+' 回复 任务编号 内容 回答当前问题。',
            '等待电脑隐私输入：只能在电脑面板填写；不能从 QQ 代填。',
            '任务编号错误：发 '+prefix+' 状态 复制实际编号，不要把示例中的“任务编号”原样发送。',
            '找不到工具：发 '+prefix+' 菜单 工具；不在列表的功能须先在电脑接入。',
            '显示失败/中断：用“结果 编号”看原因；检查工具、文件、依赖后再决定是否重新运行。',
            '连菜单都不回复：检查电脑是否开机联网、新版面板是否已连接 QQ、手机是否发到当前已连接的 QQ 个人账号会话。',
            '',
            '仅接收当前登录账号自身私聊中的文字、文件和图片，不接收群聊或其他联系人指令。',
            '电脑必须持续运行；不支持任意图形软件、现有终端接管、手机临时发送命令来执行。',
            '附件接收失败：单文件须在 50 MB 内；检查 QQ 文件是否已过期、共享接收目录是否已满。已有工具按当前用户权限运行。',
            '重新看全部说明：'+prefix+' 菜单 全部',
        ]),
    }


def all_pages(catalog,prefix='/py'):
    return [home(catalog,prefix),*tool_pages(catalog,prefix),*topics(prefix).values()]


def section(catalog,topic,prefix='/py'):
    topic=topic.strip()
    if not topic:return home(catalog,prefix)
    name,_,argument=topic.partition(' ')
    name={'1':'工具','2':'任务','3':'对话','4':'文件','5':'问题','tools':'工具',
        '6':'AI','ai':'AI','tasks':'任务','chat':'对话','files':'文件','faq':'问题','首页':'首页'}.get(name,name)
    if name in ('工具','功能'):
        try:page=int(argument.strip() or '1')
        except ValueError:raise ValueError('工具页码需要是数字，例如：%s 菜单 工具 2'%prefix) from None
        pages=tool_pages(catalog,prefix)
        if not 1<=page<=len(pages):raise ValueError('工具列表共有 %d 页，请选择有效页码。'%len(pages))
        return pages[page-1]
    if name=='首页':return home(catalog,prefix)
    if name in topics(prefix):return topics(prefix)[name]
    raise ValueError('没有这个菜单栏目。发送 %s 菜单 查看目录；某项工具用 %s 帮助 工具ID。'%(prefix,prefix))
