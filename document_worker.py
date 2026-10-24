"""Disposable file parser. Only a structured, bounded result is written to stdout."""
import json
import sys

from document_text import extract_document


def main():
    try:result={'ok':True,'document':extract_document(sys.argv[1])}
    except ValueError as error:result={'ok':False,'error':str(error)[:500]}
    except Exception:result={'ok':False,'error':'文件解析失败，请检查是否损坏、加密或格式不符；必要时重新导出后再发送。'}
    sys.stdout.buffer.write(json.dumps(result,ensure_ascii=False).encode('utf-8'))


if __name__=='__main__':main()
