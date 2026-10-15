# MVP: single image for API and Worker (same deps, different command).
ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

WORKDIR /app

# Ensure `import src` works (console scripts use src.*); belt-and-suspenders with setuptools below.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Install dependencies from the package metadata before copying application code.
# This layer stays cached when only source files change.
COPY pyproject.toml ./

ARG PIP_INDEX_URL=https://pypi.org/simple

RUN python -m pip install --no-cache-dir --upgrade pip --index-url "${PIP_INDEX_URL}" \
    && python -c 'import pathlib, subprocess, sys, tomllib; deps = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["dependencies"]; subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--index-url", sys.argv[1], *deps])' "${PIP_INDEX_URL}"

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
