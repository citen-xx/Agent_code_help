# java-code-agent

面向 Java GitHub Repository 的 Code Agent。

## 项目目标

实现一个面向 Java GitHub 仓库的 Code Agent：拉取并分析 Java 代码库、
理解代码结构，并完成编码任务（代码生成、缺陷修复、代码审查等）。

## 当前阶段

第八阶段：Java 调用图与 Impacted Code Analysis：

- Workspace 管理：从 Git 仓库地址 clone 到隔离的 `.workspaces/<workspace_id>/repository/`，支持指定 branch / commit，并记录 `creating / ready / error` 状态
- Git 只读工具：`git_status` / `git_diff` / `git_log` / `git_show`（输出长度限制 + 截断标记）
- 构建系统识别：仅检查 Workspace 根目录的 Maven `pom.xml` 或 Gradle 构建文件
- 受控测试执行：Maven 固定执行 `mvn test`，默认在临时 Docker 容器内运行，返回 exit code、stdout、stderr、超时和截断信息
- Agent 工具：`detect_build_tools`、`run_tests`，失败输出会反馈给 LLM 用于定位原因
- 原子代码修改：`apply_patch` 只修改绑定 Workspace 内已存在的 UTF-8 文本文件，失败自动回滚
- Maven 修复闭环：定位 → 读取 → 修改 → 自动 Docker Maven test → 分析失败 → 读取/再修改；测试通过立即结束，最多允许 3 轮修复 Patch
- Java 符号索引：Tree-sitter 解析 Workspace 内 `.java`，提取 package/import/类/接口/枚举/record/方法/构造器/字段
- 符号工具：`list_symbols`、`find_symbol`、`find_references`，支持重载、同名定义、嵌套类与行号定位
- 静态调用图：识别 `foo()`、`this.foo()`、`obj.foo()`、`ClassName.foo()`，记录调用方、被调方、文件、行列和候选定义
- Impact 分析：从类或方法出发，默认分析 1 层、最多 2 层调用方，并返回测试代码引用和同类相关方法
- 新接口：`POST /workspaces`、`GET /workspaces`、`GET /workspaces/{id}`、`DELETE /workspaces/{id}`
- `/agent/analyze` 支持 `workspace_id`（推荐）与 `repository_path`（向后兼容）

仍然禁止：任意 Shell、任意 Maven 参数、文件创建/删除、PostgreSQL、Redis、MCP、SSE、LSP、javac 类型解析、JAR 跨依赖分析、动态调用、Reflection、Spring Bean 依赖图和自动修复规划器。Gradle 能力保持现状，不再扩展；后续闭环只围绕 Maven。

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
│  │  ├─ build.py          # Maven/Gradle 检测与固定 test 执行
│  │  ├─ docker_sandbox.py # 临时 Docker Maven 沙箱
│  │  └─ models.py         # WorkspaceInfo 模型
│  ├─ code_intel/
│  │  ├─ parser.py         # Tree-sitter Java 符号/引用提取
│  │  ├─ index.py          # Workspace 级内存索引与增量刷新
│  │  ├─ registry.py       # 按 workspace_id 管理索引生命周期
│  │  └─ tools.py          # list_symbols/find_symbol/find_references
│  ├─ tools/
│  │  ├─ repo.py           # 只读仓库工具（list_files/read_file/search_code）
│  │  ├─ registry.py       # 仓库工具 LangChain 封装
│  │  ├─ git_tools.py      # 只读 Git 工具（status/diff/log/show）
│  │  ├─ build_tools.py    # 构建检测与测试执行工具
│  │  └─ patch_tools.py    # Workspace 绑定的原子 apply_patch
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
- Tree-sitter 与 tree-sitter-java（由 `uv sync` 安装，无额外系统依赖）
- Docker CLI + 运行中的 Docker Engine（Maven Sandbox）

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

### 构建测试配置

```env
BUILD_TEST_TIMEOUT_SECONDS=300
```

该值控制单次 Maven/Gradle `test` 的超时时间，默认 300 秒。

### Docker Maven Sandbox 配置

```env
DOCKER_MAVEN_IMAGE=maven:3.9-eclipse-temurin-17
DOCKER_MAVEN_CPUS=1.0
DOCKER_MAVEN_MEMORY=1g
DOCKER_MAVEN_PIDS_LIMIT=256
DOCKER_MAVEN_TMPFS_SIZE=256m
```

- 需要本机 Docker CLI 和运行中的 Docker Engine / Docker Desktop。
- Maven 测试固定使用 `--network none`，不读取网络开关配置。
- `DOCKER_MAVEN_IMAGE` 需要包含 Maven、JDK，以及项目测试所需的 Maven 依赖/插件缓存。
- 因容器默认无网络，项目依赖不能临时下载；可在镜像构建阶段预热 `/root/.m2`。

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

### Agent 代码修改闭环

Workspace 模式下 Agent 可以调用 `apply_patch`，输入为结构化精确替换：

```json
{
  "changes": [
    {
      "path": "src/main/java/example/Calculator.java",
      "old_text": "return left - right;",
      "new_text": "return left + right;"
    }
  ]
}
```

约束：

- `path` 必须是 repository 相对路径，且目标文件必须已经存在。
- `old_text` 必须在目标文件中恰好出现一次。
- 一次可以修改多个文件，但同一路径只能出现一次。
- 所有校验完成后才提交；任一步失败会恢复已替换文件。
- 返回 `affected_files`、当前 `git diff` 和是否截断。
- 每次 `apply_patch` 成功后 Graph 自动执行固定 Maven test，无需 LLM 再次请求。
- 自动测试在临时 Docker 容器内运行；宿主机 Maven 不会被调用，也不存在宿主 Maven 回退路径。
- Maven test 通过后立即结束 Graph，不再继续调用 LLM。
- Maven test 失败后，结构化输出会作为 ToolMessage 回传 LLM，可继续读取代码并修改。
- 测试失败后最多允许 3 次修复 Patch；第 3 次修复后测试仍失败时自动结束 Graph 并返回失败结果。

最终 `/agent/analyze` 响应新增：

- `modified_files`：本次成功修改的文件
- `repair_rounds`：测试失败后执行的修复 Patch 次数
- `test_result`：最近一次测试的结构化结果
- `diff`：运行结束时的 Git Diff

### Java Code Intelligence

Workspace 模式下的 Agent 优先使用以下只读工具：

- `list_symbols`：按文件、类名或符号类型查看符号
- `find_symbol`：按名称查找类、接口、枚举、record、方法、构造器、字段
- `find_references`：按标识符名称查找引用位置，可选择包含定义
- `find_callers`：查找调用指定方法的全部调用点
- `find_callees`：查找指定方法内部调用的方法
- `analyze_impact`：分析类或方法的潜在影响范围

索引特性：

- 首次调用符号工具时按需构建，不自动占用 Workspace 创建时间
- 只扫描 repository 内 `.java` 文件，跳过 `.git`、`target`、`build` 等目录
- 索引保存在进程内存中，不引入数据库
- `apply_patch` 成功后只重新解析受影响 `.java` 文件
- Workspace 删除时同步清理内存索引
- 单个文件 Tree-sitter 语法错误只跳过该文件，并在查询结果 `diagnostics` 中返回文件和行列
- `find_references` 是语法节点 + 标识符匹配，不进行 javac 类型解析
- 调用图同样是静态近似：`this` 和明确类名限定会优先匹配同类/目标类；对象变量调用只按方法名生成候选
- 重载或多个候选定义无法唯一确定时返回 `candidates` 并设置 `ambiguous=true`，不会伪造唯一调用关系
- `analyze_impact` 默认深度 1，最大 2；结果区分主代码与 `src/test/java`，并限制返回数量避免递归爆炸
- `apply_patch` 成功后同一批受影响 Java 文件会同时刷新符号、引用和调用关系
### 运行测试

Agent 在 Workspace 模式下可以调用：

- `detect_build_tools`：返回根目录检测到的 Maven/Gradle 项目、构建文件、固定命令和是否使用 Wrapper
- `run_tests`：参数只能是 `{"tool": "auto" | "maven" | "gradle"}`

示例任务：

```bash
curl -X POST http://127.0.0.1:8000/agent/analyze \
  -H "Content-Type: application/json" \
  -d '{"task": "运行测试并分析失败原因", "workspace_id": "1a2b3c4d5e6f"}'
```

`run_tests` 返回结构化 JSON：

```json
{
  "tool": "maven",
  "command": ["mvn.cmd", "test"],
  "exit_code": 1,
  "stdout": "Tests run: 1, Failures: 1 ...",
  "stderr": "",
  "success": false,
  "timed_out": false,
  "truncated": false,
  "error": null,
  "duration_seconds": 3.42
}
```

检测到多个构建工具时，`auto` 会拒绝猜测，需显式指定 `maven` 或 `gradle`。

## 安全说明

- Workspace 只能创建在 `.workspaces/` 下；`workspace_id` 严格校验为 12 位 hex，路径穿越无法构造
- Git 操作通过受控 subprocess 执行（`shell=False`、固定参数列表、超时），不提供通用 Shell Tool
- Git 工具创建时校验 `workspace_id` 与 `repository_path` 的精确绑定，Agent 无法传入任意路径或任意 Git 子命令
- 仓库工具与 Git 工具全部只读；禁止修改/删除文件；`DELETE /workspaces/{id}` 只能删除对应 workspace 目录
- `POST /workspaces` 默认仅接受 HTTPS，拒绝含用户名/密码的 URL；`file://` 只能由测试代码显式开启
- URL / branch / commit 均有基本校验，ref 不允许选项注入或 revision range；clone 默认超时 300 秒
- `git_diff` / `git_show` 最大输出 20,000 字符，`git_status` 最多 100 条记录，`git_log` 最多 50 条；截断时明确标记
- 测试命令固定为 Maven/Gradle 的 `test`，不接受额外命令、参数、文件路径或 Shell；Gradle 固定使用 `--no-daemon`
- Maven 容器只绑定挂载当前 Workspace 的 repository 到 `/workspace`，不挂载其他 Workspace、宿主机 Home、Docker Socket 或 Maven 缓存目录
- Maven 容器固定 `--network none`、`--cap-drop ALL`、`no-new-privileges`，并设置 CPU、内存、PID 和 tmpfs 限制
- 容器使用唯一名称，正常完成、Maven 失败、Docker 失败和超时后都会执行 `docker rm -f` 清理
- `apply_patch` 仍由宿主服务执行，Docker 容器只负责 Maven 测试，不提供容器内代码修改能力
- Docker CLI/守护进程不可用时返回结构化 `TestRunResult.error`，不会让 Agent Graph 崩溃，也不会回退宿主机 Maven
- stdout / stderr 各自最多保留 20,000 字符，超出时明确标记；测试超时默认 300 秒，可通过 `BUILD_TEST_TIMEOUT_SECONDS` 调整
- Maven 测试会执行仓库中的构建脚本和测试代码；容器隔离限制的是资源、网络和宿主文件访问，不等同于对所有恶意代码的绝对安全保证
- `apply_patch` 拒绝绝对路径、`..` 路径穿越、`.git` 目录、符号链接组件、硬链接、二进制文件、非 UTF-8 文件和超过 2 MiB 的文件
- `apply_patch`、`run_tests` 和 Git 工具都不会暴露 workspace 路径参数，全部由服务端按 `workspace_id` 绑定

## 运行测试

```bash
uv run pytest
```

所有测试使用本地临时 Git 仓库与 Fake LLM，完全离线、不消耗任何 API 额度。

真实 Docker Maven 验收（需要运行中的 Docker，以及可访问的 Maven 基础镜像；不属于 pytest）：

```bash
docker build \
  -f scripts/docker/maven-offline.Dockerfile \
  -t java-code-agent-maven-fixture:local \
  tests/fixtures/maven_basic

$env:DOCKER_MAVEN_IMAGE = "java-code-agent-maven-fixture:local"
uv run python scripts/acceptance_patch_loop.py
```

离线验收镜像在构建阶段预热 Maven 依赖，真正执行 Agent 测试时容器使用 `--network none`。

真实 Java Code Intelligence 验收（本地临时 Git Workspace，不需要 LLM）：

```bash
uv run python scripts/acceptance_code_intel.py
```

真实 Java 调用图 / Impact 验收（包含 Patch 前后两轮 Docker Maven）：

```bash
$env:DOCKER_MAVEN_IMAGE = "java-code-agent-maven-fixture:local"
uv run python scripts/acceptance_call_graph.py
```

## 后续计划

- Tree-sitter 语法级代码分析
- 持久化、检索等基础设施（按需逐步加入）
