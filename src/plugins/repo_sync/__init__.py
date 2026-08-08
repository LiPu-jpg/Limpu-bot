"""RagData 仓库定时同步：bot 启动后后台协程周期 `git pull`。

不影响主流程：所有 git 操作都有超时，失败仅记日志。
"""

from __future__ import annotations

import asyncio

from nonebot import get_driver, logger

from limpu_core.settings import get_settings

driver = get_driver()
_task: asyncio.Task | None = None

GIT_TIMEOUT = 120  # git 操作超时（秒）


async def _run_git(*args: str, cwd: str | None = None) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
        return proc.returncode or 0, (out or b"").decode(errors="replace")[-300:]
    except asyncio.TimeoutError:
        return -1, "git 操作超时"
    except Exception as e:
        return -1, str(e)


async def _sync_once() -> None:
    s = get_settings()
    repo_dir = s.rag_repo_dir
    if not s.rag_fulltext_repo:
        return

    if (repo_dir / ".git").is_dir():
        code, out = await _run_git("pull", "--ff-only", cwd=str(repo_dir))
        if code == 0:
            if "Already up to date" not in out:
                logger.info(f"RagData 仓库已更新: {out.strip()}")
        else:
            logger.warning(f"RagData 仓库 pull 失败: {out.strip()}")
        return

    # 本地不存在则浅克隆
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{s.rag_fulltext_repo}.git"
    code, out = await _run_git("clone", "--depth", "1", url, str(repo_dir))
    if code == 0:
        logger.info(f"RagData 仓库已克隆到 {repo_dir}")
    else:
        logger.warning(f"RagData 仓库克隆失败: {out.strip()}")


async def _sync_loop() -> None:
    interval = max(300, get_settings().rag_sync_interval)
    while True:
        try:
            await _sync_once()
        except Exception as e:
            logger.warning(f"RagData 同步异常: {e}")
        await asyncio.sleep(interval)


@driver.on_startup
async def _start_sync() -> None:
    global _task
    _task = asyncio.create_task(_sync_loop())


@driver.on_shutdown
async def _stop_sync() -> None:
    if _task:
        _task.cancel()
