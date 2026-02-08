# hitsz_manager

## How to start

Install dependencies (recommended: create venv first)

- `pip install -e .`

Configure environment

- Copy `.env.example` to `.env` or `.env.prod`
- At minimum, set:
- `PORT` (already in `.env`)
- `HITSZ_MANAGER_AI_API_KEY` / `HITSZ_MANAGER_AI_BASE_URL` / `HITSZ_MANAGER_AI_MODEL` (used for RAG + PR content moderation)
- `HITSZ_MANAGER_PRSERVER_BASE_URL` (used for PR submission)

Run bot

- `nb run`

## PR via QQ (with LLM moderation)

This repo provides a minimal QQ entrypoint to create PRs via `hoa-prServer`.

设计目标：解决 QQ 单条消息长度上限，尽量让“查看/定位/修改/确认/署名”都顺滑。

### 入口

1) 开始会话：

- `@bot /pr start <repo_name> <course_code> <course_name...> <repo_type>`

说明：用户自行“查到对应仓库”后，把仓库名告诉 bot。后续所有操作都基于这次会话。

2) 展示全文（合并转发 + 分段）：

- `@bot /pr show`

展示策略：

- normal：
	- 头部一段：课程名 + 课程号 + description
	- 其余：每个 `[[sections]]` 一段（段内把该主题下所有 `[[sections.items]]` 用空行连接）
- multi-project：
	- 头部一段：聚合课程名 + 课程号 + description
	- 其余：每个 `[[courses]]` 一段（包含教师/评价/该子课程 sections 等，尽量保持“一个子课程一段”）

超长处理：

- 如果某一段仍然过长，按空行/换行优先切分；必要时硬切成两半，再继续发送（直到每段长度可发送）。

### 编辑（添加 / 修改）

#### 添加

- `@bot /pr add <章节标题>`
	- 如果不带章节标题，bot 会提示你再发一次章节标题
	- 下一条消息：发送要追加的“正文”

#### 修改（按原段落定位，不用短 ID）

- `@bot /pr modify`
	- 下一条消息：粘贴你要改的“原段落”（尽量原样复制，越长越好，便于定位）
	- bot 会在仓库内容中搜索该片段（按 repo_type 覆盖不同区域）：
		- `description`
		- normal：`[[sections.items]].content`、`[[lecturers.reviews]].content`
		- multi-project：`[[courses.sections.items]].content`、`[[courses.teachers.reviews]].content`
		- 若唯一匹配：直接进入下一步
		- 若多匹配：列出候选（会标注区域 + preview），让你回复序号选择
	- 下一条消息：发送“修改后的完整段落”

### 署名（author）

在进入最终提交前，bot 会询问：

- 是否留名（y/n）
- 若留名：
	- 名字（问使用者留什么）
	- 主页链接（GitHub/博客，可空）
	- 日期自动填当前年月（YYYY-MM）

### 提交确认与落地

- bot 会先让你“确认/取消”
- 确认后流程：
	1) prServer `submit_ops (dry_run)` 生成 patched TOML
	2) Bot 用 LLM 对“整段 patched TOML”做合规审核，只做合规性，不要让他动内容
	3) 审核通过后调用 prServer `POST /v1/pr/ensure` 幂等创建/更新 PR（已有 PR 会更新，不会跳过）

### 兼容：整段 TOML 直接提交

- 在 `/pr start` 后直接粘贴完整 `readme.toml`（单独一条消息）
- bot 审核后直接 `ensure PR`

Cancel:

- `/pr cancel`

## Documentation

See [Docs](https://nonebot.dev/)
