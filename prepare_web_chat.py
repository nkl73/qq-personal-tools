"""Generate the local, paired extension without putting its token in source files."""
from web_chat import prepare_extension
from settings import STATE
import os

if __name__=='__main__':
    path=prepare_extension(STATE/'web-chat')
    print('扩展文件夹：'+str(path))
    print('在 Edge/Chrome 扩展页手动加载此文件夹，然后在扩展弹窗中点“连接此浏览器”。')
    if os.name=='nt':os.startfile(path)
