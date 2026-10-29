/**
 * 知识库页。
 *
 * 回答一个问题：**当前系统认的在售车型是哪几款、价格对不对。**
 *
 * 这是知识库唯一需要人工肉眼核对的地方——内容写错了不会报错，只会让输出慢慢偏掉。
 *
 * 三条口径：
 *
 * 1. **只读**。没有编辑、没有刷新按钮、没有写接口。要改内容就改文件重启。
 * 2. **价格两段并列**。主力价格带（对客默认口径）与全系包络（含定制/限量版）必须
 *    同时显示：只给包络会让人以为 917 卖 110 万，只给主力带又与实际报价对不上。
 * 3. **档位表默认展开**。同一车型不同档位的价格与动力都不同，收起来等于藏掉最关键的
 *    那部分——销售报价时问的就是「哪个版本多少钱」。
 *
 * 与「参考标准表」的分工：那一页回答「某个字段该怎么填」，这一页回答「我们在卖什么」。
 */

import { useEffect, useState } from 'react'

import { api, type KnowledgeConfig, type KnowledgeModel } from '../api/client'

function ModelCard({ entry }: { entry: KnowledgeModel }) {
  const price = entry.price
  return (
    <section className="kb-model">
      <header className="kb-model-head">
        <h3>{entry.canonical_name}</h3>
        <span className="chip chip-quiet">{entry.status_label}</span>
        {entry.tier ? <span className="chip chip-quiet">{entry.tier}</span> : null}
        <span className="cell-soft">{entry.series} 系列</span>
      </header>

      <p className="kb-positioning">{entry.positioning}</p>

      <dl className="meta-list">
        <div>
          <dt>主力价格带</dt>
          <dd>
            {price.min_wan}–{price.core_max_wan} 万元
            {price.has_extended ? (
              <span className="cell-soft">
                {' '}（全系含特别/定制版最高 {price.max_wan} 万元）
              </span>
            ) : null}
          </dd>
        </div>
        {price.note ? (
          <div>
            <dt>价格口径</dt>
            <dd className="cell-soft">{price.note}</dd>
          </div>
        ) : null}
        <div>
          <dt>动力形式</dt>
          <dd>{entry.powertrain_labels.join(' / ') || '—'}</dd>
        </div>
        {entry.body ? (
          <div>
            <dt>车身</dt>
            <dd>{entry.body}</dd>
          </div>
        ) : null}
        <div>
          <dt>称谓</dt>
          <dd className="cell-soft">
            {entry.aliases.length > 0 ? entry.aliases.join(' / ') : '无别名'}
          </dd>
        </div>
        {entry.audience.length > 0 ? (
          <div>
            <dt>适合人群</dt>
            <dd>{entry.audience.join('、')}</dd>
          </div>
        ) : null}
        {entry.scenarios.length > 0 ? (
          <div>
            <dt>适用场景</dt>
            <dd>{entry.scenarios.join('、')}</dd>
          </div>
        ) : null}
      </dl>

      {entry.configs.length > 0 ? (
        <div className="kb-configs">
          <h4>档位与价格</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>档位</th>
                <th>指导价</th>
                <th>限时价</th>
                <th>驱动</th>
                <th>续航</th>
                <th>动力</th>
              </tr>
            </thead>
            <tbody>
              {entry.configs.map((config) => (
                <tr key={config.name}>
                  <td>{config.name}</td>
                  <td>{config.price_wan} 万</td>
                  <td>{config.promo_price_wan ? `${config.promo_price_wan} 万` : '—'}</td>
                  <td>{config.drive || '—'}</td>
                  <td>{config.range_km ? `${config.range_km} km` : '—'}</td>
                  <td>
                    {config.powertrain_labels.length > 0
                      ? config.powertrain_labels.join(' / ')
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {entry.highlights.length > 0 ? (
        <div className="rule-text">
          <h4>核心卖点</h4>
          <ul className="kb-highlights">
            {entry.highlights.map((item) => (
              <li key={item.title}>
                <strong>{item.title}</strong>
                {item.text ? <span className="cell-soft">　{item.text}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  )
}

export function KnowledgeBase() {
  const [payload, setPayload] = useState<KnowledgeConfig | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    api
      .knowledge()
      .then(setPayload)
      .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : String(exc)))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <div className="run-list">读取知识库…</div>
  if (error) return <div className="run-list">读取知识库失败：{error}</div>

  if (payload && !payload.available) {
    return (
      <div className="run-list">
        <div className="rule-summary">
          <strong>未启用知识库</strong>
          <span className="cell-soft">{payload.reason}</span>
        </div>
        <p className="cell-soft">
          管线会以「不启用知识」的方式运行：取证侧不给在售车型清单，意向车型不做闭集校验，
          表达侧不给产品资料。这是刻意的退化路径，不是故障。
        </p>
      </div>
    )
  }

  return (
    <div className="run-list">
      <div className="rule-summary">
        <strong>{payload?.display_name ?? '—'}</strong>
        <span className="dot-sep">·</span>
        <span className="cell-soft">{payload?.pack_id}</span>
        <span className="dot-sep">·</span>
        <span>{payload?.total_models ?? 0} 款车型</span>
        <span className="dot-sep">·</span>
        <span>{payload?.total_configs ?? 0} 个档位</span>
        <span className="dot-sep">·</span>
        <span className="cell-soft">生效于 {payload?.effective_from ?? '—'}</span>
        {typeof payload?.days_since_effective === 'number' ? (
          <span className="cell-soft">（{payload.days_since_effective} 天前）</span>
        ) : null}
        <span className="dot-sep">·</span>
        <span className="cell-mono" title="models.json 的内容指纹">
          {(payload?.content_signature ?? '').slice(0, 8)}
        </span>
      </div>

      {payload?.note ? <p className="cell-soft">{payload.note}</p> : null}

      {payload?.tiers && payload.tiers.length > 0 ? (
        <div className="rule-summary">
          <span className="cell-soft">产品矩阵：</span>
          {payload.tiers.map((tier) => (
            <span key={tier.tier} className="rule-count">
              {tier.label}
              {tier.budget_min_wan != null
                ? ` ${tier.budget_min_wan}–${tier.budget_max_wan ?? '+'} 万`
                : ''}
            </span>
          ))}
        </div>
      ) : null}

      {payload?.models.map((entry) => (
        <ModelCard entry={entry} key={entry.model_id} />
      ))}

      <p className="cell-soft">
        本页只读：知识内容写入即冻结，要改就改知识包文件并重启服务。
      </p>
    </div>
  )
}
