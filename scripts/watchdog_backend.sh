#!/usr/bin/env bash
# QMT 交易系统看门狗：每 30 秒健康检查，后端掉线自动拉起。
# 用法（开机自启/手动均可）:  bash scripts/watchdog_backend.sh
# 单实例保护：已在运行则直接退出。

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
PY="${PYTHON311:-D:/programs/Python311/python.exe}"
# 必须与 start_backend.sh 的 PORT 默认值一致，否则健康检查永远失败、无限误重启后端
PORT="${PORT:-7099}"
LOG_DIR="logs"
LOG="$LOG_DIR/watchdog.log"
LOCK="$LOG_DIR/watchdog.pid"
mkdir -p "$LOG_DIR"

# ---- 单实例：锁文件 + Windows PID 存活判定 ----
# 不能用 wmic 按命令行计数：MSYS2 后台作业 fork 时会短暂出现多个同命令行的
# bash.exe（实测数到 3 个），永远误判"已在运行"。
# Git Bash 的 $$ 不是 Windows PID，但 /proc/$$/winpid 可以拿到真实 PID。
if [ -f "$LOCK" ]; then
  OLD_PID=$(awk '{print $NF}' "$LOCK" 2>/dev/null)
  if [ -n "$OLD_PID" ] && tasklist //FI "PID eq $OLD_PID" 2>/dev/null | grep -q "$OLD_PID"; then
    echo "$(date '+%F %T') watchdog 已在运行（PID $OLD_PID），本次退出" >> "$LOG"
    exit 0
  fi
fi
WINPID=$(cat "/proc/$$/winpid" 2>/dev/null || echo "$$")
echo "$(date '+%F %T') $WINPID" > "$LOCK"
echo "===== $(date '+%F %T') watchdog 启动 (WinPID $WINPID) =====" >> "$LOG"

fails=0
while true; do
  if "$PY" -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:$PORT/api/health', timeout=5).status==200 else 1)" 2>/dev/null; then
    if [ "$fails" -gt 0 ]; then
      echo "$(date '+%F %T') 后端恢复健康" >> "$LOG"
    fi
    fails=0
  else
    fails=$((fails + 1))
    echo "$(date '+%F %T') 健康检查失败 (第 $fails 次)" >> "$LOG"
    # 连续 2 次失败（约 1 分钟）才重启，避免瞬时抖动误杀
    if [ "$fails" -ge 2 ]; then
      echo "$(date '+%F %T') 判定后端掉线，自动重启..." >> "$LOG"
      "$ROOT/scripts/start_backend.sh" >> "$LOG" 2>&1
      fails=0
      sleep 30   # 给后端留启动时间
    fi
  fi
  sleep 30
done
