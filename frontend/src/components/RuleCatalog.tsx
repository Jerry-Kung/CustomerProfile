/**
 * 规则清单页。
 *
 * 回答一个问题：**这个项目现在配了哪些规则，每条具体是什么。**
 *
 * 版式沿用列表页：筛选条 → 表格 → 行内展开详情。三条口径：
 *
 * 1. **先给规模，再给细节**。顶部一行说清「共几条 / 生效几条 / 各通道几条」，
 *    筛选条只在需要时才用；打开页面不该先看到一张需要读的表。
 * 2. **每条规则的「内容」必须可见**。`message_internal` 是一句话口径，prompt 通道的
 *    `text` 是真正递给模型的正文——看规则而看不到正文，等于只看目录。
 * 3. **落点如实显示**。规则登记了却没落地（没有标记）是配置错误，页面上必须能看出来，
 *    而不是等守卫在测试里报红才发现。
 *
 * 通道与阶段用固定顺序的文案映射，不按接口返回顺序渲染：`channels` 在登记表里是
 * 元组，顺序表达的是「主要通道在前」，但展示上按固定序更易横向比较。
 */

import { Fragment, useEffect, useMemo, useState } from 'react'

import { api, type RuleConfig } from '../api/client'

const CHANNEL_LABEL: Record<string, string> = {
  code: '代码',
  prompt: '提示词',
  llm_checker: '模型复核',
}

const STAGE_LABEL: Record<string, string> = {
  extract: '取证',
  project: '投影',
  express: '表达',
  gate: '审查',
}

const ACTION_LABEL: Record<string, string> = {
  reject: '整条拒收',
  downgrade: '降级',
  rewrite: '改写',
  require_confirm: '转人工确认',
  annotate: '只加说明',
}

const SEVERITY_LABEL: Record<string, string> = {
  block: '阻断',
  repair: '修复',
  warn: '提示',
}

const KIND_LABEL: Record<string, string> = {
  hard: '硬约束',
  spec: '金标准',
  consistency: '一致性',
}

const CHANNEL_ORDER = ['code', 'prompt', 'llm_checker']

function chips(values: string[], labels: Record<string, string>, order?: string[]) {
  const sorted = order
    ? [...values].sort((a, b) => order.indexOf(a) - order.indexOf(b))
    : values
  return sorted.map((value) => (
    <span className="chip" key={value}>
      {labels[value] ?? value}
    </span>
  ))
}

function RuleDetail({ rule }: { rule: RuleConfig }) {
  return (
    <div className="rule-detail">
      <dl className="meta-list">
        <div>
          <dt>一句话口径</dt>
          <dd>{rule.message_internal || '—'}</dd>
        </div>
        <div>
          <dt>对客说法</dt>
          <dd>
            {rule.message_customer || (
              <span className="cell-soft">无需对客说明（本条不写进回传体）</span>
            )}
          </dd>
        </div>
        <div>
          <dt>作用字段</dt>
          <dd>
            {rule.scope.join('、')}
            {rule.scope.includes('*') ? (
              <span className="chip chip-quiet">全部字段</span>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>内容类</dt>
          <dd>{KIND_LABEL[rule.content_kind] ?? rule.content_kind}</dd>
        </div>
        <div>
          <dt>归属</dt>
          <dd>
            {rule.owner === 'business'
              ? '业务'
              : rule.owner === 'engineering'
                ? '工程'
                : '业务 + 工程'}
            <span className="dot-sep">·</span>版本 {rule.version}
          </dd>
        </div>
        {rule.legacy_codes.length > 0 ? (
          <div>
            <dt>旧编码</dt>
            <dd>
              {rule.legacy_codes.map((code) => (
                <span className="tag-plain" key={code}>
                  {code}
                </span>
              ))}
            </dd>
          </div>
        ) : null}
        <div>
          <dt>落点</dt>
          <dd>
            {rule.landings.length === 0 ? (
              <span className="rule-missing">未检索到落点（登记了但没落地）</span>
            ) : (
              rule.landings.map((landing) => (
                <div key={`${landing.path}:${landing.line}`} className="rule-landing">
                  {landing.path}:{landing.line}
                  <span className="chip chip-quiet">
                    {landing.kind === 'text' ? '正文' : '代码'}
                  </span>
                </div>
              ))
            )}
          </dd>
        </div>
        {rule.tests.length > 0 ? (
          <div>
            <dt>验证用例</dt>
            <dd>
              {rule.tests.map((test) => (
                <div key={test} className="rule-test">
                  {test}
                </div>
              ))}
            </dd>
          </div>
        ) : null}
      </dl>

      {rule.text ? (
        <div className="rule-text">
          <h4>提示词正文（递给模型的原话）</h4>
          <pre>{rule.text}</pre>
        </div>
      ) : null}
    </div>
  )
}

export function RuleCatalog() {
  const [rules, setRules] = useState<RuleConfig[]>([])
  const [active, setActive] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const [cluster, setCluster] = useState('')
  const [channel, setChannel] = useState('')
  const [keyword, setKeyword] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    api
      .rules()
      .then((payload) => {
        setRules(payload.rules)
        setActive(payload.active)
      })
      .catch((exc: unknown) =>
        setError(exc instanceof Error ? exc.message : String(exc)),
      )
      .finally(() => setLoading(false))
  }, [])

  // 簇清单取自数据本身，不写死：新增簇时页面自动多一个选项。
  const clusters = useMemo(() => {
    const map = new Map<string, string>()
    for (const rule of rules) map.set(rule.cluster, rule.cluster_title)
    return [...map].sort((a, b) => a[1].localeCompare(b[1], 'zh-Hans-CN'))
  }, [rules])

  const counts = useMemo(() => {
    const byChannel: Record<string, number> = {}
    for (const rule of rules) {
      for (const item of rule.channels) {
        byChannel[item] = (byChannel[item] ?? 0) + 1
      }
    }
    return byChannel
  }, [rules])

  const shown = useMemo(() => {
    const text = keyword.trim().toLowerCase()
    return rules.filter((rule) => {
      if (cluster && rule.cluster !== cluster) return false
      if (channel && !rule.channels.includes(channel)) return false
      if (!text) return true
      return (
        rule.id.toLowerCase().includes(text) ||
        rule.title.toLowerCase().includes(text) ||
        rule.message_internal.toLowerCase().includes(text) ||
        rule.text.toLowerCase().includes(text)
      )
    })
  }, [rules, cluster, channel, keyword])

  return (
    <div className="run-list">
      <div className="rule-summary">
        <strong>共配置 {rules.length} 条规则</strong>
        <span className="dot-sep">·</span>
        <span>生效 {active} 条</span>
        {CHANNEL_ORDER.map((item) =>
          counts[item] ? (
            <span key={item} className="rule-count">
              {CHANNEL_LABEL[item]} {counts[item]}
            </span>
          ) : null,
        )}
      </div>

      <div className="toolbar">
        <div className="field">
          <label htmlFor="rule-cluster">簇</label>
          <select
            id="rule-cluster"
            value={cluster}
            onChange={(event) => setCluster(event.target.value)}
          >
            <option value="">全部</option>
            {clusters.map(([value, title]) => (
              <option value={value} key={value}>
                {title}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="rule-channel">通道</label>
          <select
            id="rule-channel"
            value={channel}
            onChange={(event) => setChannel(event.target.value)}
          >
            <option value="">全部</option>
            {CHANNEL_ORDER.map((item) => (
              <option value={item} key={item}>
                {CHANNEL_LABEL[item]}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="rule-keyword">关键词</label>
          <input
            id="rule-keyword"
            type="text"
            value={keyword}
            placeholder="编号、名称或口径"
            onChange={(event) => setKeyword(event.target.value)}
          />
        </div>
        <div className="toolbar-actions">
          <span className="cell-soft">
            {shown.length} / {rules.length} 条
          </span>
        </div>
      </div>

      {error ? <div className="app-error">{error}</div> : null}

      <div className="data-card">
        <div className="data-table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: 96 }}>编号</th>
                <th>名称</th>
                <th style={{ width: 108 }}>簇</th>
                <th style={{ width: 190 }}>通道</th>
                <th style={{ width: 120 }}>阶段</th>
                <th style={{ width: 84 }}>严重度</th>
                <th style={{ width: 108 }}>默认动作</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((rule) => (
                // 主行与展开行必须同属一个 keyed 片段：片段没有 key 时 React 按数组下标
                // 复用，展开/收起会让相邻规则的行错位。
                <Fragment key={rule.id}>
                  <tr
                    onClick={() =>
                      setExpanded(expanded === rule.id ? null : rule.id)
                    }
                    title={rule.message_internal}
                  >
                    <td className="cell-mono">{rule.id}</td>
                    <td>
                      {rule.title}
                      {expanded === rule.id ? (
                        <span className="chip chip-link">已展开</span>
                      ) : null}
                    </td>
                    <td className="cell-soft">{rule.cluster_title}</td>
                    <td>{chips(rule.channels, CHANNEL_LABEL, CHANNEL_ORDER)}</td>
                    <td>{chips(rule.stages, STAGE_LABEL)}</td>
                    <td className="cell-soft">
                      {SEVERITY_LABEL[rule.severity] ?? rule.severity}
                    </td>
                    <td className="cell-soft">
                      {ACTION_LABEL[rule.default_action] ?? rule.default_action}
                    </td>
                  </tr>
                  {expanded === rule.id ? (
                    <tr className="rule-detail-row">
                      <td colSpan={7}>
                        <RuleDetail rule={rule} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
              {shown.length === 0 && !loading ? (
                <tr>
                  <td className="cell-empty" colSpan={7}>
                    没有符合条件的规则。
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>

      <p className="panel-footnote">
        规则编号与口径是内部用语，不得出现在对客产物里；此处仅供排查与核对。
      </p>
    </div>
  )
}
