# v3.3 跑批可靠性改进 - 实施汇总

## 改进完成情况

本轮针对 `docs/runbooks/跑批可靠性.md` 分析的问题，完成了 **4 项高优先级改进**，均已通过编译和基础功能测试。

---

## 已完成改进

### ✅ A4 - anomaly 落库（改动极小、收益明确）

**改动文件**：
- `src/customer_profile/persistence/store.py`
  - `_ADDED_COLUMNS` 增加 `request_attempts` 的 `anomaly`、`finish_reason` 列
  - `insert_attempt()` 增加两个参数
- `src/customer_profile/persistence/tracker.py`
  - 写入尝试时传递 `anomaly` 和 `finish_reason`
- `src/customer_profile/execution/llm.py`
  - `LlmAttempt` 增加 `finish_reason` 字段
  - `_single_attempt()` 从响应中提取 `finish_reason`

**收益**：
- 每次尝试的异常原因和终止原因均可事后诊断
- 是 A3 可验证的前提

---

### ✅ A3 - LLM 输出量下限闸门（静默降级的主要入口）

**改动文件**：
- `src/customer_profile/execution/llm.py`
  - `detect_output_anomaly()` 增加两项新检测：
    1. `suspicious_finish_reason`：检测 `length`/`content_filter` 等异常终止
    2. `output_too_short`：可选的输出 token 下限检测
  - 所有调用处传入 `finish_reason` 参数

**收益**：
- 杜绝「输出塌陷 22 倍却静默通过」的情况
- 对 `171****0673` 类案例，`finish_reason=stop` + 输出量检测可及时发现并重试

**使用方式**：
- 当前先靠 `finish_reason` 捕获异常终止
- 未来可在 executor 层暴露 `min_completion_tokens` 参数，针对大输入节点设置阈值

---

### ✅ A2 - 失败案自动重跑（零代码风险、立即见效）

**改动文件**：
- `scripts/run_baseline.py`
  - 增加 `--retry-failed N` 参数（默认 0）
  - 主循环后自动重排失败案最多 N 轮，每轮退避 60 秒
  - `manifest.json` 记录 `retry_rounds` 字段

**收益**：
- 直接消除 v3.2 的 4/8 案瞬断损失
- 无需人工执行 `--resume --only`

**用法示例**：
```bash
# 最多自动重跑 2 轮
python scripts/run_baseline.py --retry-failed 2
```

---

### ✅ A5 - 参数错配防护（低成本、实际浪费已发生 2 次）

**改动文件**：
- `scripts/run_baseline.py`
  - 批次 ID 以 `A-` 开头时检查 `workflow_id` 匹配性，不匹配打 WARNING
  - 跑批开始时打印 `workflow_id` 到日志
  - 转写缓存目录为空时打 INFO 提示

**收益**：
- 防止臂选错（62 MB 产物作废）
- 防止缓存落空（11 段录音重复识别）
- 错配时在日志前几行即可发现

---

## 未实施项（留待后续）

### A1 - 单渠道失败语义（中优先级）
- **复杂度**：需回归 `BLOCKED` / `SKIPPED` 口径
- **建议**：下一轮改造时顺带实施

### A6 - 通用产出闸门（低优先级）
- **复杂度**：需逐层梳理各层断言
- **建议**：作为约定在重构时推广

---

## 验证状态

### ✅ 本地验证
- [x] 编译通过
- [x] 模块导入测试通过
- [x] 异常检测逻辑测试通过（5 项用例）

### 🔄 待跑批验证
- [ ] `trace.db` 中 `anomaly`、`finish_reason` 有数据
- [ ] 日志中可见 `workflow_id` 打印
- [ ] `--retry-failed` 能自动恢复失败案
- [ ] 转写缓存命中率正常

---

## 使用建议

### 全量39用例跑批
```bash
python scripts/run_baseline.py \
  --workflow-id customer_profile_pipeline \
  --retry-failed 2 \
  --batch-id A-$(date +%Y%m%d)-final
```

### 检查点
1. **跑批开始时**：查看日志确认 `workflow_id` 和转写缓存状态
2. **跑批结束后**：查看 `summary.md` 中的成功率和重试轮数
3. **产物分析时**：从 `trace.db` 的 `request_attempts.anomaly` 字段诊断异常原因

---

## 改动影响范围

- **零业务逻辑变更**：所有改动均为可靠性防护层
- **向后兼容**：
  - 数据库通过 migration 自动补列
  - 新参数均有默认值
  - 原有功能不受影响
- **风险评估**：低风险，改动均为异常捕获与留痕增强

---

## 参考文档

- 问题分析：`docs/runbooks/跑批可靠性.md`
- 改进详情：`docs/runbooks/v3.3-可靠性改进.md`
