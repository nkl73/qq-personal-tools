import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile
import xml.etree.ElementTree as ET

from document_text import extract_document,format_help,read_document,read_text
from tools import builtin


def pdf(path,blank=False,chinese=False,extra_blank=False):
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject,DictionaryObject,DecodedStreamObject,NameObject
    writer=PdfWriter();page=writer.add_blank_page(width=300,height=200)
    if not blank:
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        content=b'BT /F1 12 Tf 20 150 Td (PDF sample revenue 100) Tj ET'
        if chinese:
            cmap=DecodedStreamObject();cmap.set_data(b'/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /Test def /CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange 2 beginbfchar <0001> <4E2D> <0002> <6587> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end')
            descendant=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/CIDFontType0'),NameObject('/BaseFont'):NameObject('/STSong-Light'),NameObject('/CIDSystemInfo'):DictionaryObject()})
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type0'),NameObject('/BaseFont'):NameObject('/STSong-Light'),NameObject('/Encoding'):NameObject('/Identity-H'),NameObject('/DescendantFonts'):ArrayObject([writer._add_object(descendant)]),NameObject('/ToUnicode'):writer._add_object(cmap)})
            content=b'BT /F1 12 Tf 20 150 Td <00010002> Tj ET'
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(content);page[NameObject('/Contents')]=writer._add_object(stream)
    if extra_blank:writer.add_blank_page(width=300,height=200)
    writer.write(str(path))


def pptx(path):
    presentation='http://schemas.openxmlformats.org/presentationml/2006/main'
    relation='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    drawing='http://schemas.openxmlformats.org/drawingml/2006/main'
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('ppt/presentation.xml','<p:presentation xmlns:p="'+presentation+'" xmlns:r="'+relation+'"><p:sldIdLst><p:sldId r:id="second"/><p:sldId r:id="first"/></p:sldIdLst></p:presentation>')
        archive.writestr('ppt/_rels/presentation.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="first" Type="'+relation+'/slide" Target="slides/slide1.xml"/><Relationship Id="second" Type="'+relation+'/slide" Target="slides/slide2.xml"/></Relationships>')
        for index,text in ((1,'第二页内容'),(2,'第一页内容')):
            archive.writestr('ppt/slides/slide%d.xml'%index,'<p:sld xmlns:p="'+presentation+'" xmlns:a="'+drawing+'"><a:p><a:r><a:t>'+text+'</a:t></a:r><a:br/><a:r><a:t>表格文字</a:t></a:r></a:p></p:sld>')


class CommonDocumentTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    @unittest.skipUnless(importlib.util.find_spec('pypdf'),'install requirements.txt for PDF tests')
    def test_pdf_chinese_and_mixed_blank_pages_are_reported(self):
        path=self.root/'sample.pdf';pdf(path,chinese=True,extra_blank=True)
        result=read_document(path)
        self.assertIn('中文',result['text']);self.assertIn('[第 1 页]',result['text'])
        self.assertEqual(result['units'],{'pages':2,'pages_with_text':1})
        self.assertIn('2',str(result['warnings']));self.assertIn('未完成识别',str(result['warnings']))
        self.assertIn('中文',builtin('inspect','sample.pdf',self.root))

    @unittest.skipUnless(importlib.util.find_spec('pypdf'),'install requirements.txt for PDF tests')
    def test_pdf_scanned_encrypted_corrupt_and_page_limit_errors(self):
        from pypdf import PdfReader,PdfWriter
        path=self.root/'sample.pdf';pdf(path,blank=True)
        with self.assertRaisesRegex(ValueError,'OCR'):read_document(path)
        pdf(path)
        writer=PdfWriter();writer.append(PdfReader(str(path)));writer.encrypt('fixture-password')
        writer.write(str(self.root/'encrypted.pdf'))
        with self.assertRaisesRegex(ValueError,'加密'):read_document(self.root/'encrypted.pdf')
        with patch('document_text.MAX_PAGES',0):
            with self.assertRaisesRegex(ValueError,'200 页'):extract_document(path)
        path.write_bytes(b'invalid PDF')
        with self.assertRaisesRegex(ValueError,'PDF'):read_document(path)

    @unittest.skipUnless(importlib.util.find_spec('openpyxl'),'install requirements.txt for Excel tests')
    def test_excel_multiple_sheets_dates_and_formula_cache(self):
        from datetime import date
        from openpyxl import Workbook
        path=self.root/'sample.xlsx';book=Workbook();sheet=book.active;sheet.title='订单'
        sheet.append(['名称','金额','日期']);sheet.append(['样品',100,date(2026,10,24)]);sheet['B3']='=SUM(B2:B2)'
        other=book.create_sheet('备注');other.append(['待发货']);book.save(path);book.close()
        result=read_document(path)
        for value in ('订单','备注','样品','2026-10-24','公式无缓存','行 2'):
            self.assertIn(value,result['text'])
        self.assertEqual(result['units']['sheets'],2);self.assertTrue(result['warnings'])
        # Excel/WPS stores a cached numeric value separately from the formula.
        with zipfile.ZipFile(path) as archive:parts={name:archive.read(name) for name in archive.namelist()}
        # XML backends differ in whether an empty value is <v/> or <v></v>.
        ns='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
        xml=ET.fromstring(parts['xl/worksheets/sheet1.xml'])
        cell=xml.find('.//'+ns+"c[@r='B3']")
        self.assertEqual(cell.find(ns+'f').text,'SUM(B2:B2)')
        cached=cell.find(ns+'v')
        if cached is None:cached=ET.SubElement(cell,ns+'v')
        cached.text='100'
        parts['xl/worksheets/sheet1.xml']=ET.tostring(xml,encoding='utf-8')
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,data in parts.items():archive.writestr(name,data)
        result=read_document(path)
        self.assertNotIn('公式无缓存',result['text']);self.assertEqual(result['warnings'],[])
        self.assertIn('行 3\t\t100',result['text'])

    @unittest.skipUnless(importlib.util.find_spec('openpyxl'),'install requirements.txt for Excel tests')
    def test_excel_rejects_sparse_data_beyond_limits_and_xml_entities(self):
        from openpyxl import Workbook
        path=self.root/'wide.xlsx';book=Workbook();book.active['IW1']='beyond 256 columns';book.save(path);book.close()
        with self.assertRaisesRegex(ValueError,'256 列'):read_document(path)
        book=Workbook();book.active['A6000']='too far';book.save(path);book.close()
        with self.assertRaisesRegex(ValueError,'5000 行'):read_document(path)
        book=Workbook();book.active['A1']='valid';book.save(path);book.close()
        with zipfile.ZipFile(path,'a') as archive:archive.writestr('custom.xml','<!DOCTYPE x [<!ENTITY y "x">]><x>&y;</x>')
        with self.assertRaisesRegex(ValueError,'XML 声明'):read_document(path)

    def test_pptx_uses_declared_slide_order_and_extracts_text(self):
        path=self.root/'sample.pptx';pptx(path);result=read_document(path)
        self.assertLess(result['text'].index('第一页内容'),result['text'].index('第二页内容'))
        self.assertEqual(result['units']['slides'],2);self.assertIn('表格文字',result['text'])
        self.assertIn('第 2 张幻灯片',builtin('inspect','sample.pptx',self.root))

    @unittest.skipUnless(importlib.util.find_spec('xlrd'),'install requirements.txt for XLS tests')
    def test_legacy_xls_fixture_is_read(self):
        fixture=Path(__file__).parent/'fixtures'/'example.xls'
        result=read_document(fixture)
        for value in ('销售','名称','样品','100','2026-10-24'):self.assertIn(value,result['text'])
        self.assertEqual(result['format'],'XLS')

    @unittest.skipUnless(importlib.util.find_spec('striprtf'),'install requirements.txt for RTF tests')
    def test_rtf_unicode_and_html_ignore_active_content(self):
        path=self.root/'sample.rtf';path.write_bytes(b'{\\rtf1\\ansi Hello \\u20013?\\u25991?\\par next}')
        self.assertIn('Hello 中文',read_document(path)['text'])
        html=self.root/'sample.html';html.write_text('<html><head><title>ignore title</title></head><body><h1>标题</h1><script>ignore script</script><style>ignore style</style><p>A &amp; B</p><table><tr><td>项目</td><td>100</td></tr></table></body></html>',encoding='utf-8')
        text=read_document(html)['text']
        self.assertIn('标题',text);self.assertIn('A & B',text);self.assertIn('项目\t100',text);self.assertNotIn('ignore',text)

    def test_text_encodings_tsv_source_and_legacy_guidance(self):
        for encoding in ('utf-8-sig','utf-16','utf-32','gb18030'):
            path=self.root/'sample.tsv';path.write_text('名称\t值\n样品\t100',encoding=encoding)
            self.assertIn('样品',read_document(path)['text'])
            self.assertIn('读取行数：2',builtin('inspect','sample.tsv',self.root))
        path=self.root/'example.py';path.write_text('raise RuntimeError("must never execute")',encoding='utf-8')
        self.assertIn('must never execute',read_document(path)['text'])
        old=self.root/'old.ppt';old.write_bytes(b'legacy')
        with self.assertRaisesRegex(ValueError,'不要只改扩展名'):read_document(old)
        self.assertIn('PDF',format_help())

    def test_reader_timeout_and_cancel_terminate_owned_worker(self):
        worker=self.root/'slow.py';worker.write_text('import time\ntime.sleep(60)\n',encoding='utf-8')
        path=self.root/'sample.txt';path.write_text('hello',encoding='utf-8')
        processes=[];start=time.monotonic();popen=subprocess.Popen
        def spawn(*args,**kwargs):
            process=popen(*args,**kwargs);processes.append(process);return process
        with patch('document_text.READ_WORKER',worker),patch('document_text.subprocess.Popen',side_effect=spawn):
            with self.assertRaisesRegex(ValueError,'超时'):read_document(path,timeout=.25)
            stop=threading.Event();timer=threading.Timer(.25,stop.set);timer.start()
            try:
                with self.assertRaises(EOFError):read_document(path,stop)
            finally:timer.cancel()
        self.assertTrue(all(p.poll() is not None for p in processes));self.assertLess(time.monotonic()-start,5)

    def test_missing_component_is_actionable_and_worker_keeps_errors_structured(self):
        with patch('document_text.importlib.import_module',side_effect=ImportError):
            path=self.root/'sample.pdf';path.write_bytes(b'pdf')
            with self.assertRaisesRegex(ValueError,'安装文件读取组件.cmd'):extract_document(path)
        path=self.root/'wrong.bin';path.write_bytes(b'\x00\x01')
        with self.assertRaisesRegex(ValueError,'暂不支持'):read_document(path)


if __name__=='__main__':unittest.main()
