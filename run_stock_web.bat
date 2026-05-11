@echo off
cd /d %~dp0
c:\conda\python.exe stock_web.py --host 127.0.0.1 --port 8088 >> stock_web.out.log 2>> stock_web.err.log
