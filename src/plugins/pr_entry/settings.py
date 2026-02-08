import os
from dataclasses import dataclass


def _env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _split_csv(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


@dataclass(frozen=True)
class PrEntrySettings:
    prserver_base_url: str = _env("HITSZ_MANAGER_PRSERVER_BASE_URL", "http://localhost:8000") or ""
    prserver_api_key: str = _env("HITSZ_MANAGER_PRSERVER_API_KEY", "") or ""

    # 审核模型（走 n1n.ai 中转，兼容 OpenAI 风格）
    llm_api_key: str = _env("HITSZ_MANAGER_AI_API_KEY", "") or ""
    llm_base_url: str = _env("HITSZ_MANAGER_AI_BASE_URL", "https://api.n1n.ai/v1") or ""
    llm_model: str = _env("HITSZ_MANAGER_AI_MODEL", "gemini-2.5-pro") or ""

    # 权限：为空=允许所有人（不推荐，但按你当前需求默认兼容）
    allowed_users: set[str] = _split_csv(_env("HITSZ_MANAGER_ALLOWED_USERS", ""))


settings = PrEntrySettings()
