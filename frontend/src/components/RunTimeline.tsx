/**
 * 执行顺序时间线。
 *
 * 存在的理由（`docs/specs/V0.4只读运行台.md` §2.3、迁移说明 §8.4）：
 * **不把并发伪装成线性顺序**。图用边表达依赖，这里用横向条形按真实起止时间绘制，
 * 两者并排看，才能分辨「这两个节点是并行跑的」还是「它们有依赖所以先后跑」。
 *
 * 只看图看不出并发：扇出的 13 个子流程在图上就是 13 条边，它们的实际执行是交错的。
 * 只看时间线也看不出依赖：重叠的条形可能是并行，也可能只是等待。两者缺一不可。
 */

import { useMemo } from 'react'

import type { NodeExecution } from '../api/client'

const STATUS_COLOR: Record<string, string> = {
  succeeded: '#22c55e',
  failed: '#ef4444',
  running: '#3b82f6',
  skipped: '#a855f7',
  blocked: '#f97316',
  pending: '#9ca3af',
}

export interface RunTimelineProps {
  nodes: NodeExecution[]
  /** 运行的墙钟起点，作为时间轴的 0 点。 */
  runStartedAtMs: number | null
  runDurationMs: number | null
  onSelectNode?: (nodeId: string) => void
}

export function RunTimeline({
  nodes,
  runStartedAtMs,
  runDurationMs,
  onSelectNode,
}: RunTimelineProps) {
  const { bars, total, overlapCount } = useMemo(() => {
    // 只画有墙钟起点的节点。没有起点的节点（如被跳过、未执行）无法定位到轴上，
    // 硬塞进去会给出一个假位置。
    const timed = nodes
      .filter((n) => n.started_at_ms !== null && n.started_at_ms !== undefined)
      .sort((a, b) => (a.started_at_ms ?? 0) - (b.started_at_ms ?? 0))

    const base = runStartedAtMs ?? timed[0]?.started_at_ms ?? 0
    const spans = timed.map((n) => {
      const offset = (n.started_at_ms ?? base) - base
      const duration = n.duration_ms ?? 0
      return { node: n, offset, duration, end: offset + duration }
    })

    const span = Math.max(
      runDurationMs ?? 0,
      ...spans.map((s) => s.end),
      1, // 避免除零：全部零时长时仍要能渲染
    )

    // 并发检测：区间两两重叠的对数。只用于给标题一个如实的提示，
    // 不据此重排——顺序必须是时间顺序，不能为了好看而改。
    let overlaps = 0
    for (let i = 0; i < spans.length; i += 1) {
      for (let j = i + 1; j < spans.length; j += 1) {
        if (spans[i].offset < spans[j].end && spans[j].offset < spans[i].end) {
          overlaps += 1
        }
      }
    }

    return { bars: spans, total: span, overlapCount: overlaps }
  }, [nodes, runStartedAtMs, runDurationMs])

  if (bars.length === 0) {
    return <div className="detail-empty">没有可用于时间线的执行记录</div>
  }

  return (
    <div className="timeline">
      <div className="timeline-head">
        执行顺序（共 {bars.length} 个节点，{overlapCount} 对时间重叠
        {overlapCount > 0 ? '——存在并发' : ''}，总跨度 {Math.round(total)} ms）
      </div>
      <div className="timeline-rows">
        {bars.map(({ node, offset, duration }) => (
          <div
            className="timeline-row"
            key={`${node.node_id}:${node.call_path}`}
            onClick={() => onSelectNode?.(node.node_id)}
          >
            <div className="timeline-label" title={node.node_title ?? node.node_id}>
              {node.node_title ?? node.node_id}
            </div>
            <div className="timeline-track">
              <div
                className="timeline-bar"
                style={{
                  left: `${(offset / total) * 100}%`,
                  width: `${Math.max((duration / total) * 100, 0.4)}%`,
                  background: STATUS_COLOR[node.status] ?? '#94a3b8',
                }}
                title={`${node.node_id} · 偏移 ${offset} ms · 耗时 ${duration} ms · ${node.status}`}
              />
            </div>
            <div className="timeline-value">{duration} ms</div>
          </div>
        ))}
      </div>
    </div>
  )
}
