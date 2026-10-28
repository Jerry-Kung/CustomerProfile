# Docker Compose 快速启动

当前 Worker 配置统一使用 qingtian / runner 两类平台。完整字段、旧 env 迁移和多目标示例见
[统一 Worker 配置](worker-configuration.md)。

```bash
cp .env.example .env
# 填写 MySQL、声明范围内所需的工作流凭据、Runner 地址、业务回调地址和 Key。
scripts/compose.sh config -q
scripts/compose.sh up -d --build
scripts/compose.sh logs -f worker commercial-worker profile-runner-worker
```

首次部署的迁移服务执行既有 001–008，成功后 API 与 Worker 启动。本次不新增迁移或改表。
默认 API 端口 18008；可独立设置
HOST_API_PORT（宿主机）、API_PORT（容器/本地监听）。/ready 检查数据库，失败返回 503。

默认普通擎天组接画像和邀约，商用组接画像，Runner 接生产画像；由空闲并发共同领取，不按权重。
并发是每个进程的数量，多个副本需要合计。单独邀请 Worker 可通过缩小某组任务范围配置。

更新已有 008 环境前先阅读[升级顺序](worker-configuration.md#重试与升级顺序)，保留旧目标身份，
统一替换消费者。--no-deps 跳过迁移服务；配置变更后需重建容器，restart 不会重新注入环境变量：

```bash
scripts/compose.sh up -d --build --force-recreate --no-deps api worker commercial-worker profile-runner-worker
```

使用外部环境文件时，通过 scripts/compose.sh --env-file /absolute/path/env 让插值和 env_file
使用同一份文件，不把生产文件复制或提交到仓库。
