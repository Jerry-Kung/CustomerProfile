/**
 * 节点详情面板（右栏）。
 *
 * 按设计文档 §5 组织为三个标签：**业务结果 / 原始数据 / 执行信息**。
 * 默认落在业务结果——先读到能懂的内容，技术信息一步可达。
 *
 * 口径：
 *
 * 1. **不伪造状态**。执行状态来自 `status`；结果状态由输出字段实际内容判定，
 *    两者并列展示，不合并成一个「成功」。
 * 2. **原始数据保持完整**。长文本折叠但不截断，复制拿到的始终是全文。
 * 3. **缺什么说什么**。输入为空对象时说明「记录到的输入是空对象」，
 *    不断言「该节点没有拿到共享上下文输入」——记录为空与没有输入是两件事。
 */

import { useCallback, useEffect, useState } from 'react'

import { api, type ChildRunBrief, type GraphNode, type NodeExecution, type RequestAttempt } from '../api/client'
import { NODE_STATUS_LABEL, formatClock, formatDuration, labelOf, nodeStatusTone } from '../lib/format'
import { displayTitle, nodeCategory, resultState, resultStateLabel } from '../lib/nodeSummary'
import { ResultView } from './ResultView'

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
 * 供应商键名不统一（`prompt_tokens` / `input_tokens`），两种都认。
 */
function usageSummary(usage: Record<string, unknown> | null): { text: string; model: string } {
  if (!usage || Object.keys(usage).length === 0) return { text: '', model: '' }
  const prompt = usage.prompt_tokens ?? usage.input_tokens
  const completion = usage.completion_tokens ?? usage.output_tokens
  const total = usage.total_tokens
  const parts: string[] = []
  if (total !== undefined && total !== null) parts.push(`共 ${String(total)} tokens`)
  else if ((prompt !== undefined && prompt !== null) || (completion !== undefined && completion !== null)) {
    parts.push(`入 ${String(prompt ?? '—')} / 出 ${String(completion ?? '—')}`)
  }
  return { text: parts.join('，'), model: typeof usage.model === 'string' ? usage.model : '' }
}

/** 可折叠长文本。折叠只影响显示，复制与展开拿到的都是全文。 */
function RawBlock({ title, text }: { title: string; text: string }) {
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)
  const limit = 1600
  const long = text.length > limit
  const shown = expanded || !long ? text : `${text.slice(0, limit)}\n…（已折叠，点「展开全部」查看完整内容）`

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }, [text])

  return (
    <div className="raw-block">
      <div className="raw-block-head">
        <span className="raw-block-title">{title}</span>
        <span className="raw-block-size">{text.length} 字符</span>
        <button type="button" className="link-button" onClick={() => void copy()}>
          {copied ? '已复制' : '复制'}
        </button>
        {long ? (
          <button type="button" className="link-button" onClick={() => setExpanded((value) => !value)}>
            {expanded ? '收起' : '展开全部'}
          </button>
        ) : null}
      </div>
      <pre className="raw-pre">{shown}</pre>
    </div>
  )
}

function AttemptList({ attempts, loading }: { attempts: RequestAttempt[]; loading: boolean }) {
  if (loading) return <div className="panel-empty">正在加载请求记录…</div>
  if (attempts.length === 0) {
    return <div className="panel-empty">该节点没有外部请求记录（本地代码节点通常如此）。</div>
  }
  return (
    <div className="attempt-list">
      {attempts.map((attempt) => {
        const usage = usageSummary(attempt.usage_json)
        return (
          <div className="attempt" key={attempt.attempt_id}>
            <div className="attempt-head">
              <span className="attempt-no">第 {attempt.attempt_no} 次</span>
              <span className="chip">{attempt.kind}</span>
              <span className="attempt-target" title={attempt.target ?? ''}>
                {attempt.target ?? '—'}
              </span>
              <span>{formatDuration(attempt.duration_ms)}</span>
              {attempt.status_code !== null ? <span>HTTP {attempt.status_code}</span> : null}
              {usage.model ? <span>{usage.model}</span> : null}
              {usage.text ? <span>{usage.text}</span> : null}
              {attempt.replayed ? <span className="tag-plain">回放</span> : null}
            </div>
            {attempt.error ? (
              <div className="inline-error">
                错误：{attempt.error}
                {attempt.error_code ? `（${attempt.error_code}）` : ''}
              </div>
            ) : null}
            {attempt.request_json ? (
              <RawBlock
                title={attempt.kind === 'llm' ? '实际请求（含 messages）' : '实际请求'}
                text={asText(attempt.request_json)}
              />
            ) : null}
            {attempt.response_text ? <RawBlock title="响应" text={attempt.response_text} /> : null}
          </div>
        )
      })}
    </div>
  )
}

type Tab = 'result' | 'raw' | 'meta'

const TAB_LABEL: Record<Tab, string> = {
  result: '业务结果',
  raw: '原始数据',
  meta: '执行信息',
}

export interface NodeDetailProps {
  execution: NodeExecution
  /** 当次定义快照里的同 ID 节点，用于取技术标题与前置依赖。 */
  definitionNode: GraphNode | undefined
  titleOf: (nodeId: string) => string
  child: ChildRunBrief | undefined
  runId: string
  onClose: () => void
  onDrillDown: (runId: string) => void
  onError: (message: string) => void
}

export function NodeDetail({
  execution,
  definitionNode,
  titleOf,
  child,
  runId,
  onClose,
  onDrillDown,
  onError,
}: NodeDetailProps) {
  const [tab, setTab] = useState<Tab>('result')
  const [attempts, setAttempts] = useState<RequestAttempt[]>([])
  const [loadingAttempts, setLoadingAttempts] = useState(false)

  const title = displayTitle(
    execution.node_title,
    definitionNode?.title ?? execution.node_id,
  )
  const state = resultState(execution)
  const predecessors = definitionNode?.direct_predecessors ?? []

  // 切节点时回到业务结果：上一个节点停在「原始数据」，换一个后默认落在原始数据上
  // 会让人以为新节点没有业务结果。
  useEffect(() => {
    setTab('result')
  }, [execution.node_id, execution.call_path])

  // 尝试按需拉取。节点 ID + 调用路径共同定位——只按 node_id 会把迭代各轮混在一起。
  useEffect(() => {
    if (execution.attempt_count === 0) {
      setAttempts([])
      return
    }
    let cancelled = false
    setLoadingAttempts(true)
    setAttempts([])
    api
      .nodeAttempts(runId, execution.node_id, execution.call_path || undefined)
      .then((rows) => {
        if (!cancelled) setAttempts(rows)
      })
      .catch((exc: unknown) => {
        if (!cancelled) onError(exc instanceof Error ? exc.message : String(exc))
      })
      .finally(() => {
        if (!cancelled) setLoadingAttempts(false)
      })
    return () => {
      cancelled = true
    }
  }, [runId, execution.node_id, execution.call_path, execution.attempt_count, onError])

  const hasInputs =
    execution.inputs_json !== null && Object.keys(execution.inputs_json).length > 0

  return (
    <section className="node-detail" aria-label="节点详情">
      <header className="node-detail-head">
        <div className="node-detail-title-line">
          <h2 className="node-detail-title">{title}</h2>
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label="关闭节点详情"
            title="关闭节点详情"
          >
            ×
          </button>
        </div>

        <div className="node-detail-subline">
          <span className={`status-tag tone-${nodeStatusTone(execution.status)}`}>
            {labelOf(NODE_STATUS_LABEL, execution.status)}
          </span>
          <span className="dot-sep">·</span>
          <span className="result-state">{resultStateLabel(state)}</span>
          <span className="dot-sep">·</span>
          <span>耗时 {formatDuration(execution.duration_ms)}</span>
          {child ? (
            <>
              <span className="dot-sep">·</span>
              <button
                type="button"
                className="link-button"
                onClick={() => onDrillDown(child.run_id)}
              >
                查看子流程 ↗
              </button>
            </>
          ) : null}
        </div>
      </header>

      <nav className="tabs" role="tablist">
        {(Object.keys(TAB_LABEL) as Tab[]).map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? 'tab active' : 'tab'}
            onClick={() => setTab(key)}
          >
            {TAB_LABEL[key]}
          </button>
        ))}
      </nav>

      <div className="node-detail-body">
        {tab === 'result' ? (
          <>
            {execution.error ? (
              <div className="inline-error">执行错误：{execution.error}</div>
            ) : null}
            <ResultView
              outputs={execution.outputs_json}
              emptyHint={
                child
                  ? '该节点的结果产出在子流程中，可点上方「查看子流程」进入。'
                  : undefined
              }
            />
            <p className="panel-footnote">
              完整输入、输出与运行信息可在对应标签中查看。
            </p>
          </>
        ) : null}

        {tab === 'raw' ? (
          <>
            <div className="raw-block">
              <div className="raw-block-head">
                <span className="raw-block-title">输入</span>
              </div>
              {hasInputs ? (
                <pre className="raw-pre">{asText(execution.inputs_json)}</pre>
              ) : (
                <div className="panel-empty">
                  记录到的输入为空对象。这不代表该节点没有从共享上下文取到输入。
                </div>
              )}
            </div>

            <div className="raw-block">
              <div className="raw-block-head">
                <span className="raw-block-title">输出</span>
              </div>
              {execution.outputs_json !== null ? (
                <pre className="raw-pre">{asText(execution.outputs_json)}</pre>
              ) : (
                <div className="panel-empty">本节点未记录输出字段。</div>
              )}
            </div>

            {execution.attempt_count > 0 ? (
              <>
                <h4 className="raw-group-title">
                  请求记录（{execution.attempt_count} 次）
                </h4>
                <AttemptList attempts={attempts} loading={loadingAttempts} />
              </>
            ) : null}
          </>
        ) : null}

        {tab === 'meta' ? (
          <dl className="meta-list">
            <div>
              <dt>节点标识</dt>
              <dd className="mono">{execution.node_id}</dd>
            </div>
            {execution.node_title ? (
              <div>
                <dt>技术标题</dt>
                <dd>{execution.node_title}</dd>
              </div>
            ) : null}
            <div>
              <dt>节点类型</dt>
              <dd className="mono">{execution.node_type}</dd>
            </div>
            <div>
              <dt>展示类别</dt>
              <dd>{nodeCategory(execution.node_type, Boolean(child))}</dd>
            </div>
            {execution.call_path ? (
              <div>
                <dt>调用路径</dt>
                <dd className="mono">{execution.call_path}</dd>
              </div>
            ) : null}
            {execution.branch ? (
              <div>
                <dt>分支</dt>
                <dd>{execution.branch}</dd>
              </div>
            ) : null}
            {predecessors.length > 0 ? (
              <div>
                <dt>前置依赖</dt>
                <dd>{predecessors.map((id) => titleOf(id)).join('、')}</dd>
              </div>
            ) : null}
            {execution.queued_at_ms !== null ? (
              <div>
                <dt>入队时刻</dt>
                <dd>{formatClock(execution.queued_at_ms)}</dd>
              </div>
            ) : null}
            {execution.started_at_ms !== null ? (
              <div>
                <dt>开始时刻</dt>
                <dd>{formatClock(execution.started_at_ms)}</dd>
              </div>
            ) : null}
            <div>
              <dt>节点耗时</dt>
              <dd>{formatDuration(execution.duration_ms)}</dd>
            </div>
            <div>
              <dt>请求次数</dt>
              <dd>{execution.attempt_count}</dd>
            </div>
            {child ? (
              <div>
                <dt>子运行</dt>
                <dd>
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => onDrillDown(child.run_id)}
                  >
                    {child.workflow_id ?? child.run_id}（
                    {labelOf(NODE_STATUS_LABEL, child.status ?? '') ||
                      child.status ||
                      '—'}
                    ）
                  </button>
                </dd>
              </div>
            ) : null}
            {execution.sub_run_id && !child ? (
              <div>
                <dt>子运行 ID</dt>
                <dd className="mono">{execution.sub_run_id}</dd>
              </div>
            ) : null}
            {execution.error ? (
              <div>
                <dt>错误详情</dt>
                <dd className="inline-error">{execution.error}</dd>
              </div>
            ) : null}
          </dl>
        ) : null}
      </div>
    </section>
  )
}
