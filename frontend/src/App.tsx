/**
 * 运行台主界面。
 *
 * 两个标签页：任务列表、提交任务。运行详情从列表点入后占同一位置。
 * 刻意不引入路由库——三个视图、无深链接需求，一个状态变量够用，
 * 加一个依赖就要多解释一层。
 *
 * 原「工作流结构」标签页已移除：它必须先渲染一张依赖图，而连线图这一表达方式
 * 本身已被放弃。看某个工作流的结构，改为在运行详情里按实际执行顺序看。
 */

import { useEffect, useState } from 'react'

import { api, type RuntimeInfo } from './api/client'
import { RunList } from './components/RunList'
import { RunDetail } from './components/RunDetail'
import { SubmitRun } from './components/SubmitRun'

type Tab = 'runs' | 'submit'

function App() {
  const [tab, setTab] = useState<Tab>('runs')
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null)

  /**
   * 运行详情的导航栈。
   *
   * 用栈而不是单个 runId：子运行可继续下钻（主入口 → 画像生成 → 其子流程），
   * 单值会让「返回」只能退回列表，看不到上一层。
   */
  const [runStack, setRunStack] = useState<string[]>([])
  const currentRun = runStack[runStack.length - 1] ?? ''

  // 运行态只拉一次：它描述的是进程配置，运行期不会变。
  // 失败不覆盖全局错误条——提交页缺了这份信息仍可提交，不该因此挡住整个界面。
  useEffect(() => {
    api
      .runtime()
      .then(setRuntime)
      .catch(() => setRuntime(null))
  }, [])

  return (
    <div className="app">
      <header className="app-header">
        <h1>客户画像运行台</h1>
        <nav className="app-tabs">
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
      </header>

      <main className="app-body">
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
