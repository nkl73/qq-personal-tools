from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import base64
import hashlib
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote,urlsplit
from urllib.request import url2pathname

from bridge_core import BridgeClient,ChatIO
from file_channel import FileChannel,FileStore,incoming_files
from file_io import Attachment,MAX_FILE,copy_bounded,download,image_suffix,safe_name,shared_file,validate_download_url
from onebot_adapter import EventReceiver,OneBotClient,relay_background
from task_hub import Dispatcher,FINAL,Manager,run_hub
from tools import Catalog,Tool,builtin
from test_onebot import ACCOUNT,TOKEN,event,post

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


def attachment_event(name='input.txt',identifier='fixture-file',size=6,kind='file',**changes):
    return event(message=[{'type':kind,'data':{'name':name,'file_id':identifier,'file_size':size}}],**changes)


class QQFiles:
    def __init__(self):
        self.sent=[];self.uploads=[];self.images=[];self.calls=[];self.assets={'fixture-file':b'hello\n'}
        self.bad_receipt=False;self.cached=None;self.on_upload=None
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.server.seen_download_auth=self.headers.get('Authorization')
                body=owner.assets[self.path.rsplit('/',1)[-1]]
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_POST(self):
                if self.headers.get('Authorization')!='Bearer '+TOKEN:self.send_error(401);return
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path,body))
                if self.path=='/get_login_info':data={'user_id':int(ACCOUNT)}
                elif self.path=='/get_private_file_url':data={'url':owner.url+'/download/'+body['file_id']}
                elif self.path in ('/get_image','/get_file'):
                    data={'file':str(owner.cached),'file_size':str(owner.cached.stat().st_size)} if owner.cached else {'url':owner.url+'/download/'+body.get('file_id',body.get('file',''))}
                elif self.path=='/upload_private_file':
                    owner.uploads.append({'body':body,'bytes':Path(body['file']).read_bytes()})
                    if owner.on_upload:owner.on_upload()
                    data={} if owner.bad_receipt else {'file_id':'sent-file-'+str(len(owner.uploads))}
                elif self.path=='/send_private_msg':
                    owner.sent.append(body)
                    for segment in body['message']:
                        if segment['type']=='image':owner.images.append(Path(url2pathname(urlsplit(segment['data']['file']).path)).read_bytes())
                    data={'message_id':1000+len(owner.sent)}
                else:self.send_error(404);return
                raw=json.dumps({'status':'ok','retcode':0,'data':data}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name)
        self.shared=self.root/'shared';self.shared.mkdir()
        self.server=QQFiles();self.api=OneBotClient(self.server.url,TOKEN)
        self.notices=queue.Queue();self.stop=threading.Event();self.channel=None

    def tearDown(self):
        self.stop.set()
        if self.channel:self.channel.close()
        self.server.close();self.folder.cleanup()

    def channel_start(self,**kwargs):
        self.channel=FileChannel(self.api,ACCOUNT,self.shared,self.root/'channel',self.notices,self.stop,**kwargs)
        return self.channel

    def until(self,predicate,timeout=5):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.02)
        self.fail('文件处理没有在预期时间内完成')

    def status(self,uid):
        with self.channel.store.lock:
            row=self.channel.store.db.execute('SELECT status FROM transfers WHERE id=?',(uid,)).fetchone()
            return row[0] if row else None

    def allow_fixture(self,url):
        if not url.startswith(self.server.url+'/download/'):raise ValueError('仅本机测试资源')
        return url

    def test_accepts_own_new_files_images_and_offline_notice_only(self):
        now=time.time();value=attachment_event()
        self.assertEqual(incoming_files(value,ACCOUNT,now)[0]['name'],'input.txt')
        native=event(message=[{'type':'file','data':{'file':'native report.txt','file_id':'native-uuid','file_size':'6'}}])
        self.assertEqual(incoming_files(native,ACCOUNT,now)[0]['name'],'native report.txt')
        self.assertEqual(incoming_files(attachment_event(kind='image'),ACCOUNT,now)[0]['kind'],'image')
        notice={'post_type':'notice','notice_type':'offline_file','self_id':ACCOUNT,'user_id':ACCOUNT,
            'time':int(time.time()),'file':{'name':'offline.txt','size':6,'url':'https://files.qq.com/a'}}
        self.assertEqual(incoming_files(notice,ACCOUNT,now)[0]['name'],'offline.txt')
        for changed in ({'user_id':12345},{'self_id':12345},{'target_id':12345},{'message_type':'group'},
            {'time':now-100},{'post_type':'request'},{'message_id':None}):
            self.assertEqual(incoming_files(attachment_event(**changed),ACCOUNT,now),[])
        self.assertEqual(incoming_files(attachment_event(identifier='C:\\private\\secret.txt'),ACCOUNT,now),[])

    def test_path_names_and_shared_file_boundary(self):
        for name in ('../../report.txt','C:\\path\\report.txt','report.txt'):
            self.assertEqual(safe_name(name),'report.txt')
        self.assertTrue(safe_name('CON.txt').startswith('_'))
        self.assertNotIn(':',safe_name('report.txt:secret'))
        (self.shared/'good.txt').write_text('data',encoding='utf-8')
        (self.root/'outside.txt').write_text('private',encoding='utf-8')
        self.assertEqual(shared_file(self.shared,'good.txt').name,'good.txt')
        for value in ('../outside.txt',str(self.root/'outside.txt'),'.env','good.txt:stream','https://example.com/a'):
            with self.assertRaises(ValueError):shared_file(self.shared,value)
        large=self.shared/'large.bin'
        with large.open('wb') as stream:stream.truncate(MAX_FILE+1)
        with self.assertRaises(ValueError):shared_file(self.shared,'large.bin')

    def test_download_rejects_local_networks_non_qq_hosts_and_credentials(self):
        for url in ('file:///a','http://127.0.0.1/a','http://192.168.1.2/a','http://localhost/a',
            'https://example.com/a','https://key@files.qq.com/a','https://files.qq.com:8080/a'):
            with self.assertRaises(ValueError):validate_download_url(url)
        with patch('file_io.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError):validate_download_url('https://files.qq.com/a')

    def test_download_is_bounded_and_failed_transfer_leaves_no_partial_file(self):
        path=self.root/'download.part'
        with patch('file_io.validate_download_url',side_effect=self.allow_fixture):
            self.assertEqual(download(self.server.url+'/download/fixture-file',path,self.stop,6),6)
            self.assertEqual(path.read_bytes(),b'hello\n')
            self.assertIsNone(self.server.server.seen_download_auth)
            path.unlink()
            with self.assertRaises(ValueError):download(self.server.url+'/download/fixture-file',path,self.stop,99)
            self.assertFalse(path.exists())
            with patch('file_io.MAX_FILE',3):
                with self.assertRaises(ValueError):download(self.server.url+'/download/fixture-file',path,self.stop)
            self.assertFalse(path.exists())

    def test_inbound_file_download_dedup_and_acknowledgement(self):
        channel=self.channel_start(prefix='/work')
        value=attachment_event(name='../../input.txt');item=incoming_files(value,ACCOUNT,time.time())[0]
        with patch('file_io.validate_download_url',side_effect=self.allow_fixture):
            channel.accept(value,time.time());channel.accept(value,time.time())
            self.until(lambda:self.status(item['id'])=='received')
        paths=list((self.shared/'inbox').glob('*'))
        self.assertEqual(len(paths),1);self.assertEqual(paths[0].read_bytes(),b'hello\n')
        texts='\n'.join(list(self.notices.queue))
        self.assertIn('已收到',texts);self.assertIn('/work 菜单',texts)
        self.assertNotIn(TOKEN,texts);self.assertNotIn('/download/',texts)

    def test_cached_image_reads_only_backend_cache_roots(self):
        cache=self.root/'cache';cache.mkdir();self.server.cached=cache/'photo.png';self.server.cached.write_bytes(PNG)
        channel=self.channel_start(cached_roots=[cache])
        value=attachment_event(name='phone',identifier='cached-image',size=len(PNG),kind='image')
        uid=incoming_files(value,ACCOUNT,time.time())[0]['id']
        channel.accept(value,time.time());self.until(lambda:self.status(uid)=='received')
        image=next((self.shared/'inbox').glob('*.png'));self.assertEqual(image.read_bytes(),PNG)
        self.server.cached=self.root/'private.png';self.server.cached.write_bytes(PNG)
        bad=attachment_event(name='outside',identifier='private-image',size=len(PNG),kind='image',message_id=125)
        uid=incoming_files(bad,ACCOUNT,time.time())[0]['id']
        channel.accept(bad,time.time());self.until(lambda:self.status(uid)=='failed')
        self.assertEqual(len(list((self.shared/'inbox').glob('*'))),1)

    def test_outbound_files_use_private_upload_snapshots_and_ignore_echo(self):
        source=self.shared/'result.pdf';source.write_bytes(b'%PDF-fixture')
        channel=self.channel_start();uid=channel.enqueue(Attachment('result.pdf','file'))
        self.until(lambda:self.status(uid)=='sent')
        self.assertEqual(self.server.uploads[0]['bytes'],b'%PDF-fixture')
        body=self.server.uploads[0]['body'];self.assertEqual(body['user_id'],ACCOUNT)
        self.assertEqual(body['name'],'result.pdf');self.assertNotEqual(Path(body['file']),source)
        self.assertTrue(source.exists());self.until(lambda:not Path(body['file']).exists())
        echo=attachment_event(name='result.pdf',identifier='sent-file-1',size=source.stat().st_size,message_id=999)
        channel.accept(echo,time.time());self.assertIsNone(channel.store.next('in'))

    def test_images_send_inline_and_other_files_cannot_be_forced_to_image(self):
        (self.shared/'image.png').write_bytes(PNG)
        (self.shared/'plain.svg').write_text('<svg/>',encoding='utf-8')
        channel=self.channel_start();uid=channel.enqueue(Attachment('image.png','image'))
        self.until(lambda:self.status(uid)=='sent')
        self.assertEqual(self.server.images,[PNG]);self.assertEqual(self.server.uploads,[])
        with self.assertRaises(ValueError):channel.enqueue(Attachment('plain.svg','image'))
        uid=channel.enqueue(Attachment('plain.svg'))
        self.until(lambda:self.status(uid)=='sent')
        self.assertEqual(self.server.uploads[0]['bytes'],b'<svg/>')

    def test_phone_can_send_back_the_same_file_and_image(self):
        (self.shared/'input.txt').write_bytes(b'hello\n')
        (self.shared/'image.png').write_bytes(PNG)
        channel=self.channel_start()
        for path in ('input.txt','image.png'):
            uid=channel.enqueue(Attachment(path));self.until(lambda:self.status(uid)=='sent')
        image_id=hashlib.md5(PNG).hexdigest().upper()+'.png'
        self.server.assets[image_id]=PNG
        values=[attachment_event(),attachment_event(name='image.png',identifier=image_id,size=len(PNG),kind='image',message_id=456)]
        with patch('file_io.validate_download_url',side_effect=self.allow_fixture):
            for value in values:
                uid=incoming_files(value,ACCOUNT,time.time())[0]['id']
                channel.accept(value,time.time());self.until(lambda:self.status(uid)=='received')
        self.assertEqual(sorted(p.read_bytes() for p in (self.shared/'inbox').iterdir()),sorted([b'hello\n',PNG]))

    def test_self_echo_before_upload_response_waits_for_receipt(self):
        (self.shared/'result.txt').write_bytes(b'hello\n')
        channel=self.channel_start()
        echo=attachment_event(name='result.txt',identifier='sent-file-1',message_id=987)
        incoming_id=incoming_files(echo,ACCOUNT,time.time())[0]['id']
        def callback():
            channel.accept(echo,time.time());time.sleep(.15)
        self.server.on_upload=callback
        uid=channel.enqueue(Attachment('result.txt','file'))
        self.until(lambda:self.status(uid)=='sent')
        self.until(lambda:self.status(incoming_id)=='ignored')
        self.assertFalse((self.shared/'inbox').exists())

    def test_uncertain_attachment_is_not_retried_and_restart_does_not_replay(self):
        (self.shared/'result.txt').write_text('result',encoding='utf-8');self.server.bad_receipt=True
        channel=self.channel_start();uid=channel.enqueue(Attachment('result.txt','file'))
        self.until(lambda:self.status(uid)=='uncertain');time.sleep(.3)
        self.assertEqual(len(self.server.uploads),1)
        folder=self.root/'recovery';store=FileStore(folder)
        store.add('pending','in',{});store.add('sending','out',{});store.set('sending','sending');store.close()
        store=FileStore(folder)
        try:
            states=dict(store.db.execute('SELECT id,status FROM transfers'))
            self.assertEqual(states,{'pending':'interrupted','sending':'uncertain'})
            self.assertIsNone(store.next('out'))
        finally:store.close()

    def test_normal_and_interactive_tasks_receive_file_and_output_paths(self):
        catalog=Catalog(self.root/'tools.json',self.shared)
        (self.shared/'input with spaces.txt').write_text('original text',encoding='utf-8')
        source=self.root/'processor.py'
        source.write_text('import os,sys\nfrom pathlib import Path\nx=Path(sys.argv[1]).read_text()\nPath(os.environ["QQ_BRIDGE_OUTPUT_DIR"],"result.txt").write_text(x.upper())\nprint("done",flush=True)\n',encoding='utf-8')
        catalog.save(Tool('process','处理文件',target=str(source),arguments=['{file}']))
        notices=queue.Queue();manager=Manager(catalog,notices,self.root/'tasks')
        try:
            uid=manager.start('process','input with spaces.txt')
            self.until(lambda:manager.store.get(uid)['state'] in FINAL)
            self.assertEqual(manager.store.get(uid)['state'],'completed')
            self.until(lambda:any(isinstance(x,Attachment) for x in list(notices.queue)))
            attachment=next(x for x in list(notices.queue) if isinstance(x,Attachment))
            self.assertEqual((self.shared/attachment.path).read_text(),'ORIGINAL TEXT')
            source.write_text('import os\nfrom pathlib import Path\nx=input("回复：")\nPath(os.environ["QQ_BRIDGE_OUTPUT_DIR"],"chat.txt").write_text(x,encoding="utf-8")\n',encoding='utf-8')
            catalog.save(Tool('chatfile','交互文件','interactive',target=str(source)))
            uid=manager.start('chatfile')
            self.until(lambda:manager.store.get(uid)['state']=='waiting_input');manager.answer(uid,'手机回答')
            self.until(lambda:manager.store.get(uid)['state'] in FINAL)
            self.assertEqual((self.shared/'results'/uid/'chat.txt').read_text(encoding='utf-8'),'手机回答')
            # Literal phone text is not reinterpreted as a new placeholder.
            literal=Tool('literal','literal',target=str(source),arguments=['{text}'])
            self.assertEqual(literal.arguments_for('{file}',self.shared,self.shared),['{file}'])
        finally:manager.close()

    def test_fileinfo_and_dispatcher_only_attach_explicit_shared_files(self):
        catalog=Catalog(self.root/'tools.json',self.shared);notices=queue.Queue();manager=Manager(catalog,notices,self.root/'tasks')
        (self.shared/'input.txt').write_text('你好 hello',encoding='utf-8')
        dispatcher=Dispatcher(manager)
        try:
            uid=manager.start('fileinfo','input.txt');self.until(lambda:manager.store.get(uid)['state'] in FINAL)
            self.until(lambda:any(isinstance(x,Attachment) for x in list(notices.queue)))
            attachment=next(x for x in list(notices.queue) if isinstance(x,Attachment))
            self.assertIn('汉字：2',(self.shared/attachment.path).read_text(encoding='utf-8'))
            self.assertIn('input.txt',dispatcher.handle('文件'))
            self.assertIn('回传队列',dispatcher.handle('发文件 input.txt'))
            with self.assertRaises(ValueError):dispatcher.handle('发文件 ../outside.txt')
            self.assertIn('fileinfo',dispatcher.handle('菜单 工具'))
        finally:manager.close()

    def test_full_signed_receive_process_and_attachment_return_over_http(self):
        ready=threading.Event();receivers=[];notices=queue.Queue()
        catalog=Catalog(self.root/'tools.json',self.shared)
        config=self.root/'connection.json';config.write_text(json.dumps({'api_url':self.server.url,'event_port':0,'token':TOKEN}),encoding='utf-8')
        def created(*args,**kwargs):
            value=EventReceiver(*args,**kwargs);receivers.append(value);ready.set();return value
        with ChatIO(self.root/'wire',port=0,console=False) as bridge:
            hub=threading.Thread(target=run_hub,args=(bridge,catalog,notices,self.stop),kwargs={'directory':self.root/'task-state'})
            transport=threading.Thread(target=relay_background,args=(BridgeClient(bridge.connection_path),self.stop),
                kwargs={'notices':notices,'directory':self.root/'transport','config_path':config,'shared_dir':self.shared,'log':lambda _:None})
            with patch('onebot_adapter.EventReceiver',side_effect=created),patch('file_io.validate_download_url',side_effect=self.allow_fixture):
                hub.start();transport.start()
                try:
                    self.assertTrue(ready.wait(4));post(receivers[0],attachment_event())
                    self.until(lambda:bool(list((self.shared/'inbox').glob('*.txt'))) if (self.shared/'inbox').exists() else False)
                    relative=next((self.shared/'inbox').glob('*.txt')).relative_to(self.shared).as_posix()
                    post(receivers[0],event('/py 运行 fileinfo '+relative,message_id=501))
                    self.until(lambda:len(self.server.uploads)==1,timeout=8)
                    self.assertIn(b'hello',self.server.uploads[0]['bytes'])
                    texts='\n'.join(segment['data']['text'] for item in self.server.sent for segment in item['message'] if segment['type']=='text')
                    self.assertIn('已收到',texts);self.assertNotIn(TOKEN,texts)
                    post(receivers[0],event('/py 发文件 '+relative,message_id=502))
                    self.until(lambda:len(self.server.uploads)==2)
                    self.assertEqual(self.server.uploads[-1]['bytes'],b'hello\n')
                finally:
                    self.stop.set();bridge.stop_event.set();hub.join(timeout=8);transport.join(timeout=12)
                self.assertFalse(hub.is_alive());self.assertFalse(transport.is_alive())


if __name__=='__main__':unittest.main()
