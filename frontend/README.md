# 客户画像运行台（只读前端）

V0.4 的运行台前端。Vite + React + TypeScript + [@xyflow/react](https://reactflow.dev)（React Flow 12）。

设计依据：`docs/specs/V0.4只读运行台.md`。**只读**——没有编辑、没有重跑、没有拖拽保存（`Dify迁移任务说明.md` §8）。

## 构建与入库约定（重要）

**构建产物 `dist/` 是入库的**，而不是加进 `.gitignore`。

- 原因：运行台要能在**未安装 node** 的机器上直接启动。
- 代价：每次改前端都必须重新构建并提交产物，否则跑的是旧界面。
- 实现：仓库根 `.gitignore` 的 `!frontend/dist/` 例外；脚手架自带的 `frontend/.gitignore`
  里 `dist` 一行也被注释掉。两处都要保持放开，缺一个产物就会被忽略。

**改完前端务必执行：**

```bash
cd frontend
npm run build        # tsc -b && vite build
git add frontend/dist
```

判断产物是否过期：`git status` 里 `frontend/dist` 无改动即已同步。

## 开发

```bash
npm install
npm run dev          # Vite dev server，API 请求按 vite.config.ts 的 proxy 转给 127.0.0.1:8000
```

后端另起：`python -m customer_profile.main`。

## 与后端的两条契约

1. **坐标由后端给**（`definition.layout`），前端**不**计算布局。布局算法在
   `src/customer_profile/layout.py`，由 Python 测试覆盖（`tests/test_layout.py`）。
   前端只渲染，保证「只有一份拓扑」。
2. **图与状态分两个字段**（`definition` / `nodes`）。不要合并：合并后「图里有但没执行」
   与「执行了但已不在图里」无从分辨。

相关端点：`GET /workflows`、`GET /workflows/{id}/topology`、`GET /definition-versions/{id}`、
`GET /runs/{id}/graph`。

## 数据边界

运行数据含客户信息（手机号、通话内容、截图内容），后端按用户决定**原文返回**、由部署侧鉴权。
前端侧约束（`docs/specs/V0.4只读运行台.md` §5）：不记录日志到外部、不把数据发往任何第三方、
不引入会把数据外发的 SDK。新增依赖时请守住这一条。
