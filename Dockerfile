FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    CONFIG_DIR=/app/config \
    PORT=5005

# tzdata：任务的"运行星期"、每日签到都按本地时间算
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.title="kuakego-plus" \
      org.opencontainers.image.description="夸克网盘追更：多分享链接按序检查，只转存 Emby 里缺少的集数" \
      org.opencontainers.image.licenses="AGPL-3.0"

WORKDIR /app
COPY requirements.txt .
# 国内网络访问不了 pypi.org，默认走清华镜像；海外可在构建时覆盖：
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt
# 可选：GuessIt 文件名识别（内置规则解析不出时兜底）。装不上也不影响使用
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} guessit \
    || echo "WARNING: guessit 安装失败，将只使用内置的文件名解析规则"

COPY app ./app

VOLUME /app/config
EXPOSE 5005

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('PORT','5005'),timeout=3)"

CMD ["python", "-m", "app.main"]
