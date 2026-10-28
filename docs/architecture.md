# 当前架构

API 接收任务并持久化 MySQL；若干 Worker 通过同一 TaskService/Repository 原子领取，
按 WORKFLOW_PLATFORM 选择 qingtian 或 runner 适配器。API 与 Worker 是独立进程，同一个镜像。
Dify 保留历史兼容入口。没有额外 broker、权重、分桶或商用队列门槛。

## 进程边界

- API 初始化数据库、任务服务、API 参数；不初始化工作流客户端、上传目录或 WorkerExecutor。
- Worker 初始化所选适配器、共享解析器、WorkerExecutor 和线程 Runner。
- 迁移只加载数据库和迁移参数；日志初始化前加载环境。
- 所有运行时环境变量读取集中于 config/settings.py；logging_utils 使用同一环境读取入口。

## Worker 合约

每个组声明任务类型、环境、每进程并发、目标 ID/revision 和模式。同类平台可以有多个
不同目标；同目标可以有多个副本。local/commercial 字段仅用于旧数据兼容及旧策略预算，
不控制新任务是否准入。现行字段与 Compose 映射见[配置指南](worker-configuration.md)。

目标身份必须对应稳定的实际工作流/凭据，不同平台不能复用同一身份。新任务竞争队列后
由实际领取目标记录运行归属。单目标重试保留目标，只清理旧 run_id；同目标副本可接续。
数据库写回始终验证 generation、claim_token 和 processing 状态。用户重提开启新代次，旧
持有者不能写入新任务。邀约额外使用 execution_id 核对本次业务回写。

## 状态与恢复

queued -> processing -> succeeded / failed / retry_wait；retry_wait 再进入 processing。
状态变化由事务 SQL 的条件更新保护；state_machine.py 提供可读状态图和基线测试。
领取使用 FOR UPDATE SKIP LOCKED，更新与租约同事务。远端 ID 在轮询前落库；未知提交
不盲目创建新运行。恢复扫描有独立维护循环，不因执行线程全部繁忙而停止。

默认 single_target_v1，旧 local_first_v1/throughput_first_v1 保留兼容历史记录。
009 从最后一次同代 attempt 恢复升级前丢失的单目标绑定；有尝试却无法证明目标的任务
不参与未绑定竞争，保留并告警。新任务不需要分桶或额外路由表。

## 结果、契约与观测

适配器校验各平台信封/业务身份，共享解析器接受扩展 JSON 对象并提取可选显示字段。
手写 OpenAPI 的表单字段与实际 API 一起校验。业务成功、工作流成功和失败通知分别观察。
Worker 汇总按目标与能力范围统计；用 queued_at/运行开始时间计算耗时，心跳只反映租约存活。
API 提供保持兼容的 /health，以及用 HTTP 503 表示 DB 不可用的 /ready。
