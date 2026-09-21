#!/usr/bin/env bash
# 启动 / 重启 QMT 交易系统后端 (FastAPI + 常驻调度器)
# 用法: 在 MINGW64 里执行  ./scripts/start_backend.sh
#   默认后台常驻（nohup），日志写入 logs/backend.log；
#   ./scripts/start_backend.sh --fg  前台运行（调试用，Ctrl+C 停止）
# 必须用系统 Python 3.11（含 uvicorn + 项目依赖），受管环境 3.13 没有这些包。
set -e
cd "$(dirname "$0")/.."            # 切到项目根 /f/workspace/my_qmt_trade
PY="${PYTHON311:-D:/programs/Python311/python.exe}"
PORT="${PORT:-7099}"
LOG_DIR="logs"
LOG="$LOG_DIR/backend.log"
mkdir -p "$LOG_DIR"

# ---- DuckDB 单进程锁：qmt.duckdb 同一时刻只允许一个进程持有 ----
if [ -n "$QMT_RUNTIME_DB" ]; then
  DB_FILE="${QMT_RUNTIME_DB//\\//}"
else
  DB_FILE="data/db/qmt.duckdb"
fi
db_locked() {                       # 打不开（被别的进程独占）→ 返回 0
  [ -f "$DB_FILE" ] || return 1
  ! "$PY" -c "import sys
try:
    open(sys.argv[1], 'rb').read(16)
except OSError:
    sys.exit(1)
sys.exit(0)" "$DB_FILE" 2>/dev/null
}

# 若端口已被占用（例如上次的后端没退），连子进程树一起结束，
# 否则 spawn 出来的回测计算进程会变成孤儿、继续抱着 DuckDB 不放。
PIDS=$(netstat -ano 2>/dev/null | grep -E ":$PORT[[:space:]]" | grep LISTEN | awk '{print $5}' | sort -u)
for OLD in $PIDS; do
  echo "端口 $PORT 被 PID $OLD 占用，先终止旧进程（含子进程）..."
  taskkill //PID "$OLD" //T //F >/dev/null 2>&1 || true
done

# 端口已让出但仍握着库的孤儿后端（上次启动失败/被强杀后残留）也要清掉
ORPHANS=$("$PY" - <<'PYEOF' 2>/dev/null
try:
    import os, psutil
except Exception:
    raise SystemExit(0)
me = os.getpid()
for p in psutil.process_iter(["pid", "cmdline"]):
    try:
        if p.info["pid"] == me:
            continue
        if any("server.main:app" in a for a in (p.info["cmdline"] or [])):
            print(p.info["pid"])
    except Exception:
        pass
PYEOF
)
for OLD in $ORPHANS; do
  echo "发现残留后端进程 PID $OLD（未监听 $PORT 但持有 DuckDB），终止..."
  taskkill //PID "$OLD" //T //F >/dev/null 2>&1 || true
done

# 被强杀的进程释放几百 MB 的库文件句柄需要时间：等到锁真正放开再启
if [ -n "$PIDS$ORPHANS" ] || db_locked; then
  WAIT=0
  while db_locked && [ "$WAIT" -lt 30 ]; do
    sleep 1
    WAIT=$((WAIT + 1))
  done
  if db_locked; then
    echo "错误: $DB_FILE 仍被其他进程独占（DuckDB 只允许单进程持有），已等待 ${WAIT}s。"
    echo "      请结束持有它的 python 进程后重试，例如 PowerShell 里："
    echo "      Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object CommandLine -like '*server.main:app*' | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }"
    exit 1
  fi
  if [ "$WAIT" -gt 0 ]; then echo "数据库锁已释放（等待 ${WAIT}s）"; fi
fi

if [ "$1" = "--fg" ]; then
  echo "用 $PY 前台启动后端  http://0.0.0.0:$PORT  (Ctrl+C 停止)"
  exec "$PY" -m uvicorn server.main:app --host 0.0.0.0 --port "$PORT"
fi

echo "用 $PY 后台常驻启动后端  http://0.0.0.0:$PORT"
echo "===== $(date '+%F %T') backend restart =====" >> "$LOG"
# PYTHONUTF8=1 避免 Windows GBK 控制台写日志报错
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 nohup "$PY" -m uvicorn server.main:app \
  --host 0.0.0.0 --port "$PORT" >> "$LOG" 2>&1 &
disown || true

# 等待健康检查（最多 30 秒）
for i in $(seq 1 15); do
  sleep 2
  if "$PY" -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:$PORT/api/health', timeout=2).status==200 else 1)" 2>/dev/null; then
    echo "后端已就绪 ✔  http://127.0.0.1:$PORT （常驻调度器随进程启动）"
    echo "日志: $LOG"
    exit 0
  fi
done
echo "警告: 30 秒内未通过健康检查，请查看 $LOG"
exit 1
