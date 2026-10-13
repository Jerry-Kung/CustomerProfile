/**
 * 轻量 Markdown 渲染。
 *
 * **不引解析依赖**（设计文档 §7：不为视觉改版引入重量级新依赖），
 * 也**不使用 `dangerouslySetInnerHTML`**——本渲染器直接产出 React 元素，
 * 内容一律当文本处理，因此结构上不存在 HTML 注入面。
 * 链接同样只放行 http/https，其余按纯文本显示。
 *
 * 覆盖面按实际输出取样确定：标题、有序/无序列表（含一层缩进）、表格、引用、
 * 围栏代码、分隔线、段落，行内粗体/斜体/删除线/代码/链接。
 * 认不出的语法按普通段落输出——**降级成原文，不吞掉内容**。
 */

import { Fragment, type ReactNode } from 'react'

/* ---------------------------------------------------------------- 行内解析 */

const INLINE_PATTERN =
  /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(~~[^~\n]+~~)|(\[[^\]\n]*\]\([^)\s]+\))|(\*[^*\n]+\*|_[^_\n]+_)/g

function safeHref(url: string): string | null {
  const trimmed = url.trim()
  // 只放行 http/https：`javascript:` 之类的伪协议一律按纯文本处理。
  return /^https?:\/\//i.test(trimmed) ? trimmed : null
}

function inline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = []
  let cursor = 0
  let index = 0
  INLINE_PATTERN.lastIndex = 0

  let match: RegExpExecArray | null = INLINE_PATTERN.exec(text)
  while (match !== null) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index))
    const token = match[0]
    const key = `${keyPrefix}-i${index++}`

    if (token.startsWith('`')) {
      nodes.push(
        <code className="md-code" key={key}>
          {token.slice(1, -1)}
        </code>,
      )
    } else if (token.startsWith('**')) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>)
    } else if (token.startsWith('~~')) {
      nodes.push(<del key={key}>{token.slice(2, -2)}</del>)
    } else if (token.startsWith('[')) {
      const split = token.indexOf('](')
      const label = token.slice(1, split)
      const href = safeHref(token.slice(split + 2, -1))
      nodes.push(
        href ? (
          <a href={href} key={key} target="_blank" rel="noreferrer noopener">
            {label}
          </a>
        ) : (
          <Fragment key={key}>{label}</Fragment>
        ),
      )
    } else {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>)
    }
    cursor = match.index + token.length
    match = INLINE_PATTERN.exec(text)
  }
  if (cursor < text.length) nodes.push(text.slice(cursor))
  return nodes
}

/* ---------------------------------------------------------------- 块级解析 */

const HEADING = /^(#{1,6})\s+(.*)$/
const FENCE = /^\s*```/
const RULE = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/
const QUOTE = /^\s*>\s?(.*)$/
const BULLET = /^(\s*)([-*+])\s+(.*)$/
const NUMBERED = /^(\s*)(\d+)[.)]\s+(.*)$/
const TABLE_ROW = /^\s*\|(.+)\|\s*$/
const TABLE_SEP = /^\s*\|?[\s:|-]+\|?\s*$/

function splitRow(line: string): string[] {
  const inner = line.trim().replace(/^\|/, '').replace(/\|$/, '')
  return inner.split('|').map((cell) => cell.trim())
}

interface ListItem {
  indent: number
  ordered: boolean
  marker: string
  text: string
  children: ListItem[]
}

function buildList(lines: { indent: number; ordered: boolean; marker: string; text: string }[]): ListItem[] {
  const roots: ListItem[] = []
  const stack: ListItem[] = []
  for (const line of lines) {
    const item: ListItem = { ...line, children: [] }
    while (stack.length > 0 && stack[stack.length - 1].indent >= item.indent) stack.pop()
    if (stack.length === 0) roots.push(item)
    else stack[stack.length - 1].children.push(item)
    stack.push(item)
  }
  return roots
}

function renderList(items: ListItem[], keyPrefix: string): ReactNode {
  if (items.length === 0) return null
  const ordered = items[0].ordered
  const Tag = ordered ? 'ol' : 'ul'
  return (
    <Tag className="md-list" key={keyPrefix}>
      {items.map((item, index) => (
        <li key={`${keyPrefix}-l${index}`}>
          {inline(item.text, `${keyPrefix}-l${index}`)}
          {item.children.length > 0
            ? renderList(item.children, `${keyPrefix}-l${index}-c`)
            : null}
        </li>
      ))}
    </Tag>
  )
}

function parseBlocks(text: string): ReactNode[] {
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  let index = 0
  let key = 0
  const nextKey = (kind: string) => `${kind}-${key++}`

  while (index < lines.length) {
    const line = lines[index]

    if (!line.trim()) {
      index += 1
      continue
    }

    // 围栏代码：整段原样保留，不做行内解析。
    if (FENCE.test(line)) {
      const language = line.trim().replace(/^```/, '').trim()
      const body: string[] = []
      index += 1
      while (index < lines.length && !FENCE.test(lines[index])) {
        body.push(lines[index])
        index += 1
      }
      index += 1 // 跳过闭合围栏（缺失时也自然结束）
      blocks.push(
        <pre className="md-pre" key={nextKey('code')}>
          {language ? <span className="md-lang">{language}</span> : null}
          <code>{body.join('\n')}</code>
        </pre>,
      )
      continue
    }

    if (RULE.test(line)) {
      blocks.push(<hr className="md-rule" key={nextKey('rule')} />)
      index += 1
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      const level = Math.min(heading[1].length, 6)
      const Tag = `h${level}` as 'h1'
      blocks.push(
        <Tag className="md-heading" key={nextKey('h')}>
          {inline(heading[2], nextKey('h'))}
        </Tag>,
      )
      index += 1
      continue
    }

    // 引用：连续若干行合并为一个块。
    if (QUOTE.test(line)) {
      const body: string[] = []
      while (index < lines.length && QUOTE.test(lines[index])) {
        body.push(QUOTE.exec(lines[index])![1])
        index += 1
      }
      blocks.push(
        <blockquote className="md-quote" key={nextKey('quote')}>
          {parseBlocks(body.join('\n'))}
        </blockquote>,
      )
      continue
    }

    // 表格：当前行是行、下一行是分隔行才认。
    if (TABLE_ROW.test(line) && index + 1 < lines.length && TABLE_SEP.test(lines[index + 1])) {
      const header = splitRow(line)
      index += 2
      const rows: string[][] = []
      while (index < lines.length && TABLE_ROW.test(lines[index])) {
        rows.push(splitRow(lines[index]))
        index += 1
      }
      const tableKey = nextKey('table')
      blocks.push(
        <div className="md-table-wrap" key={tableKey}>
          <table className="md-table">
            <thead>
              <tr>
                {header.map((cell, cellIndex) => (
                  <th key={`${tableKey}-h${cellIndex}`}>{inline(cell, tableKey)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={`${tableKey}-r${rowIndex}`}>
                  {header.map((_, cellIndex) => (
                    <td key={`${tableKey}-r${rowIndex}c${cellIndex}`}>
                      {inline(row[cellIndex] ?? '', `${tableKey}-r${rowIndex}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    // 列表：连续的列表行收成一块，缩进决定层级。
    if (BULLET.test(line) || NUMBERED.test(line)) {
      const collected: { indent: number; ordered: boolean; marker: string; text: string }[] = []
      while (index < lines.length) {
        const current = lines[index]
        const bullet = BULLET.exec(current)
        const numbered = NUMBERED.exec(current)
        if (bullet) {
          collected.push({
            indent: bullet[1].replace(/\t/g, '  ').length,
            ordered: false,
            marker: bullet[2],
            text: bullet[3],
          })
        } else if (numbered) {
          collected.push({
            indent: numbered[1].replace(/\t/g, '  ').length,
            ordered: true,
            marker: numbered[2],
            text: numbered[3],
          })
        } else if (current.trim() && /^\s+\S/.test(current) && collected.length > 0) {
          // 悬挂缩进（续行）接回上一条，避免被拆成新段落。
          collected[collected.length - 1].text += ` ${current.trim()}`
        } else {
          break
        }
        index += 1
      }
      blocks.push(renderList(buildList(collected), nextKey('list')))
      continue
    }

    // 段落：吃到空行或下一个块起始行为止。
    const paragraph: string[] = []
    while (index < lines.length) {
      const current = lines[index]
      if (
        !current.trim() ||
        HEADING.test(current) ||
        FENCE.test(current) ||
        RULE.test(current) ||
        QUOTE.test(current) ||
        BULLET.test(current) ||
        NUMBERED.test(current) ||
        (TABLE_ROW.test(current) && index + 1 < lines.length && TABLE_SEP.test(lines[index + 1]))
      ) {
        break
      }
      paragraph.push(current.trim())
      index += 1
    }
    const paragraphKey = nextKey('p')
    blocks.push(
      <p className="md-p" key={paragraphKey}>
        {inline(paragraph.join(' '), paragraphKey)}
      </p>,
    )
  }

  return blocks
}

export function Markdown({ text }: { text: string }) {
  return <div className="md">{parseBlocks(text)}</div>
}
