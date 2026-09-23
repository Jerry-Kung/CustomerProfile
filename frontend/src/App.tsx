/**
 * 运行台主界面。
 *
 * 两个标签页：工作流结构（v0.4.1）与任务列表（v0.4.2）。运行详情（v0.4.3）从列表
 * 点入后占同一位置。刻意不引入路由库——三个视图、无深链接需求，一个状态变量够用，
 * 加一个依赖就要多解释一层。
 */

import { useEffect, useState } from 'react'

import { api, type Definition, type WorkflowBrief } from './api/client'
import { TopologyGraph } from './components/TopologyGraph'
import { RunList } from './components/RunList'

type Tab = 'structure' | 'runs'

function App() {
  const [tab, setTab] = useState<Tab>('structure')
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
    if (!selected || tab !== 'structure') return
    setError('')
    api
      .topology(selected)
      .then(setDefinition)
      .catch((exc: unknown) =>
        setError(exc instanceof Error ? exc.message : String(exc)),
      )
  }, [selected, tab])

  return (
    <div className="app">
      <header className="app-header">
        <h1>客户画像运行台</h1>
        <nav className="app-tabs">
          <button
            type="button"
            className={tab === 'structure' ? 'active' : ''}
            onClick={() => setTab('structure')}
          >
            工作流结构
          </button>
          <button
            type="button"
            className={tab === 'runs' ? 'active' : ''}
            onClick={() => setTab('runs')}
          >
            任务列表
          </button>
        </nav>
        {tab === 'structure' && (
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
        )}
      </header>

      {error && <div className="app-error">{error}</div>}

      <main className="app-body">
        {tab === 'structure' ? (
          definition ? (
            <TopologyGraph definition={definition} />
          ) : (
            <div className="app-placeholder">正在加载工作流结构…</div>
          )
        ) : (
          <RunList onOpenRun={(runId) => console.info('查看运行', runId)} />
        )}
      </main>
    </div>
  )
}

export default App
