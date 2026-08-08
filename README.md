# Limpu-bot v2

基于 NoneBot2 + OneBot V11 的 QQ 机器人。**v2 重新设计为「瘦 QQ 前端」**：所有数据与 AI 能力通过本机环回 HTTP 调用专职后端服务，bot 进程只负责消息解析、会话状态机与格式化输出。

```
QQ群 ←→ NapCat (OneBot v11 反向WS) ←→ Limpu-bot (:8090)
                                          │
                            ┌─────────────┴──────────────┐
                      course-server :8081              agent-backend :8080
                      课程查询 / PR 提交           RAG / 教师 / 健康检查
                      DeepSeek（LLM 对话 / 内容合规审核，bot 直连）
```

## 功能与指令

群里使用需 `@bot`。

| 指令 | 说明 | 后端 |
|------|------|------|
| `/搜 <关键词>` | 模糊搜索课程（名/代码/教师） | course-server |
| `/查 <代码\|昵称>` | 课程详情（长文自动合并转发） | course-server |
| `/设置昵称 <昵称> <课程代码>` | 课程昵称（存 bot 本地） | — |
| `/问 <问题>` | RAG 知识库问答（检索 + 全文补全 + LLM 生成） | agent-backend + RagData 本地克隆 |
| `/教师 <姓名\|拼音>` | 教师主页查询 | agent-backend |
| `/ai <内容>` | AI 对话（群内按用户保留短期上下文，可追问） | DeepSeek |
| `/pr …` | 课程仓库贡献流程（见下） | course-server + DeepSeek 审核 |

### PR 提交流程

1. `/pr start <课程代码|昵称>` —— 开始会话
2. `/pr add <章节标题>` —— 追加章节内容（下一条消息为正文）
3. `/pr review <教师名>` —— 添加教师评价（下一条消息为正文）
4. `/pr list` / `/pr undo` —— 查看 / 撤销
5. `/pr sign <名字> [链接]` —— 署名（可选）
6. `/pr preview` —— 预览合并后的文档与警告
7. `/pr submit` —— LLM 合规审核 → 回复「确认」→ 幂等提交 PR
8. `/pr cancel` —— 放弃会话

操作映射为 course-server 的结构化 ops（`add_section_item` / `add_lecturer_review`），提交走 `/v1/course:submit`（幂等键防重复）。

## 部署

```bash
python3 -m venv .venv
./.venv/bin/pip install -e .
cp .env.example .env   # 按需填写
./.venv/bin/python bot.py
```

生产环境由 systemd 托管（`limpu-bot.service`），协议端 NapCat 由 `napcat.service` 托管，配置反向 WS 到 `ws://127.0.0.1:8090/onebot/v11/ws`。

### 配置项（.env）

| 变量 | 说明 | 默认 |
|------|------|------|
| `HOST` / `PORT` | OneBot 反向 WS 监听 | `127.0.0.1` / `8090` |
| `LIMPU_COURSESERVER_BASE_URL` | course-server 地址 | `http://127.0.0.1:8081` |
| `LIMPU_AGENT_BASE_URL` | agent-backend 地址 | `http://127.0.0.1:8080` |
| `LIMPU_CAMPUS` | 校区 | `shenzhen` |
| `LIMPU_LLM_API_KEY` / `LIMPU_LLM_BASE_URL` / `LIMPU_LLM_MODEL` | LLM（OpenAI 兼容） | DeepSeek |
| `LIMPU_PR_ALLOWED_USERS` | PR 提交白名单（逗号分隔，空=所有人） | 空 |
| `LIMPU_SESSION_TTL` | 会话超时（秒） | `1800` |
| `LIMPU_RAG_FULLTEXT_REPO` | RAG 原文仓库（公开，doc_id 即仓库路径） | `HIT-A/HITA_RagData` |
| `LIMPU_RAG_REPO_DIR` | 该仓库的本地克隆目录 | `data/HITA_RagData` |
| `LIMPU_RAG_SYNC_INTERVAL` | 定时 `git pull` 间隔（秒，下限 300） | `1800` |
| `LIMPU_GITHUB_TOKEN` | 可选，API 兜底时防匿名限流 | 空 |

### RAG 全文补全

`/问` 的向量检索命中只有 280 字截断片段。bot 会把 `LIMPU_RAG_FULLTEXT_REPO`
浅克隆到本地（`repo_sync` 插件启动后每 30 分钟 `git pull`），命中最多片段的
文档若是仓库内文件，直接读全篇喂给 LLM；非仓库来源（爬虫页等）回退片段模式。

## 项目结构

```
src/
├── limpu_core/          # 共享层
│   ├── settings.py      # 配置
│   ├── courseserver.py      # course-server API 客户端
│   ├── agent_backend.py # agent-backend API 客户端
│   ├── llm.py           # LLM（对话 + 合规审核）
│   ├── nicknames.py     # 课程昵称本地存储
│   ├── rag_fulltext.py  # RAG 原文补全（本地克隆优先，API 兜底）
│   └── qq.py            # 长消息合并转发工具
└── plugins/
    ├── course_query/    # /搜 /查 /设置昵称
    ├── rag_query/       # /问
    ├── teacher_query/   # /教师
    ├── ai_chat/         # /ai
    ├── repo_sync/       # RagData 仓库定时 git pull
    └── pr_entry/        # /pr 提交流程
```

## v1 → v2 变更

- 删：本地 TOML 扫描、GitPython 仓库同步（`/刷`）、本地 ChromaDB + sentence-transformers（`/重构知识库`）、Dockerfile
- 改：课程数据 / RAG / 教师查询全部改调后端 HTTP API；PR 流程适配 course-server 新协议（preview/submit/lookup + 结构化 ops）
- 增：`/ai` DeepSeek 对话、`/教师` 教师查询
- 部署：Docker → systemd 裸机
