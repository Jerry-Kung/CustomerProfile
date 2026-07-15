# K8s 部署快速上手

本文说明如何把 Customer Profile Backend 部署到 Kubernetes。当前 K8s 方案包含：

- `Job/customer-profile-migrate`：一次性执行数据库迁移。
- `Deployment/customer-profile-api`：HTTP API，可横向扩容。
- `Service/customer-profile-api`：集群内访问入口，`80 -> 18008`。
- `Deployment/customer-profile-worker`：后台队列消费者，固定单副本。
- `ConfigMap/customer-profile-config`：非敏感配置与常用调参项。
- `Secret/customer-profile-secret`：MySQL 密码、工作流 API Key 等敏感配置。
- `PVC/customer-profile-task-uploads`：API 与 Worker 共享上传目录。
- `PVC/customer-profile-logs`：API、Worker 与迁移任务共享持久化日志目录。

## 1. 前置条件

- K8s 节点能运行 `linux/arm64` 镜像。
- 集群能访问镜像仓库 `registry.dfmc.com.cn`。
- 集群 Pod 能访问外部 MySQL、Dify 或 QingTian。
- MySQL 数据库已创建，例如 `customer_profile`。
- 集群存在可用 StorageClass，并支持 `ReadWriteMany` PVC。

> 如果集群没有 `ReadWriteMany` 存储，`customer-profile-task-uploads` 和 `customer-profile-logs` 会一直 Pending。生产建议准备共享文件存储；如果确认不依赖共享，可再按实际情况改为每 Pod 独立存储。

## 2. 文件说明

```text
Dockerfile
deploy/k8s/build-arm64-image.sh
deploy/k8s/customer-profile-backend.aizt01-pro.yaml
deploy/k8s/customer-profile-backend.local-test.yaml
```

`customer-profile-backend.aizt01-pro.yaml` 是生产/测试集群部署文件，目标 namespace 为 `aizt01-pro`。

`customer-profile-backend.local-test.yaml` 只用于 Docker Desktop K8s 本地冒烟测试：

- 使用本地镜像 `customer-profile-backend:k8s-local`
- 只启动 API，不启动 worker，不执行迁移
- 使用 `emptyDir`，不依赖集群共享存储

## 3. 构建镜像

本地构建并加载到 Docker：

```bash
MODE=load \
IMAGE=registry.dfmc.com.cn/mhero-ai/customer-profile-backend:arm64-latest \
./deploy/k8s/build-arm64-image.sh
```

推送到镜像仓库：

```bash
docker login registry.dfmc.com.cn

MODE=push \
IMAGE=registry.dfmc.com.cn/mhero-ai/customer-profile-backend:arm64-latest \
./deploy/k8s/build-arm64-image.sh
```

如需使用内网 PyPI 源：

```bash
PIP_INDEX_URL=https://your-pypi-mirror/simple \
MODE=push \
IMAGE=registry.dfmc.com.cn/mhero-ai/customer-profile-backend:arm64-latest \
./deploy/k8s/build-arm64-image.sh
```

## 4. 用 tar 包转移镜像

在本机导出：

```bash
docker save -o dist/customer-profile-backend-arm64-latest.tar \
  registry.dfmc.com.cn/mhero-ai/customer-profile-backend:arm64-latest
```

把 tar 复制到能访问镜像仓库的服务器后执行：

```bash
docker load -i customer-profile-backend-arm64-latest.tar

docker images | grep customer-profile-backend

docker login registry.dfmc.com.cn

docker push registry.dfmc.com.cn/mhero-ai/customer-profile-backend:arm64-latest
```

如果导入后的 tag 不符合预期，先重新打 tag：

```bash
docker tag <IMAGE_ID_OR_OLD_TAG> \
  registry.dfmc.com.cn/mhero-ai/customer-profile-backend:arm64-latest

docker push registry.dfmc.com.cn/mhero-ai/customer-profile-backend:arm64-latest
```

## 5. 本地 Docker Desktop K8s 验证

先给本地测试打一个短 tag：

```bash
docker tag \
  registry.dfmc.com.cn/mhero-ai/customer-profile-backend:arm64-latest \
  customer-profile-backend:k8s-local
```

部署本地测试 YAML：

```bash
kubectl create namespace customer-profile-local --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/k8s/customer-profile-backend.local-test.yaml
kubectl -n customer-profile-local rollout status deployment/customer-profile-api
```

本地访问：

```bash
kubectl -n customer-profile-local port-forward svc/customer-profile-api 18088:80
curl http://127.0.0.1:18088/health
```

如果返回：

```json
{"status":"ok","db_status":"down"}
```

说明 API 在 K8s 内已启动，但 Docker Desktop Pod 访问不到 `.env` 里的 MySQL。只要部署到目标集群后 Pod 能访问 MySQL，`db_status` 应变为 `up`。

清理本地测试：

```bash
kubectl delete namespace customer-profile-local
```

## 6. 部署到 K8s

生产 YAML 先做服务端校验：

```bash
kubectl apply --dry-run=server -f deploy/k8s/customer-profile-backend.aizt01-pro.yaml
```

首次部署：

```bash
kubectl apply -f deploy/k8s/customer-profile-backend.aizt01-pro.yaml
```

查看迁移：

```bash
kubectl -n aizt01-pro get job customer-profile-migrate
kubectl -n aizt01-pro logs job/customer-profile-migrate
```

查看服务：

```bash
kubectl -n aizt01-pro get pods
kubectl -n aizt01-pro rollout status deployment/customer-profile-api
kubectl -n aizt01-pro rollout status deployment/customer-profile-worker
```

临时访问 API：

```bash
kubectl -n aizt01-pro port-forward svc/customer-profile-api 18008:80
curl http://127.0.0.1:18008/health
```

对外访问还需要额外配置 Ingress、Gateway 或负载均衡 Service。

## 7. 更新服务

推荐每次发布使用明确 tag，例如：

```bash
IMAGE_TAG=20260707-001

MODE=push \
IMAGE=registry.dfmc.com.cn/mhero-ai/customer-profile-backend:${IMAGE_TAG} \
./deploy/k8s/build-arm64-image.sh
```

更新 API 和 Worker 镜像：

```bash
kubectl -n aizt01-pro set image deployment/customer-profile-api \
  container-customer-profile-api=registry.dfmc.com.cn/mhero-ai/customer-profile-backend:${IMAGE_TAG}

kubectl -n aizt01-pro set image deployment/customer-profile-worker \
  container-customer-profile-worker=registry.dfmc.com.cn/mhero-ai/customer-profile-backend:${IMAGE_TAG}
```

观察发布：

```bash
kubectl -n aizt01-pro rollout status deployment/customer-profile-api
kubectl -n aizt01-pro rollout status deployment/customer-profile-worker
```

如果本次包含数据库迁移，先删除旧 Job，再重新 apply：

```bash
kubectl -n aizt01-pro delete job customer-profile-migrate --ignore-not-found
kubectl apply -f deploy/k8s/customer-profile-backend.aizt01-pro.yaml
kubectl -n aizt01-pro logs job/customer-profile-migrate
```

## 8. 后续调参方式

常用调参项已经集中在 `ConfigMap/customer-profile-config`：

| 参数 | 影响范围 | 说明 |
|---|---|---|
| `WORKER_CONCURRENCY` | worker | 单个 worker 进程内并发数。调大提升吞吐，也会增加 MySQL 和工作流平台压力。 |
| `TASK_QUEUE_MAX_WAITING` | api | 队列最大等待任务数，超过后提交接口返回队列满。 |
| `LOG_LEVEL` | api / worker | 日志级别，排查问题时可临时调为 `DEBUG`。 |
| `LOG_DIR` | api / worker / migrate | Loguru 文件目录，K8s 中为 `/app/logs`。 |
| `LOG_ROTATION` | api / worker / migrate | 文件轮转策略，默认每天零点。 |
| `LOG_RETENTION` | api / worker / migrate | 日志保留时间，默认 30 天。 |
| `DIFY_POLL_TIMEOUT_MS` | worker | 等待 Dify 工作流完成的超时。 |
| `QINGTIAN_POLL_TIMEOUT_MS` | worker | 等待 QingTian 工作流完成的超时。 |
| `MIGRATION_DB_WAIT_MAX_TRIES` | migrate | 迁移等待 MySQL 可用的重试次数。 |
| `MIGRATION_DB_WAIT_INTERVAL_MS` | migrate | 迁移等待 MySQL 的重试间隔。 |

编辑 ConfigMap：

```bash
kubectl -n aizt01-pro edit configmap customer-profile-config
```

让新配置生效：

```bash
kubectl -n aizt01-pro rollout restart deployment/customer-profile-api
kubectl -n aizt01-pro rollout restart deployment/customer-profile-worker
```

只改 worker 并发时，只需要重启 worker：

```bash
kubectl -n aizt01-pro rollout restart deployment/customer-profile-worker
```

API 副本数可以直接扩容：

```bash
kubectl -n aizt01-pro scale deployment/customer-profile-api --replicas=3
```

Worker 默认固定 `replicas: 1`，不要水平扩容。它会从 MySQL 队列表中领取任务，虽然代码使用 `SKIP LOCKED` 支持并发抢占，但当前部署约定是通过 `WORKER_CONCURRENCY` 调吞吐，不通过增加 worker Pod 调吞吐。

## 9. 回滚

查看发布历史：

```bash
kubectl -n aizt01-pro rollout history deployment/customer-profile-api
kubectl -n aizt01-pro rollout history deployment/customer-profile-worker
```

回滚：

```bash
kubectl -n aizt01-pro rollout undo deployment/customer-profile-api
kubectl -n aizt01-pro rollout undo deployment/customer-profile-worker
```

回滚后再次确认：

```bash
kubectl -n aizt01-pro get pods
kubectl -n aizt01-pro logs deployment/customer-profile-api --tail=100
kubectl -n aizt01-pro logs deployment/customer-profile-worker --tail=100
```

## 10. 注意事项

- `customer-profile-backend.aizt01-pro.yaml` 当前包含真实配置和密钥，不要提交到公共仓库或外发。
- 生产建议尽快接入外部密钥系统、Sealed Secrets 或 External Secrets。
- `customer-profile-api` 可以扩容；`customer-profile-worker` 保持单副本。
- `Job/customer-profile-migrate` 是幂等迁移，但 Job 的 Pod 模板更新是不可变的，重复发布前建议先删除旧 Job。
- API readiness 会检查 `/health` 且要求 DB 可用；liveness 只检查端口，避免 MySQL 短暂故障导致 API 被反复重启。
