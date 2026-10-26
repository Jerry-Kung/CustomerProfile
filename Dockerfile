# Customer Profile —— 运行镜像
#
# 一个镜像承载三个进程（API / worker / inbound），具体命令由 docker-compose 指定。
# 三份几乎相同的镜像各自构建会互相漂移，因此只在 compose 里换 command。
#
# 构建（在仓库根目录）：
#     docker build -t customer-profile:latest .
#
# Python 依赖优先走清华 PyPI 镜像，不可用时回退官方 PyPI。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# 探针使用 Python 标准库；基础镜像已提供 HTTPS CA，不额外安装 curl 或 ffmpeg。
# ---- 1. Python 依赖：pip 换清华源 ----------------------------------------
# 先装依赖再拷源码：源码改动不会让这一层缓存失效。
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    || python -m pip install --no-cache-dir -r requirements.txt \
      --index-url https://pypi.org/simple

# ---- 2. 代码与运行期资源 --------------------------------------------------
# src 布局不打包安装，靠上面的 PYTHONPATH=/app/src 让 `python -m customer_profile.*` 可导入。
COPY src ./src

# 运行台的构建产物随仓库入库（frontend/dist 的 git 例外在 .gitignore 里），
# 因此镜像内不做 npm 构建。改了前端要在构建镜像**之前**跑 `cd frontend && npm run build`。
COPY frontend/dist ./frontend/dist

# REPLAY_MODE=fixture 的固定响应是运行期数据（不只是测试夹具），缺了它该模式在容器里
# 必失败。tests/ 其余部分不进镜像。
COPY tests/fixtures/replay/human_corrected_info.json ./tests/fixtures/replay/human_corrected_info.json

# ---- 3. 数据目录与运行用户 ------------------------------------------------
# /app/data 是 SQLite 与运行产物的落位（settings.PROJECT_ROOT=/app，DATA_DIR=data）。
# 必须以**本地卷**挂载：WAL 依赖共享内存（-shm），网络文件系统上会锁失效并可能损坏库。
RUN groupadd --gid 10001 customer-profile \
    && useradd --uid 10001 --gid 10001 --no-log-init --create-home customer-profile \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data
USER 10001:10001

# 18173 内部只读面（运行台与查询，实际端口由 API_PORT 决定），8099 对外写面（接收任务队列推送）。
# EXPOSE 只是声明，不改变容器内实际的监听端口；换端口改 .env 的 API_PORT 即可。
EXPOSE 18173 8099

# 缺省起 API；worker 与 inbound 由 compose 覆盖 command。
CMD ["python", "-m", "customer_profile.main"]
