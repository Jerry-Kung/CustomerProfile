/**
 * 节点业务名、结果摘要与结果状态。
 *
 * 三条口径：
 *
 * 1. **只读不改**。这里产出的全部是**展示副本**：原始标题、原始输出一律不动，
 *    详情页照旧给出完整数据。摘要是给列表扫读用的，不替代、不改写结果。
 * 2. **不推断**。缺少可靠依据时给中性描述（「已执行，未记录输出」），
 *    绝不由「耗时短」「空对象」推演出「无有效信息」这类结论。
 * 3. **空串是合法产出**（见 `docs/design/执行语义口径.md` P4）。判断「有没有内容」
 *    看的是「有没有记到输出字段」，不是「值非空」。
 */

import type { GraphNode, NodeExecution } from '../api/client'

/* ------------------------------------------------------------------ 业务名 */

/**
 * 清洗展示用节点名。
 *
 * 真实标题带开发期前缀与后缀：`（新）SubAgent - 微信主页信息提取`、
 * `Notes生成 - 工具`、`（新）SubAgent - 极光数据（生产环境）`。
 * 这些在列表里反复出现会挤掉真正有辨识度的部分。
 *
 * 只在**能判定是开发标记**时剥离，且剥完为空则原样返回——
 * 宁可不洗，不能把标题洗没。
 */
export function displayTitle(raw: string | null | undefined, fallback = ''): string {
  if (!raw) return fallback
  let text = raw.trim()
  const original = text

  // 前缀：`（新）SubAgent - ` / `（新）SubAgent-` / `（新）`
  text = text.replace(/^[（(]新[）)]\s*SubAgent\s*[-–—]\s*/i, '')
  text = text.replace(/^[（(]新[）)]\s*/, '')
  // 后缀：` - 工具` / `（工具）`；`（生产环境）` 保留——它区分同一职责的两个节点。
  text = text.replace(/\s*[-–—]\s*工具$/, '')
  text = text.replace(/[（(]\s*工具\s*[）)]$/, '')
  // 尾部单独的「（新）」。真实标题里有「…（生产环境）（新）」这种形态，
  // 前缀剥掉之后尾巴上还挂着一个开发标记。
  text = text.replace(/\s*[（(]\s*新\s*[）)]\s*$/, '')
  text = text.trim()

  return text || original
}

/* ------------------------------------------------------- 节点类别（展示用） */

/**
 * 节点类别别称。
 *
 * 这是**按节点类型给的展示归类**，不是从业务语义推断出来的结论。
 * 类型本身仍在详情的「执行信息」里原样展示。
 */
const TYPE_CATEGORY: Record<string, string> = {
  start: '用户输入',
  end: '结果输出',
  llm: '模型分析',
  tool: '数据获取',
  'http-request': '接口调用',
  code: '数据处理',
  'template-transform': '文本组织',
  'if-else': '条件判断',
  iteration: '迭代处理',
  'iteration-start': '迭代项',
  'variable-aggregator': '结果汇总',
}

export function nodeCategory(nodeType: string, hasChildRun: boolean): string {
  // 有子运行的节点，其真实产出在子流程里；标成「子流程」比标成「数据获取」有用。
  if (hasChildRun) return '子流程'
  return TYPE_CATEGORY[nodeType] ?? nodeType
}

/* --------------------------------------------------- 阶段与执行成本（展示用） */

/**
 * 阶段芯片：从节点标题的 `L<数字>` 前缀派生（`L0 记录层` → `L0`，`L1-1a 事实提取层` → `L1`）。
 *
 * **取不到即返回 null**，由调用方不显示该芯片——不猜。
 * 这是刻意的：标题前缀不覆盖全部节点（如 `确定性字段注入` 属 L1 却无前缀），
 * 用节点 ID 硬编码去补会把展示层绑死在某一条工作流上。
 */
const STAGE_PREFIX = /^L(\d)(?!\d)/

export function stageOf(title: string | null | undefined): string | null {
  if (!title) return null
  const matched = STAGE_PREFIX.exec(title.trim())
  return matched ? `L${matched[1]}` : null
}

/** 不产生 token 的节点类型：本地执行，无外部模型/接口调用。 */
const LOCAL_TYPE = new Set([
  'code',
  'template-transform',
  'if-else',
  'iteration',
  'iteration-start',
  'variable-aggregator',
  'start',
  'end',
])

/**
 * 执行成本标签。只区分「LLM」（消耗 token）与「纯代码」（0 token）两类。
 *
 * 设计稿的成本结论（L0/L4/L2 投影/L2 应用审查/L3 校验装配全为 0 token）是这一轮的
 * 第一等结论，界面上不体现它，运行台就与设计脱节。该判据只看 `node_type`，
 * 不依赖节点命名，对全部工作流都成立。
 *
 * `tool` / `http-request` 不标注：前者是子流程（内部可能含 LLM），后者是外部接口，
 * 都既不属「纯代码」也不属「LLM」，硬归一类会是错的。
 */
export function costLabelOf(nodeType: string): string | null {
  if (nodeType === 'llm') return 'LLM'
  if (LOCAL_TYPE.has(nodeType)) return '纯代码'
  return null
}

/* ------------------------------------------------------------ 输出内容读取 */

/** 认为「可能是正文」的字段名，按优先级排列。 */
const TEXT_KEYS = [
  'text',
  'result',
  'output',
  'result1',
  'content',
  'message',
  'summary',
  'body',
  'data',
]

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * 从字符串里尝试解出一层 JSON。
 *
 * **只解一层**，且解析失败就原样返回：深层反复解析会破坏代码、路径与合法原文
 * （设计文档 §5.1）。解出来的对象不替换原始文本，只用于生成摘要与结构化视图。
 */
export function parseJsonLoose(value: string): unknown {
  const text = value.trim()
  if (!text) return null
  if (!(text.startsWith('{') || text.startsWith('['))) return null
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

/** 判断一个输出对象是否「记到了字段」。`{}` 与 `null` 都算没记到。 */
export function hasRecordedFields(outputs: Record<string, unknown> | null): boolean {
  if (!outputs) return false
  return Object.keys(outputs).length > 0
}

/**
 * 取输出里的主载荷。
 *
 * 顺序：已知正文键 → 唯一的字符串值 → 唯一的任意值。
 * 取不到就返回 null，由调用方降级，不猜。
 */
export function primaryOutput(
  outputs: Record<string, unknown> | null,
): { key: string; value: unknown } | null {
  if (!outputs) return null
  for (const key of TEXT_KEYS) {
    if (key in outputs) return { key, value: outputs[key] }
  }
  const entries = Object.entries(outputs)
  if (entries.length === 1) return { key: entries[0][0], value: entries[0][1] }
  const strings = entries.filter(([, v]) => typeof v === 'string' && v.trim() !== '')
  if (strings.length === 1) return { key: strings[0][0], value: strings[0][1] }
  // 多字段且没有已知正文键。此处返回 null 不是「没内容」，而是「没有单一主载荷」——
  // 由 summarizeOutputs 逐字段兜底，绝不据此判定输出为空。
  return null
}

/** 键名转人话：只做下划线与连字符的替换，不翻译键名。 */
function prettyKey(key: string): string {
  return key.replace(/[_-]+/g, ' ').trim()
}

/** 去掉 Markdown 记号，用于把长文本压成一行摘要。 */
function stripMarkup(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[*_`~]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

/**
 * 把任意输出压成一行摘要。
 *
 * 字符串可解析成 JSON 时，只在这一层里再找一次正文键；仍找不到就报字段数，
 * 而不是把整个 JSON 塞进摘要行。
 */
export function summarizeValue(value: unknown, maxLength = 90): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') {
    const parsed = parseJsonLoose(value)
    if (parsed !== null) return summarizeValue(parsed, maxLength)
    const text = stripMarkup(value)
    return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return ''
    const first = value[0]
    if (typeof first === 'string') return summarizeValue(first, maxLength)
    const head = summarizeValue(first, 36)
    return head ? `共 ${value.length} 条，首条：${head}` : `共 ${value.length} 条记录`
  }
  if (isPlainObject(value)) {
    for (const key of TEXT_KEYS) {
      if (key in value) {
        const inner = summarizeValue(value[key], maxLength)
        if (inner) return inner
      }
    }
    const entries = Object.entries(value)
    if (entries.length === 0) return ''
    // 没有已知正文键时逐字段压缩：报「字段：摘要」，比只报字段数有用得多。
    // 全字段都压不出内容（例如 `{a: "", b: {}}`）才返回空串。
    const parts: string[] = []
    for (const [key, item] of entries) {
      const inner = summarizeValue(item, 40)
      if (!inner) continue
      parts.push(entries.length > 1 ? `${prettyKey(key)}：${inner}` : inner)
      if (parts.length >= 3) break
    }
    if (parts.length === 0) return ''
    const joined = parts.join(' · ')
    return joined.length > maxLength ? `${joined.slice(0, maxLength)}…` : joined
  }
  return String(value)
}

/**
 * 一份输出的摘要。**列表行与详情共用这一个入口**，避免两处对同一份数据给出不同说法。
 *
 * 找不到主载荷时逐字段兜底；只有「确有字段、但每个字段都压不出内容」才返回空串。
 */
export function summarizeOutputs(outputs: Record<string, unknown> | null): string {
  if (!hasRecordedFields(outputs)) return ''
  const primary = primaryOutput(outputs!)
  if (primary) {
    const text = summarizeValue(primary.value)
    if (text) return text
  }
  return summarizeValue(outputs)
}

/* ------------------------------------------------------------ 结果状态判定 */

export type ResultState =
  | 'running'
  | 'pending'
  | 'failed'
  | 'blocked'
  | 'skipped'
  | 'info'
  | 'blank'
  | 'unrecorded'

/**
 * 结果状态。**与执行状态分开**：执行成功不等于产出了有效内容，
 * 执行成功也不等于没产出——空串是合法产出，`{}` 只说明没记到字段。
 */
export function resultState(execution: NodeExecution): ResultState {
  switch (execution.status) {
    case 'running':
      return 'running'
    case 'pending':
      return 'pending'
    case 'failed':
      return 'failed'
    case 'blocked':
      return 'blocked'
    case 'skipped':
      return 'skipped'
    default:
      break
  }
  // 至此只剩 succeeded（及其它非终态之外的取值），按输出内容分档。
  if (!hasRecordedFields(execution.outputs_json)) return 'unrecorded'
  // `blank` 只留给「确有字段、但每个字段都压不出内容」这一种情形
  // （例如 `{result: ""}`）。多字段无正文键不算空——那只是没有单一主载荷。
  return summarizeOutputs(execution.outputs_json) ? 'info' : 'blank'
}

/** 结果状态文案。只对「执行成功」的三种情况给出措辞。 */
const RESULT_STATE_LABEL: Record<ResultState, string> = {
  running: '运行中',
  pending: '待执行',
  failed: '执行失败',
  blocked: '被阻断',
  skipped: '已跳过',
  info: '已获取信息',
  blank: '本次未产出内容',
  unrecorded: '未记录输出',
}

export function resultStateLabel(state: ResultState): string {
  return RESULT_STATE_LABEL[state]
}

/* ------------------------------------------------------------ 回写跳过识别 */

/**
 * 从一份产出里找出「回写已跳过」载荷。
 *
 * 判据只有一条：某个字符串产出（或产出本身）解析出对象且带 `writeback_skipped: true`。
 * **不绑定工作流、不绑定节点 ID**——回写节点换名、换臂，这条判据照样成立。
 *
 * 回写节点的产出形态是 `{"body": "<json 串>", "status": 0}`，因此要逐个字符串值尝试解析；
 * 注意不能只解一层就断言「没有标记」。
 */
export function writebackPayload(
  outputs: Record<string, unknown> | null,
): Record<string, unknown> | null {
  if (!outputs) return null
  for (const value of Object.values(outputs)) {
    const parsed = typeof value === 'string' ? parseJsonLoose(value) : value
    if (isPlainObject(parsed) && parsed.writeback_skipped === true) return parsed
  }
  return null
}

/* ------------------------------------------------------------ 回传体定位 */

/**
 * 从当次定义快照定位回写节点，取它上游构造节点产出的**回传体**（六键 `analysis_result`）。
 *
 * 三步，全部来自运行数据，**不写死工作流名或节点 ID**：
 * 1. 定义快照里 `config['@writeback'] === true` 的节点即回写节点；
 * 2. 它的第一个直接前置即构造节点（`callback_build`）；
 * 3. 该节点产出的 `callback_json` 是 JSON **字符串**，解一层得到回传体对象。
 *
 * 找不到回写节点、构造节点未执行、或产出不是合法对象时返回 null——由调用方不渲染该段，
 * 绝不用别处的数据顶替。
 */
export function callbackPayloadOf(
  definitionNodes: GraphNode[],
  executions: NodeExecution[],
): Record<string, unknown> | null {
  const writeback = definitionNodes.find((node) => node.config?.['@writeback'] === true)
  if (!writeback) return null
  const builderId = writeback.direct_predecessors?.[0]
  if (!builderId) return null
  const builder = executions.find((item) => item.node_id === builderId)
  if (!builder?.outputs_json) return null
  const raw = builder.outputs_json['callback_json']
  const parsed = typeof raw === 'string' ? parseJsonLoose(raw) : raw
  return isPlainObject(parsed) ? parsed : null
}

/**
 * 列表第二行的结果摘要。
 *
 * 排序即优先级：先讲「为什么没有结果」，再讲「结果是什么」。
 */
export function resultSummary(execution: NodeExecution): string {
  const child = execution.sub_run_id
  switch (execution.status) {
    case 'running':
      return '正在执行…'
    case 'pending':
      return '等待执行。'
    case 'failed':
      return firstLine(execution.error) || '执行失败，展开查看错误详情。'
    case 'blocked':
      return '前置节点未成功，本次未执行。'
    case 'skipped':
      return execution.error
        ? `未走这条分支：${firstLine(execution.error)}`
        : '本次未走这条分支。'
    default:
      break
  }

  // 回写跳过是一个**有明确业务含义的正常态**（调测期不回传），不是通用输出。
  // 先于通用摘要判定，避免它被渲染成一段要解读的英文键值（键名英文、布尔值未中文化、
  // 理由被 40 字截断到半句）。
  const skipped = writebackPayload(execution.outputs_json)
  if (skipped) {
    const reason = typeof skipped.reason === 'string' ? skipped.reason.trim() : ''
    return reason ? `回写已跳过（${reason}）` : '回写已跳过（本轮未开启回写）。'
  }

  const state = resultState(execution)
  if (state === 'unrecorded') {
    // 有子运行的节点输出常为空对象——真实结果在子流程里，这是正常情形，不是缺数据。
    return child
      ? '结果产出在子流程中，可进入查看。'
      : '本次未记录输出字段，可展开查看输入与调用记录。'
  }
  if (state === 'blank') return '已执行，本次输出为空。'

  return summarizeOutputs(execution.outputs_json) || '已生成输出，查看详情。'
}

function firstLine(text: string | null | undefined): string {
  if (!text) return ''
  const line = text.split('\n').find((item) => item.trim()) ?? ''
  return line.length > 120 ? `${line.slice(0, 120)}…` : line.trim()
}

/* ------------------------------------------------------------------ 搜索 */

/** 搜索用文本：业务名、原始标题、节点 ID、类别。 */
export function searchTextOf(execution: NodeExecution, fallbackTitle: string): string {
  const title = displayTitle(execution.node_title, fallbackTitle)
  return [
    title,
    execution.node_title ?? '',
    execution.node_id,
    execution.node_type,
    nodeCategory(execution.node_type, Boolean(execution.sub_run_id)),
  ]
    .join(' ')
    .toLowerCase()
}

/**
 * 执行身份：节点 ID + 调用路径。
 *
 * 迭代节点的各轮共享同一个 `node_id`，只有 `call_path` 不同；只按节点 ID 定位会
 * 把多轮混成一条。选中项、尝试查询、滚动锚点都用这个键。
 */
export function executionKey(execution: {
  node_id: string
  call_path: string
}): string {
  return `${execution.node_id}:${execution.call_path}`
}

/* --------------------------------------------------------------- 完成时刻 */

/**
 * 排序键：完成时刻。
 *
 * 用户期望「已完成节点按完成时间从上到下」；库里有 `started_at_ms` 与 `duration_ms`，
 * 两者相加即完成时刻。缺任一项的排到最后——不拿开始时间冒充完成时间（设计文档 §6.1）。
 *
 * **`0` 与 `null` 同等看待**：实测未执行的节点（`blocked` / `skipped`）在库里记的是
 * `started_at_ms = 0, duration_ms = 0`，而不是 null。只判 null 会让它们在键值 0 处
 * 排到时间线最前面，把「输出（被阻断）」顶到第 01 位——那既不是开始时间，也不是完成时间。
 */
export function completionOrderKey(execution: NodeExecution): number {
  const { started_at_ms: started, duration_ms: duration } = execution
  if (!started || started <= 0) return Number.POSITIVE_INFINITY
  if (duration === null || duration === undefined) return Number.POSITIVE_INFINITY
  return started + duration
}
