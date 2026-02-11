# hitsz_manager

一个基于 NoneBot2 + OneBot V11 的 QQ 机器人，当前包含两类能力：

- **课程数据查询**：从本地 `data/courses/**/*.toml` 读取课程信息，支持模糊搜索、详情查看、昵称映射、从远端仓库拉取更新。
- **PR 提交闭环**：对话式编辑/定位修改 `readme.toml`，合规审核后调用 `hoa-prServer` 的 `POST /v1/pr/ensure` 幂等创建或更新 PR。

## How to start

### (1) 安装依赖（建议先建 venv）

- Windows（PowerShell）：
  - `py -m venv .venv`
  - `./.venv/Scripts/python.exe -m pip install -U pip`
  - `./.venv/Scripts/python.exe -m pip install -e .`

`nb run` 需要 NoneBot CLI：

- `./.venv/Scripts/python.exe -m pip install nb-cli`

### (2) 配置环境变量

复制 `.env.example` 为 `.env` 或 `.env.prod`，并按需填写。

必填/常用项（见 `.env.example`）：

- `PORT`：NoneBot 监听端口（默认 8081）
- `HITSZ_MANAGER_PRSERVER_BASE_URL`：prServer 地址（例如 `http://127.0.0.1:8000`）
- `HITSZ_MANAGER_PRSERVER_API_KEY`：可选，若 prServer 开启了 API Key 校验
- `HITSZ_MANAGER_ALLOWED_USERS`：可选，逗号分隔 QQ 号；为空=允许所有人

LLM（用于 **RAG 问答** + **PR 内容合规审核**）：

- `HITSZ_MANAGER_AI_API_KEY`
- `HITSZ_MANAGER_AI_BASE_URL`
- `HITSZ_MANAGER_AI_MODEL`

课程数据仓库（用于 `/刷` 拉取更新）：

- 推荐（从 GitHub Org 同步多仓库到 `data/courses/<repo>/`）：
  - `HITSZ_MANAGER_GITHUB_ORG`（默认 `HITSZ-OpenAuto`）
  - `HITSZ_MANAGER_GITHUB_TOKEN`（可选：避免 API 限流）
  - `HITSZ_MANAGER_GIT_SYNC_CONCURRENCY`（默认 `4`）
  - 过滤规则：仓库名首字符大写，且不包含 `-`

- `HITSZ_MANAGER_COURSE_REPO_URL`（默认 `https://github.com/LiPu-jpg/Allrepo-temp.git`）
- `HITSZ_MANAGER_DATA_ROOT`（默认 `data`）

### (3) 配置 OneBot V11 连接（QQ 侧）

本项目使用 OneBot V11 适配器，并以 **反向 WebSocket** 方式接入（常见于 go-cqhttp / NapCat 等）。

- NoneBot 默认 WS 入口通常为：
  - `ws://127.0.0.1:<PORT>/onebot/v11/ws`

你需要在 QQ 客户端侧（go-cqhttp / NapCat）配置反向 WS 指向上述地址。

### (4) 启动

- `nb run`

在群里使用指令时，由于设置了 `to_me()` 规则，一般需要 `@bot`。

## 功能与指令

### (A) 课程查询（course_manager）

数据来源：启动时从 `data/courses/**/*.toml` 扫描加载（`course_code` 作为主键）。

- `@bot /搜 <关键词>`
  - 模糊搜索课程名/课程号/昵称，返回最多 10 个候选
  - 别名：`@bot /search <关键词>`
- `@bot /查 <课程代码|课程全名|昵称>`
  - 展示课程详情（合并转发）：头部信息 + 教师评价 + 若干板块内容 + 站点链接
  - 别名：`@bot /info <...>`
- `@bot /设置昵称 <昵称> <课程代码>`
  - 例：`@bot /设置昵称 自控 AUTO1001`
  - 保存到 `data/nicknames.json`
- `@bot /刷`
  - 默认：从 `HITSZ_MANAGER_GITHUB_ORG` 枚举仓库并同步到 `data/courses/<repo_name>/`
  - 过滤规则：仓库名首字符大写，且不包含 `-`
  - 若 Org 同步失败：回退到单仓库 `HITSZ_MANAGER_COURSE_REPO_URL`
  - 更新后会重载内存数据
  - 别名：`@bot /update`

### (B) RAG 问答（course_manager.rag_engine）

知识库目录：`data/rag_docs/**/*.txt`（需要你自行放入 txt 文档）。向量库目录：`data/chroma_db/`。

- `@bot /重构知识库`
  - 读取 `data/rag_docs` 下的 `.txt` 并构建/重建向量索引（CPU 占用较高）
- `@bot /问 <问题>`
  - 基于向量检索 + LLM 回答
  - 若未构建知识库会提示先重构
  - 别名：`@bot /ask <问题>`

### (C) PR 提交闭环（pr_entry）

依赖：需要 prServer 可访问（`HITSZ_MANAGER_PRSERVER_BASE_URL`）。

注意：PR 提交前会做 **LLM 合规审核**。如果未配置 `HITSZ_MANAGER_AI_API_KEY`，审核会直接拒绝，因此 PR 流程无法继续。

#### (0) 帮助

- `@bot /pr` 或 `@bot /pr help`

兼容写法（不带斜杠）：`@bot pr ...`

#### (1) 开始会话

- 推荐：`@bot /pr start <repo_name>`
  - 例：`@bot /pr start AUTO2001`
  - 会从 prServer 自动补齐 `course_code/course_name/repo_type`
- 支持：`@bot /pr start <课程代码|课程全名|昵称>`
  - 例：`@bot /pr start 自动化专业导论`
  - 说明：课程全名/昵称请尽量不要带空格；若你的仓库名不等于课程代码，请改用 repo_name 写法
- 兼容旧写法：`@bot /pr start <repo_name> <course_code> <course_name...> <repo_type>`
  - 例：`@bot /pr start AUTO2001 AUTO2001 自动化专业导论 normal`
  - `repo_type`：常用为 `normal` 或 `multi-project`
  - 进入会话后，你可以继续用结构化指令修改，或直接粘贴整段 `readme.toml`

#### (2) 查看全文（合并转发 + 分段）

- `@bot /pr show`
  - 兼容：`@bot /pr view`

展示策略：

- `normal`：头部（课程名/代码/description）+ 每个 `[[sections]]` 一段
- `multi-project`：头部 + 每个 `[[courses]]` 一段（包含教师/评价/子课程 sections）

超长会自动拆分；若合并转发发送失败（风控/版本原因），可改用“直接粘贴整段 TOML 提交”。

#### (3) 添加内容（追加到某章节）

- `@bot /pr add <章节标题>`
  - 下一条消息：发送要追加的正文
- `@bot /pr add`（不带标题）
  - 机器人会先问章节标题
  - 再问正文

#### (4) 修改内容（两种方式）

方式 A：按序号改（基于 `/pr show` 后的结构摘要）

- `@bot /pr edit <章节标题> <序号>`
  - 下一条消息：发送修改后的完整正文

方式 B：按“原段落定位”改（不需要短 ID）

- `@bot /pr modify`
  - 兼容：`@bot /pr mod`
  - 下一条消息：粘贴要改的“原段落”（越长越利于定位）
  - 若唯一匹配：直接进入下一步
  - 若多匹配：机器人会列出候选，让你回复序号
  - 下一条消息：发送“修改后的完整段落”

定位范围（会覆盖 normal/multi-project 的常见区域）：

- `description`
- `normal`：`[[sections.items]].content`、`[[lecturers.reviews]].content`
- `multi-project`：`[[courses.sections.items]].content`、`[[courses.teachers.reviews]].content`

#### (5) 署名（author）

在最终提交前，机器人会询问是否留名：

- 回复 `y/n`
- 若 `y`：再问显示名字与主页链接（可空），日期自动填 `YYYY-MM`

#### (6) 确认与提交

机器人会先 dry-run 生成 patched TOML，然后提示你：

- 回复：`确认` / `取消`

确认后流程：

1) LLM 合规审核（只做合规，不改内容）
2) 调用 prServer：`POST /v1/pr/ensure` 幂等创建/更新 PR

#### (7) 兼容：整段 TOML 直接提交

在 `/pr start` 后，直接发送一条消息粘贴完整 `readme.toml`：

- 机器人审核通过后直接 ensure PR

#### (8) 取消当前会话（未提交前）

- `@bot /pr cancel`

## Documentation

See [Docs](https://nonebot.dev/)
