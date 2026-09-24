/**
 * 提交任务。
 *
 * 这是运行台**唯一**的写操作。四条口径：
 *
 * 1. **只服务主入口的手机号触发。** 本次不做通用入参表单：其它工作流的入参契约各不相同
 *    （录音类要 `customer_data` / `data_source`），做成一个「什么都能填」的表单等于假装
 *    它们同构。`customer_profile_entry` 的契约是 `phone_number` 必填 + `batch_id` 可选。
 * 2. **前端也校验一次手机号。** 后端会校验（422 带 detail），前端先拦一道只是为了省一次
 *    往返；**不替代**后端校验——真正生效的是后端那条。
 * 3. **提交中禁用按钮。** 真实运行一次约二十一分钟，重复点击会堆叠真实模型调用，
 *    而后端会以 429 拒绝后来者。禁用是最容易做对的一道闸门。
 * 4. **成功后跳运行详情**，不在这里显示进度。详情页已能表达过程（依赖图 + 时间线 +
 *    每次尝试）并支持子运行下钻，另做一套简化进度视图等于维护第二份展示逻辑。
 */

import { useState } from 'react'

import { api, type RuntimeInfo } from '../api/client'

/** 与后端 `api.py` 的 PHONE_PATTERN、脚本里的脱敏正则同一形状。 */
const PHONE_PATTERN = /^1[3-9]\d{9}$/

const ENTRY_WORKFLOW_ID = 'customer_profile_entry'

export interface SubmitRunProps {
  runtime: RuntimeInfo | null
  onSubmitted: (runId: string) => void
}

export function SubmitRun({ runtime, onSubmitted }: SubmitRunProps) {
  const [phone, setPhone] = useState('')
  const [batchId, setBatchId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const trimmed = phone.trim()
  const looksValid = PHONE_PATTERN.test(trimmed)

  async function submit() {
    if (!looksValid || submitting) return
    setSubmitting(true)
    setError('')
    try {
      const result = await api.submitRun({
        workflow_id: ENTRY_WORKFLOW_ID,
        inputs: { phone_number: trimmed, batch_id: batchId.trim() },
        // 业务标识取手机号：列表页按它检索，子运行也会自动带上同一个号码。
        business_ref: trimmed,
      })
      setPhone('')
      setBatchId('')
      onSubmitted(result.run_id)
    } catch (exc: unknown) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="submit-run">
      <form
        className="submit-form"
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        <h2>提交任务</h2>
        <p className="submit-hint">
          运行 <code>{ENTRY_WORKFLOW_ID}</code>（主入口）。提交后跳到运行详情，
          在那里可以看到完整的运行过程与每一次模型调用。
        </p>

        <label htmlFor="submit-phone">手机号</label>
        <input
          id="submit-phone"
          type="text"
          inputMode="numeric"
          autoComplete="off"
          value={phone}
          placeholder="11 位手机号"
          onChange={(event) => setPhone(event.target.value)}
        />

        <label htmlFor="submit-batch">批次号（可选）</label>
        <input
          id="submit-batch"
          type="text"
          autoComplete="off"
          value={batchId}
          placeholder="留空即无批次"
          onChange={(event) => setBatchId(event.target.value)}
        />

        <button type="submit" disabled={!looksValid || submitting}>
          {submitting ? '提交中…' : '提交任务'}
        </button>

        {trimmed !== '' && !looksValid && (
          <div className="submit-warn">
            手机号须为 11 位中国大陆号码（1 开头，第二位 3-9）
          </div>
        )}
        {error && <div className="submit-error">{error}</div>}
      </form>

      {runtime && (
        <aside className="submit-runtime">
          <h3>当前运行态</h3>
          <dl>
            <dt>回写</dt>
            <dd className={runtime.writeback_enabled ? '' : 'runtime-off'}>
              {runtime.writeback_enabled
                ? '开启（会写入生产库）'
                : '已关闭（不回写生产库）'}
            </dd>
            <dt>回放</dt>
            <dd>
              {runtime.replay_mode === 'off'
                ? '关闭（走真实模型与接口）'
                : `开启（${runtime.replay_mode}，不出网）`}
            </dd>
            <dt>模型</dt>
            <dd>{runtime.llm_model || '—'}</dd>
            <dt>并发</dt>
            <dd>
              {runtime.max_active_runs === null
                ? '不限制'
                : `${runtime.active_runs ?? 0}/${runtime.max_active_runs}`}
            </dd>
          </dl>
          {!runtime.writeback_enabled && (
            <p className="submit-hint">
              回写被 <code>WRITEBACK_ENABLED=false</code> 拦下时，回写节点只产出
              「已跳过」的结果，而运行仍会显示成功——**成功不等于画像已写进生产库**。
            </p>
          )}
        </aside>
      )}
    </div>
  )
}
