# Helm 部署与升级

本文说明如何在 `aizt01-pro` 部署可扩展的擎天 Worker。当前目标拓扑是 1 条本地工作流
+ 3 条商用工作流；以后增减工作流只修改 Helm values，不需要再增加固定模板。

## 1. Chart 模式

固定资源包括 API、Service、迁移 Job、ConfigMap、公共/本地/商用 Secret，以及两个
通过 `existingClaim` 复用的 PVC：

- `customer-profile-logs`
- `customer-profile-task-uploads`

Worker 有两种渲染模式：

| values | Worker 拓扑 | 消费/重试策略 | 用途 |
|---|---|---|---|
| `workflowTargets: []` | 旧版 1 本地 + 1 商用 | `local_preferred` / `local_first_v1` | 接管旧环境 |
| `values.workflow-targets.example.yaml` | 1 本地 + 3 商用 | `parallel` / `throughput_first_v1` | 当前最终拓扑 |
| `values.workflow-targets.transition.example.yaml` | 2 个旧恢复目标 + 新 1+3 | 新任务并行；旧目标只恢复 | 旧环境无损切换 |

最终 4 个 active Worker Deployment：

- `customer-profile-worker-local-01`
- `customer-profile-worker-commercial-01`
- `customer-profile-worker-commercial-02`
- `customer-profile-worker-commercial-03`

`workflowTargets[].id` 是数据库中的稳定目标标识。不同 AppKey 必须使用不同 ID；同一
目标扩容副本数或并发数时保持 ID 不变。

## 2. 私密配置和额外目标 Secret

从现有 1+1 生产 YAML 生成私密 values：

```bash
python3 scripts/generate_helm_production_values.py \
  --source-dir /path/to/exported-production-yaml \
  --image-tag <不可变镜像标签>
```

输出文件：

```text
deploy/helm/customer-profile-backend/values.production.private.yaml
```

该文件包含数据库密码和已有本地/商用密钥，权限为 `0600` 且已被 Git 忽略。生成脚本
会校验 API、本地 Worker、商用 Worker 和迁移任务之间的数据库、工作流密钥及镜像
关系。

新增加的两条商用目标使用集群中预先存在的 Secret：

```bash
kubectl -n aizt01-pro create secret generic customer-profile-workflow-commercial-02 \
  --from-literal=QINGTIAN_API_KEY_TEST='...' \
  --from-literal=QINGTIAN_API_KEY_PRODUCTION='...'

kubectl -n aizt01-pro create secret generic customer-profile-workflow-commercial-03 \
  --from-literal=QINGTIAN_API_KEY_TEST='...' \
  --from-literal=QINGTIAN_API_KEY_PRODUCTION='...'
```

不要把真实值写入示例 overlay。Secret 名称可修改，但必须同步修改对应目标的
`secretName`。

## 3. 校验和构建

构建脚本支持重复传入 `--values`，按出现顺序合并。

最终 1+3 安装包：

```bash
python3 scripts/build_helm_package.py \
  --values deploy/helm/customer-profile-backend/values.production.private.yaml \
  --values deploy/helm/customer-profile-backend/values.workflow-targets.example.yaml \
  --version <Chart版本>
```

迁移期安装包把第二个 overlay 换成：

```text
deploy/helm/customer-profile-backend/values.workflow-targets.transition.example.yaml
```

脚本会检查占位符、执行 `helm lint` 和 `helm template`，但不会把 Secret 清单打印到
终端。输出位于 `dist/helm/`，文件权限为 `0600`。包内包含已有生产 Secret，必须按
密钥文件管理；`commercial-02/03` 的外部 Secret 仍须预先存在。

也可以只做不泄密的手工校验：

```bash
helm lint deploy/helm/customer-profile-backend \
  -f deploy/helm/customer-profile-backend/values.production.private.yaml \
  -f deploy/helm/customer-profile-backend/values.workflow-targets.example.yaml

helm template customer-profile-backend \
  deploy/helm/customer-profile-backend \
  -n aizt01-pro \
  -f deploy/helm/customer-profile-backend/values.production.private.yaml \
  -f deploy/helm/customer-profile-backend/values.workflow-targets.example.yaml \
  >/dev/null
```

## 4. 全新环境安装

确认 kubectl context、namespace 和两个额外 Secret 后执行：

```bash
helm upgrade --install customer-profile-backend \
  dist/helm/customer-profile-backend-<Chart版本>.tgz \
  -n aizt01-pro \
  --atomic \
  --wait \
  --timeout 15m
```

`customer-profile-migrate` 是 `pre-install,pre-upgrade` hook。它会先执行包括
`005_add_qingtian_retry_fields.sql` 和
`006_add_workflow_target_routing.sql` 在内的全部幂等迁移；成功后 API 和 Worker 才会
更新。

## 5. 从正在运行的旧版 1+1 环境切换

不要直接从旧清单切到最终 1+3 overlay。006 会把当时正在运行或等待重试的旧任务绑定
到 `local-default` / `commercial-default`；直接删除这两个目标会让旧 `runId` 无人
恢复。

### 5.1 首次 Helm 接管

备份原资源，并给现有三个 Deployment 和 Service 增加 Helm 归属：

```bash
RELEASE_NAME=customer-profile-backend
TARGET_NAMESPACE=aizt01-pro

kubectl -n "$TARGET_NAMESPACE" get \
  deployment/customer-profile-backend \
  deployment/customer-profile-worker \
  deployment/customer-profile-worker-secondary \
  service/customer-profile-backend \
  job/customer-profile-migrate \
  -o yaml > customer-profile-backend-before-helm.yaml

kubectl -n "$TARGET_NAMESPACE" label \
  deployment/customer-profile-backend \
  deployment/customer-profile-worker \
  deployment/customer-profile-worker-secondary \
  service/customer-profile-backend \
  app.kubernetes.io/managed-by=Helm \
  --overwrite

kubectl -n "$TARGET_NAMESPACE" annotate \
  deployment/customer-profile-backend \
  deployment/customer-profile-worker \
  deployment/customer-profile-worker-secondary \
  service/customer-profile-backend \
  meta.helm.sh/release-name="$RELEASE_NAME" \
  meta.helm.sh/release-namespace="$TARGET_NAMESPACE" \
  --overwrite

kubectl -n "$TARGET_NAMESPACE" delete job/customer-profile-migrate \
  --ignore-not-found
```

不要接管两个 PVC。先使用仅含私密 values 的旧版兼容包完成 006 和新版代码升级：

```bash
python3 scripts/build_helm_package.py \
  --values deploy/helm/customer-profile-backend/values.production.private.yaml \
  --version <兼容版Chart版本>

helm upgrade --install "$RELEASE_NAME" \
  dist/helm/customer-profile-backend-<兼容版Chart版本>.tgz \
  -n "$TARGET_NAMESPACE" \
  --atomic \
  --wait \
  --timeout 15m
```

此时两个 Worker 的目标 ID 分别是 `local-default` 和 `commercial-default`，可以继续
恢复迁移前创建的运行。

### 5.2 启动过渡拓扑

创建 `commercial-02/03` Secret，构建并部署 transition overlay。该 overlay 将两个
旧 Deployment 改为 `recovery_only`，同时启动新的 1+3 active 目标：

```bash
python3 scripts/build_helm_package.py \
  --values deploy/helm/customer-profile-backend/values.production.private.yaml \
  --values deploy/helm/customer-profile-backend/values.workflow-targets.transition.example.yaml \
  --version <过渡版Chart版本>

helm upgrade customer-profile-backend \
  dist/helm/customer-profile-backend-<过渡版Chart版本>.tgz \
  -n aizt01-pro \
  --atomic \
  --wait \
  --timeout 15m
```

旧目标不再领取未绑定任务，只会恢复已绑定到自己的存量 `runId`。

### 5.3 清理旧恢复目标

通过 MySQL 检查旧目标是否还有活动任务：

```sql
SELECT workflow_target_id, status, COUNT(*) AS task_count
FROM customer_profile_tasks
WHERE workflow_target_id IN ('local-default', 'commercial-default')
  AND status IN ('processing', 'retry_wait')
GROUP BY workflow_target_id, status;
```

查询连续为空后，使用最终 overlay 再升级一次。Helm 会删除两个旧恢复 Deployment，
最终只保留 1+3：

```bash
python3 scripts/build_helm_package.py \
  --values deploy/helm/customer-profile-backend/values.production.private.yaml \
  --values deploy/helm/customer-profile-backend/values.workflow-targets.example.yaml \
  --version <最终版Chart版本>

helm upgrade customer-profile-backend \
  dist/helm/customer-profile-backend-<最终版Chart版本>.tgz \
  -n aizt01-pro \
  --atomic \
  --wait \
  --timeout 15m
```

## 6. 部署后验证

检查迁移、API 和所有目标：

```bash
helm status customer-profile-backend -n aizt01-pro

kubectl -n aizt01-pro logs job/customer-profile-migrate

kubectl -n aizt01-pro get deployment \
  -l app.kubernetes.io/component=worker

kubectl -n aizt01-pro rollout status \
  deployment/customer-profile-backend \
  --timeout=5m

for target in \
  customer-profile-worker-local-01 \
  customer-profile-worker-commercial-01 \
  customer-profile-worker-commercial-02 \
  customer-profile-worker-commercial-03
do
  kubectl -n aizt01-pro rollout status "deployment/$target" --timeout=5m
done
```

不输出密钥地核对每个 Worker 的身份和策略：

```bash
for target in \
  customer-profile-worker-local-01 \
  customer-profile-worker-commercial-01 \
  customer-profile-worker-commercial-02 \
  customer-profile-worker-commercial-03
do
  kubectl -n aizt01-pro exec "deployment/$target" -- \
    sh -c 'printf "%s %s %s %s %s\n" \
      "$WORKER_ROLE" "$WORKFLOW_TARGET_ID" "$WORKFLOW_TARGET_REVISION" \
      "$WORKER_TARGET_MODE" "$RETRY_POLICY_VERSION"'
done
```

期望四个 ID 唯一、mode 为 `active`、策略为 `throughput_first_v1`。

检查 API：

```bash
kubectl -n aizt01-pro port-forward \
  service/customer-profile-backend 18008:18008

curl -fsS http://127.0.0.1:18008/health
```

提交测试任务后，应确认：

- 新任务可以被四个 active 目标共同消费。
- 同一个任务不会被两个 Worker 重复领取。
- 已持久化 `runId` 只由原 `workflow_target_id` 轮询。
- 明确失败后的下一次新运行使用尚未尝试过的目标，总运行次数不超过 3。
- 同一 `runId` 首次轮询后最多再恢复 3 次，且总观察时长不超过 7200 秒。

## 7. 扩缩容和密钥轮换

- 同一目标只增加处理并发：调整 `replicas` 或 `concurrency`，ID/revision 不变。
- 新增 AppKey/工作流：追加新的唯一目标 ID 和 Secret。
- 暂停接收未绑定任务：把目标设为 `draining` 或 `recovery_only`。
- 目标连续 3 次 HTTP/传输失败会自动暂停未绑定领取 60 秒；通过
  `WORKER_TARGET_FAILURE_THRESHOLD` / `WORKER_TARGET_COOLDOWN_SECONDS` 调整。
- AppKey 轮换：不要原地覆盖 Secret。保留旧 ID/revision 的 recovery Worker，新 Key
  使用新 ID；存量 `runId` 清空后再移除旧目标。
- 从 `parallel` 切回 `local_preferred` 前，先让旧
  `throughput_first_v1` 的 `retry_wait` 清空，再决定是否下线商用目标。

## 8. 回滚与安全

```bash
helm history customer-profile-backend -n aizt01-pro

helm rollback customer-profile-backend <REVISION> \
  -n aizt01-pro \
  --wait \
  --timeout 15m
```

Helm 回滚不会回退 005/006 数据库结构。回滚拓扑前先确认被删除目标没有绑定中的
`processing` / `retry_wait`，否则应保留对应 recovery Worker。

- 不要提交 `values.production.private.yaml` 或生产 `.tgz`。
- 不要在聊天、工单或终端输出渲染后的 Secret。
- 不要让 Helm 接管现有 PVC；卸载 release 时保留业务数据。
- `helm uninstall` 会删除 Helm 管理的 Secret，但不会删除通过 `existingClaim` 引用
  的两个 PVC，也不会删除预先创建的 `commercial-02/03` 外部 Secret。
