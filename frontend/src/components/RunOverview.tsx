/**
 * 顶部运行概览。
 *
 * 顺序按设计文档 §3.1：面包屑 → 业务标题 + 执行状态 → 次要信息行 → 主要操作。
 * 另加一条紧凑的运行概况，讲清「这次运行产出了什么、还有什么没拿到」。
 *
 * 三条口径：
 *
 * 1. **业务名与技术名分开**。主标题取当次定义快照的 `display_name`（例如「客户初始画像」），
 *    技术标识 `workflow_id` 放在次要信息与详情里——不改工作流标识。
 * 2. **版本显示当次实际使用的那一份**（`definition_version_id`），不取当前最新版本。
 * 3. **产出状态如实说**。没有最终产出就讲运行概况，不为了填满设计图而新造一套聚合逻辑；
 *    节点结果状态各有多少个，按实际数据数出来。
 */

import { RUN_STATUS_LABEL, formatClock, formatDuration, labelOf, runStatusTone } from '../lib/format'
import { displayTitle, resultState, type ResultState } from '../lib/nodeSummary'
import type { NodeExecution, RunDetailPayload, RunSummary } from '../api/client'

export interface RunOverviewProps {
  run: RunSummary
  detail: RunDetailPayload | null
  /** 当次定义快照的显示名。快照缺失时回退 undefined。 */
  displayName: string | undefined
  executions: NodeExecution[]
  onBack: () => void
  onOpenResult: () => void
  onCopyResult: () => void
  copied: boolean
}

/** 概况条里的一句话结论。只描述这次运行的状态，不解释业务含义。 */
function headlineOf(run: RunSummary, completed: number, total: number): string {
  switch (run.status) {
    case 'succeeded':
      return total > 0 && completed === total
        ? '本次运行已执行完毕，可查看各节点产出与最终结果。'
        : '本次运行已执行完毕，可查看各节点产出。'
    case 'running':
      return '本次运行正在执行，页面会自动更新。'
    case 'queued':
      return '任务已入队，等待执行 worker 领取。此阶段尚无节点执行记录。'
    case 'failed':
      return '本次运行未完成，失败原因见下方错误信息。'
    case 'interrupted':
      return '本次运行被中断，已完成的节点结果仍然保留。'
    case 'cancelled':
      return '本次运行已取消。'
    default:
      return '本次运行状态见上。'
  }
}

export function RunOverview({
  run,
  detail,
  displayName,
  executions,
  onBack,
  onOpenResult,
  onCopyResult,
  copied,
}: RunOverviewProps) {
  // 已完成：走到了终态的节点。运行中与待执行不计入。
  const completed = executions.filter(
    (item) => !['running', 'pending'].includes(item.status),
  ).length
  const total = executions.length

  // 结果状态的分布，用于如实说明「有哪些节点没产出内容」。
  const states = executions.map(resultState)
  const tally = (state: ResultState) => states.filter((item) => item === state).length
  const blankCount = tally('blank') + tally('unrecorded')
  const skippedCount = tally('skipped')
  const blockedCount = tally('blocked') + tally('failed')

  const hasResult = detail !== null && Object.keys(detail.outputs ?? {}).length > 0
  // 工作流的 `display_name` 也可能带开发期前缀（实测「（新）客户初始画像（生产环境）」），
  // 与节点标题用同一套清洗，否则标题行还留着一个只有内部人才看得懂的标记。
  const name = displayTitle(displayName, run.workflow_id)

  return (
    <header className="overview">
      <nav className="overview-crumbs">
        <button type="button" className="link-button" onClick={onBack}>
          任务列表
        </button>
        <span className="crumb-sep">/</span>
        <span>运行详情</span>
      </nav>

      <div className="overview-head">
        <div className="overview-title-line">
          <h1 className="overview-title">{name}</h1>
          <span className={`status-tag tone-${runStatusTone(run.status)}`}>
            {labelOf(RUN_STATUS_LABEL, run.status)}
          </span>
          {run.is_replay ? <span className="tag-plain">回放</span> : null}
        </div>

        <div className="overview-actions">
          <button
            type="button"
            className="btn btn-primary"
            onClick={onOpenResult}
            disabled={!hasResult}
            title={hasResult ? undefined : '本次运行没有记录最终产出'}
          >
            查看聚合结果
          </button>
          <button
            type="button"
            className="btn"
            onClick={onCopyResult}
            disabled={!hasResult}
            title={hasResult ? undefined : '本次运行没有记录最终产出'}
          >
            {copied ? '已复制' : '复制结果'}
          </button>
        </div>
      </div>

      <dl className="overview-meta">
        <div>
          <dt>业务标识</dt>
          <dd>{run.business_ref ?? '—'}</dd>
        </div>
        <div>
          <dt>总耗时</dt>
          <dd>{formatDuration(run.duration_ms)}</dd>
        </div>
        <div>
          <dt>节点执行</dt>
          <dd>
            {completed} / {total} 已完成
          </dd>
        </div>
        <div>
          <dt>定义版本</dt>
          <dd>
            {run.workflow_id} ·{' '}
            {detail?.definition_version_id !== null &&
            detail?.definition_version_id !== undefined
              ? `#${detail.definition_version_id}（本次使用）`
              : '未记录'}
          </dd>
        </div>
        {run.started_at_ms !== null ? (
          <div>
            <dt>开始时间</dt>
            <dd>{formatClock(run.started_at_ms)}</dd>
          </div>
        ) : null}
      </dl>

      <div className={`overview-banner tone-${runStatusTone(run.status)}`}>
        <div className="overview-banner-main">
          <p className="overview-banner-title">
            {headlineOf(run, completed, total)}
          </p>
          <p className="overview-banner-facts">
            {[
              `已获取信息 ${tally('info')}`,
              blankCount > 0 ? `未产出内容 ${blankCount}` : '',
              skippedCount > 0 ? `未走分支 ${skippedCount}` : '',
              blockedCount > 0 ? `失败或被阻断 ${blockedCount}` : '',
              tally('running') > 0 ? `运行中 ${tally('running')}` : '',
            ]
              .filter(Boolean)
              .join(' · ')}
          </p>
        </div>
        {run.error ? <p className="overview-banner-error">{run.error}</p> : null}
      </div>
    </header>
  )
}
