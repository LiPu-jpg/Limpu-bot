import os
from pathlib import Path
from pydantic import BaseModel


def _env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default

class Config(BaseModel):
    # --- 基础路径配置 ---
    # 假设运行目录在项目根目录
    DATA_ROOT: Path = Path(_env("HITSZ_MANAGER_DATA_ROOT", "data") or "data")
    
    # 课程数据路径
    COURSE_DIR: Path = DATA_ROOT / "courses"
    REPO_DIR: Path = COURSE_DIR / (_env("HITSZ_MANAGER_COURSE_REPO_DIR", "Allrepo-temp") or "Allrepo-temp")
    
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
    GITHUB_ORG: str = _env("HITSZ_MANAGER_GITHUB_ORG", "HITSZ-OpenAuto") or ""
    GITHUB_TOKEN: str = _env("HITSZ_MANAGER_GITHUB_TOKEN", "") or ""
    GITHUB_API_BASE: str = _env("HITSZ_MANAGER_GITHUB_API_BASE", "https://api.github.com") or ""
    GIT_SYNC_CONCURRENCY: int = int(_env("HITSZ_MANAGER_GIT_SYNC_CONCURRENCY", "4") or 4)

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
