"""Demonstrate multiple input prompts and local-only secret entry."""
from getpass import getpass
name=input('怎么称呼你？')
code=getpass('演示用本机口令（请勿输入真实密码）：')
topic=input('你想了解什么主题？')
print(f'你好，{name}。主题是：{topic}。本机口令长度：{len(code)}。')
