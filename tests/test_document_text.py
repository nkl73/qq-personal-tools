from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from document_text import MAX_DOCUMENT_CHARS,WORD,read_text


def docx(path,body):
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml','<w:document xmlns:w="'+WORD[1:-1]+'"><w:body>'+body+'</w:body></w:document>')
        archive.writestr('word/header1.xml','<ignored>not main body</ignored>')
        # Extraction must not follow external document relationships.
        archive.writestr('word/_rels/document.xml.rels','<Relationship Target="https://invalid.example/private" TargetMode="External"/>')


class DocumentTextTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name);self.path=self.root/'sample.docx'

    def tearDown(self):self.folder.cleanup()

    def test_docx_extracts_paragraphs_tables_breaks_and_hyperlink_text(self):
        docx(self.path,'<w:p><w:r><w:t>文件</w:t><w:tab/><w:t>概述</w:t><w:br/><w:t>下一行</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>数量</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '<w:p><w:hyperlink><w:r><w:t>链接文字</w:t></w:r></w:hyperlink><w:del><w:r><w:delText>已删除文字</w:delText></w:r></w:del></w:p>')
        self.assertEqual(read_text(self.path),'文件\t概述\n下一行\n项目\t数量\n链接文字')
        self.assertEqual(list(self.root.iterdir()),[self.path])

    def test_corrupt_missing_body_images_and_legacy_formats_have_clear_errors(self):
        self.path.write_bytes(b'not zip')
        with self.assertRaisesRegex(ValueError,'无法读取 DOCX'):read_text(self.path)
        with zipfile.ZipFile(self.path,'w') as archive:archive.writestr('other.xml','missing body')
        with self.assertRaisesRegex(ValueError,'无法读取 DOCX'):read_text(self.path)
        docx(self.path,'<w:p><w:r><w:drawing/></w:r></w:p>')
        with self.assertRaisesRegex(ValueError,'图片'):read_text(self.path)
        old=self.root/'old.doc';old.write_bytes(b'legacy')
        with self.assertRaisesRegex(ValueError,'旧版 DOC'):read_text(old)

    def test_docx_rejects_expansion_and_excessive_file_xml_and_text(self):
        xml='<!DOCTYPE document [<!ENTITY x "expanded">]><document>&x;</document>'
        for encoding in ('utf-8','utf-16','utf-32'):
            with zipfile.ZipFile(self.path,'w') as archive:archive.writestr('word/document.xml',xml.encode(encoding))
            with self.assertRaisesRegex(ValueError,'声明'):read_text(self.path)
        docx(self.path,'<w:p><w:r><w:t>'+('甲'*MAX_DOCUMENT_CHARS)+'</w:t></w:r></w:p>')
        with self.assertRaisesRegex(ValueError,'20 万字'):read_text(self.path)
        docx(self.path,'<w:p><w:r><w:t>normal</w:t></w:r></w:p>')
        with patch('document_text.MAX_DOCUMENT_XML',10):
            with self.assertRaisesRegex(ValueError,'正文数据过大'):read_text(self.path)
        with patch('document_text.MAX_DOCX_BYTES',10):
            with self.assertRaisesRegex(ValueError,'10 MB'):read_text(self.path)

    def test_utf8_bom_size_and_invalid_encoding(self):
        path=self.root/'text.txt';path.write_text('文字',encoding='utf-8-sig')
        self.assertEqual(read_text(path),'文字')
        path.write_bytes(b'\xff')
        with self.assertRaisesRegex(ValueError,'UTF-8'):read_text(path)
        path.write_bytes(b'01234567890')
        with patch('document_text.MAX_TEXT_BYTES',10):
            with self.assertRaisesRegex(ValueError,'1 MB'):read_text(path)
