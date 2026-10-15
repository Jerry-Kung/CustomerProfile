# customer_profile_backend

MVP 后端：客户画像任务提交、列表、结果查询；可扩展异步 Worker 调用 Dify、擎天或独立画像 Runner 并回写 MySQL。

## 文档

从 [文档导航](docs/README.md) 开始；历史设计和阶段材料统一保存在 [历史归档](docs/archive/README.md)。

| 文档 | 说明 |
|------|------|
| [部署快速上手](docs/deploy-quickstart.md) | 统一 Compose 启动、更新和日常命令 |
| [部署运行指南](docs/deploy-and-run.md) | 完整配置、本地开发、迁移与接口验证 |
| [Runner 接入与部署](docs/profile-runner-integration.md) | 画像生成服务、分流比例、升级与回滚 |
| [当前架构](docs/architecture.md) | API、队列、状态、执行目标和恢复 |
| [前端接口指南](docs/frontend-api-guide.md) | 提交、列表和结果查询 |
| [Ubuntu 环境准备](docs/docker-deploy-ubuntu24.md) | Docker 安装、宿主机网络和端口 |
| [Helm 部署与升级](docs/helm-deploy.md) | Kubernetes 配置生成、升级和回滚 |
| [运维与调试脚本](scripts/README.md) | 任务查询、测试提交与手动探针 |

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

默认将本机 **18008** 端口映射到 API（可用环境变量 `HOST_API_PORT` 修改，见部署文档）。

同一份 `docker-compose.yml` 会先执行迁移，再统一启动 API、普通 Worker、商业 Worker 和 `profile-runner-worker`。启动前填写 `.env` 中的 `PROFILE_RUNNER_BASE_URL_PRODUCTION`；共享的 `WORKFLOW_PLATFORM` 保持 `dify` 或 `qingtian`，Runner 服务会单独覆盖为 `runner`。

将部分画像任务分流到独立生成服务，参见 [Runner 接入与部署](docs/profile-runner-integration.md)。Runner Worker 已合入原 Compose，与旧 Worker 统一启停，使用原队列和重试机制，支持可配置的 80/20 首次分配；邀约继续使用原工作流。
