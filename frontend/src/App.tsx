/**
 * 运行台主界面。
 *
 * 三个标签页：工作流结构（v0.4.1）、任务列表（v0.4.2）、提交任务（v0.5.1）。
 * 运行详情（v0.4.3）从列表点入后占同一位置。刻意不引入路由库——四个视图、无深链接需求，
 * 一个状态变量够用，加一个依赖就要多解释一层。
 */

import { useEffect, useState } from 'react'

import {
  api,
  type Definition,
  type RuntimeInfo,
  type WorkflowBrief,
} from './api/client'
import { TopologyGraph } from './components/TopologyGraph'
import { RunList } from './components/RunList'
import { RunDetail } from './components/RunDetail'
import { SubmitRun } from './components/SubmitRun'

type Tab = 'structure' | 'runs' | 'submit'

function App() {
  const [tab, setTab] = useState<Tab>('structure')
  const [workflows, setWorkflows] = useState<WorkflowBrief[]>([])
  const [selected, setSelected] = useState<string>('')
  const [definition, setDefinition] = useState<Definition | null>(null)
  const [error, setError] = useState<string>('')
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null)

  /**
   * 运行详情的导航栈。
   *
   * 用栈而不是单个 runId：子运行可继续下钻（主入口 → 画像生成 → 其子流程），
   * 单值会让「返回」只能退回列表，看不到上一层。
   */
  const [runStack, setRunStack] = useState<string[]>([])
  const currentRun = runStack[runStack.length - 1] ?? ''

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

  // 运行态只拉一次：它描述的是进程配置，运行期不会变。
  // 失败不覆盖全局错误条——提交页缺了这份信息仍可提交，不该因此挡住整个界面。
  useEffect(() => {
    api
      .runtime()
      .then(setRuntime)
      .catch(() => setRuntime(null))
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
          <button
            type="button"
            className={tab === 'submit' ? 'active' : ''}
            onClick={() => setTab('submit')}
          >
            提交任务
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
        {tab === 'structure' && (
          definition ? (
            <TopologyGraph definition={definition} />
          ) : (
            <div className="app-placeholder">正在加载工作流结构…</div>
          )
        )}

        {tab === 'submit' && (
          <SubmitRun
            runtime={runtime}
            onSubmitted={(runId) => {
              // 提交后直接落到该运行的详情页并开始轮询。
              // 后端保证此时运行记录已落库，因此详情页立刻查得到。
              setRunStack([runId])
              setTab('runs')
            }}
          />
        )}

        {tab === 'runs' && !currentRun && (
          <RunList
            onOpenRun={(runId) => {
              setRunStack([runId])
            }}
          />
        )}

        {tab === 'runs' && currentRun && (
          <RunDetail
            runId={currentRun}
            onDrillDown={(runId) => setRunStack((stack) => [...stack, runId])}
            onBack={() =>
              setRunStack((stack) => (stack.length > 1 ? stack.slice(0, -1) : []))
            }
          />
        )}
      </main>
    </div>
  )
}

export default App
