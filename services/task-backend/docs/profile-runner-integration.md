# Runner 接入与部署

本文的 `runner` 仍只负责画像。CustomerProfile 高代码邀约使用独立的 `invitation_runner`
适配器和鉴权入站协议，见[原生邀约服务接入](native-invitation-runner.md)。

Runner 与擎天使用同一 Worker 合约。当前默认 Compose 支持两种平台共同消费画像，配置、
扩容、分离邀约、多目标及升级步骤统一见[Worker 配置指南](worker-configuration.md)。

## 协议

Runner 请求 POST /runs，workflow_id=customer_profile_pipeline，inputs 中携带 phone_number、
batch_id，并用 business_ref 关联业务身份。GET /runs/{run_id} 查询并校验运行、手机号、
batch、工作流版本及非回放标记。结果将 result1 中的 analysis_result 归一到后端 output。

画像服务需要先启动。默认示例地址 http://host.docker.internal:18000 是宿主机映射地址；
服务原生端口可为 18173。实际以部署映射为准。8099 的入站协议与 /runs 不同。

## 范围和身份

Runner 仅支持 customer_profile，手机号为有效中国手机号，batch_id 长度 1–50。
默认组只接 production；新增测试组时显式配置测试 URL 和环境，不能期待回退到生产地址。
不同工作流/凭据使用独立目标 ID/revision；同目标副本可以共享身份接续恢复。

新任务默认 single_target_v1，明确失败后重试保持原目标，不会由于同属 local 资源池
而切换到旧擎天画像。轮询故障保留同一 run_id，不新建远端运行；未知提交、停止/中断且
副作用不确定时保留不确定语义，等待核对。旧任务原有重试策略继续保留。

## 业务完成

执行服务成功与业务画像实际更新需分别核对。业务写回依赖画像服务自己的 WRITEBACK_ENABLED
和 M2M 配置；backend 的 PROFILE_API_* 不会替画像服务自动开启写回。
PROFILE_FAILURE_CALLBACK_ENABLED=true 同时用于最终失败通知与邀请回写确认，不能随意关闭。

## 容量调整和退出旧流程

通过 PROFILE_RUNNER_CONCURRENCY 与副本数调容量，不保证固定流量比例。先验证目标级
成功率、延迟和样本质量再扩容。旧擎天画像完全退出时先设对应 Worker 为 draining，等待
绑定运行、重试及相关失败通知排空再停止。邀请仍走擎天时保留其目标和密钥。

原来 main 只让 Runner 接新画像、旧商业固定 recovery_only 的部署分工已经退出；不要
继续使用该旧配置假定当前并发和范围。本次不新增数据库迁移，旧重试兼容与回滚方式详见统一配置指南。
