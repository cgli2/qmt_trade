@echo off
REM QMT 交易系统 - 开机自启入口（复制到 Windows 启动文件夹）
REM 启动看门狗：看门狗会自动拉起后端（start_backend.sh），
REM 并每 30 秒健康检查，后端崩溃/被杀后约 1 分钟内自动恢复。
REM 日志：项目 logs\watchdog.log 与 logs\backend.log
setlocal
set "GITBASH=D:\programs\Git\bin\bash.exe"
if not exist "%GITBASH%" set "GITBASH=D:\Programs\Git\bin\bash.exe"
if not exist "%GITBASH%" set "GITBASH=C:\Program Files\Git\bin\bash.exe"
start "" /min "%GITBASH%" -lc "cd /f/workspace/my_qmt_trade && ./scripts/watchdog_backend.sh"
exit /b 0
