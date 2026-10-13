/**
 * 展示层格式化。
 *
 * 全文只从这里取时长、时刻与状态文案，避免同一页面出现两种写法
 * （例如「9 m 46 s」与「9分46秒」并存）。
 */

const pad = (n: number) => String(n).padStart(2, '0')

/**
 * 时长。三档：毫秒 / 秒 / 分秒。
 *
 * 与既有实现保持同样的分档，只把秒档文案统一成中文；不足 1 秒的仍报毫秒，
 * 因为大量节点耗时确实在毫秒级，四舍五入成「0.0 秒」会丢掉有效信息。
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 0) return '—'
  if (ms < 1000) return `${ms} 毫秒`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes} 分 ${rest} 秒`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分`
}

/** 绝对时刻。用于执行信息里的「开始时刻」等。 */
export function formatClock(ms: number | null | undefined): string {
  if (!ms) return '—'
  const date = new Date(ms)
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

/** 运行状态文案。与后端 `RunStatus` 同值。 */
export const RUN_STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '执行成功',
  failed: '执行失败',
  cancelled: '已取消',
  skipped: '已跳过',
  interrupted: '已中断',
}

/** 节点状态文案。`blocked` 是前置失败导致未执行，与 `skipped`（未走分支）语义不同。 */
export const NODE_STATUS_LABEL: Record<string, string> = {
  pending: '待执行',
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  skipped: '已跳过',
  blocked: '被阻断',
}

/**
 * 状态色调。
 *
 * 只分四档，而不是给每个状态一个颜色：成功与「不需要处理」的中性态用灰蓝，
 * 运行中与信息态用蓝，需要人介入的用橙，真正的失败用红。
 * 颜色永远配文字或图标出现，不单独承载语义。
 */
export type Tone = 'ok' | 'info' | 'warn' | 'danger' | 'muted'

export function runStatusTone(status: string): Tone {
  switch (status) {
    case 'succeeded':
      return 'ok'
    case 'running':
      return 'info'
    case 'queued':
      return 'muted'
    case 'failed':
      return 'danger'
    case 'interrupted':
      return 'warn'
    default:
      return 'muted'
  }
}

export function nodeStatusTone(status: string): Tone {
  switch (status) {
    case 'succeeded':
      return 'ok'
    case 'running':
      return 'info'
    case 'failed':
      return 'danger'
    case 'blocked':
      return 'warn'
    case 'skipped':
    case 'pending':
    default:
      return 'muted'
  }
}

export function labelOf(map: Record<string, string>, value: string): string {
  return map[value] ?? value
}
