/**
 * 参考标准表页。
 *
 * 回答一个问题：**某个回传字段，到底该怎么填。**「规则清单」按规则组织，这一页按**字段**
 * 组织——排查「这个字段为什么填错了」时，人是从字段出发的。
 *
 * 三条口径：
 *
 * 1. **派生列不在这里维护**。名称、业务类型、输出模式、定义全部由后端现读契约与编外
 *    登记表，前端只负责排版。契约一变，刷新即对齐。
 * 2. **准入模式必须写在最显眼的位置**。它是本页唯一需要人工判断的一列：事实提取的字段
 *    只能指回原文，受控推断的才允许归纳。看到某个字段降级了，先看它是哪一类。
 * 3. **挂在本行的规则要能看到正文**。这些口径是真正递给模型的话，只显示编号等于只给目录。
 *
 * 与「规则清单」的分工：单字段提示词规则已收进本页，规则清单只留全局规则与跨字段规则。
 */

import { Fragment, useEffect, useMemo, useState } from 'react'

import { api, type StandardRow, type StandardTable as StandardTablePayload } from '../api/client'

const ADMISSION_ORDER = ['fact', 'controlled_inference']

const CHANNEL_LABEL: Record<string, string> = {
  code: '代码',
  prompt: '提示词',
  llm_checker: '模型复核',
}

function StandardDetail({ row }: { row: StandardRow }) {
  return (
    <div className="rule-detail">
      <dl className="meta-list">
        <div>
          <dt>定义</dt>
          <dd>{row.description || '—'}</dd>
        </div>
        <div>
          <dt>准入模式</dt>
          <dd>
            <span className={`chip admission-${row.admission}`}>{row.admission_label}</span>
            <span className="cell-soft"> {row.admission_hint}</span>
          </dd>
        </div>
        <div>
          <dt>输出模式</dt>
          <dd>{row.output_mode}</dd>
        </div>
        {[
          ['标准推理路径', row.reasoning_path],
          ['禁止项', row.forbidden],
          ['参考备注', row.note],
        ].map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>
              {value ? (
                value
              ) : (
                <span className="cell-soft">暂无附加口径（判定以左下规则正文与字段定义为准）</span>
              )}
            </dd>
          </div>
        ))}
      </dl>

      {row.rules.length > 0 ? (
        <div className="rule-text">
          <h4>本字段的规则正文（递给模型的原话）</h4>
          {row.rules.map((rule) => (
            <div className="standard-rule" key={rule.id}>
              <div className="standard-rule-head">
                <span className="cell-mono">{rule.id}</span>
                <span>{rule.title}</span>
                <span className="chip chip-quiet">
                  {rule.channels.map((item) => CHANNEL_LABEL[item] ?? item).join(' / ')}
                </span>
              </div>
              {rule.text ? <pre>{rule.text}</pre> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

export function StandardTable() {
  const [payload, setPayload] = useState<StandardTablePayload | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const [category, setCategory] = useState('')
  const [admission, setAdmission] = useState('')
  const [keyword, setKeyword] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    api
      .standards()
      .then(setPayload)
      .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : String(exc)))
      .finally(() => setLoading(false))
  }, [])

  const rows = useMemo(() => payload?.rows ?? [], [payload])

  // 分类清单取自数据本身，不写死：契约新增业务类型时页面自动多一个选项。
  const categories = useMemo(() => {
    const seen: string[] = []
    for (const row of rows) if (!seen.includes(row.category)) seen.push(row.category)
    return seen
  }, [rows])

  const shown = useMemo(() => {
    const text = keyword.trim().toLowerCase()
    return rows.filter((row) => {
      if (category && row.category !== category) return false
      if (admission && row.admission !== admission) return false
      if (!text) return true
      return (
        row.code.toLowerCase().includes(text) ||
        row.attribute_key.toLowerCase().includes(text) ||
        row.name.toLowerCase().includes(text) ||
        row.description.toLowerCase().includes(text)
      )
    })
  }, [rows, category, admission, keyword])

  const modeCounts = payload?.by_admission ?? {}

  return (
    <div className="run-list">
      <div className="rule-summary">
        <strong>共 {payload?.total ?? 0} 个字段</strong>
        <span className="dot-sep">·</span>
        {ADMISSION_ORDER.map((mode) =>
          modeCounts[mode] ? (
            <span key={mode} className={`rule-count chip admission-${mode}`}>
              {payload?.admission_labels[mode] ?? mode} {modeCounts[mode]}
            </span>
          ) : null,
        )}
        <span className="dot-sep">·</span>
        <span className="cell-soft">契约版本 {payload?.contract_signature ?? '—'}</span>
      </div>

      <div className="toolbar">
        <div className="field">
          <label htmlFor="standard-category">业务类型</label>
          <select
            id="standard-category"
            value={category}
            onChange={(event) => setCategory(event.target.value)}
          >
            <option value="">全部</option>
            {categories.map((item) => (
              <option value={item} key={item}>
                {item}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="standard-admission">准入模式</label>
          <select
            id="standard-admission"
            value={admission}
            onChange={(event) => setAdmission(event.target.value)}
          >
            <option value="">全部</option>
            {ADMISSION_ORDER.map((mode) => (
              <option value={mode} key={mode}>
                {payload?.admission_labels[mode] ?? mode}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="standard-keyword">关键词</label>
          <input
            id="standard-keyword"
            type="text"
            value={keyword}
            placeholder="编号、字段名或系统键"
            onChange={(event) => setKeyword(event.target.value)}
          />
        </div>
        <div className="toolbar-actions">
          <span className="cell-soft">
            {shown.length} / {rows.length} 个字段
          </span>
        </div>
      </div>

      {error ? <div className="app-error">{error}</div> : null}

      <div className="data-card">
        <div className="data-table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: 72 }}>编号</th>
                <th>字段</th>
                <th style={{ width: 128 }}>业务类型</th>
                <th style={{ width: 116 }}>准入模式</th>
                <th style={{ width: 240 }}>输出模式</th>
                <th style={{ width: 96 }}>规则</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => (
                // 主行与展开行同属一个 keyed 片段：否则展开/收起会让相邻行错位。
                <Fragment key={row.code}>
                  <tr
                    onClick={() => setExpanded(expanded === row.code ? null : row.code)}
                    title={row.description}
                  >
                    <td className="cell-mono">{row.code}</td>
                    <td>
                      {row.name}
                      <span className="cell-soft"> {row.attribute_key}</span>
                      {row.scope_label !== '回传 Notes' ? (
                        <span className="chip chip-quiet">{row.scope_label}</span>
                      ) : null}
                      {expanded === row.code ? (
                        <span className="chip chip-link">已展开</span>
                      ) : null}
                    </td>
                    <td className="cell-soft">{row.category}</td>
                    <td>
                      <span className={`chip admission-${row.admission}`}>
                        {row.admission_label}
                      </span>
                    </td>
                    <td className="cell-soft cell-clip">{row.output_mode}</td>
                    <td className="cell-soft">
                      {row.rules.length > 0 ? row.rules.length : '—'}
                    </td>
                  </tr>
                  {expanded === row.code ? (
                    <tr className="rule-detail-row">
                      <td colSpan={6}>
                        <StandardDetail row={row} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
              {shown.length === 0 && !loading ? (
                <tr>
                  <td className="cell-empty" colSpan={6}>
                    没有符合条件的字段。
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>

      <p className="panel-footnote">
        本页为只读视图，内容由契约、字段登记表与规则正文现读拼成，不单独维护；单字段规则已并入本页，
        全局与跨字段规则仍在「规则清单」。字段编号仅供内部指代，不得出现在对客产物里。
      </p>
    </div>
  )
}
