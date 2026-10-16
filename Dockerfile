# Customer Profile —— 运行镜像
#
# 一个镜像承载三个进程（API / worker / inbound），具体命令由 docker-compose 指定。
# 三份几乎相同的镜像各自构建会互相漂移，因此只在 compose 里换 command。
#
# 构建（在仓库根目录）：
#     docker build -t customer-profile:latest .
#
# 依赖源统一走清华镜像（apt 与 pip），替换方式取自 Dockerfile.example。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# ---- 1. apt：换清华源 + 装运行期系统依赖 ----------------------------------
#   curl              —— 容器健康检查与排障（compose 的 healthcheck 用它）
#   ca-certificates   —— 对外 HTTPS 必需（LLM / 火山 AUC / mhero）
#
# 不装 ffmpeg（Dockerfile.example 里有，这里是照抄时会误带的一项）：本项目音频走
# 云端 URL，仓库内无 ffmpeg / ffprobe / pydub 调用，装了只增体积。
#
# Debian 12 起源定义在 deb822 格式的 debian.sources；同时兼容旧式 sources.list，
# 以免将来换基础镜像标签时替换静默失效（换不上源会退化成官方源，慢但不出声）。
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i \
            -e 's|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
            -e 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i \
            -e 's|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
            -e 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
            /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# ---- 2. Python 依赖：pip 换清华源 ----------------------------------------
# 先装依赖再拷源码：源码改动不会让这一层缓存失效。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# ---- 3. 代码与运行期资源 --------------------------------------------------
# src 布局不打包安装，靠上面的 PYTHONPATH=/app/src 让 `python -m customer_profile.*` 可导入。
COPY src ./src

# 运行台的构建产物随仓库入库（frontend/dist 的 git 例外在 .gitignore 里），
# 因此镜像内不做 npm 构建。改了前端要在构建镜像**之前**跑 `cd frontend && npm run build`。
COPY frontend/dist ./frontend/dist

# REPLAY_MODE=fixture 的固定响应是运行期数据（不只是测试夹具），缺了它该模式在容器里
# 必失败。tests/ 其余部分不进镜像。
COPY tests/fixtures/replay ./tests/fixtures/replay

# ---- 4. 数据目录与运行用户 ------------------------------------------------
# /app/data 是 SQLite 与运行产物的落位（settings.PROJECT_ROOT=/app，DATA_DIR=data）。
# 必须以**本地卷**挂载：WAL 依赖共享内存（-shm），网络文件系统上会锁失效并可能损坏库。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

# 8000 内部只读面（运行台与查询），8099 对外写面（接收任务队列推送）。
EXPOSE 8000 8099

# 缺省起 API；worker 与 inbound 由 compose 覆盖 command。
CMD ["python", "-m", "customer_profile.main"]
