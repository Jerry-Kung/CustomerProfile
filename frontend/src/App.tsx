/**
 * 运行台主界面。
 *
 * V0.4.1 交付结构视图（工作流下拉 + 拓扑渲染）。任务列表与运行详情在 v0.4.2 / v0.4.3
 * 接入，因此这里的导航刻意留成占位而不是先建空页面。
 */

import { useEffect, useState } from 'react'

import { api, type Definition, type WorkflowBrief } from './api/client'
import { TopologyGraph } from './components/TopologyGraph'

function App() {
  const [workflows, setWorkflows] = useState<WorkflowBrief[]>([])
  const [selected, setSelected] = useState<string>('')
  const [definition, setDefinition] = useState<Definition | null>(null)
  const [error, setError] = useState<string>('')

  useEffect(() => {
    api
      .listWorkflows()
      .then((list) => {
        setWorkflows(list)
        // 缺省选中主入口：它是全流程起点，比按字母序选第一个更有意义。
        const entry = list.find((w) => w.workflow_id === 'customer_profile_entry')
        setSelected(entry?.workflow_id ?? list[0]?.workflow_id ?? '')
      })
      .catch((exc: unknown) =>
        setError(exc instanceof Error ? exc.message : String(exc)),
      )
  }, [])

  useEffect(() => {
    if (!selected) return
    setError('')
    api
      .topology(selected)
      .then(setDefinition)
      .catch((exc: unknown) =>
        setError(exc instanceof Error ? exc.message : String(exc)),
      )
  }, [selected])

  return (
    <div className="app">
      <header className="app-header">
        <h1>客户画像运行台</h1>
        <div className="app-controls">
          <label htmlFor="workflow-select">工作流</label>
          <select
            id="workflow-select"
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
          >
            {workflows.map((workflow) => (
              <option key={workflow.workflow_id} value={workflow.workflow_id}>
                {workflow.display_name}（{workflow.node_count} 节点）
              </option>
            ))}
          </select>
          {definition && (
            <span className="app-hint">
              来源：{definition.source_dsl ?? '人工构造'} ·{' '}
              {definition.nodes.length} 节点 / {definition.edges.length} 边
            </span>
          )}
        </div>
      </header>

      {error && <div className="app-error">{error}</div>}

      <main className="app-body">
        {definition ? (
          <TopologyGraph definition={definition} />
        ) : (
          <div className="app-placeholder">正在加载工作流结构…</div>
        )}
      </main>
    </div>
  )
}

export default App
