# customer_profile_backend

客户画像异步后端：客户画像任务提交、列表、结果查询；可扩展异步 Worker 调用擎天或独立画像 Runner（保留 Dify 历史兼容） 并回写 MySQL。

## 文档

从 [文档导航](docs/README.md) 开始；历史设计和阶段材料统一保存在 [历史归档](docs/archive/README.md)。

| 文档 | 说明 |
|------|------|
| [统一 Worker 配置](docs/worker-configuration.md) | 消费范围、并发、多目标、配置迁移与升级 |
| [部署快速上手](docs/deploy-quickstart.md) | 统一 Compose 启动、更新和日常命令 |
| [部署运行指南](docs/deploy-and-run.md) | 完整配置、本地开发、迁移与接口验证 |
| [Runner 接入与部署](docs/profile-runner-integration.md) | 画像生成服务、任务分工、升级与回滚 |
| [当前架构](docs/architecture.md) | API、队列、状态、执行目标和恢复 |
| [前端接口指南](docs/frontend-api-guide.md) | 提交、列表和结果查询 |
| [Ubuntu 环境准备](docs/docker-deploy-ubuntu24.md) | Docker 安装、宿主机网络和端口 |
| [Helm 历史参考](docs/helm-deploy.md) | 保留的 Kubernetes 部署方案，本次未验证 |
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

同一份 Compose 会先迁移，再启动 API 和多个统一 Worker。主线支持 `qingtian`、`runner` 两种平台；每个组独立配置任务范围、环境、目标身份、并发和模式。商用也是普通擎天目标，旧工作流与新服务可按空闲并发共同消费画像；邀请可以配置独立组。

新任务默认同目标重试，远端运行可由相同目标的其他副本恢复。增加不同目标或扩容副本无需新增业务执行代码。配置来源、现有线上 env 迁移、009 升级保护和多目标示例统一见 [Worker 配置指南](docs/worker-configuration.md)。

使用外部环境文件时推荐 `scripts/compose.sh --env-file /absolute/path/env`，让 Compose 插值和容器注入使用同一份文件。API 和迁移不再要求工作流凭据，也不初始化未使用的上传目录。
