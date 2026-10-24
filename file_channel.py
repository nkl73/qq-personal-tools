"""Independent, persistent QQ attachment queues with explicit send receipts."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from file_io import Attachment,MAX_FILE,MAX_IMAGE,MAX_INBOX,cache_roots,copy_bounded,download,image_suffix,safe_name,shared_file


def incoming_files(event,self_id,started):
    if not isinstance(event,dict) or str(event.get('self_id',''))!=str(self_id) or str(event.get('user_id',''))!=str(self_id):return []
    if event.get('target_id') is not None and str(event['target_id'])!=str(self_id):return []
    try:
        if float(event.get('time',0))<started-2:return []
    except (TypeError,ValueError):return []
    segments=[];mid=event.get('message_id')
    if event.get('post_type') in ('message','message_sent') and event.get('message_type')=='private':
        if not isinstance(mid,(int,str)) or isinstance(mid,bool) or len(str(mid))>160:return []
        segments=event.get('message')
        if not isinstance(segments,list) or len(segments)>30:return []
    elif event.get('post_type')=='notice' and event.get('notice_type')=='offline_file':
        data=event.get('file')
        if not isinstance(data,dict):return []
        segments=[{'type':'file','data':data}]
    else:return []
    result=[]
    for index,segment in enumerate(segments):
        if not isinstance(segment,dict) or segment.get('type') not in ('file','image'):continue
        data=segment.get('data')
        if not isinstance(data,dict):continue
        kind=segment['type'];identifier=data.get('file_id') or data.get('id') or data.get('file') or ''
        url=data.get('url') or ''
        if not isinstance(identifier,str) or len(identifier)>4096 or not isinstance(url,str) or len(url)>16000:continue
        if not identifier and not url:continue
        if not url and identifier.startswith(('https://','http://')):url=identifier
        # Local paths and arbitrary schemes in an event never become file reads.
        if identifier and (re.match(r'(?i)^(?:https?|file|base64|[a-z]):',identifier) or identifier.startswith(('\\','/'))):
            identifier=''
        if not identifier and not url:continue
        try:
            size=int(data.get('file_size',data.get('size',0)) or 0)
            if size<0:continue
        except (TypeError,ValueError):continue
        filename=data.get('name') or data.get('file_name')
        if not filename and isinstance(data.get('file'),str) and not re.match(r'(?i)^(?:https?|file|base64):',data['file']):
            filename=data['file']
        name=safe_name(filename or ('image' if kind=='image' else 'file.bin'))
        identity=str(mid) if mid is not None else str(event.get('time'))+':'+identifier+':'+name
        uid=hashlib.sha256(('qq-file:'+str(self_id)+':'+identity+':'+str(index)).encode()).hexdigest()
        result.append({'id':uid,'kind':kind,'file_id':identifier,'url':url,'name':name,'size':size,'message_id':str(mid or ''),
            'image_file':str(data.get('file',''))[:4096]})
        if len(result)>=10:break
    return result


class FileStore:
    def __init__(self,folder):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.db=sqlite3.connect(self.folder/'files.sqlite3',check_same_thread=False);self.db.row_factory=sqlite3.Row
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS transfers (id TEXT PRIMARY KEY,direction TEXT,status TEXT,data TEXT,result TEXT,updated REAL)')
            self.db.execute("UPDATE transfers SET status='interrupted' WHERE status IN ('queued','receiving')")
            self.db.execute("UPDATE transfers SET status='uncertain' WHERE status='sending'")

    def add(self,uid,direction,data):
        with self.lock,self.db:
            count=self.db.execute("SELECT COUNT(*) FROM transfers WHERE direction=? AND status='queued'",(direction,)).fetchone()[0]
            if count>=20:raise ValueError('文件等待队列已满，请稍后再发送。')
            return bool(self.db.execute('INSERT OR IGNORE INTO transfers VALUES (?,?,?,?,?,?)',
                (uid,direction,'queued',json.dumps(data,ensure_ascii=False),'',time.time())).rowcount)

    def next(self,direction):
        with self.lock:
            row=self.db.execute("SELECT * FROM transfers WHERE direction=? AND status='queued' ORDER BY rowid LIMIT 1",(direction,)).fetchone()
            return dict(row) if row else None

    def set(self,uid,status,result=''):
        with self.lock,self.db:self.db.execute('UPDATE transfers SET status=?,result=?,updated=? WHERE id=?',(status,result,time.time(),uid))

    def echo(self,item):
        with self.lock:
            rows=self.db.execute("SELECT result FROM transfers WHERE direction='out' AND status IN ('sending','sent','uncertain') AND updated>?",(time.time()-300,)).fetchall()
        for row in rows:
            if item.get('file_id') and item['file_id']==row['result']:return True
            if item.get('message_id') and item['message_id']==row['result']:return True
        return False

    def sending(self):
        with self.lock:
            return self.db.execute("SELECT 1 FROM transfers WHERE direction='out' AND status='sending' LIMIT 1").fetchone() is not None

    def close(self):
        with self.lock:self.db.close()


class FileChannel:
    def __init__(self,api,self_id,shared,folder,notices,stop,*,cached_roots=None,prefix='/py'):
        self.api=api;self.self_id=str(self_id);self.shared=Path(shared).resolve()
        self.shared.mkdir(parents=True,exist_ok=True)
        self.folder=Path(folder);self.store=FileStore(self.folder)
        self.notices=notices;self.stop=stop
        self.prefix=prefix
        self.cached_roots=cached_roots if cached_roots is not None else cache_roots(self.folder.parent)
        self.workers=[threading.Thread(target=self.worker,args=(direction,),daemon=True) for direction in ('in','out')]
        for worker in self.workers:worker.start()

    def say(self,text,detail=False):
        from phone_feedback import Feedback
        if not detail or Feedback(self.folder.parent).detailed:self.notices.put(text)

    def accept(self,event,started):
        for item in incoming_files(event,self.self_id,started):
            if self.store.echo(item):continue
            try:
                self.store.add(item['id'],'in',item)
            except ValueError:self.say('文件等待队列已满，请稍后重新发送附件。')

    def enqueue(self,attachment):
        if attachment.mode not in ('auto','file','image'):raise ValueError('无效的附件发送类型。')
        source=shared_file(self.shared,attachment.path)
        uid=secrets.token_hex(12)
        snapshot_dir=self.folder/'outgoing'/uid;snapshot_dir.mkdir(parents=True)
        destination=snapshot_dir/safe_name(source.name)
        meta=copy_bounded(source,destination,self.stop,expected=source.stat().st_size)
        with destination.open('rb') as stream:suffix=image_suffix(stream.read(16))
        kind='image' if suffix and meta['size']<=MAX_IMAGE and attachment.mode!='file' else 'file'
        if attachment.mode=='image' and kind!='image':
            destination.unlink(missing_ok=True)
            raise ValueError('直接发图片支持不超过 10 MB 的 PNG、JPEG、GIF、WebP；其他内容请用“发文件”。')
        data={**meta,'snapshot':str(destination.resolve()),'name':destination.name,'kind':kind,
            'relative':source.relative_to(self.shared).as_posix(),'task':attachment.task_id}
        try:self.store.add(uid,'out',data)
        except Exception:destination.unlink(missing_ok=True);raise
        return uid

    def worker(self,direction):
        while not self.stop.is_set():
            row=self.store.next(direction)
            if row is None:self.stop.wait(.1);continue
            data=json.loads(row['data']);uid=row['id']
            try:
                if direction=='in':
                    # A self-message callback can precede the upload response. Wait
                    # for its receipt before checking identity; identical content
                    # or names can also be a legitimate new upload from the phone.
                    deadline=time.monotonic()+9
                    while self.store.sending() and time.monotonic()<deadline:
                        if self.stop.wait(.05):raise EOFError('文件传输已停止')
                    if self.store.echo(data):
                        self.store.set(uid,'ignored','本机发送回声');continue
                    self.say('收到附件，正在接收：'+data['name'],detail=True)
                    self.store.set(uid,'receiving');relative=self.receive(data,uid)
                    self.store.set(uid,'received',relative)
                    from phone_feedback import Feedback
                    self.say('附件已保存：'+relative+'\n可交给文件工具处理，或发送“'+self.prefix+' 发文件 '+relative+'”取回。' if Feedback(self.folder.parent).detailed
                        else '已收到《'+data['name']+'》。用 '+self.prefix+' 菜单 选择处理方式。')
                else:self.send(data,uid)
            except EOFError:
                if direction=='in':self.store.set(uid,'interrupted')
                elif self.store.next('out') and self.store.next('out')['id']==uid:self.store.set(uid,'interrupted')
            except Exception:
                # Raw transport errors may contain signed URLs, tokens or cache paths.
                if direction=='in':
                    self.store.set(uid,'failed','接收失败')
                    self.say('附件接收失败：'+data['name']+'。文件上限 50 MB；请重新发送。仅接受 QQ 下载地址或后台受限缓存。')
                else:
                    self.store.set(uid,'uncertain','发送未确认')
                    self.say('附件回传未确认：'+data['name']+'。已保留文件，不会自动重发；请先检查手机，确实未收到时再用“发文件”取回。')

    def receive(self,data,uid):
        self.api.require_account(self.self_id)
        if data['size']>MAX_FILE:raise ValueError('附件超过 50 MB。')
        inbox=self.shared/'inbox';inbox.mkdir(exist_ok=True)
        if not inbox.resolve().is_relative_to(self.shared):raise ValueError('接收目录无效。')
        if sum(p.stat().st_size for p in inbox.iterdir() if p.is_file())+data['size']>MAX_INBOX:raise ValueError('接收目录已满。')
        target=inbox/('.'+uid+'.part');target.unlink(missing_ok=True)
        expected=data['size'] if data['size'] else None
        identifier=data['file_id'];url='';cached=None
        if identifier:
            try:
                info=self.api.call('get_image' if data['kind']=='image' else 'get_private_file_url',
                    {'file':identifier,'file_id':identifier})
                if isinstance(info,dict):
                    url=info.get('url','')
                    cached=info.get('file')
            except (OSError,ValueError):pass
        if not isinstance(url,str) or not url.startswith(('http://','https://')):url=data['url']
        if url and url.startswith(('http://','https://')):download(url,target,self.stop,expected)
        else:
            if identifier and not cached:
                info=self.api.call('get_file',{'file_id':identifier})
                if isinstance(info,dict):cached=info.get('file');url=info.get('url','')
            if url and url.startswith(('http://','https://')):download(url,target,self.stop,expected)
            elif cached:
                source=Path(cached).resolve()
                if not any(source.is_relative_to(Path(root).resolve()) for root in self.cached_roots):raise ValueError('缓存目录不允许。')
                copy_bounded(source,target,self.stop,expected)
            else:raise ValueError('没有可接收的附件。')
        if sum(p.stat().st_size for p in inbox.iterdir() if p.is_file())>MAX_INBOX:
            target.unlink(missing_ok=True);raise ValueError('接收目录已满。')
        name=data['name']
        if data['kind']=='image':
            with target.open('rb') as stream:suffix=image_suffix(stream.read(16))
            if suffix:name=Path(name).stem+suffix
        destination=inbox/(uid[:12]+'-'+safe_name(name))
        target.replace(destination)
        return destination.relative_to(self.shared).as_posix()

    def send(self,data,uid):
        self.api.require_account(self.self_id)
        path=Path(data['snapshot'])
        if not path.resolve().is_relative_to((self.folder/'outgoing').resolve()) or not path.is_file():raise ValueError('发送快照不存在。')
        self.store.set(uid,'sending')
        if data['kind']=='image':
            result=self.api.call('send_private_msg',{'user_id':self.self_id,'message':[{'type':'image','data':{'file':path.resolve().as_uri()}}]})
            receipt=result.get('message_id') if isinstance(result,dict) else None
        else:
            result=self.api.call('upload_private_file',{'user_id':self.self_id,'file':str(path.resolve()),'name':data['name']})
            receipt=result.get('file_id') if isinstance(result,dict) else None
        if receipt is None or receipt=='':raise ValueError('后台没有返回附件发送凭据。')
        self.store.set(uid,'sent',str(receipt))
        try:path.unlink(missing_ok=True)
        except OSError:pass
        self.say('附件已交给 QQ：'+data['name']+'。请在手机查看，回执表示后台已接受发送。',detail=True)

    def close(self):
        self.stop.set()
        for worker in self.workers:worker.join(timeout=10)
        if any(worker.is_alive() for worker in self.workers):raise RuntimeError('文件传输尚未停止。')
        self.store.close()
