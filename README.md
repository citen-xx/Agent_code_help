# java-code-agent

面向 Java GitHub Repository 的 Code Agent。

## 项目目标

实现一个面向 Java GitHub 仓库的 Code Agent：拉取并分析 Java 代码库、
理解代码结构，并完成编码任务（代码生成、缺陷修复、代码审查等）。

## 当前阶段

第三阶段：Git 仓库 Workspace + Git 只读工具：

- Workspace 管理：从 Git 仓库地址 clone 到隔离的 `.workspaces/<workspace_id>/repository/`，支持指定 branch / commit，并记录 `creating / ready / error` 状态
- Git 只读工具：`git_status` / `git_diff` / `git_log` / `git_show`（输出长度限制 + 截断标记）
- 新接口：`POST /workspaces`、`GET /workspaces`、`GET /workspaces/{id}`、`DELETE /workspaces/{id}`
- `/agent/analyze` 支持 `workspace_id`（推荐）与 `repository_path`（向后兼容）

仍然禁止：修改/删除代码、Shell 任意执行、Maven/Gradle、Docker Sandbox、Tree-sitter、PostgreSQL、Redis、MCP、SSE。

## 推荐流程

```text
Git Repository
      ↓
Create Workspace   (POST /workspaces)
      ↓
workspace_id
      ↓
POST /agent/analyze
      ↓
Agent 分析仓库
```

## 项目结构

```text
java-code-agent/
├─ app/
│  ├─ main.py              # FastAPI 入口
│  ├─ api/
│  │  ├─ health.py         # GET /health
│  │  ├─ agent.py          # POST /agent/analyze
│  │  └─ workspaces.py     # POST/GET/LIST/DELETE /workspaces
│  ├─ core/
│  │  ├─ config.py         # Pydantic Settings（LLM_*、WORKSPACE_ROOT）
│  │  └─ logging.py        # logging 配置
│  ├─ llm/
│  │  └─ client.py         # OpenAI-compatible ChatModel 工厂
│  ├─ agent/
│  │  ├─ state.py          # Agent State 定义
│  │  └─ graph.py          # LangGraph Agent Loop（agent ↔ tools）
│  ├─ workspace/
│  │  ├─ manager.py        # WorkspaceManager（隔离目录、URL/ref 校验）
│  │  ├─ git.py            # 受控 Git CLI 封装（白名单参数 + 超时）
│  │  └─ models.py         # WorkspaceInfo 模型
│  ├─ tools/
│  │  ├─ repo.py           # 只读仓库工具（list_files/read_file/search_code）
│  │  ├─ registry.py       # 仓库工具 LangChain 封装
│  │  └─ git_tools.py      # 只读 Git 工具（status/diff/log/show）
│  └─ schemas/
│     ├─ common.py         # 通用 Pydantic 模型
│     ├─ agent.py          # /agent/analyze 请求/响应模型
│     └─ workspace.py      # /workspaces 请求模型
├─ tests/                  # 全部离线（本地临时 git 仓库 + Fake LLM）
├─ .env.example
├─ pyproject.toml
└─ uv.lock
```

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Git CLI（需在 PATH 中可用，用于 clone 与只读 Git 工具）

## 快速开始

```bash
# 1. 安装依赖（uv 自动创建 .venv 并锁定 uv.lock）
uv sync

# 2. 配置 LLM（见下一节，不配置则 /agent/analyze 返回 503）

# 3. 启动开发服务器
uv run fastapi dev app/main.py
```

## 配置真实 LLM API

项目通过 OpenAI-compatible API 接入 LLM（OpenAI、DeepSeek、通义千问等均可）。

你只需要在项目根目录创建或修改 `.env` 文件（已加入 .gitignore，不会提交到版本库）：

```env
LLM_API_KEY=...
LLM_BASE_URL=...
LLM_MODEL=...
```

- `LLM_API_KEY`：你的 API Key
- `LLM_BASE_URL`：OpenAI-compatible 接口地址，例如 `https://api.deepseek.com/v1`
- `LLM_MODEL`：模型名称，例如 `deepseek-chat`

> 项目根目录已自动生成 `.env` 空模板；参考 `.env.example`。
> 未配置时调用 `/agent/analyze` 会返回 503 并提示缺少的配置项。

配置完成后**无需修改任何代码**，重启服务即可使用真实 LLM。

## 接口

### GET /health

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","service":"java-code-agent"}
```

### POST /workspaces

从 Git 仓库地址创建隔离 Workspace（clone 到 `.workspaces/<id>/repository/`）：

```bash
curl -X POST http://127.0.0.1:8000/workspaces \
  -H "Content-Type: application/json" \
  -d '{"repository_url": "https://github.com/spring-projects/spring-petclinic.git", "branch": "main", "commit": null}'
```

默认 clone 仓库默认分支；也可以指定 branch，或直接定位到某个 commit：

```bash
curl -X POST http://127.0.0.1:8000/workspaces \
  -H "Content-Type: application/json" \
  -d '{"repository_url": "https://github.com/spring-projects/spring-petclinic.git", "branch": null, "commit": "3e87c2c"}'
```

返回：

```json
{
  "workspace_id": "1a2b3c4d5e6f",
  "repository_path": "D:/Project/java-code-agent/.workspaces/1a2b3c4d5e6f/repository",
  "repository_url": "https://github.com/spring-projects/spring-petclinic.git",
  "branch": "main",
  "commit": "abc123...",
  "status": "ready"
}
```

### GET /workspaces

列出当前 Workspace 根目录下的全部 Workspace：

```bash
curl http://127.0.0.1:8000/workspaces
```

响应为 `WorkspaceInfo` 数组，每个元素包含仓库地址、branch、commit、状态和本地 repository 路径。

### GET /workspaces/{workspace_id}

查询 Workspace 基本状态（同上响应结构）。`status` 可能为：

- `creating`：正在 clone / checkout
- `ready`：可以用于 Agent 分析
- `error`：仓库目录缺失或 Git 元数据不可读

### DELETE /workspaces/{workspace_id}

显式删除对应 Workspace（返回 204）。无自动后台清理任务。

### POST /agent/analyze

推荐使用 `workspace_id`：

```bash
curl -X POST http://127.0.0.1:8000/agent/analyze \
  -H "Content-Type: application/json" \
  -d '{"task": "帮我找到创建订单的主要代码", "workspace_id": "1a2b3c4d5e6f"}'
```

也保留 `repository_path` 直连本地目录的方式（向后兼容）：

```json
{"task": "分析订单创建逻辑", "repository_path": "D:/projects/example-java-project"}
```

返回（至少包含）：

```json
{"answer": "...", "steps": 3}
```

workspace 模式下 Agent 额外拥有 4 个只读 Git 工具（git_status / git_diff / git_log / git_show）。
如果 Workspace 状态不是 `ready`，接口返回 409，不会启动 Agent。

## 安全说明

- Workspace 只能创建在 `.workspaces/` 下；`workspace_id` 严格校验为 12 位 hex，路径穿越无法构造
- Git 操作通过受控 subprocess 执行（`shell=False`、固定参数列表、超时），不提供通用 Shell Tool
- Git 工具创建时校验 `workspace_id` 与 `repository_path` 的精确绑定，Agent 无法传入任意路径或任意 Git 子命令
- 仓库工具与 Git 工具全部只读；禁止修改/删除文件；`DELETE /workspaces/{id}` 只能删除对应 workspace 目录
- `POST /workspaces` 默认仅接受 HTTPS，拒绝含用户名/密码的 URL；`file://` 只能由测试代码显式开启
- URL / branch / commit 均有基本校验，ref 不允许选项注入或 revision range；clone 默认超时 300 秒
- `git_diff` / `git_show` 最大输出 20,000 字符，`git_status` 最多 100 条记录，`git_log` 最多 50 条；截断时明确标记

## 运行测试

```bash
uv run pytest
```

所有测试使用本地临时 Git 仓库与 Fake LLM，完全离线、不消耗任何 API 额度。

## 后续计划

- Tree-sitter 语法级代码分析
- 受控的代码修改能力
- 持久化、检索等基础设施（按需逐步加入）
