# 历史归档

这里保存项目演进记录。归档内容中的命令、字段、状态机和实施约束属于当时版本；当前操作入口见[文档导航](../README.md)。

| 目录 / 文件 | 记录范围 |
|---|---|
| [初始设计](initial-design) | 第一阶段异步架构、数据库设计和 Dify 对齐方案 |
| [阶段开发提示词](execute-prompts/README.md) | Phase 00–06 的实施材料 |
| [2026-05 概览](overview-2026-05/README.md) | 早期代码与运维说明快照 |
| [005 重试设计](qingtian-retry-005.md) | 擎天可靠重试的历史基线 |
| [003 单次迁移工具](tools/apply_migration_003.py.txt) | 原专项升级脚本的文本快照 |

日常数据库升级统一运行 `customer-profile-migrate` 或 Compose 的 `migrate` 服务。保留原始 `db/migrations/001`–`006` SQL 文件，不使用归档工具替代当前迁移执行器。
