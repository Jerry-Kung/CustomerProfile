/**
 * 节点列表（左栏）。
 *
 * 取代原先「点一行在列表内展开」的 FlowList。四条口径：
 *
 * 1. **排序按完成时刻**（设计文档 §6.1 采纳的用户期望）。完成时刻由
 *    `started_at_ms + duration_ms` 得到；两项缺任一的排到最后，不拿开始时间冒充。
 *    排序口径直接写在列表头上，避免「按开始还是按完成」再有分歧。
 * 2. **三行结构**：业务名 + 状态 + 耗时 / 结果摘要 / 类别标签。摘要只是帮助扫读，
 *    完整内容在右侧详情里。
 * 3. **筛选只用可靠判据**。「有结果」按输出字段里是否真取到内容判定；
 *    「异常」是失败与被阻断。判不准的一律归入「全部」，不臆测。
 * 4. **不抢阅读位置**。轮询刷新不动滚动；只有从子流程返回、恢复选中项时，
 *    才把那一条滚回视野。
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import type { ChildRunBrief, GraphNode, NodeExecution } from '../api/client'
import { NODE_STATUS_LABEL, formatDuration, labelOf, nodeStatusTone } from '../lib/format'
import {
  completionOrderKey,
  displayTitle,
  executionKey,
  nodeCategory,
  resultState,
  resultSummary,
  searchTextOf,
} from '../lib/nodeSummary'

type Filter = 'all' | 'withResult' | 'abnormal'

const FILTER_LABEL: Record<Filter, string> = {
  all: '全部',
  withResult: '有结果',
  abnormal: '异常',
}

export interface NodeListProps {
  definitionNodes: GraphNode[]
  executions: NodeExecution[]
  childRuns: Record<string, ChildRunBrief>
  selectedKey: string | null
  /** 当前运行标识。它一变就尝试把选中项滚回视野，用于从子流程返回时恢复阅读位置。 */
  runId: string
  onSelect: (execution: NodeExecution) => void
}

export function NodeList({
  definitionNodes,
  executions,
  childRuns,
  selectedKey,
  runId,
  onSelect,
}: NodeListProps) {
  const [filter, setFilter] = useState<Filter>('all')
  const [keyword, setKeyword] = useState('')
  const containerRef = useRef<HTMLDivElement | null>(null)
  // 已恢复过的运行。同一个运行内轮询刷新不重滚——那会把正在读的位置拽走。
  const restoredRunRef = useRef<string | null>(null)

  const titleOf = useMemo(
    () => new Map(definitionNodes.map((node) => [node.node_id, node.title])),
    [definitionNodes],
  )

  const ordered = useMemo(() => {
    return [...executions].sort((a, b) => {
      const aKey = completionOrderKey(a)
      const bKey = completionOrderKey(b)
      if (aKey !== bKey) return aKey - bKey
      // 同刻（或都缺完成时刻）时用记录顺序兜底，保证顺序稳定不跳动。
      return a.execution_id - b.execution_id
    })
  }, [executions])

  const visible = useMemo(() => {
    const lowered = keyword.trim().toLowerCase()
    return ordered.filter((execution) => {
      const fallbackTitle = titleOf.get(execution.node_id) ?? execution.node_id
      if (lowered && !searchTextOf(execution, fallbackTitle).includes(lowered)) return false
      if (filter === 'withResult') return resultState(execution) === 'info'
      if (filter === 'abnormal') {
        return execution.status === 'failed' || execution.status === 'blocked'
      }
      return true
    })
  }, [ordered, filter, keyword, titleOf])

  /**
   * 恢复阅读位置。
   *
   * 每次运行只做一次（`restoredRunRef` 挡住重复触发）：轮询刷新同样会走到这里，
   * 每次都滚会把人正读着的位置拽走。条目不在当前筛选结果里时什么都不做——
   * 被筛掉说明用户当下要看别的，不打扰。
   */
  useEffect(() => {
    if (restoredRunRef.current === runId) return
    restoredRunRef.current = runId
    if (!selectedKey) return
    const container = containerRef.current
    if (!container) return
    const row = container.querySelector<HTMLElement>(
      `[data-key="${CSS.escape(selectedKey)}"]`,
    )
    if (row) row.scrollIntoView({ block: 'center' })
  }, [runId, selectedKey, visible])

  if (ordered.length === 0) {
    return (
      <div className="node-list">
        <div className="panel-empty">这次运行还没有节点执行记录。</div>
      </div>
    )
  }

  return (
    <div className="node-list">
      <div className="node-list-head">
        <div className="node-list-title">
          <h2>执行过程</h2>
          <span className="node-list-sub">按完成时间排列</span>
        </div>
        <div className="node-list-tools">
          <input
            className="search-input"
            type="search"
            value={keyword}
            placeholder="搜索节点"
            aria-label="搜索节点"
            onChange={(event) => setKeyword(event.target.value)}
          />
          <div className="segmented" role="group" aria-label="节点筛选">
            {(Object.keys(FILTER_LABEL) as Filter[]).map((key) => (
              <button
                key={key}
                type="button"
                className={filter === key ? 'segment active' : 'segment'}
                onClick={() => setFilter(key)}
              >
                {FILTER_LABEL[key]}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="node-rows" ref={containerRef}>
        {visible.map((execution, index) => {
          const key = executionKey(execution)
          const fallbackTitle = titleOf.get(execution.node_id) ?? execution.node_id
          const title = displayTitle(execution.node_title, fallbackTitle)
          const child = childRuns[execution.node_id]
          const state = resultState(execution)
          const selected = key === selectedKey

          return (
            <button
              key={key}
              type="button"
              data-key={key}
              className={selected ? 'node-row selected' : 'node-row'}
              aria-current={selected}
              onClick={() => onSelect(execution)}
            >
              <span className="node-row-no">{String(index + 1).padStart(2, '0')}</span>

              <span className="node-row-main">
                <span className="node-row-line1">
                  <span className="node-row-title">{title}</span>
                  <span className="node-row-time">
                    <span className={`status-dot tone-${nodeStatusTone(execution.status)}`} />
                    {labelOf(NODE_STATUS_LABEL, execution.status)}
                    <span className="dot-sep">·</span>
                    {formatDuration(execution.duration_ms)}
                  </span>
                </span>

                <span className="node-row-line2">{resultSummary(execution)}</span>

                <span className="node-row-line3">
                  <span className="chip">{nodeCategory(execution.node_type, Boolean(child))}</span>
                  {execution.branch ? (
                    <span className="chip chip-quiet">分支 {execution.branch}</span>
                  ) : null}
                  {state === 'blank' || state === 'unrecorded' ? (
                    <span className="chip chip-quiet">未产出内容</span>
                  ) : null}
                  {child ? <span className="chip chip-link">子流程 ↗</span> : null}
                  {execution.attempt_count > 0 ? (
                    <span className="chip chip-quiet">{execution.attempt_count} 次请求</span>
                  ) : null}
                </span>
              </span>
            </button>
          )
        })}

        {visible.length === 0 ? (
          <div className="panel-empty">没有符合条件的节点。</div>
        ) : null}
      </div>

      <div className="node-list-foot">
        显示 {visible.length} / {ordered.length} 个节点执行
      </div>
    </div>
  )
}
