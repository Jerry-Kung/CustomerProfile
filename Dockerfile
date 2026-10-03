FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt

COPY src ./src
COPY frontend/dist ./frontend/dist

RUN groupadd --gid 10001 customer-profile \
    && useradd --uid 10001 --gid 10001 --no-log-init --create-home customer-profile \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data

USER 10001:10001

EXPOSE 8000

CMD ["python", "-m", "customer_profile.main"]
