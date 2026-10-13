/**
 * 业务结果渲染。
 *
 * 目标：让人**先读到能懂的内容**，原始数据完整可达（设计文档 §5.1）。
 *
 * 三条口径：
 *
 * 1. **只解一层 JSON**。输出常见形态是「JSON 里套一个 Markdown 字符串」，
 *    解一层就能拿到正文；再往下反复解析会破坏代码、路径与合法原文。
 *    解不出来就按原始文本渲染。
 * 2. **不改写结论**。原文里的不确定性、来源限制、待核实措辞原样保留，
 *    这里只做排版，不做归纳，也不生成新的画像结论。
 * 3. **认不出的结构降级为通用视图**，而不是渲染失败或显示空白。
 */

import { useState } from 'react'

import { Markdown } from './Markdown'
import { parseJsonLoose, primaryOutput, summarizeValue } from '../lib/nodeSummary'

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * 数组的紧凑视图。
 *
 * **对象元素必须走摘要**：直接 `String(对象)` 会渲染成 `[object Object]`，
 * 那等于把内容丢掉还装作展示了。逐项压成一行（`summarizeValue`），
 * 压不出内容才退回条数。
 */
function ArrayChips({ value, limit }: { value: unknown[]; limit: number }) {
  return (
    <span className="rv-chip-row">
      {value.slice(0, limit).map((item, index) => {
        const text = isScalar(item) ? scalarText(item) : summarizeValue(item, 56)
        return (
          <span className="rv-chip" key={index}>
            {text || '—'}
          </span>
        )
      })}
      {value.length > limit ? (
        <span className="rv-empty">等 {value.length} 项</span>
      ) : null}
    </span>
  )
}

/** 只是载体的键名，不作为分组标题。 */
const GENERIC_KEYS = new Set(['result', 'result1', 'output', 'text', 'body', 'data'])

/** 标量：一行文本能说完的值。 */
function isScalar(value: unknown): boolean {
  return (
    typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
  )
}

function scalarText(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (value === '') return '（空字符串）'
  return String(value)
}

/** 键名转人话：只做下划线与连字符的替换，不翻译键名。 */
function prettyKey(key: string): string {
  return key.replace(/[_-]+/g, ' ').trim()
}

/** 短字符串当值，长字符串或含换行的当正文块。 */
function isProse(text: string): boolean {
  return text.length > 120 || text.includes('\n')
}

function Prose({ text }: { text: string }) {
  // 含 Markdown 结构就渲染，否则原样保留换行——纯文本里的空行是有意义的。
  const looksMarkdown = /(^|\n)\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|```|\|)/.test(text)
  if (looksMarkdown) return <Markdown text={text} />
  return <div className="rv-prose">{text}</div>
}

/** 字符串单元格：可能还是 JSON（例如子流程把对象序列化进了字符串）。 */
function StringValue({ text }: { text: string }) {
  const parsed = parseJsonLoose(text)
  if (parsed !== null) return <Node value={parsed} name="" depth={0} />
  if (!text) return <span className="rv-empty">空字符串</span>
  return <Prose text={text} />
}

/** 记录表：取各行的键并集做表头，超过上限的列折进详情。 */
function RecordTable({ rows, depth }: { rows: Record<string, unknown>[]; depth: number }) {
  const columns: string[] = []
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!columns.includes(key)) columns.push(key)
    }
  }
  const shown = columns.slice(0, 6)
  const hidden = columns.length - shown.length

  return (
    <div className="rv-table-wrap">
      <table className="rv-table">
        <thead>
          <tr>
            {shown.map((column) => (
              <th key={column}>{prettyKey(column)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {shown.map((column) => {
                const cell = row[column]
                return (
                  <td key={column}>
                    {isScalar(cell) ? (
                      isProse(String(cell ?? '')) ? (
                        <span className="rv-cell-long">
                          {String(cell).slice(0, 200)}…
                        </span>
                      ) : (
                        scalarText(cell)
                      )
                    ) : Array.isArray(cell) ? (
                      <ArrayChips value={cell} limit={6} />
                    ) : (
                      <Node value={cell} name="" depth={depth + 1} />
                    )}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {hidden > 0 ? (
        <div className="rv-table-hint">
          另有 {hidden} 个字段未在此展开，可在「原始数据」中查看完整输出。
        </div>
      ) : null}
    </div>
  )
}

function FieldGrid({ entries }: { entries: [string, unknown][] }) {
  return (
    <dl className="rv-fields">
      {entries.map(([key, value]) => (
        <div className="rv-field" key={key}>
          <dt>{prettyKey(key)}</dt>
          <dd>
            {isScalar(value) ? (
              <span className="rv-field-value">{scalarText(value)}</span>
            ) : Array.isArray(value) ? (
              <ArrayChips value={value} limit={8} />
            ) : (
              <span className="rv-field-value">
                {`${isPlainObject(value) ? Object.keys(value).length : 0} 个字段`}
              </span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  )
}

/** 通用结构与标量视图。深度超过 3 层即停止展开，避免病态嵌套把页面撑爆。 */
function Node({ value, name, depth }: { value: unknown; name: string; depth: number }) {
  if (value === null || value === undefined) {
    return <span className="rv-empty">—</span>
  }
  if (typeof value === 'string') return <StringValue text={value} />
  if (isScalar(value)) return <span className="rv-field-value">{scalarText(value)}</span>

  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="rv-empty">空列表</span>
    const allObjects = value.every(isPlainObject)
    if (allObjects && depth < 3) {
      return <RecordTable rows={value as Record<string, unknown>[]} depth={depth} />
    }
    if (value.every(isScalar)) {
      return <ArrayChips value={value} limit={30} />
    }
    return (
      <div className="rv-stack">
        {value.slice(0, 20).map((item, index) => (
          <div className="rv-stack-item" key={index}>
            <Node value={item} name={`#${index + 1}`} depth={depth + 1} />
          </div>
        ))}
        {value.length > 20 ? (
          <div className="rv-empty">另有 {value.length - 20} 项，见「原始数据」</div>
        ) : null}
      </div>
    )
  }

  // 对象：键值对里短的走字段表，长的（正文）单独成块。
  const entries = Object.entries(value as Record<string, unknown>)
  if (entries.length === 0) return <span className="rv-empty">空对象</span>

  const shortEntries: [string, unknown][] = []
  const longEntries: [string, unknown][] = []
  for (const entry of entries) {
    const [, item] = entry
    if (typeof item === 'string' && isProse(item)) longEntries.push(entry)
    else if (!isScalar(item) && !Array.isArray(item) && isPlainObject(item) && depth < 2) {
      longEntries.push(entry)
    } else shortEntries.push(entry)
  }

  return (
    <div className="rv-node">
      {name ? <div className="rv-node-name">{prettyKey(name)}</div> : null}
      {shortEntries.length > 0 ? <FieldGrid entries={shortEntries} /> : null}
      {longEntries.map(([key, item]) =>
        typeof item === 'string' ? (
          <section className="rv-section" key={key}>
            <h4 className="rv-section-title">{prettyKey(key)}</h4>
            <Prose text={item} />
          </section>
        ) : depth >= 3 ? (
          <section className="rv-section" key={key}>
            <h4 className="rv-section-title">{prettyKey(key)}</h4>
            <pre className="rv-fallback">{JSON.stringify(item, null, 2)}</pre>
          </section>
        ) : (
          // 对象子节点自带标题，不再外套 h4——否则它自己的字段组读起来像 h4 的兄弟。
          <Node key={key} value={item} name={key} depth={depth + 1} />
        ),
      )}
    </div>
  )
}

export interface ResultViewProps {
  /** 一份输出对象（节点的 `outputs_json`，或运行级 `outputs`）。 */
  outputs: Record<string, unknown> | null
  /** 空输出时的说明。 */
  emptyHint?: string
}

export function ResultView({ outputs, emptyHint }: ResultViewProps) {
  const [showRaw, setShowRaw] = useState(false)

  if (!outputs || Object.keys(outputs).length === 0) {
    return (
      <div className="rv-empty-block">
        {emptyHint ?? '本次未记录输出字段。'}
        <span className="rv-empty-note">
          空对象表示这一条执行没有记到输出内容，不代表节点判定为「无数据」。
        </span>
      </div>
    )
  }

  const primary = primaryOutput(outputs)
  const rest = primary
    ? Object.fromEntries(Object.entries(outputs).filter(([key]) => key !== primary.key))
    : outputs

  return (
    <div className="rv">
      {primary ? (
        <section className="rv-section">
          {/* 泛用键名（result / output / text）不重复当标题：它只是载体名，
              写出来不增加信息，反而多一层噪声。 */}
          {Object.keys(outputs).length > 1 && !GENERIC_KEYS.has(primary.key) ? (
            <h4 className="rv-section-title">{prettyKey(primary.key)}</h4>
          ) : null}
          <Node value={primary.value} name="" depth={0} />
        </section>
      ) : null}

      {Object.keys(rest).length > 0 ? <Node value={rest} name="" depth={0} /> : null}

      <button
        type="button"
        className="rv-raw-toggle"
        onClick={() => setShowRaw((value) => !value)}
      >
        {showRaw ? '收起原始 JSON' : '查看原始 JSON'}
      </button>
      {showRaw ? (
        <pre className="rv-fallback rv-fallback-full">
          {JSON.stringify(outputs, null, 2)}
        </pre>
      ) : null}
    </div>
  )
}
