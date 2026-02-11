import os
from pathlib import Path
from pydantic import BaseModel


def _env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_any(keys: list[str], default: str | None = None) -> str | None:
    for key in keys:
        value = _env(key)
        if value is not None:
            return value
    return default


def _fallback_dir() -> Path | None:
    raw = _env("HITSZ_MANAGER_COURSE_FALLBACK_DIR", "") or ""
    raw = raw.strip()
    return Path(raw).expanduser() if raw else None

class Config(BaseModel):
    # --- 基础路径配置 ---
    # 假设运行目录在项目根目录
    DATA_ROOT: Path = Path(_env("HITSZ_MANAGER_DATA_ROOT", "data") or "data")
    
    # 课程数据路径
    COURSE_DIR: Path = DATA_ROOT / "courses"
    REPO_DIR: Path = COURSE_DIR / (_env("HITSZ_MANAGER_COURSE_REPO_DIR", "Allrepo-temp") or "Allrepo-temp")

    # 可选：课程数据备份兜底目录（只读亦可）。当 COURSE_DIR 中找不到可用 toml 时，会从这里补充。
    # 典型用法：把宿主机的精简 readme.toml 目录挂载到容器 /seed/courses，然后设置本变量。
    COURSE_FALLBACK_DIR: Path | None = _fallback_dir()
    
    # RAG 相关路径
    RAG_DOCS_DIR: Path = DATA_ROOT / "rag_docs"    # 放 txt 的地方
    VECTOR_DB_DIR: Path = DATA_ROOT / "chroma_db"  # ChromaDB 存储路径
    
    # 昵称存储
    NICKNAME_FILE: Path = DATA_ROOT / "nicknames.json"

    # --- Git 配置 ---
    REPO_URL: str = _env("HITSZ_MANAGER_COURSE_REPO_URL", "https://github.com/LiPu-jpg/Allrepo-temp.git") or ""

    # --- GitHub Org 同步配置（推荐） ---
    # 若设置为非空，则 /刷 会从 GitHub Org 枚举仓库并同步到 data/courses/<repo_name>/
    # 过滤规则（按你当前约定）：仓库名首字符大写，且不包含 '-'
    GITHUB_ORG: str = _env_any(["HITSZ_MANAGER_GITHUB_ORG", "GITHUB_ORG"], "HITSZ-OpenAuto") or ""
    # 兼容：很多部署习惯把 token 命名为 GITHUB_TOKEN（prServer 也用这个）。
    # 若未提供 token，会以匿名方式调用 GitHub API，极易触发 403 rate limit。
    GITHUB_TOKEN: str = _env_any(["HITSZ_MANAGER_GITHUB_TOKEN", "GITHUB_TOKEN"], "") or ""
    GITHUB_API_BASE: str = _env("HITSZ_MANAGER_GITHUB_API_BASE", "https://api.github.com") or ""
    # 默认并发略保守，降低触发 429 的概率；需要更快可通过环境变量调高。
    GIT_SYNC_CONCURRENCY: int = int(_env("HITSZ_MANAGER_GIT_SYNC_CONCURRENCY", "2") or 2)

    # clone 提速：默认浅克隆，仅拉取最近提交；设为 0 可禁用（全量 clone）
    GIT_CLONE_DEPTH: int = int(_env("HITSZ_MANAGER_GIT_CLONE_DEPTH", "1") or 1)

    # 同步模式：
    # - git: clone/pull 仓库（默认；兼容历史结构，但国内可能慢）
    # - toml: 只下载根目录 readme.toml（更快；依赖 GitHub API/Raw，可能受限流影响）
    GIT_SYNC_MODE: str = (_env("HITSZ_MANAGER_GIT_SYNC_MODE", "git") or "git").strip().lower()

    # --- LLM 配置 (Gemini via OneAPI/NewAPI) ---
    # 注意：不要把 key 写进代码。请用环境变量配置：HITSZ_MANAGER_AI_API_KEY
    AI_API_KEY: str = _env("HITSZ_MANAGER_AI_API_KEY", "") or ""
    AI_BASE_URL: str = _env("HITSZ_MANAGER_AI_BASE_URL", "https://api.n1n.ai/v1") or ""
    AI_MODEL: str = _env("HITSZ_MANAGER_AI_MODEL", "gemini-2.5-pro") or ""
    
    # Embedding 模型路径或名称 (HuggingFace)
    # 2G 内存推荐使用轻量级模型
    EMBEDDING_MODEL: str = _env(
        "HITSZ_MANAGER_EMBEDDING_MODEL",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    ) or ""

    # 可选：HuggingFace 镜像（例如 https://hf-mirror.com）
    HF_ENDPOINT: str = _env("HITSZ_MANAGER_HF_ENDPOINT", "") or ""

config = Config()

# 自动创建必要的文件夹
config.RAG_DOCS_DIR.mkdir(parents=True, exist_ok=True)
config.COURSE_DIR.mkdir(parents=True, exist_ok=True)
config.VECTOR_DB_DIR.mkdir(parents=True, exist_ok=True)
