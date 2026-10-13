/**
 * 运行详情页装配。
 *
 * 版式（设计文档 §3）：顶部运行概览 + 左栏节点列表 + 右栏节点详情。
 * 详情可关闭，关闭后列表占满整行。
 *
 * 四条口径：
 *
 * 1. **数据分两处取**：`/graph` 给当次定义快照与节点状态，`/runs/{id}` 给运行级
 *    输入输出（顶部概览的最终产出）。两者并行请求，互不阻塞。
 * 2. **选中项按运行实例保存**。`node_id:call_path` 是执行身份，迭代各轮据此区分；
 *    下钻子流程再返回时，父运行的选中项按 runId 恢复，不因重挂载而丢失。
 * 3. **轮询不打断阅读**。运行中每 2 秒刷新，只替换数据，不动选中项与滚动位置。
 * 4. **保留原有能力**：子流程下钻、返回、错误提示、复制结果。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { api, type RunDetailPayload, type RunGraph } from '../api/client'
import { NodeDetail } from './NodeDetail'
import { NodeList } from './NodeList'
import { executionKey } from '../lib/nodeSummary'
import { ResultView } from './ResultView'
import { RunOverview } from './RunOverview'

export interface RunDetailProps {
  runId: string
  onDrillDown: (runId: string) => void
  onBack: () => void
}

export function RunDetail({ runId, onDrillDown, onBack }: RunDetailProps) {
  const [graph, setGraph] = useState<RunGraph | null>(null)
  const [payload, setPayload] = useState<RunDetailPayload | null>(null)
  const [error, setError] = useState('')

  /**
   * 每个运行各自的选中项与滚动锚点，按 runId 索引。
   *
   * 用映射而不是单个值：下钻到子流程再返回时，父运行的选中项与阅读位置要还在。
   */
  const [selectedByRun, setSelectedByRun] = useState<Record<string, string | null>>({})

  const [copied, setCopied] = useState(false)
  const [resultOpen, setResultOpen] = useState(false)

  const load = useCallback(async () => {
    setError('')
    try {
      const [graphData, detailData] = await Promise.all([
        api.runGraph(runId),
        // 运行级输入输出。失败不阻断主视图：概览缺最终产出仍可读节点。
        api.runDetail(runId).catch(() => null),
      ])
      setGraph(graphData)
      setPayload(detailData)
    } catch (exc: unknown) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [runId])

  useEffect(() => {
    void load()
  }, [load])

  // 运行中轮询，进入终态即停。首版就是轮询，不做推送。
  useEffect(() => {
    if (!graph) return
    if (!['queued', 'running'].includes(graph.run.status)) return
    const timer = window.setInterval(() => void load(), 2000)
    return () => window.clearInterval(timer)
  }, [graph, load])

  // 显式 memo：`graph?.nodes ?? []` 里的 `??` 分支每次求值都是新数组，
  // 会让下面几个 memo 与回调每轮渲染都失效。
  const executions = useMemo(() => graph?.nodes ?? [], [graph])
  const definitionNodes = useMemo(() => graph?.definition?.nodes ?? [], [graph])

  const selectedKey = selectedByRun[runId] ?? null
  const selected = useMemo(
    () => executions.find((item) => executionKey(item) === selectedKey) ?? null,
    [executions, selectedKey],
  )

  const definitionNode = useMemo(
    () => definitionNodes.find((node) => node.node_id === selected?.node_id),
    [definitionNodes, selected],
  )
  const titleOf = useCallback(
    (nodeId: string) =>
      definitionNodes.find((node) => node.node_id === nodeId)?.title ?? nodeId,
    [definitionNodes],
  )

  const displayName = (
    graph?.definition as { display_name?: string } | undefined
  )?.display_name

  const hasResult = payload !== null && Object.keys(payload.outputs ?? {}).length > 0

  const copyResult = useCallback(async () => {
    if (!payload) return
    try {
      await navigator.clipboard.writeText(JSON.stringify(payload.outputs, null, 2))
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopied(false)
    }
  }, [payload])

  // Esc 关闭聚合结果浮层。
  useEffect(() => {
    if (!resultOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setResultOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [resultOpen])

  if (!graph) {
    return (
      <div className="run-detail">
        {error ? <div className="app-error">{error}</div> : null}
        <div className="placeholder">正在加载运行详情…</div>
      </div>
    )
  }

  return (
    <div className="run-detail">
      <RunOverview
        run={graph.run}
        detail={payload}
        displayName={displayName}
        executions={executions}
        onBack={onBack}
        onOpenResult={() => setResultOpen(true)}
        onCopyResult={() => void copyResult()}
        copied={copied}
      />

      {error ? <div className="app-error">{error}</div> : null}

      <div className={selected ? 'detail-columns' : 'detail-columns single'}>
        <NodeList
          definitionNodes={definitionNodes}
          executions={executions}
          childRuns={graph.child_runs}
          selectedKey={selectedKey}
          runId={runId}
          onSelect={(execution) =>
            setSelectedByRun((current) => ({ ...current, [runId]: executionKey(execution) }))
          }
        />

        {selected ? (
          <NodeDetail
            execution={selected}
            definitionNode={definitionNode}
            titleOf={titleOf}
            child={graph.child_runs[selected.node_id]}
            runId={runId}
            onClose={() =>
              setSelectedByRun((current) => ({ ...current, [runId]: null }))
            }
            onDrillDown={onDrillDown}
            onError={setError}
          />
        ) : (
          <aside className="node-detail node-detail-hint">
            <p>选择左侧任意节点，在这里查看它的业务结果、原始数据与执行信息。</p>
          </aside>
        )}
      </div>

      {resultOpen && payload ? (
        <div
          className="modal-backdrop"
          role="presentation"
          onClick={() => setResultOpen(false)}
        >
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="聚合结果"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="modal-head">
              <div>
                <h2>聚合结果</h2>
                <p className="modal-sub">
                  本次运行的最终产出（{graph.run.workflow_id}）
                </p>
              </div>
              <button
                type="button"
                className="icon-button"
                onClick={() => setResultOpen(false)}
                aria-label="关闭"
              >
                ×
              </button>
            </header>
            <div className="modal-body">
              <ResultView outputs={payload.outputs} />
            </div>
          </div>
        </div>
      ) : null}

      {!hasResult && graph.run.status === 'succeeded' ? (
        <p className="run-detail-footnote">
          本次运行没有记录最终产出字段。各节点的产出仍可在左侧逐个查看。
        </p>
      ) : null}
    </div>
  )
}
