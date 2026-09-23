/**
 * 只读 API 客户端。
 *
 * 后端与前端同源托管（V0.4.4 由 FastAPI 挂载 `frontend/dist`），因此基址缺省为空、
 * 走相对路径。开发模式用 Vite 的 proxy 转给后端（见 `vite.config.ts`）。
 *
 * **不引入任何会把数据发往第三方的 SDK。** 运行数据含客户信息（手机号、通话内容、
 * 截图内容），按 docs/specs/V0.4只读运行台.md §5 的约定，前端只与自家后端通信。
 */

/** 运行状态。与后端 `definitions.RunStatus` 同值。 */
export type RunStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'skipped'
  | 'interrupted'

/** 节点状态。`blocked` 是前置失败导致未执行，`skipped` 是没走这条分支。 */
export type NodeStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'blocked'

export interface RunSummary {
  run_id: string
  workflow_id: string
  status: RunStatus
  business_ref: string | null
  started_at_ms: number | null
  finished_at_ms: number | null
  duration_ms: number | null
  error: string | null
  is_replay: boolean
}

export interface NodeExecution {
  execution_id: number
  run_id: string
  node_id: string
  node_title: string | null
  node_type: string
  call_path: string
  status: NodeStatus
  branch: string | null
  inputs_json: Record<string, unknown> | null
  outputs_json: Record<string, unknown> | null
  error: string | null
  queued_at_ms: number | null
  started_at_ms: number | null
  duration_ms: number | null
  sub_run_id: string | null
  attempt_count: number
}

/** 图中的一条边。`source_handle` 保留分支名（`true` / `false` / `source`）。 */
export interface GraphEdge {
  source: string
  target: string
  source_handle: string
}

export interface GraphNode {
  node_id: string
  title: string
  type: string
  direct_predecessors: string[]
  branch_gates: Record<string, string>
  outputs: string[]
  config: Record<string, unknown>
  coords: [number, number] | null
}

export interface Definition {
  workflow_id: string
  display_name: string
  source_dsl: string | null
  entries: string[]
  exits: string[]
  outputs: Record<string, string>
  nodes: GraphNode[]
  edges: GraphEdge[]
  /** 节点 ID → `[x, y]`。后端按最长路径分层算出，见 layout.py。 */
  layout: Record<string, [number, number]>
}

export interface RunGraph {
  run: RunSummary
  definition_version_id: number | null
  definition: Definition
  nodes: NodeExecution[]
  /** 触发节点 ID → 子运行摘要。点该节点即可下钻。 */
  child_runs: Record<string, ChildRunBrief>
  is_replay: boolean
}

export interface ChildRunBrief {
  run_id: string
  workflow_id: string | null
  status: RunStatus | null
  duration_ms: number | null
}

/** 一个工作流的元信息，用于结构页的下拉选择。 */
export interface WorkflowBrief {
  workflow_id: string
  display_name: string
  source_dsl: string | null
  node_count: number
  outputs: Record<string, string>
}

export interface DefinitionVersion {
  version_id: number
  workflow_id: string
  definition_hash: string
  code_commit: string | null
  created_at_ms: number
  definition: Definition
}

const BASE = ''

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { accept: 'application/json' },
  })
  if (!response.ok) {
    // 把后端给的 detail 带出来。只报状态码会让「哪个 run 不存在」这类问题
    // 在前端变成无从下手的 404。
    let detail = ''
    try {
      const body = await response.json()
      detail = typeof body?.detail === 'string' ? body.detail : ''
    } catch {
      detail = ''
    }
    throw new Error(detail || `请求失败：${response.status} ${path}`)
  }
  return (await response.json()) as T
}

export const api = {
  listWorkflows: () => getJson<WorkflowBrief[]>('/workflows'),

  topology: (workflowId: string) =>
    getJson<Definition>(
      `/workflows/${encodeURIComponent(workflowId)}/topology`,
    ),

  definitionVersion: (versionId: number) =>
    getJson<DefinitionVersion>(`/definition-versions/${versionId}`),

  runGraph: (runId: string) =>
    getJson<RunGraph>(`/runs/${encodeURIComponent(runId)}/graph`),

  listRuns: (params: {
    workflow_id?: string
    status?: string
    business_ref?: string
    include_children?: boolean
    created_after_ms?: number
    created_before_ms?: number
    order_by?: string
    limit?: number
    offset?: number
  } = {}) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') query.set(key, String(value))
    }
    const suffix = query.toString() ? `?${query}` : ''
    return getJson<RunSummary[]>(`/runs${suffix}`)
  },

  /**
   * 与 `listRuns` **同一组筛选条件**下的总数，供分页算页数。
   *
   * 参数必须与 `listRuns` 一起传，且共用同一个对象——条件分叉会让总页数与实际
   * 数据不匹配，表现为翻到后半段是空页。
   */
  countRuns: (params: {
    workflow_id?: string
    status?: string
    business_ref?: string
    include_children?: boolean
    created_after_ms?: number
    created_before_ms?: number
  } = {}) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') query.set(key, String(value))
    }
    const suffix = query.toString() ? `?${query}` : ''
    return getJson<{ total: number }>(`/runs/count${suffix}`).then((r) => r.total)
  },
}
