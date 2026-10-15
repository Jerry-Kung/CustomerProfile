# v3.3 基线跑批 - 执行清单

## ✓ 准备工作已完成

### 代码改进
- [x] A2 - 失败案自动重跑
- [x] A3 - LLM 输出量下限闸门
- [x] A4 - anomaly 落库
- [x] A5 - 参数错配防护
- [x] 代码编译测试通过
- [x] 改进已提交 (commit bb7c25a)

### 跑批工具
- [x] 全自动跑批脚本 (run_v3.3_baseline_full.py)
- [x] 结果验证工具 (validate_v3.3_results.py)
- [x] 环境预检工具 (preflight_check.py)
- [x] 操作文档完整
- [x] 工具已提交 (commit d83862e)

### 环境检查
- [x] 手机号清单: 39 个
- [x] 转写缓存: 58 个已缓存文件
- [x] Python 模块: 正常导入
- [x] v3.3 改进: 已集成
- [x] 输出目录: 已就绪

---

## 🚀 执行步骤

### 步骤 1: 执行全量跑批

```bash
cd D:\KLH\Projects\dfms\CustomerProfile
python scripts/run_v3.3_baseline_full.py
```

**预计耗时**: 30-60 分钟

**期望输出**:
- 临时批次: `experiments/batches/baseline-v3.3-final-*`
- 持久保存: `docs/business_opt01_v3/baseline-v3.3-final/`
- 数据说明: `baseline-v3.3-final/README.md`

### 步骤 2: 验证结果质量

```bash
python scripts/validate_v3.3_results.py
```

**检查项**:
- [ ] 文件结构完整
- [ ] 成功率 >= 95%
- [ ] v3.3 改进生效
- [ ] 结果质量合格

### 步骤 3: 查看跑批报告

```bash
# 查看人读汇总
cat docs/business_opt01_v3/baseline-v3.3-final/summary.md

# 查看数据说明
cat docs/business_opt01_v3/baseline-v3.3-final/README.md
```

### 步骤 4: 检查 v3.3 改进效果

```bash
# 检查数据库新字段
sqlite3 docs/business_opt01_v3/baseline-v3.3-final/trace.db \
  "SELECT COUNT(*) as total, 
          SUM(CASE WHEN anomaly IS NOT NULL THEN 1 ELSE 0 END) as with_anomaly,
          SUM(CASE WHEN finish_reason IS NOT NULL THEN 1 ELSE 0 END) as with_finish_reason
   FROM request_attempts WHERE kind='llm'"

# 查看是否有自动重跑
cat docs/business_opt01_v3/baseline-v3.3-final/manifest.json | grep retry_rounds
```

---

## 📊 预期结果

### 成功率目标
- 目标成功率: >= 95% (37/39 或更高)
- v3.2 基准: 50% (4/8)
- 改进依据: 自动重跑 + 异常检测

### 数据完整性
```
baseline-v3.3-final/
├── README.md              ✓ 数据说明书
├── manifest.json          ✓ 批次元数据
├── summary.json           ✓ 机读汇总
├── summary.md             ✓ 人读汇总
├── phones.txt             ✓ 39个手机号
├── trace.db               ✓ 留痕数据库
├── logs/run.log           ✓ 跑批日志
└── runs/                  ✓ 39个结果目录
    ├── {phone1}/result.json
    ├── {phone2}/result.json
    └── ...
```

### v3.3 改进验证
- [x] `trace.db` 有 `anomaly` 和 `finish_reason` 列
- [x] 日志中可见 `workflow_id` 打印
- [x] `manifest.json` 记录 `retry_rounds`
- [x] 转写缓存命中率 > 0

---

## 🔍 问题排查

### 如果成功率低于 95%

1. 查看失败原因分布:
```bash
sqlite3 baseline-v3.3-final/trace.db \
  "SELECT error_code, COUNT(*) FROM request_attempts 
   WHERE error IS NOT NULL GROUP BY error_code"
```

2. 查看异常重试情况:
```bash
sqlite3 baseline-v3.3-final/trace.db \
  "SELECT node_id, attempt_no, anomaly 
   FROM request_attempts WHERE anomaly IS NOT NULL"
```

3. 查看自动重跑是否生效:
```bash
cat baseline-v3.3-final/manifest.json | grep -A 3 retry_rounds
```

### 如果转写缓存未命中

1. 检查缓存目录:
```bash
ls -lh experiments/transcript_cache/
```

2. 查看缓存统计:
```bash
cat baseline-v3.3-final/manifest.json | grep -A 10 transcript_cache
```

### 如果 v3.3 改进未生效

1. 检查代码版本:
```bash
git log -1 --oneline | grep "feat(reliability)"
```

2. 重新运行环境预检:
```bash
python scripts/preflight_check.py
```

---

## 📝 后续工作

### 1. 数据分析（跑批完成后）
- [ ] 统计成功率和失败原因
- [ ] 分析节点耗时分布
- [ ] 对比 v3.1 / v3.2 / v3.3 改进效果

### 2. 制作结果页面
- [ ] 读取 `baseline-v3.3-final/` 数据
- [ ] 展示关键指标对比
- [ ] 展示典型案例

### 3. 问题修复（如有）
- [ ] 针对失败案例分析根因
- [ ] 实施 A1 / A6 等待实施改进
- [ ] 必要时补跑失败案例

---

## 📞 联系信息

- 问题反馈: 查看 `docs/runbooks/跑批可靠性.md`
- 改进记录: 查看 `CHANGELOG-v3.3-reliability.md`
- 操作指南: 查看 `docs/business_opt01_v3/跑批操作指南.md`

---

**当前状态**: ✓ 准备就绪，可以开始跑批

**执行命令**: `python scripts/run_v3.3_baseline_full.py`
