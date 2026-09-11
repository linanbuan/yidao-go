FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GO_DOCS_ENABLED=false

# KataGo(Eigen/CPU 版) 运行时依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# 国内网络可用 --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple 换源。
# 这个 ARG 从前声明了却没用（审计 1.22），改了构建源也不生效。
ARG PIP_INDEX=https://pypi.org/simple
RUN pip install -i ${PIP_INDEX} -r requirements.txt

COPY app ./app
COPY katago ./katago
COPY scripts ./scripts
# tests/ 与 conftest.py **不进镜像**（审计 1.22）：运行镜像里没有 pytest，
# 把测试搬进去只是白增体积，还会让人以为镜像是"自带验收"的。
# 要跑测试用源码目录 + backend/.venv，与运行镜像无关。

# 构建期下载 KataGo（默认 CPU/Eigen 版，纯 CPU 也能跑）
# 不需要引擎时可： docker build --build-arg WITH_KATAGO=false ...
ARG WITH_KATAGO=true
ARG KATAGO_BACKEND=eigen
# 下载失败只警告，于是会出现"没有引擎的健康容器"——健康检查会把它暴露出来
# （/api/health 现在带 engine 状态，见 api/system.py）。
RUN if [ "$WITH_KATAGO" = "true" ]; then \
        python katago/download.py --backend ${KATAGO_BACKEND} || \
        echo "[warn] KataGo 下载失败，运行时将使用内置启发式引擎"; \
    fi

RUN mkdir -p data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
