/**
 * 运行详情。
 *
 * 四条口径：
 *
 * 1. **定义来自当次快照**，不是当前代码。这由后端保证：详情页只调
 *    `/runs/{id}/graph`，不从别处取结构。
 * 2. **过程按时间线呈现**，逐节点展开看明细。连线图已移除——它表达的是定义里的
 *    依赖关系，而「这次实际怎么跑的」是时间顺序问题，用列表更直白。
 * 3. **并行关系读前置依赖项**，不伪造并列分组：共享同一个前置的相邻节点即并行扇出。
 * 4. **子运行可下钻**。带子运行的节点显示入口，点进去换 `run_id` 重新加载。
 */

import { useCallback, useEffect, useState } from 'react'

import { api, type RunGraph } from '../api/client'
import { FlowList } from './FlowList'

const STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  skipped: '已跳过',
  interrupted: '已中断',
}

function formatDuration(ms: number | null): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  return `${Math.floor(seconds / 60)} m ${Math.round(seconds % 60)} s`
}

export interface RunDetailProps {
  runId: string
  onDrillDown: (runId: string) => void
  onBack: () => void
}

export function RunDetail({ runId, onDrillDown, onBack }: RunDetailProps) {
  const [graph, setGraph] = useState<RunGraph | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setError('')
    try {
      setGraph(await api.runGraph(runId))
    } catch (exc: unknown) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [runId])

  useEffect(() => {
    void load()
  }, [load])

  // 运行仍在跑时轮询，进入终态即停。首版就是轮询，不做推送。
  useEffect(() => {
    if (!graph) return
    if (!['queued', 'running'].includes(graph.run.status)) return
    const timer = window.setInterval(() => void load(), 2000)
    return () => window.clearInterval(timer)
  }, [graph, load])

  if (!graph) {
    return (
      <div className="run-detail">
        {error ? <div className="app-error">{error}</div> : null}
        <div className="app-placeholder">正在加载运行详情…</div>
      </div>
    )
  }

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

      {graph.run.status === 'queued' && (
        <div className="detail-hint">
          排队中：已持久化入队，等待执行 worker 领取。此阶段尚无节点执行记录。
        </div>
      )}
      {graph.run.error && <div className="app-error">{graph.run.error}</div>}
      {error && <div className="app-error">{error}</div>}

      <div className="detail-body">
        <FlowList
          definitionNodes={graph.definition.nodes}
          executions={graph.nodes}
          childRuns={graph.child_runs}
          runId={runId}
          onDrillDown={onDrillDown}
          onError={setError}
        />
      </div>
    </div>
  )
}
