FROM docker.1ms.run/library/python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# fonts-wqy-microhei：标签渲染（订单码/机台码/物料码）要画中文，slim 镜像里
# 一个中文字体都没有，缺了会让三个标签接口全部 500。装不上也不阻断构建
# （下面用 || 兜住），否则一次镜像源抽风就会导致整站发不出去。
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && (apt-get install -y --no-install-recommends fonts-wqy-microhei \
        || echo "WARN: fonts-wqy-microhei 安装失败，标签渲染将缺中文字体") \
    && rm -rf /var/lib/apt/lists/*


COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
