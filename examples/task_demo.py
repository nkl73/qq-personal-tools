"""Offline task used to demonstrate progress, completion and cancellation."""
import sys
import time
try:steps=int(sys.argv[1]) if len(sys.argv)>1 and sys.argv[1] else 5
except ValueError:steps=5
steps=max(1,min(steps,60))
for index in range(steps):
    print('已完成 %d/%d 步'%(index+1,steps),flush=True)
    time.sleep(1)
print('任务完成：这是一项普通后台程序任务，不需要 input()。')
