"""全局配置：从 NoneBot dotenv 配置读取（.env 中的 LIMPU_* 项）。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from nonebot import get_driver


class Settings:
    def __init__(self, cfg: Any) -> None:
        # 新名优先，旧名 LIMPU_PRSERVER_BASE_URL 兼容过渡
        self.courseserver_base_url: str = (
            getattr(cfg, "limpu_courseserver_base_url", "")
            or getattr(cfg, "limpu_prserver_base_url", "")
            or "http://127.0.0.1:8081"
        )
        self.agent_base_url: str = getattr(cfg, "limpu_agent_base_url", "http://127.0.0.1:8080")
        self.campus: str = getattr(cfg, "limpu_campus", "shenzhen")

        self.llm_api_key: str = getattr(cfg, "limpu_llm_api_key", "")
        self.llm_base_url: str = getattr(cfg, "limpu_llm_base_url", "https://api.deepseek.com")
        self.llm_model: str = getattr(cfg, "limpu_llm_model", "deepseek-chat")

        allowed = getattr(cfg, "limpu_pr_allowed_users", "") or ""
        if isinstance(allowed, (list, tuple)):
            self.pr_allowed_users: set[str] = {str(u).strip() for u in allowed if str(u).strip()}
        else:
            self.pr_allowed_users = {u.strip() for u in str(allowed).split(",") if u.strip()}

        self.session_ttl: int = int(getattr(cfg, "limpu_session_ttl", 1800) or 1800)

        # RAG 全文补全：命中片段所属文档所在的 GitHub 仓库（公开）
        self.rag_fulltext_repo: str = getattr(cfg, "limpu_rag_fulltext_repo", "HIT-A/HITA_RagData")
        # 可选：GitHub token，避免匿名 API 限流
        self.github_token: str = getattr(cfg, "limpu_github_token", "")
        self.data_root: Path = Path(__file__).resolve().parent.parent.parent / "data"
        self.nickname_file: Path = self.data_root / "nicknames.json"

        # RagData 本地克隆目录与同步间隔（秒）
        self.rag_repo_dir: Path = Path(
            getattr(cfg, "limpu_rag_repo_dir", "") or (self.data_root / "HITA_RagData")
        )
        self.rag_sync_interval: int = int(getattr(cfg, "limpu_rag_sync_interval", 1800) or 1800)

        # GitHub token（推送 QA 归档到 RagData 仓库；优先 bot 配置，回退读 agent-backend env）
        self.rag_github_token: str = getattr(cfg, "limpu_rag_github_token", "") or ""
        if not self.rag_github_token:
            self.rag_github_token = self._read_fallback_token()

        self.data_root.mkdir(parents=True, exist_ok=True)

    def pr_allowed(self, user_id: int | str) -> bool:
        if not self.pr_allowed_users:
            return True
        return str(user_id) in self.pr_allowed_users

    def _read_fallback_token(self) -> str:
        path = os.getenv("AGENT_ENV_FILE", "/root/agent-backend/config/agent-backend.env")
        try:
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line.startswith("GITHUB_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("\"'") or ""
        except OSError:
            pass
        return ""

    def qa_dir(self) -> Path:
        return self.rag_repo_dir / "qq-qa"


@lru_cache
def get_settings() -> Settings:
    return Settings(get_driver().config)
