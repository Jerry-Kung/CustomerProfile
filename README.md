# Customer Profile

潜在目标客户人设画像分析工作流。给定一个手机号，产出一份该客户的画像 JSON，并按回写契约写到 Notes 服务。

用于汽车营销场景：帮助销售人员快速掌握客户基础信息，便于针对性制定销售策略。

## 这个仓库是什么

一个可留痕的 Python 工作流执行器，外加一个只读的运行台前端。

- **执行器**：按依赖就绪调度工作流定义，把运行、节点执行、每次外部请求尝试与定义快照四层全部落到 SQLite。
- **运行台**：查看运行列表与某次运行的完整过程。运行详情为「顶部运行概览 + 左侧节点清单 + 右侧节点详情」：清单按**完成时间**排列、带业务名与结果摘要，详情分业务结果 / 原始数据 / 执行信息三标签，可看每次请求的请求与响应、并下钻子运行；V0.5.1 起可在页面上提交任务。
- **对外任务接口**：生产画像使用 `POST /runs`，邀约使用鉴权的 `POST /api/v1/invitation-tasks`，分别由画像和邀约进程执行。单进程兼容模式仍提供 `POST /api/v1/tasks`；其契约见 `docs/design/对外任务接口.md`。
- **数据基线**：拿一批固定手机号跑真实工作流并归档全部中间结果，供日后判断「改了工作流之后输出质量有没有崩」。

系统形态与关键约束见 `docs/architecture.md`；部署与运维见 `docs/runbooks/`。

邀约工作流迁移分支新增 `invitation_strategy`，复用现有执行器生成话术、外呼质检与待办。
接单、原 Dify 逻辑、迁移差异及跨仓库适配边界见 [邀约工作流高代码迁移](docs/design/邀约工作流高代码迁移.md)。

## 快速开始

部署配置、日志、原数据卷升级与接口验收见 [部署与运行](docs/runbooks/部署.md)。

**容器部署**（画像和邀约各有 API 与 worker，Compose 自动构建镜像，可分别更新，共用原 SQLite 历史卷）：

```
cp .env.example .env          # 然后填入真实端点与凭据
docker compose up -d --build
```

**直接跑进程**：

```
pip install -r requirements.txt
cp .env.example .env          # 然后填入真实端点与凭据

# 起服务（API 进程默认同时消费队列，单机形态即可跑通）
python -m customer_profile.main
```

两种方式都起好后，提交一次任务：

```
curl -X POST localhost:18173/runs \
  -H 'content-type: application/json' \
  -d '{"workflow_id":"customer_profile_pipeline","inputs":{"phone_number":"13800000000"},"business_ref":"13800000000"}'
```

运行台只在前端构建过之后可用：`cd frontend && npm run build`。容器部署时 `SERVE_UI` 已打开；
直接跑进程时把它设为 `true`。

## 目录

| 路径 | 内容 |
|---|---|
| `src/customer_profile/` | 执行器、定义层、持久化、API 与 worker |
| `src/customer_profile/workflows/` | 各工作流的 Python 定义 |
| `src/customer_profile/templates/` | 提示词与模板正文（运行期唯一来源） |
| `frontend/` | 运行台前端（Vite + React） |
| `scripts/` | 运维脚本：基线跑批、冒烟、回放录制、号码收集 |
| `tests/` | 测试（不触达外网，全部走固定响应或确定性代码节点） |
| `data/` | 运行期 SQLite 与产物（可重建，已 gitignore） |
| `experiments/` | 功能验证与问题回归场地：UAT 运行数据（`uat_data/`）与本地跑批批次（`batches/`），体积大，已 gitignore |
| `docs/` | 文档：架构、设计决策、运行手册、变更日志；历史归档见 `docs/history/` |

## 文档

读写 `docs/` 前先看 `docs/README.md` 的管理规范。任务相关约定见 `CLAUDE.md`。
