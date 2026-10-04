/**
 * 流程列表：按时间顺序逐节点展示一次运行的实际过程。
 *
 * 取代原来的画布/连线视图。四条口径：
 *
 * 1. **顺序即实际执行顺序**，按 `started_at_ms` 排。它是时间事实，不是图论排序。
 * 2. **并行关系由「前置依赖项」读出**：共享同一个前置的节点就是并行扇出。
 *    列表本身不伪造并列分组。
 * 3. **节点可展开**。展开后给出运行时间、输入输出、每次尝试的模型与 token 消耗、
 *    以及子运行入口。尝试按需拉取（`response_text` 单条可达上万字符）。
 * 4. **迭代与分支如实呈现**。迭代的各轮靠 `call_path` 分行，不合并成一行；
 *    `branch` 字段标明 if-else 实际走的那一支。
 */

import { useState } from 'react'

import {
  api,
  type ChildRunBrief,
  type GraphNode,
  type NodeExecution,
  type RequestAttempt,
} from '../api/client'

const STATUS_LABEL: Record<string, string> = {
  pending: '待执行',
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  skipped: '已跳过',
  blocked: '被阻断',
}

const STATUS_COLOR: Record<string, string> = {
  pending: '#9ca3af',
  running: '#3b82f6',
  succeeded: '#22c55e',
  failed: '#ef4444',
  skipped: '#a855f7',
  blocked: '#f97316',
}

function formatDuration(ms: number | null): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  return `${Math.floor(seconds / 60)} m ${Math.round(seconds % 60)} s`
}

/** 把 JSON 值渲染成可读文本。对象用缩进，字符串原样。 */
function asText(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/**
 * 从一次尝试的 `usage_json` 里摘出 token 与模型名。
 *
 * 字段缺失就返回空串，不编造「0 tokens」——那会让「没拿到 usage」看起来像「用了零」。
 * 供应商的键名不统一（`prompt_tokens` / `input_tokens`），两种都认。
 */
function usageSummary(
  usage: Record<string, unknown> | null,
): { text: string; model: string } {
  if (!usage || Object.keys(usage).length === 0) return { text: '', model: '' }
  const prompt = usage.prompt_tokens ?? usage.input_tokens
  const completion = usage.completion_tokens ?? usage.output_tokens
  const total = usage.total_tokens
  const parts: string[] = []
  if (total !== undefined) parts.push(`共 ${String(total)} tokens`)
  else if (prompt !== undefined || completion !== undefined) {
    parts.push(`入 ${String(prompt ?? '—')} / 出 ${String(completion ?? '—')}`)
  }
  return {
    text: parts.join('，'),
    model: typeof usage.model === 'string' ? usage.model : '',
  }
}

/** 折叠显示长文本，避免一条 LLM 响应把整个面板撑成几屏。 */
function Collapsible({
  title,
  text,
  limit = 1200,
}: {
  title: string
  text: string
  limit?: number
}) {
  const [expanded, setExpanded] = useState(false)
  const long = text.length > limit
  const shown = expanded || !long ? text : `${text.slice(0, limit)}…`
  return (
    <div className="detail-block">
      <div className="detail-block-title">
        {title}
        <span className="detail-size">{text.length} 字符</span>
        {long && (
          <button type="button" onClick={() => setExpanded((v) => !v)}>
            {expanded ? '收起' : '展开全部'}
          </button>
        )}
      </div>
      <pre className="detail-pre">{shown}</pre>
    </div>
  )
}

function AttemptList({ attempts }: { attempts: RequestAttempt[] }) {
  if (attempts.length === 0) {
    return <div className="detail-empty">该节点没有外部请求尝试</div>
  }
  return (
    <div className="attempt-list">
      {attempts.map((attempt) => {
        const usage = usageSummary(attempt.usage_json)
        return (
          <div className="attempt" key={attempt.attempt_id}>
            <div className="attempt-head">
              <span className="attempt-no">第 {attempt.attempt_no} 次</span>
              <span className="attempt-kind">{attempt.kind}</span>
              <span className="attempt-target">{attempt.target ?? '—'}</span>
              <span>{formatDuration(attempt.duration_ms)}</span>
              {attempt.status_code !== null && (
                <span>HTTP {attempt.status_code}</span>
              )}
              {usage.model && <span>{usage.model}</span>}
              {usage.text && <span>{usage.text}</span>}
              {attempt.replayed ? <span className="tag-replay">回放</span> : null}
            </div>
            {attempt.error && (
              <div className="attempt-error">
                错误：{attempt.error}
                {attempt.error_code ? `（${attempt.error_code}）` : ''}
              </div>
            )}
            {/* request_json 里 llm 是完整 payload（含 model 与 messages），http 是请求明细。
                「实际 messages」就在这一层，不需要另开接口。 */}
            {attempt.request_json && (
              <Collapsible
                title={
                  attempt.kind === 'llm' ? '实际请求（含 messages）' : '实际请求'
                }
                text={asText(attempt.request_json)}
                limit={800}
              />
            )}
            {attempt.response_text && (
              <Collapsible title="响应" text={attempt.response_text} />
            )}
          </div>
        )
      })}
    </div>
  )
}

export interface FlowListProps {
  /** 当次定义快照的节点，用于取标题与前置依赖。 */
  definitionNodes: GraphNode[]
  executions: NodeExecution[]
  /** 触发节点 ID → 子运行摘要。点该节点即可下钻。 */
  childRuns: Record<string, ChildRunBrief>
  runId: string
  onDrillDown: (runId: string) => void
  onError: (message: string) => void
}

export function FlowList({
  definitionNodes,
  executions,
  childRuns,
  runId,
  onDrillDown,
  onError,
}: FlowListProps) {
  const [openKey, setOpenKey] = useState<string | null>(null)
  const [attempts, setAttempts] = useState<RequestAttempt[]>([])
  const [loadingAttempts, setLoadingAttempts] = useState(false)

  const titleOf = new Map(definitionNodes.map((n) => [n.node_id, n.title]))
  const predsOf = new Map(
    definitionNodes.map((n) => [n.node_id, n.direct_predecessors]),
  )

  // 按实际开始时间排序。没有起点的（未执行、被跳过）排在最后，同刻的按记录顺序，
  // 保证「时间线」这一条口径不被重排成别的顺序。
  const ordered = [...executions].sort((a, b) => {
    const at = a.started_at_ms ?? Number.MAX_SAFE_INTEGER
    const bt = b.started_at_ms ?? Number.MAX_SAFE_INTEGER
    if (at !== bt) return at - bt
    return a.execution_id - b.execution_id
  })

  async function toggle(execution: NodeExecution) {
    const key = `${execution.node_id}:${execution.call_path}`
    if (openKey === key) {
      setOpenKey(null)
      return
    }
    setOpenKey(key)
    setAttempts([])
    if (execution.attempt_count === 0) return
    setLoadingAttempts(true)
    try {
      setAttempts(
        await api.nodeAttempts(
          runId,
          execution.node_id,
          execution.call_path || undefined,
        ),
      )
    } catch (exc: unknown) {
      onError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoadingAttempts(false)
    }
  }

  if (ordered.length === 0) {
    return <div className="detail-empty">这次运行还没有节点执行记录</div>
  }

  return (
    <div className="flow-list">
      <div className="flow-head">
        共 {ordered.length} 个节点执行 · 按开始时间排列
        （并行关系可由各节点的前置依赖项对照看出）
      </div>
      {ordered.map((execution, index) => {
        const key = `${execution.node_id}:${execution.call_path}`
        const isOpen = openKey === key
        const child = childRuns[execution.node_id]
        const predecessors = predsOf.get(execution.node_id) ?? []
        return (
          <div className={`flow-item ${isOpen ? 'open' : ''}`} key={key}>
            <button
              type="button"
              className="flow-row"
              onClick={() => void toggle(execution)}
            >
              <span className="flow-no">
                {String(index + 1).padStart(2, '0')}
              </span>
              <span
                className="flow-dot"
                style={{
                  background: STATUS_COLOR[execution.status] ?? '#cbd5e1',
                }}
              />
              <span className="flow-title">
                {execution.node_title ??
                  titleOf.get(execution.node_id) ??
                  execution.node_id}
              </span>
              <span className="flow-type">{execution.node_type}</span>
              {execution.branch && (
                <span className="flow-branch">分支 {execution.branch}</span>
              )}
              {predecessors.length > 0 && (
                <span
                  className="flow-preds"
                  title={predecessors
                    .map((p) => titleOf.get(p) ?? p)
                    .join('、')}
                >
                  前置 {predecessors.length} 项
                </span>
              )}
              {child && <span className="flow-child">子运行</span>}
              <span className="flow-duration">
                {formatDuration(execution.duration_ms)}
              </span>
              <span className={`status-pill status-${execution.status}`}>
                {STATUS_LABEL[execution.status] ?? execution.status}
              </span>
            </button>

            {isOpen && (
              <div className="flow-detail">
                <div className="detail-meta">
                  <div>
                    节点 ID：<code>{execution.node_id}</code>
                  </div>
                  {execution.call_path && (
                    <div>
                      调用路径：<code>{execution.call_path}</code>
                    </div>
                  )}
                  {predecessors.length > 0 && (
                    <div>
                      前置依赖项：
                      {predecessors.map((p) => titleOf.get(p) ?? p).join('、')}
                    </div>
                  )}
                  {execution.queued_at_ms !== null && (
                    <div>
                      入队时刻：{new Date(execution.queued_at_ms).toLocaleString()}
                    </div>
                  )}
                  {execution.started_at_ms !== null && (
                    <div>
                      开始时刻：{new Date(execution.started_at_ms).toLocaleString()}
                    </div>
                  )}
                </div>

                {execution.error && (
                  <div className="attempt-error">{execution.error}</div>
                )}

                {child && (
                  <button
                    type="button"
                    className="drill-down"
                    onClick={() => onDrillDown(child.run_id)}
                  >
                    进入子运行 {child.workflow_id}（{child.status}）
                  </button>
                )}

                {execution.inputs_json && (
                  <Collapsible title="输入" text={asText(execution.inputs_json)} />
                )}
                {execution.outputs_json && (
                  <Collapsible
                    title="输出"
                    text={asText(execution.outputs_json)}
                  />
                )}

                {execution.attempt_count > 0 && (
                  <>
                    <h3>
                      请求尝试（{execution.attempt_count}
                      {loadingAttempts ? '，加载中…' : ''}）
                    </h3>
                    <AttemptList attempts={attempts} />
                  </>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
