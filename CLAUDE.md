# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

PPT Agent：AI PPT 生成工作台。把「项目初始化 → 资料搜索 → 页级研究 → 初稿 SVG → 设计稿 SVG → PPTX 导出」串成一条可观察、可干预的 agent workflow。前端 React 19 + Vite 6 + Tailwind 4（`src/`），后端 FastAPI + SQLAlchemy（`backend/`）。项目为纯 Vibe Coding 产物，存在需求偏移，注意验证现状而非轻信文档。

## 常用命令

前端（仓库根目录）：

```bash
npm run dev       # Vite dev server，端口 3000，/api 和 /storage 代理到 127.0.0.1:8000
npm run build
npm run lint      # 实为 tsc --noEmit，无 eslint
```

后端（Windows，仓库根目录）：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e backend
$env:PYTHONPATH="backend"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- 配置统一读仓库根 `.env`（复制 `.env.example`）。关键项：`DATABASE_URL`（本地可用 `sqlite:///./backend/data/ppt_agent.db`）、`CONTEXT_LLM_API_KEY`、`SVG_LLM_API_KEY`、`MCP_BOCHA_URL`、`TAVILY_API_KEY`（抓全文）。搜索/解析/模型也可在设置页配置并存库（`services/*_settings.py`），`.env` 只作种子。
- **已失效的配置项，不要照旧文档使用**：`EMBEDDING_API_KEY` / `EMBEDDING_*` 在 `core/config.py` 里**根本不存在**（`extra="ignore"` 会静默吞掉），全仓也没有任何 `/embeddings` 调用——`store_chunk_embeddings()` 只做切块入库，`vector_status` 字段和前端"向量完成"徽章都是重构残留，检索走的是 `services/evidence.py` 的确定性 `select_evidence`（Bocha 排名 + 每文档前 6 段 + token 预算），没有向量。`MCP_JINA_URL` 虽在 config 里但 `mcp_gateway` 不用它，抓全文只有 tavily / firecrawl / web_fetch（`services/reader_settings.py`）。
- 测试：pyproject 配置了 pytest（`testpaths = ["tests"]`，需 `pip install -e "backend[dev]"`），但 `backend/tests/` 目前不存在——新增测试放在该目录。
- 前端换后端地址：`$env:VITE_API_PROXY_TARGET="http://host:8000"`。

## 架构

### 后端（backend/app/）

接口前缀 `/api/v1`。核心链路是一个按项目阶段推进的状态机：`init → outline → search → draft → design → export`（见 `services/orchestrator.py` 的 `PROJECT_STAGE_ORDER`），每页独立维护状态（`empty/ready/running/confirmed/stale/failed`），每阶段都需要用户 confirm 才进入下一阶段。

- `api/routes/`：`projects.py`（项目、消息、SSE 事件流）、`requirements.py`（需求单/确认/背景资源）、`outline.py`、`pages.py`(research/draft/design/confirm/export)。路由清单见 `backend/README.md`。
- `services/orchestrator.py`：状态机与流程编排的中枢，路由决策、阶段流转、导出都在这里。
- `services/research.py`：初始化搜索（`init_corpus`）与页级研究，页与页的资料池相互隔离。
- `services/generation.py`：draft SVG / design SVG 生成。
- `services/model_gateway.py`：OpenAI 兼容模型调用（文本模型与 SVG 模型分开配 key）。
- `services/mcp_gateway.py`：Bocha/Jina 检索的 MCP 调用。
- `services/background.py` + `services/events.py`：任务通过本地后台线程异步执行（`dispatcher`），过程写事件，前端经 SSE (`/events/stream`) 实时接收 router 决策与状态变化。
- `models/entities.py`：全部 ORM 实体（Project、ProjectPage、OutlineVersion、DraftVersion、DesignVersion、ResearchSession、ExportJob 等），版本化实体（*Version）支持回放。
- 启动时自动创建 `storage/` 下的上传、背景、导出目录。

### 前端（src/）

单页工作台：`App.tsx` → `Home.tsx`（项目列表）/ `ProjectStart.tsx`（初始化）/ `Editor.tsx`（主编辑器，含 `editor/` 子组件）/ `AgentActivity.tsx`（agent 过程可视化）。`lib/ppt-api.ts` 封装全部后端调用，`lib/workflow-ui.ts` 管理工作流 UI 状态，`lib/single-flight.ts` 做请求去重。

### 项目内硬约束

`orchestrator.py` 的 `WORKFLOW_CONSTRAINTS` 定义了开发约束，需遵守：

- 禁止无意义 fallback（不能返回看似成功、实际错误的兜底结果）
- 移除无用代码，不保留失效旧逻辑
- 数据库无历史包袱，允许清库换 schema，不需要兼容老版本
- agent 消息展示可参考 `static/` 下的占位样式

## 注意事项

- `static/` 是旧版静态 POC，仅作 UI 参考，不是主入口；根目录 `index.html`/`vite.config.ts` 与 `src/` 内各有一份，实际入口以 `src/` 为准。
- 根 README 引用的 `docs/README.md` 实际不存在；docs/old 下有编号设计文档（01-09）与 `final-target-agent-workflow.md`（目标工作流约束），文档可能滞后于代码，以代码为准。

## 本项目工作原则

- 生成的文档直接放到放在docs目录下，不用考虑其它目录
- 生成的临时文件放到tmp-loc目录下，不考虑其它目录