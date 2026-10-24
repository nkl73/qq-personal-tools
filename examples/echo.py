"""Safe first-run example: no model, credentials, files or shell execution."""
print('QQ Python Bridge 演示。输入 exit 退出。')
while True:
    text=input('请输入一段文字：')
    if text.lower()=='exit':
        print('演示结束。')
        break
    print('收到：'+text)
