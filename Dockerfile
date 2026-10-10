# HYT Container Load Planner —— 网站镜像（FastAPI 后端 + 静态前端，无需前端构建）
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 国内服务器可在 .env 中把 PIP_INDEX_URL 换成镜像源（如阿里云 / 清华）
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements-server.txt .
RUN pip install --index-url "${PIP_INDEX_URL}" -r requirements-server.txt

COPY packer_pro.py planner_core.py ./
COPY server/ server/
COPY web/ web/

# 以非 root 用户运行
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1

# 单进程即可：计算任务在独立子进程中并行执行（数量由 LOADER_MAX_RUNNING 控制）
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
