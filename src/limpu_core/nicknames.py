"""课程昵称本地存储（data/nicknames.json）。

格式：{"昵称": "COURSE_CODE", ...}
"""

from __future__ import annotations

import json
import threading

from .settings import get_settings

_lock = threading.Lock()


def _load() -> dict[str, str]:
    path = get_settings().nickname_file
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save(data: dict[str, str]) -> None:
    path = get_settings().nickname_file
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get(code_or_nick: str) -> str:
    """把昵称解析为课程代码；查不到则原样返回。"""
    return _load().get(code_or_nick.strip(), code_or_nick.strip())


def set_nick(nickname: str, course_code: str) -> None:
    with _lock:
        data = _load()
        data[nickname.strip()] = course_code.strip().upper()
        _save(data)


def all_nicks() -> dict[str, str]:
    return _load()
