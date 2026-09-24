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

/** 一次外部请求尝试。`request_json` 的形状随 `kind` 不同（llm 有 model/messages，http 有 method/url）。 */
export interface RequestAttempt {
  attempt_id: number
  run_id: string
  node_id: string
  call_path: string
  attempt_no: number
  kind: 'llm' | 'http' | string
  target: string | null
  request_json: Record<string, unknown> | null
  response_text: string | null
  status_code: number | null
  duration_ms: number | null
  error: string | null
  error_code: string | null
  usage_json: Record<string, unknown> | null
  provider_request_id: string | null
  replayed: number
  created_at_ms: number
}

/** 提交一次运行的返回值。 */
export interface SubmitResult {
  run_id: string
  workflow_id: string
}

/** 当前运行态。**只有配置，没有凭据**——三个 API Key 一律不在此。 */
export interface RuntimeInfo {
  replay_mode: 'off' | 'fixture' | string
  /** 回写开关。false 时回写节点只产出「已跳过」的结果，运行仍可能 succeeded。 */
  writeback_enabled: boolean
  llm_model: string
  max_active_runs: number | null
  max_concurrent_requests: number | null
  /** 当前占用的运行名额；未装闸门时为 null。 */
  active_runs: number | null
  available_run_slots: number | null
  serve_ui: boolean
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

/**
 * 提交一次运行。这是前端**唯一**的写操作。
 *
 * 与 `getJson` 分开而不是合并成一个通用 `request`：写与读的失败语义不同——提交被拒
 * （429 并发已满 / 422 入参非法）必须把后端的 `detail` 原样带给用户，那是用户唯一能
 * 据以纠正的线索。
 *
 * 对外契约由后端保证：拿到 `run_id` 时运行记录**已经落库**，因此可以立刻跳到详情页开始轮询。
 */
async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    let detail = ''
    try {
      const payload = await response.json()
      detail = typeof payload?.detail === 'string' ? payload.detail : ''
    } catch {
      detail = ''
    }
    throw new Error(detail || `请求失败：${response.status} ${path}`)
  }
  return (await response.json()) as T
}

export const api = {
  listWorkflows: () => getJson<WorkflowBrief[]>('/workflows'),

  /** 当前运行态（回放模式、回写开关、并发上限）。 */
  runtime: () => getJson<RuntimeInfo>('/runtime'),

  /**
   * 提交一次运行，返回 `run_id`。
   *
   * `business_ref` 缺省传手机号：列表页按业务标识检索，子运行也会自动带上它
   * （后端从 `inputs["phone_number"]` 派生）。不传的话，父运行在列表里显示 `—`，
   * 而它的子运行却带着号码。
   */
  submitRun: (payload: {
    workflow_id: string
    inputs: Record<string, unknown>
    business_ref?: string
  }) => postJson<SubmitResult>('/runs', payload),

  topology: (workflowId: string) =>
    getJson<Definition>(
      `/workflows/${encodeURIComponent(workflowId)}/topology`,
    ),

  definitionVersion: (versionId: number) =>
    getJson<DefinitionVersion>(`/definition-versions/${versionId}`),

  runGraph: (runId: string) =>
    getJson<RunGraph>(`/runs/${encodeURIComponent(runId)}/graph`),

  /**
   * 某个节点的每一次外部请求尝试。
   *
   * 按节点懒加载而不并进 `/graph`：`response_text` 单条可达上万字符，主入口跑一次有
   * 17 处 LLM 调用，全部内联会让详情页首屏为了画一张图加载好几 MB。节点行已带
   * `attempt_count`，据此决定是否需要拉。
   */
  nodeAttempts: (runId: string, nodeId: string, callPath?: string) => {
    const query = callPath ? `?call_path=${encodeURIComponent(callPath)}` : ''
    return getJson<RequestAttempt[]>(
      `/runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeId)}/attempts${query}`,
    )
  },

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
