# MVP: single image for API and Worker (same deps, different command).
ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

WORKDIR /app

# Ensure `import src` works (console scripts use src.*); belt-and-suspenders with setuptools below.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# 优化方案：
# 1. 先复制 pyproject.toml，从中提取并安装第三方依赖（这层会被缓存）
# 2. 再复制源码并安装项目本身

COPY pyproject.toml ./

ARG PIP_INDEX_URL=https://pypi.org/simple

# 升级 pip，然后安装第三方依赖（从 pyproject.toml 中提取）
RUN pip install --no-cache-dir --upgrade pip --index-url "${PIP_INDEX_URL}" \
    && pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
        "fastapi>=0.115.0" \
        "httpx>=0.28.0" \
        "loguru>=0.7.3" \
        "mysql-connector-python>=9.4.0" \
        "PyYAML>=6.0.1" \
        "python-multipart>=0.0.9" \
        "uvicorn>=0.31.0" \
        "python-dotenv>=1.0.1"

# 复制代码和其他资源
COPY src ./src
COPY contracts ./contracts
COPY db ./db

RUN mkdir -p /app/logs

# 安装项目本身（不重新安装依赖）
RUN pip install --no-cache-dir --no-deps . --index-url "${PIP_INDEX_URL}"

EXPOSE 18008

# Override with docker-compose: worker uses customer-profile-worker
CMD ["customer-profile-api"]
