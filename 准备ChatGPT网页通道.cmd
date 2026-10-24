@echo off
setlocal
pushd "%~dp0"
py -3 -X utf8 prepare_web_chat.py
popd
