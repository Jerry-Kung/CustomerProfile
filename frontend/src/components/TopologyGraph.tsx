/**
 * 拓扑图组件：把后端定义渲染成可缩放、可平移、可点选的图。
 *
 * 三条硬约束（docs/specs/V0.4只读运行台.md §2）：
 *
 * 1. **坐标由后端给**（`definition.layout`），前端不算布局。前端只保留一套渲染逻辑。
 * 2. **边带分支**。`source_handle` 为 `true` / `false` 时是 if-else 的两支，用不同颜色
 *    区分。把两支画成一样的线会让「这条边实际没走」在视觉上无从判断。
 * 3. **只读**。没有拖拽保存、没有连线编辑（迁移说明 §8 明确不做）。
 */

import { useMemo } from 'react'
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import type { Definition, NodeExecution, NodeStatus } from '../api/client'

/** 节点状态 → 颜色。与 `definitions.NodeStatus` 同值域。 */
const STATUS_COLOR: Record<NodeStatus, string> = {
  pending: '#9ca3af',
  running: '#3b82f6',
  succeeded: '#22c55e',
  failed: '#ef4444',
  skipped: '#a855f7',
  blocked: '#f97316',
}

/** 分支边的颜色。非分支边用中性色。 */
const BRANCH_COLOR: Record<string, string> = {
  true: '#22c55e',
  false: '#f97316',
  'fail-branch': '#ef4444',
}

export interface TopologyGraphProps {
  definition: Definition
  /** 运行详情用：节点 ID → 执行记录。不传则为纯结构视图。 */
  executions?: NodeExecution[]
  onSelectNode?: (nodeId: string) => void
}

export function TopologyGraph({
  definition,
  executions,
  onSelectNode,
}: TopologyGraphProps) {
  const statusByNode = useMemo(() => {
    const map = new Map<string, NodeExecution>()
    for (const execution of executions ?? []) map.set(execution.node_id, execution)
    return map
  }, [executions])

  const nodes = useMemo<Node[]>(
    () =>
      definition.nodes.map((node) => {
        const position = definition.layout[node.node_id] ?? [0, 0]
        const execution = statusByNode.get(node.node_id)
        const status: NodeStatus | null = execution?.status ?? null
        return {
          id: node.node_id,
          position: { x: position[0], y: position[1] },
          data: {
            label: (
              <div className="node-card">
                <div className="node-title">{node.title}</div>
                <div className="node-type">{node.type}</div>
                {execution && execution.duration_ms !== null && (
                  <div className="node-meta">{execution.duration_ms} ms</div>
                )}
                {execution && execution.attempt_count > 0 && (
                  <div className="node-meta">
                    {execution.attempt_count} 次尝试
                  </div>
                )}
              </div>
            ),
          },
          style: {
            borderColor: status ? STATUS_COLOR[status] : '#cbd5e1',
            borderWidth: status ? 3 : 1,
            background: '#fff',
            borderRadius: 6,
            padding: 6,
            width: 200,
          },
        }
      }),
    [definition, statusByNode],
  )

  const edges = useMemo<Edge[]>(
    () =>
      definition.edges.map((edge) => {
        const color =
          edge.source_handle === 'source'
            ? '#94a3b8'
            : (BRANCH_COLOR[edge.source_handle] ?? '#94a3b8')
        return {
          id: `${edge.source}->${edge.target}:${edge.source_handle}`,
          source: edge.source,
          target: edge.target,
          type: 'smoothstep',
          animated: false,
          style: { stroke: color },
          // 分支名画在边中间。看不出「哪条边是哪一支」时，分支图等于没画。
          label:
            edge.source_handle === 'source' ? undefined : edge.source_handle,
        }
      }),
    [definition],
  )

  return (
    <div className="graph-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodeClick={(_, node) => onSelectNode?.(node.id)}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        fitView
        minZoom={0.05}
      >
        <Background />
        <Controls />
        <MiniMap
          pannable
          zoomable
          nodeColor={(node) => {
            const execution = statusByNode.get(node.id)
            return execution ? STATUS_COLOR[execution.status] : '#cbd5e1'
          }}
        />
      </ReactFlow>
    </div>
  )
}
