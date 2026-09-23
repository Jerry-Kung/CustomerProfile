/**
 * 运行详情。
 *
 * 四条口径（docs/specs/V0.4只读运行台.md §2、§4）：
 *
 * 1. **图来自当次定义快照**，不是当前代码——历史运行展示当时的图。这由后端保证：
 *    详情页只用 `/runs/{id}/graph`，从不请求 `/workflows/{id}/topology`。
 * 2. **节点状态叠加在图上**，点节点看明细。节点按状态着色（与列表的徽标同一套语义）。
 * 3. **尝试按需拉取**。节点行带 `attempt_count`，点开才请求——`response_text` 很大，
 *    全部内联会让首屏为了画图加载好几 MB。
 * 4. **子运行可下钻**。带 `sub_run_id` 的节点显示入口，点进去换 run_id 重新加载。
 */

import { useCallback, useEffect, useState } from 'react'

import {
  api,
  type NodeExecution,
  type RequestAttempt,
  type RunGraph,
} from '../api/client'
import { TopologyGraph } from './TopologyGraph'

const STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  skipped: '已跳过',
  interrupted: '已中断',
  blocked: '被阻断',
  pending: '待执行',
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
  return (
    <div className="attempt-list">
      {attempts.map((attempt) => (
        <div className="attempt" key={attempt.attempt_id}>
          <div className="attempt-head">
            <span className="attempt-no">第 {attempt.attempt_no} 次</span>
            <span className="attempt-kind">{attempt.kind}</span>
            <span className="attempt-target">{attempt.target ?? '—'}</span>
            <span>{formatDuration(attempt.duration_ms)}</span>
            {attempt.status_code !== null && <span>HTTP {attempt.status_code}</span>}
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
          {attempt.usage_json && Object.keys(attempt.usage_json).length > 0 && (
            <div className="attempt-usage">
              用量：{asText(attempt.usage_json).replace(/\s+/g, ' ')}
            </div>
          )}
        </div>
      ))}
      {attempts.length === 0 && (
        <div className="detail-empty">该节点没有外部请求尝试</div>
      )}
    </div>
  )
}

export interface RunDetailProps {
  runId: string
  onDrillDown: (runId: string) => void
  onBack: () => void
}

export function RunDetail({ runId, onDrillDown, onBack }: RunDetailProps) {
  const [graph, setGraph] = useState<RunGraph | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<NodeExecution | null>(null)
  const [attempts, setAttempts] = useState<RequestAttempt[]>([])
  const [loadingAttempts, setLoadingAttempts] = useState(false)

  const load = useCallback(async () => {
    setError('')
    try {
      const data = await api.runGraph(runId)
      setGraph(data)
      // 换运行时清掉上一个节点的选择与尝试，否则会看到「新运行里那个节点 ID 的
      // 旧尝试」，看着像这个运行自己的记录。
      setSelected(null)
      setAttempts([])
    } catch (exc: unknown) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [runId])

  useEffect(() => {
    void load()
  }, [load])

  // 运行仍在跑时轮询，进入终态即停（§4：首版轮询）。
  useEffect(() => {
    if (!graph) return
    if (!['queued', 'running'].includes(graph.run.status)) return
    const timer = window.setInterval(() => void load(), 2000)
    return () => window.clearInterval(timer)
  }, [graph, load])

  const openNode = useCallback(
    async (nodeId: string) => {
      if (!graph) return
      const execution = graph.nodes.find((n) => n.node_id === nodeId) ?? null
      setSelected(execution)
      setAttempts([])
      if (!execution || execution.attempt_count === 0) return
      setLoadingAttempts(true)
      try {
        setAttempts(
          await api.nodeAttempts(runId, nodeId, execution.call_path || undefined),
        )
      } catch (exc: unknown) {
        setError(exc instanceof Error ? exc.message : String(exc))
      } finally {
        setLoadingAttempts(false)
      }
    },
    [graph, runId],
  )

  if (!graph) {
    return (
      <div className="run-detail">
        {error ? <div className="app-error">{error}</div> : null}
        <div className="app-placeholder">正在加载运行详情…</div>
      </div>
    )
  }

  const childRun = selected ? graph.child_runs[selected.node_id] : undefined

  return (
    <div className="run-detail">
      <div className="detail-header">
        <button type="button" onClick={onBack}>
          ← 返回列表
        </button>
        <strong>{graph.run.workflow_id}</strong>
        <span className={`status-pill status-${graph.run.status}`}>
          {STATUS_LABEL[graph.run.status] ?? graph.run.status}
        </span>
        <span>业务标识：{graph.run.business_ref ?? '—'}</span>
        <span>耗时：{formatDuration(graph.run.duration_ms)}</span>
        <span>节点：{graph.nodes.length}</span>
        {graph.is_replay && <span className="tag-replay">回放</span>}
        {graph.definition_version_id !== null && (
          <span className="detail-hint">
            定义版本 #{graph.definition_version_id}（当时）
          </span>
        )}
      </div>

      {graph.run.error && <div className="app-error">{graph.run.error}</div>}
      {error && <div className="app-error">{error}</div>}

      <div className="detail-body">
        <div className="detail-graph">
          <TopologyGraph
            definition={graph.definition}
            executions={graph.nodes}
            onSelectNode={(id) => void openNode(id)}
          />
        </div>

        <aside className="detail-panel">
          {!selected && (
            <div className="detail-empty">点击左侧节点查看输入输出与尝试</div>
          )}

          {selected && (
            <>
              <h2>{selected.node_title ?? selected.node_id}</h2>
              <div className="detail-meta">
                <div>
                  类型：<code>{selected.node_type}</code>
                </div>
                <div>
                  状态：
                  <span className={`status-pill status-${selected.status}`}>
                    {STATUS_LABEL[selected.status] ?? selected.status}
                  </span>
                </div>
                <div>耗时：{formatDuration(selected.duration_ms)}</div>
                {selected.branch && <div>分支：{selected.branch}</div>}
                {selected.call_path && <div>调用路径：{selected.call_path}</div>}
                <div>
                  节点 ID：<code>{selected.node_id}</code>
                </div>
              </div>

              {selected.error && (
                <div className="attempt-error">{selected.error}</div>
              )}

              {childRun && (
                <button
                  type="button"
                  className="drill-down"
                  onClick={() => onDrillDown(childRun.run_id)}
                >
                  进入子运行 {childRun.workflow_id}（{childRun.status}）
                </button>
              )}

              {selected.inputs_json && (
                <Collapsible title="输入" text={asText(selected.inputs_json)} />
              )}
              {selected.outputs_json && (
                <Collapsible title="输出" text={asText(selected.outputs_json)} />
              )}

              {selected.attempt_count > 0 && (
                <>
                  <h3>
                    请求尝试（{selected.attempt_count}
                    {loadingAttempts ? '，加载中…' : ''}）
                  </h3>
                  <AttemptList attempts={attempts} />
                </>
              )}
            </>
          )}
        </aside>
      </div>
    </div>
  )
}
