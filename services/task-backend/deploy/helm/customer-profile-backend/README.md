# Customer Profile Backend Helm Chart

该 Chart 管理以下工作负载：

- API：`Deployment/customer-profile-backend` 与同名 `Service`
- 可扩展目标 Worker：由 `workflowTargets` 列表生成任意数量的 Deployment
- 数据库迁移：`Job/customer-profile-migrate`
- 公共、本地工作流、商用工作流三组 Secret

数据库迁移 Job 是 `pre-install,pre-upgrade` Helm hook。迁移成功后才会继续安装或升级业务工作负载。

`workflowTargets: []` 时保留旧版 1 本地 + 1 商用 Deployment，默认使用
`local_preferred` + `local_first_v1`，用于平滑接管旧环境。当前 1 本地 + 3 商用布局使用
仓库中的 `values.workflow-targets.example.yaml` 覆盖层，切换为
`parallel` + `throughput_first_v1`。

从正在运行的旧版环境切换时，先使用
`values.workflow-targets.transition.example.yaml`：它保留
`local-default` / `commercial-default` 两个 `recovery_only` 目标恢复存量 `runId`，
同时启动新的 1+3 active 目标。存量清空后再换成最终覆盖层。

生产环境默认复用已有的两个 PVC，不创建或接管它们：

- `customer-profile-logs`
- `customer-profile-task-uploads`

## Values 分层

仓库内的 `values.yaml` 只包含结构、非敏感默认值和占位符，不可直接用于生产部署。生产私密配置由脚本从当前生产 YAML 生成：

```bash
python3 scripts/generate_helm_production_values.py \
  --source-dir /path/to/exported-production-yaml
```

生成文件：

```text
deploy/helm/customer-profile-backend/values.production.private.yaml
```

该文件包含数据库密码和工作流密钥，已被 Git 忽略，文件权限会设置为 `0600`。

生成脚本只从旧版 1+1 清单提取已有两套密钥。启用 1+3 覆盖层前，还要在目标
namespace 创建 `commercial-02` / `commercial-03` 引用的两个 Secret；命令模板见
`values.workflow-targets.example.yaml`。不同 AppKey 必须使用不同
`workflowTargets[].id`。

## 校验

不输出渲染后的 Secret 内容：

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

## 构建生产安装包

```bash
python3 scripts/build_helm_package.py \
  --values deploy/helm/customer-profile-backend/values.production.private.yaml \
  --values deploy/helm/customer-profile-backend/values.workflow-targets.example.yaml
```

输出：

```text
dist/helm/customer-profile-backend-0.1.0.tgz
```

安装包已内置生产 values，可以直接安装，但其中也包含生产 Secret，必须按密钥文件管理，不得提交 Git、发送到公共文件服务或上传到普通制品库。覆盖层引用的额外目标
Secret 必须已经存在于集群中。

目标 AppKey 轮换时不要直接覆盖原 ID 的 Secret。保留旧 ID/revision 的
`recovery_only` Worker 处理存量 `runId`，并给新 AppKey 分配新的目标 ID；确认存量
清空后再移除旧目标。

完整的首次接管、升级与验证步骤见 `docs/helm-deploy.md`。
