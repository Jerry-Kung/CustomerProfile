# v3.3 全量基线跑批 - 快速开始

## 环境检查 ✓

所有准备工作已完成：
- [x] 手机号清单: 39 个
- [x] 转写缓存: 58 个已缓存文件
- [x] Python 模块: 正常导入
- [x] v3.3 改进: 已集成
- [x] 输出目录: 已就绪

## 执行跑批

### 方式一：全自动（推荐）

直接运行全量跑批脚本：

```bash
python scripts/run_v3.3_baseline_full.py
```

这个脚本会：
1. 运行 39 个手机号的完整基线
2. 自动重跑失败案 2 轮
3. 结果保存到 `docs/business_opt01_v3/baseline-v3.3-final/`
4. 生成数据说明书 (README.md)
5. 验证 v3.3 改进是否生效

**预计耗时**: 30-60 分钟

### 方式二：手动执行

如果想自定义参数：

```bash
python scripts/run_baseline.py \
  --workflow-id customer_profile_pipeline \
  --phones experiments/batches/baseline-39-dsv41-noThink/phones.txt \
  --batch-id baseline-v3.3-final-$(date +%Y%m%d-%H%M%S) \
  --out-dir experiments \
  --transcript-cache-dir experiments/transcript_cache \
  --retry-failed 2 \
  --concurrency 4
```

然后手动复制结果到 `docs/business_opt01_v3/baseline-v3.3-final/`

## 小批量测试（可选）

如果想先验证环境，可以只跑 1 个号码：

```bash
python scripts/run_baseline.py \
  --workflow-id customer_profile_pipeline \
  --phones experiments/batches/baseline-39-dsv41-noThink/phones.txt \
  --limit 1 \
  --retry-failed 1
```

**预计耗时**: 2-5 分钟

## 验证结果

跑批完成后，运行验证脚本：

```bash
python scripts/validate_v3.3_results.py
```

检查项：
- 文件结构完整性
- 成功率 >= 95%
- v3.3 改进生效（anomaly/finish_reason）
- 结果质量

## 查看结果

跑批完成后，结果保存在：
```
docs/business_opt01_v3/baseline-v3.3-final/
```

关键文件：
- `README.md` - 数据说明书（含读取示例）
- `summary.json` - 机读汇总
- `summary.md` - 人读汇总
- `trace.db` - 留痕数据库
- `runs/{phone}/result.json` - 每个号码的画像结果

## 常见问题

### Q: 转写缓存会命中吗？
A: 会。现有 58 个已缓存文件会被复用，只有新号码或缓存过期才会重新识别。

### Q: 失败案会自动重跑吗？
A: 会。`--retry-failed 2` 会自动重跑失败案最多 2 轮。

### Q: 结果保存在哪里？
A: `docs/business_opt01_v3/baseline-v3.3-final/`（持久化保存）

### Q: 如何查看实时进度？
A: 查看日志：`tail -f experiments/batches/baseline-v3.3-final-*/logs/run.log`

## v3.3 可靠性改进

本次跑批自动应用：
1. **A2 - 自动重跑**: 失败案自动重试 2 轮
2. **A3 - 输出闸门**: 检测异常终止和输出塌陷
3. **A4 - anomaly 落库**: 异常原因可事后诊断
4. **A5 - 参数校验**: 检查 workflow_id 和缓存配置

详见: `CHANGELOG-v3.3-reliability.md`

## 下一步

1. ✓ 环境检查完成
2. ⏳ 执行全量跑批 ← 当前步骤
3. ⏳ 验证结果质量
4. ⏳ 制作结果对比页面

---

**操作建议**:
- 全量跑批建议在服务器空闲时段执行
- 跑批期间可以去做其他工作，会有进度日志
- 跑批完成后记得验证结果质量
