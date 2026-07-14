# customer_profile_backend

MVP 后端：客户画像任务提交、列表、结果查询；异步 Worker 调用 Dify 并回写 MySQL。

## 文档

| 文档 | 说明 |
|------|------|
| [docs/deploy-and-run.md](docs/deploy-and-run.md) | 部署、Docker Compose、本地启动、迁移、API curl 集成测试 |
| [docs/docker-deploy-ubuntu24.md](docs/docker-deploy-ubuntu24.md) | **Ubuntu 24.04 + Docker** 详细步骤、端口与防火墙、前端 Base URL |
| [docs/frontend-api-guide.md](docs/frontend-api-guide.md) | 前端调用三个业务接口的约定与示例 |

## 快速启动（本地）

```bash
pip install -e ".[dev]"
# 配置 .env 后：
customer-profile-api      # HTTP API
customer-profile-worker   # 队列消费者（另开终端）
```

## 快速启动（Docker）

```bash
docker compose up -d --build
```

默认将本机 **8000** 端口映射到 API（可用环境变量 `HOST_API_PORT` 修改，见部署文档）。
