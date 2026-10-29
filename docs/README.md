# 文档导航

本目录维护当前实现的说明。新同事先阅读快速上手，部署和配置细节以部署运行指南为准。

| 内容 | 文档 |
|---|---|
| 统一 Worker、多个目标/副本、线上 env 迁移 | [Worker 配置指南](worker-configuration.md) |
| Docker Compose 快速启动、更新 | [部署快速上手](deploy-quickstart.md) |
| 环境变量、平台切换、迁移、运行与故障排查 | [部署运行指南](deploy-and-run.md) |
| API / Worker 分层、队列、状态与恢复 | [当前架构](architecture.md) |
| Runner 配置、并发领取、统一启动与回滚 | [Runner 接入与部署](profile-runner-integration.md) |
| CustomerProfile 高代码邀约、独立入站密钥与启用步骤 | [原生邀约服务接入](native-invitation-runner.md) |
| 邀约执行标识、最终失败投递与回写确认 | [邀约状态同步](invitation-delivery.md) |
| 提交、列表和结果接口 | [前端接口指南](frontend-api-guide.md) |
| Ubuntu Docker 安装、宿主机网络和端口 | [Ubuntu 环境准备](docker-deploy-ubuntu24.md) |
| 任务查询、测试提交、Dify 手动探针 | [运维与调试脚本](../scripts/README.md) |
| 历史 Kubernetes Helm 安装、目标扩展与升级 | [Helm 历史参考](helm-deploy.md) |
| 历史原始 Kubernetes YAML 方式 | [旧 K8s 部署说明](k8s-deploy-guide.md) |
| 初始设计、阶段提示词、旧概览与 005 重试设计 | [历史归档](archive/README.md) |

契约以 `contracts/` 为准，数据库升级历史保留在 `db/migrations/`，协作约定维护在根目录 [AGENTS.md](../AGENTS.md)。历史归档中的参数、状态机和代码路径不作为现行操作依据。
