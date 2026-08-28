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

# 若端口已被占用（例如上次的后端没退），先把它结束掉再重启
PID=$(netstat -ano 2>/dev/null | grep -E ":$PORT[[:space:]]" | grep LISTEN | awk '{print $5}' | head -1)
if [ -n "$PID" ]; then
  echo "端口 $PORT 被 PID $PID 占用，先终止旧进程..."
  taskkill //PID "$PID" //F >/dev/null 2>&1 || true
  sleep 2
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
