# PPT Agent Backend

FastAPI 服务，接口前缀 `/api/v1`。配置统一读仓库根目录 `.env`（复制 `.env.example`）。

## 启动

Windows（仓库根目录）：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e "backend[dev]"
$env:PYTHONPATH="backend"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

macOS / Linux：

```bash
PYTHONPATH=backend .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

默认会创建 `FILE_STORAGE_ROOT` 下的 `uploads` / `backgrounds` / `exports`。数据库默认 SQLite：`sqlite:///./backend/data/ppt_agent.db`（WAL + foreign_keys）。也支持 PostgreSQL。

健康检查：`GET /healthz`。

## 阶段机

项目阶段：`init → outline → search → draft → design → export`。每页独立状态（`empty/ready/running/confirmed/stale/failed`）。打开搜索页不会自动搜索或自动 summary，所有动作显式触发。结构变更只标 `stale`，不删 draft/design。

任务经 `agent_tasks` 入队，后台线程领取执行；可用 `POST /projects/{id}/tasks:cancel` 在阶段边界取消。

## 关键接口

前缀均为 `/api/v1`。

### 项目与事件

```text
GET    /projects
POST   /projects
GET    /projects/{project_id}
POST   /projects/{project_id}/bootstrap:retry
GET    /projects/{project_id}/messages
POST   /projects/{project_id}/messages
GET    /projects/{project_id}/events/stream
```

### 需求单（init）

```text
GET    /projects/{project_id}/requirements/form
POST   /projects/{project_id}/requirements/answers:batch
PATCH  /projects/{project_id}/requirements/answers/{question_code}
POST   /projects/{project_id}/requirements/search-results/{source_id}:retry
POST   /projects/{project_id}/requirements/questions
PATCH  /projects/{project_id}/requirements/questions/{question_code}
DELETE /projects/{project_id}/requirements/questions/{question_code}
POST   /projects/{project_id}/requirements/confirm
POST   /projects/{project_id}/assets/backgrounds
```

`POST /requirements/confirm` 仅在 `current_stage=init` 时允许，否则 409。

### 大纲

```text
GET    /projects/{project_id}/outline
POST   /projects/{project_id}/outline:retry
PATCH  /projects/{project_id}/outline/storyboard
POST   /projects/{project_id}/outline/confirm
```

`POST /outline:retry` 仅在 `current_stage=outline` 且尚未落库大纲时允许，用于生成失败后重新入队。

### 页面、批量与导出

```text
GET    /projects/{project_id}/pages
GET    /projects/{project_id}/pages/{page_id}
PATCH  /projects/{project_id}/pages/{page_id}/outline
POST   /projects/{project_id}/pages/{page_id}/search-results/{source_id}:retry
POST   /projects/{project_id}/pages/{page_id}/search-queries:generate
POST   /projects/{project_id}/pages/{page_id}/search:run
POST   /projects/{project_id}/pages/{page_id}/summary:generate
PATCH  /projects/{project_id}/pages/{page_id}/summary
POST   /projects/{project_id}/pages/{page_id}/draft:generate
GET    /projects/{project_id}/pages/{page_id}/draft
POST   /projects/{project_id}/pages/{page_id}/design:generate
GET    /projects/{project_id}/pages/{page_id}/design
POST   /projects/{project_id}/actions/batch
POST   /projects/{project_id}/tasks:cancel
POST   /projects/{project_id}/exports
GET    /projects/{project_id}/exports/{export_id}
GET    /projects/{project_id}/exports/{export_id}/download
```

页级资料池按 `collection_id` 隔离。批量动作会跳过未过期页，并按页并发入队。

PPTX 导出：PNG 光栅作为主图，SVG 以 `asvg:svgBlip` 扩展写入。任一页缺少 ready 设计稿则 422。

### 模型设置

```text
GET    /settings/models
POST   /settings/models
PATCH  /settings/models/{provider_id}
DELETE /settings/models/{provider_id}
POST   /settings/models/{provider_id}/test
POST   /settings/models:catalog
GET    /settings/model-bindings
PUT    /settings/model-bindings
GET    /settings/search
PUT    /settings/search
```

角色有 `context`、`svg`、`search`。搜索方式可选博查 Key 或大模型联网搜索。API Key 脱敏返回。`.env` 为空时启动不崩，前端引导去配置。

## 测试

```powershell
$env:PYTHONPATH="backend"
.venv\Scripts\python.exe -m pytest backend/tests
```
