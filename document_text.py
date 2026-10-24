"""Local, bounded text extraction shared by AI and offline file tools."""
import importlib
import importlib.util
import json
import logging
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import time
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
import zipfile
import zlib

from process_control import options,terminate

MAX_TEXT_BYTES=1024*1024
MAX_DOCX_BYTES=10*1024*1024
MAX_DOCUMENT_XML=8*1024*1024
MAX_DOCUMENT_CHARS=200000
WORD='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
DRAW='{http://schemas.openxmlformats.org/drawingml/2006/main}'
PRESENTATION='{http://schemas.openxmlformats.org/presentationml/2006/main}'
REL='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
TEXT_SUFFIXES={'.txt','.md','.markdown','.csv','.tsv','.json','.jsonl','.ndjson','.yaml','.yml',
    '.xml','.log','.ini','.cfg','.toml','.py','.js','.ts','.jsx','.tsx','.css','.sql','.sh','.ps1','.bat','.c','.cpp','.h','.java','.go','.rs'}
FORMAT_LABEL='PDF、DOCX、XLSX/XLSM/XLS、PPTX、RTF、HTML、TXT/MD/CSV/TSV/JSON/YAML/XML 及常见代码文本'
MAX_FILE_BYTES=20*1024*1024
MAX_ZIP_TOTAL=64*1024*1024
MAX_PAGES=200
MAX_SHEETS=50
MAX_ROWS=5000
MAX_COLUMNS=256
MAX_CELLS=100000
READ_TIMEOUT=25
READ_WORKER=Path(__file__).with_name('document_worker.py')


def format_help():
    missing=[]
    for name,label in (('pypdf','PDF'),('openpyxl','XLSX/XLSM'),('xlrd','XLS'),('striprtf','RTF')):
        if importlib.util.find_spec(name) is None:missing.append(label)
    return '\n'.join(['【可读取的文件格式】',FORMAT_LABEL,
        '文字最多 1 MB；PDF/表格/PPTX 最多 20 MB；DOCX 最多 10 MB；全文最多 20 万字，AI 每页读取 12000 字。',
        'PDF/PPTX 最多 200 页；表格最多 50 张工作表、合计 5000 行、256 列、10 万单元格。',
        'PDF 扫描页和图片需要 OCR；旧 DOC/PPT 请另存为 DOCX/PPTX 或 PDF。加密文件需先解除密码。',
        '表格公式只读已有结果，不重新计算；文件中的宏、代码和外链不会执行。',
        '尚缺组件：'+','.join(missing)+'；在电脑双击“安装文件读取组件.cmd”。' if missing else 'PDF、Excel、RTF 读取组件已就绪。'])


def optional_module(name):
    try:return importlib.import_module(name)
    except ImportError:
        raise ValueError('缺少文件读取组件，请在电脑双击“安装文件读取组件.cmd”后重试；不需要填写 API 或更改 AI 设置。') from None


def limited(text):
    if len(text)>MAX_DOCUMENT_CHARS:raise ValueError('文档文字超过 20 万字，请先拆分文件。')
    return text


def safe_xml(raw):
    if len(raw)>MAX_DOCUMENT_XML:raise ValueError('文档 XML 数据过大，请先拆分文件。')
    declarations=raw.replace(b'\x00',b'').upper()
    if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:raise ValueError('文档包含不支持的 XML 声明。')
    return ET.fromstring(raw)


def checked_archive(path):
    archive=zipfile.ZipFile(path)
    try:
        entries=archive.infolist()
        if len(entries)>4000 or sum(e.file_size for e in entries)>MAX_ZIP_TOTAL:
            raise ValueError('文档展开数据过大，请先拆分文件。')
        if len({e.filename for e in entries})!=len(entries):raise ValueError('文档包含重复部件，无法可靠读取。')
        if any(e.flag_bits&1 for e in entries):raise ValueError('请先移除文档的加密或密码保护。')
        return archive
    except Exception:
        archive.close();raise


def xml_part(archive,name):
    entry=archive.getinfo(name)
    if entry.file_size>MAX_DOCUMENT_XML:raise ValueError('文档 XML 数据过大，请先拆分文件。')
    with archive.open(entry) as stream:return safe_xml(stream.read(MAX_DOCUMENT_XML+1))


def decode_text(raw):
    if raw.startswith((b'\xff\xfe\x00\x00',b'\x00\x00\xfe\xff')):encoding='utf-32'
    elif raw.startswith((b'\xff\xfe',b'\xfe\xff')):encoding='utf-16'
    else:encoding=None
    try:
        if encoding:text=raw.decode(encoding)
        else:
            try:text=raw.decode('utf-8-sig')
            except UnicodeError:text=raw.decode('gb18030')
        if any(ord(c)<32 and c not in '\t\r\n\f' for c in text):raise UnicodeError()
        return text
    except UnicodeError:raise ValueError('无法识别文字编码；支持 UTF-8、带 BOM 的 UTF-16/32 和 GB18030，请先另存为 UTF-8。') from None


def read_text(path):
    """Synchronous extraction, also used by the isolated reader worker."""
    return extract_document(path)['text']


def extract_document(path):
    path=Path(path)
    suffix=path.suffix.lower()
    if path.stat().st_size>MAX_FILE_BYTES:raise ValueError('直接读取的文件最多 20 MB，请先拆分文件。')
    try:
        if suffix=='.docx':result={'text':read_docx(path),'scope':'DOCX 正文段落和表格文字；不含图片、页眉页脚或嵌入附件'}
        elif suffix=='.pdf':result=read_pdf(path)
        elif suffix in ('.xlsx','.xlsm'):result=read_xlsx(path)
        elif suffix=='.xls':result=read_xls(path)
        elif suffix=='.pptx':result=read_pptx(path)
        elif suffix=='.rtf':result=read_rtf(path)
        elif suffix in ('.html','.htm'):result=read_html(path)
        elif suffix in TEXT_SUFFIXES:result={'text':read_plain(path),'scope':'文字文件全文；仅作为数据读取，不执行代码'}
        elif suffix in ('.doc','.ppt'):
            raise ValueError('旧版 DOC/PPT 暂不直接解析；请在 Word/WPS/PowerPoint 另存为 DOCX/PPTX 或导出 PDF 后再发，不要只改扩展名。')
        else:raise ValueError('暂不支持读取此格式。可读：'+FORMAT_LABEL+'；图片或扫描件需要 OCR。')
    except (zipfile.BadZipFile,KeyError,ET.ParseError,zlib.error):
        raise ValueError('文件格式无效或内容已损坏，请重新保存后再发送。') from None
    result['text']=limited(result['text'])
    result.setdefault('warnings',[]);result.setdefault('units',{})
    result['format']=suffix.lstrip('.').upper()
    return result


def read_plain(path):
    with path.open('rb') as stream:raw=stream.read(MAX_TEXT_BYTES+1)
    if len(raw)>MAX_TEXT_BYTES:raise ValueError('AI 直接读取的文字文件最多 1 MB，请先用已授权工具拆分。')
    return decode_text(raw)


def read_document(path,stop=None,timeout=None):
    """Keep slow/corrupt parser work outside the GUI and terminate on cancellation."""
    path=Path(path).resolve()
    if stop is not None and stop.is_set():raise EOFError('文件读取已停止')
    process=subprocess.Popen([sys.executable,'-X','utf8',str(READ_WORKER),str(path)],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,**options())
    deadline=time.monotonic()+(READ_TIMEOUT if timeout is None else timeout)
    try:
        while True:
            if stop is not None and stop.is_set():raise EOFError('文件读取已停止')
            if time.monotonic()>=deadline:raise ValueError('文件读取超时，已停止解析；请缩小文件或分段导出后重试。')
            try:raw,_=process.communicate(timeout=.1);break
            except subprocess.TimeoutExpired:pass
        if len(raw)>2*1024*1024:raise ValueError('文件解析结果过大，请先拆分文件。')
        try:result=json.loads(raw.decode('utf-8'))
        except (ValueError,UnicodeError):raise ValueError('文件读取进程异常退出，请检查文件或安装读取组件。') from None
        if not result.get('ok'):raise ValueError(result.get('error','文件读取失败。'))
        return result['document']
    finally:
        terminate(process)
        if process.stdout:process.stdout.close()


def read_pdf(path):
    module=optional_module('pypdf');logging.getLogger('pypdf').setLevel(logging.CRITICAL)
    try:
        with path.open('rb') as stream:
            reader=module.PdfReader(stream,strict=False)
            if reader.is_encrypted:raise ValueError('PDF 已加密，请在电脑解除密码保护后再发。')
            count=len(reader.pages)
            if count>MAX_PAGES:raise ValueError('PDF 超过 200 页，请先按页拆分。')
            pieces=[];empty=[];chars=0
            for index,page in enumerate(reader.pages,1):
                contents=page.get_contents()
                if contents is not None and len(contents.get_data())>MAX_DOCUMENT_XML:
                    raise ValueError('PDF 单页内容过大，请拆分或重新导出。')
                value=(page.extract_text() or '').strip()
                if not value:empty.append(index);continue
                value='[第 %d 页]\n%s'%(index,value);chars+=len(value)
                if chars>MAX_DOCUMENT_CHARS:raise ValueError('文档文字超过 20 万字，请先拆分文件。')
                pieces.append(value)
            if not pieces:raise ValueError('PDF 没有可提取文字，可能是扫描件或图片 PDF；需要先用 OCR 识别文字后再发。')
            return {'text':'\n\n'.join(pieces),'scope':'PDF 可提取文字，带原始页码；不识别图片、表格网格和扫描文字',
                'units':{'pages':count,'pages_with_text':len(pieces)},
                'warnings':['以下页没有可提取文字，可能是空白或扫描页，未完成识别：'+','.join(map(str,empty))] if empty else []}
    except module.errors.PdfReadError:
        raise ValueError('无法读取 PDF，请检查是否损坏、加密或文件格式不符。') from None


def read_xlsx(path):
    module=optional_module('openpyxl')
    with checked_archive(path) as archive:
        # Preflight all XML parsed by the workbook reader, including shared strings.
        for entry in archive.infolist():
            if not entry.filename.endswith(('.xml','.rels')):continue
            xml=xml_part(archive,entry.filename)
            if entry.filename.startswith('xl/worksheets/'):
                for cell in xml.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c'):
                    match=re.fullmatch(r'([A-Z]+)([0-9]+)',cell.get('r',''))
                    if not match:raise ValueError('表格包含无效单元格位置。')
                    column=0
                    for char in match[1]:column=column*26+ord(char)-64
                    if column>MAX_COLUMNS or int(match[2])>MAX_ROWS:
                        raise ValueError('表格超过 5000 行或 256 列，请先筛选或拆分。')
    formulas=values=None
    try:
        formulas=module.load_workbook(path,read_only=True,data_only=False,keep_links=False,keep_vba=False)
        values=module.load_workbook(path,read_only=True,data_only=True,keep_links=False,keep_vba=False)
        if len(formulas.worksheets)>MAX_SHEETS:raise ValueError('工作表超过 50 张，请拆分文件。')
        pieces=[];chars=0;rows=0;cells=0;missing=False
        for sheet,cached in zip(formulas.worksheets,values.worksheets):
            if (sheet.max_column or 0)>MAX_COLUMNS:raise ValueError('表格超过 256 列，请先选择需要的列导出。')
            pieces.append('[工作表：'+sheet.title+('（隐藏）' if sheet.sheet_state!='visible' else '')+']')
            # Some producers write inaccurate dimensions. Read real rows with a fixed bound.
            sheet.reset_dimensions();cached.reset_dimensions()
            for index,(row,cache) in enumerate(zip(sheet.iter_rows(max_col=MAX_COLUMNS),cached.iter_rows(max_col=MAX_COLUMNS)),1):
                if index>MAX_ROWS:raise ValueError('表格数据超过 5000 行，请先筛选或拆分。')
                last=max((i+1 for i,c in enumerate(row) if c.value is not None),default=0)
                if not last:continue
                if index>MAX_ROWS or rows>=MAX_ROWS:raise ValueError('表格数据超过 5000 行，请先筛选或拆分。')
                cells+=last
                if cells>MAX_CELLS:raise ValueError('表格超过 10 万个单元格，请先筛选或拆分。')
                output=[]
                for cell,value in zip(row[:last],cache[:last]):
                    item=cell.value
                    if cell.data_type=='f':
                        if value.value is None:item=str(item)+' [公式无缓存，未计算]';missing=True
                        else:item=value.value
                    output.append(cell_text(item))
                line='行 %d\t'%index+'\t'.join(output);chars+=len(line)
                if chars>MAX_DOCUMENT_CHARS:raise ValueError('文档文字超过 20 万字，请先拆分文件。')
                pieces.append(line);rows+=1
        if not rows:raise ValueError('表格没有可读取的单元格。')
        return {'text':'\n'.join(pieces),'scope':'工作表名称、原始行号和单元格值；公式优先读取已有缓存，不重算、不执行宏，不读取图表图片',
            'units':{'sheets':len(formulas.worksheets),'rows':rows,'cells':cells},
            'warnings':['部分公式没有缓存结果，已保留公式并标明未计算；请在 Excel/WPS 重新计算并保存后再发。'] if missing else []}
    except module.utils.exceptions.InvalidFileException:
        raise ValueError('无法读取 Excel，请另存为有效的 XLSX/XLSM 文件。') from None
    finally:
        if formulas is not None:formulas.close()
        if values is not None:values.close()


def cell_text(value):
    if value is None:return ''
    if hasattr(value,'isoformat'):value=value.isoformat()
    return str(value).replace('\t','\\t').replace('\r','').replace('\n','\\n')


def read_xls(path):
    module=optional_module('xlrd')
    try:book=module.open_workbook(str(path),on_demand=True,ragged_rows=True)
    except module.XLRDError:raise ValueError('无法读取 XLS；请检查文件是否完整、未加密且确实为旧版 Excel 格式。') from None
    try:
        if book.nsheets>MAX_SHEETS:raise ValueError('工作表超过 50 张，请拆分文件。')
        pieces=[];rows=0;cells=0;chars=0
        for sheet in book.sheets():
            if sheet.ncols>MAX_COLUMNS or rows+sheet.nrows>MAX_ROWS:raise ValueError('表格超过 5000 行或 256 列，请先筛选或拆分。')
            pieces.append('[工作表：'+sheet.name+']')
            for index in range(sheet.nrows):
                output=[]
                for cell in sheet.row(index):
                    value=cell.value
                    if cell.ctype==module.XL_CELL_DATE:value=module.xldate_as_datetime(value,book.datemode)
                    if cell.ctype==module.XL_CELL_BOOLEAN:value=bool(value)
                    output.append(cell_text(value))
                if not any(output):continue
                cells+=len(output)
                if cells>MAX_CELLS:raise ValueError('表格超过 10 万个单元格，请先筛选或拆分。')
                line='行 %d\t'%(index+1)+'\t'.join(output);chars+=len(line)
                if chars>MAX_DOCUMENT_CHARS:raise ValueError('文档文字超过 20 万字，请先拆分文件。')
                pieces.append(line);rows+=1
        if not rows:raise ValueError('表格没有可读取的单元格。')
        return {'text':'\n'.join(pieces),'scope':'旧版 XLS 的工作表、行号与已保存单元格结果；不重新计算公式，不执行宏或读取图表图片',
            'units':{'sheets':book.nsheets,'rows':rows,'cells':cells}}
    finally:book.release_resources()


def read_pptx(path):
    with checked_archive(path) as archive:
        presentation=xml_part(archive,'ppt/presentation.xml')
        relationships=xml_part(archive,'ppt/_rels/presentation.xml.rels')
        targets={r.get('Id'):r.get('Target','') for r in relationships if r.get('TargetMode')!='External' and r.get('Type','').endswith('/slide')}
        slides=presentation.findall(PRESENTATION+'sldIdLst/'+PRESENTATION+'sldId')
        if len(slides)>MAX_PAGES:raise ValueError('PPTX 超过 200 页，请先拆分文件。')
        pieces=[];empty=[];chars=0
        for index,slide in enumerate(slides,1):
            target=targets.get(slide.get(REL+'id'),'')
            part=posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('ppt',target))
            if not target or not part.startswith('ppt/slides/') or '\\' in part or not part.endswith('.xml'):
                raise ValueError('PPTX 的幻灯片引用无效。')
            body=xml_part(archive,part)
            paragraphs=[]
            for paragraph in body.iter(DRAW+'p'):
                text=''.join((node.text or '') if node.tag==DRAW+'t' else '\n' if node.tag==DRAW+'br' else '' for node in paragraph.iter()).strip()
                if text:paragraphs.append(text)
            if not paragraphs:empty.append(index);continue
            value='[第 %d 张幻灯片]\n'%index+'\n'.join(paragraphs);chars+=len(value)
            if chars>MAX_DOCUMENT_CHARS:raise ValueError('文档文字超过 20 万字，请先拆分文件。')
            pieces.append(value)
        if not pieces:raise ValueError('PPTX 没有可读取的文字，可能只有图片；需要先做 OCR。')
        return {'text':'\n\n'.join(pieces),'scope':'按演示顺序读取幻灯片文本框与表格文字；不读取图片、图表数据、SmartArt、演讲备注或动画',
            'units':{'slides':len(slides),'slides_with_text':len(pieces)},
            'warnings':['以下幻灯片没有提取到文字：'+','.join(map(str,empty))] if empty else []}


class VisibleHTML(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.hidden=[];self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','head','template'):self.hidden.append(tag)
        if not self.hidden and tag in ('p','div','br','tr','li','h1','h2','h3','h4'):self.parts.append('\n')
        if not self.hidden and tag in ('td','th'):self.parts.append('\t')
    def handle_endtag(self,tag):
        if self.hidden and tag==self.hidden[-1]:self.hidden.pop()
        if not self.hidden and tag in ('p','div','tr','li','h1','h2','h3','h4'):self.parts.append('\n')
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def read_html(path):
    parser=VisibleHTML();parser.feed(read_plain(path));parser.close()
    return {'text':'\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip()),
        'scope':'HTML 中的正文文字；忽略 script/style/head，不执行脚本、不加载链接资源，未运行浏览器 CSS 排版'}


def read_rtf(path):
    module=optional_module('striprtf.striprtf')
    with path.open('rb') as stream:raw=stream.read(MAX_TEXT_BYTES+1)
    if len(raw)>MAX_TEXT_BYTES:raise ValueError('RTF 最多 1 MB，请先拆分。')
    if not raw.lstrip().startswith(b'{\\rtf'):raise ValueError('RTF 文件格式无效，请重新保存后再发送。')
    return {'text':module.rtf_to_text(raw.decode('latin-1'),errors='strict'),'scope':'RTF 正文文字，不读取图片或嵌入附件'}


def read_docx(path):
    if path.stat().st_size>MAX_DOCX_BYTES:raise ValueError('AI 直接读取的 DOCX 最多 10 MB。')
    try:
        with zipfile.ZipFile(path) as archive:
            entry=archive.getinfo('word/document.xml')
            if entry.file_size>MAX_DOCUMENT_XML:raise ValueError('DOCX 正文数据过大，请先拆分文档。')
            if entry.flag_bits&1:raise ValueError('请先移除 DOCX 的加密或密码保护。')
            with archive.open(entry) as stream:raw=stream.read(MAX_DOCUMENT_XML+1)
        if len(raw)>MAX_DOCUMENT_XML:raise ValueError('DOCX 正文数据过大，请先拆分文档。')
        # Reject declarations even when XML is encoded as UTF-16/32.
        declarations=raw.replace(b'\x00',b'').upper()
        if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
            raise ValueError('DOCX 正文包含不支持的 XML 声明。')
        document=ET.fromstring(raw)
        body=document.find(WORD+'body')
        if document.tag!=WORD+'document' or body is None:raise ValueError('DOCX 缺少可读取的正文。')
        pieces=[];size=0
        def append(value):
            nonlocal size
            size+=len(value)
            if size>MAX_DOCUMENT_CHARS:raise ValueError('DOCX 正文超过 20 万字，请先拆分文档。')
            pieces.append(value)
        # An explicit stack avoids recursive parsing of deeply nested XML.
        stack=[(body,False)]
        while stack:
            node,closing=stack.pop();tag=node.tag
            if closing:
                if tag in (WORD+'tc',WORD+'tr'):
                    while pieces and pieces[-1] in ('\n','\t'):pieces.pop()
                if tag in (WORD+'p',WORD+'tr'):append('\n')
                elif tag==WORD+'tc':append('\t')
            elif tag==WORD+'del':continue
            elif tag==WORD+'t':append(node.text or '')
            elif tag==WORD+'tab':append('\t')
            elif tag in (WORD+'br',WORD+'cr'):append('\n')
            else:
                stack.append((node,True))
                stack.extend((child,False) for child in reversed(list(node)))
        text=''.join(pieces).strip()
        if not text:raise ValueError('DOCX 正文没有可读取的文字，可能只有图片；当前未接入图片识别。')
        return text
    except (zipfile.BadZipFile,KeyError,ET.ParseError,RuntimeError,NotImplementedError):
        raise ValueError('无法读取 DOCX；请检查文件是否完整、未加密且确实为 Word DOCX 格式。') from None
