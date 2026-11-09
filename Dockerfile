# One image, separate Python environments for workflow execution and task dispatch.
ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    PATH=/opt/runtime-venv/bin:$PATH

WORKDIR /app

# Install each component's own dependency declarations before copying source.
COPY requirements.txt /tmp/runtime-requirements.txt
COPY services/task-backend/pyproject.toml /tmp/task-pyproject.toml
RUN python -m venv /opt/runtime-venv \
    && python -m venv /opt/task-venv \
    && python -c 'import pathlib,tomllib; p=tomllib.loads(pathlib.Path("/tmp/task-pyproject.toml").read_text()); pathlib.Path("/tmp/task-requirements.txt").write_text("\n".join(p["build-system"]["requires"] + p["project"]["dependencies"]) + "\n")'
RUN /opt/runtime-venv/bin/pip install -r /tmp/runtime-requirements.txt --index-url "${PIP_INDEX_URL}" \
    || /opt/runtime-venv/bin/pip install -r /tmp/runtime-requirements.txt --index-url https://pypi.org/simple
RUN /opt/task-venv/bin/pip install -r /tmp/task-requirements.txt --index-url "${PIP_INDEX_URL}" \
    || /opt/task-venv/bin/pip install -r /tmp/task-requirements.txt --index-url https://pypi.org/simple

# ---- 2. 代码与运行期资源 --------------------------------------------------
# src 布局不打包安装，靠上面的 PYTHONPATH=/app/src 让 `python -m customer_profile.*` 可导入。
COPY src ./src

# 运行台的构建产物随仓库入库（frontend/dist 的 git 例外在 .gitignore 里），
# 因此镜像内不做 npm 构建。改了前端要在构建镜像**之前**跑 `cd frontend && npm run build`。
COPY frontend/dist ./frontend/dist

# REPLAY_MODE=fixture 的固定响应是运行期数据（不只是测试夹具），缺了它该模式在容器里
# 必失败。tests/ 其余部分不进镜像。
COPY tests/fixtures/replay/human_corrected_info.json ./tests/fixtures/replay/human_corrected_info.json

COPY services/task-backend/pyproject.toml ./services/task-backend/pyproject.toml
COPY services/task-backend/src ./services/task-backend/src
COPY services/task-backend/contracts ./services/task-backend/contracts
COPY services/task-backend/db ./services/task-backend/db
RUN /opt/task-venv/bin/pip install --no-deps --no-build-isolation ./services/task-backend
COPY --chmod=755 scripts/container-entrypoint.sh /usr/local/bin/customer-profile-entrypoint

# ---- 3. 数据目录与运行用户 ------------------------------------------------
# /app/data 是 SQLite 与运行产物的落位（settings.PROJECT_ROOT=/app，DATA_DIR=data）。
# 必须以**本地卷**挂载：WAL 依赖共享内存（-shm），网络文件系统上会锁失效并可能损坏库。
RUN groupadd --gid 10001 customer-profile \
    && useradd --uid 10001 --gid 10001 --no-log-init --create-home customer-profile \
    && mkdir -p /app/data /app/logs \
    && chown 10001:10001 /app/data /app/logs
ENV LOG_DIR=/app/logs
USER 10001:10001

# 18173 内部只读面（运行台与查询，实际端口由 API_PORT 决定），8099 对外写面（接收任务队列推送）。
# EXPOSE 只是声明，不改变容器内实际的监听端口；换端口改 .env 的 API_PORT 即可。
EXPOSE 18008 18173 8099

ARG BUILD_REVISION=unknown
LABEL org.opencontainers.image.revision=${BUILD_REVISION} \
      io.customer-profile.layout=v1

# 缺省起 API；worker 与 inbound 由 compose 覆盖 command。
ENTRYPOINT ["/usr/local/bin/customer-profile-entrypoint"]
CMD ["python", "-m", "customer_profile.main"]
