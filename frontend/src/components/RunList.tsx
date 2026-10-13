/**
 * 任务列表。
 *
 * 三条口径（docs/history/V0.4只读运行台.md §4）：
 *
 * 1. **状态与错误摘要并排显示**。只显示状态而不给错误摘要，排查时还得再点进去；
 *    只显示摘要而不给状态，则要在文字里判断成败。
 * 2. **筛选条件与后端同源**。前端的筛选下拉直接取 `/workflows`，不写死工作流清单
 *    —— 写死会在新增工作流后静默漏掉它。
 * 3. **子运行默认不混入列表**。子运行数量远多于父运行（一次主入口跑出十几个），
 *    混在一起会把父运行冲散。给一个显式开关。
 *
 * 展示层这一轮只做对齐：业务名用定义里的 `display_name`，技术标识留在 `title` 里，
 * 时长与时刻改用与详情页同一套格式化。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { api, type RunSummary, type WorkflowBrief } from '../api/client'
import { RUN_STATUS_LABEL, formatClock, formatDuration, labelOf, runStatusTone } from '../lib/format'

const PAGE_SIZE = 25

export interface RunListProps {
  onOpenRun: (runId: string) => void
}

export function RunList({ onOpenRun }: RunListProps) {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [workflows, setWorkflows] = useState<WorkflowBrief[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // 筛选条件
  const [workflowId, setWorkflowId] = useState('')
  const [status, setStatus] = useState('')
  const [businessRef, setBusinessRef] = useState('')
  const [includeChildren, setIncludeChildren] = useState(false)
  const [orderBy, setOrderBy] = useState('created_desc')

  useEffect(() => {
    api
      .listWorkflows()
      .then(setWorkflows)
      .catch(() => setWorkflows([]))
  }, [])

  // 工作流 ID → 业务显示名。取不到就退回 ID：显示名缺失不该让整列变空。
  const nameOf = useMemo(() => {
    const map = new Map(workflows.map((item) => [item.workflow_id, item.display_name]))
    return (id: string) => map.get(id) ?? id
  }, [workflows])

  const filters = useCallback(
    () => ({
      workflow_id: workflowId || undefined,
      status: status || undefined,
      business_ref: businessRef || undefined,
      include_children: includeChildren,
      order_by: orderBy,
    }),
    [workflowId, status, businessRef, includeChildren, orderBy],
  )

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const params = { ...filters(), limit: PAGE_SIZE, offset: page * PAGE_SIZE }
      const [rows, count] = await Promise.all([
        api.listRuns(params),
        api.countRuns(filters()),
      ])
      setRuns(rows)
      setTotal(count)
    } catch (exc: unknown) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }, [filters, page])

  // 筛选条件变化时回到第 0 页：留在第 5 页而新条件下只有 1 页，会看到一张空表，
  // 那看起来像「查不到数据」而不是「页码越界」。
  useEffect(() => {
    setPage(0)
  }, [workflowId, status, businessRef, includeChildren, orderBy])

  useEffect(() => {
    void load()
  }, [load])

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="run-list">
      <div className="toolbar">
        <div className="field">
          <label htmlFor="f-workflow">工作流</label>
          <select
            id="f-workflow"
            value={workflowId}
            onChange={(event) => setWorkflowId(event.target.value)}
          >
            <option value="">全部</option>
            {workflows.map((workflow) => (
              <option key={workflow.workflow_id} value={workflow.workflow_id}>
                {workflow.display_name}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="f-status">状态</label>
          <select
            id="f-status"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="">全部</option>
            {Object.entries(RUN_STATUS_LABEL).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="f-ref">业务标识</label>
          <input
            id="f-ref"
            type="text"
            value={businessRef}
            placeholder="如手机号"
            onChange={(event) => setBusinessRef(event.target.value)}
          />
        </div>

        <div className="field">
          <label htmlFor="f-order">排序</label>
          <select
            id="f-order"
            value={orderBy}
            onChange={(event) => setOrderBy(event.target.value)}
          >
            <option value="created_desc">最新优先</option>
            <option value="created_asc">最早优先</option>
            <option value="duration_desc">耗时最长</option>
          </select>
        </div>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={includeChildren}
            onChange={(event) => setIncludeChildren(event.target.checked)}
          />
          含子运行
        </label>

        <div className="toolbar-actions">
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void load()}
            disabled={loading}
          >
            {loading ? '刷新中…' : '刷新'}
          </button>
        </div>
      </div>

      {error ? <div className="app-error">{error}</div> : null}

      <div className="data-card">
        <div className="data-table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>状态</th>
                <th>工作流</th>
                <th>业务标识</th>
                <th>开始时间</th>
                <th>耗时</th>
                <th>错误摘要</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id} onClick={() => onOpenRun(run.run_id)}>
                  <td>
                    <span className={`status-tag tone-${runStatusTone(run.status)}`}>
                      {labelOf(RUN_STATUS_LABEL, run.status)}
                    </span>
                    {run.is_replay ? <span className="tag-plain">回放</span> : null}
                  </td>
                  <td title={run.workflow_id}>{nameOf(run.workflow_id)}</td>
                  <td>{run.business_ref ?? '—'}</td>
                  <td className="cell-soft">{formatClock(run.started_at_ms)}</td>
                  <td className="cell-soft">{formatDuration(run.duration_ms)}</td>
                  <td className="cell-clip" title={run.error ?? ''}>
                    {run.error ? run.error.slice(0, 80) : '—'}
                  </td>
                </tr>
              ))}
              {runs.length === 0 && !loading ? (
                <tr>
                  <td colSpan={6} className="cell-empty">
                    没有符合条件的运行
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        <div className="pager">
          <button
            type="button"
            disabled={page === 0}
            onClick={() => setPage((value) => Math.max(0, value - 1))}
          >
            上一页
          </button>
          <span>
            第 {page + 1} / {pageCount} 页 · 共 {total} 条
          </span>
          <button
            type="button"
            disabled={page + 1 >= pageCount}
            onClick={() => setPage((value) => value + 1)}
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  )
}
