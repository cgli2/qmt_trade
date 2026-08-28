# my_qmt_trade — 多阶段构建
# Stage 1: Node 构建 Vue 前端 → webui/dist
# Stage 2: Python slim 运行 FastAPI 后端（同源托管 dist，单端口 7099）
#
# 适用模式：sim / paper（纯 Python 依赖，可完整容器化）。
# live 实盘：xtquant 仅 Windows 可用，无法在 Linux 容器中连接 QMT 网关，
#            live 请在 Windows 宿主机按 docs/部署与使用说明.md 原生方式运行。

# ---------- Stage 1: 前端构建 ----------
FROM node:20-alpine AS webui-builder
WORKDIR /build
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui/ .
RUN npm run build

# ---------- Stage 2: Python 运行时 ----------
FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -sf /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY qmt_trade/ ./qmt_trade/
COPY server/ ./server/
COPY config/ ./config/
COPY --from=webui-builder /build/dist ./webui/dist

# 持久化目录（运行时用 volume 挂载，容器重建不丢账本/缓存）
RUN mkdir -p data logs reports

EXPOSE 7099
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7099/api/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "7099"]
