import asyncio
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import git
import httpx
from .config import config

class CourseManager:
    def __init__(self):
        # 核心数据结构
        # cache: 简单的列表，用于快速遍历
        self.courses_cache: List[Dict[str, Any]] = []
        # map: key=COURSE_CODE(大写), value=课程数据字典
        self.course_map: Dict[str, Dict[str, Any]] = {}
        # nicknames: key=昵称, value=COURSE_CODE
        self.nicknames: Dict[str, str] = {}

    def load_data(self):
        """主加载流程"""
        print("📥 开始加载课程数据...")
        self._load_from_toml()
        self._load_nicknames()
        print(f"🚀 数据加载完成: 课程 {len(self.course_map)} 门, 昵称 {len(self.nicknames)} 个")

    def _load_from_toml(self):
        self.courses_cache.clear()
        self.course_map.clear()

        def _load_toml(path: Path) -> Dict[str, Any] | None:
            # tomllib.load 对 UTF-8 BOM 不够友好；这里用 utf-8-sig 解码可自动去 BOM。
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig", errors="replace")
            data = tomllib.loads(text)
            return data if isinstance(data, dict) else None

        def _hint_first_line(path: Path) -> str:
            try:
                raw = path.read_bytes()[:256]
            except Exception:
                return ""
            s = raw.decode("utf-8-sig", errors="replace").strip().split("\n", 1)[0].strip()
            if not s:
                return "（文件为空/不可读）"
            pv = s[:120] + ("…" if len(s) > 120 else "")
            if pv.startswith("<"):
                return f"（首行像 HTML：{pv}）"
            if pv.startswith("{"):
                return f"（首行像 JSON：{pv}）"
            return f"（首行预览：{pv}）"

        def _collect_candidates(base_dir) -> List[Path]:
            try:
                if not base_dir or not base_dir.exists():
                    return []
            except Exception:
                return []

            # 优先加载新结构：readme.toml（避免误索引 teachers_reviews.toml 等辅助文件）
            readme_files = list(base_dir.rglob("readme.toml"))
            if readme_files:
                return readme_files

            # 兼容旧结构：扫描所有 .toml，但排除常见辅助文件
            return [
                p
                for p in base_dir.rglob("*.toml")
                if p.name.lower() not in {"teachers_reviews.toml"}
            ]

        # 主目录：可写、用于 /刷 同步
        primary = _collect_candidates(config.COURSE_DIR)
        primary_errs = 0
        for file in primary:
            try:
                data = _load_toml(file)
                if data:
                    self._index_course_doc(data)
            except Exception as e:
                primary_errs += 1
                if primary_errs <= 5:
                    print(f"❌ 解析文件 {file} 失败: {e} {_hint_first_line(file)}")
                elif primary_errs == 6:
                    print("⚠️ 解析失败的文件过多，后续错误将不再逐条输出。")

        # 兜底目录：只在主目录缺失时补充（不会覆盖已存在的课程 code）
        fb_dir = getattr(config, "COURSE_FALLBACK_DIR", None)
        fallback = _collect_candidates(fb_dir) if fb_dir else []
        if fallback:
            fallback_errs = 0
            for file in fallback:
                try:
                    data = _load_toml(file)
                    if data:
                        self._index_course_doc(data, _fallback=True)
                except Exception as e:
                    fallback_errs += 1
                    if fallback_errs <= 5:
                        print(f"❌ 解析备份文件 {file} 失败: {e} {_hint_first_line(file)}")
                    elif fallback_errs == 6:
                        print("⚠️ 备份目录里无效的 TOML 太多，后续错误将不再逐条输出。")

    def _index_course_doc(self, data: Dict[str, Any], _fallback: bool = False) -> None:
        """将一个 TOML 文档索引到 course_map/courses_cache。

        当前仅支持两类 schema：
        - normal: 顶层 course_code/course_name + sections/lecturers
        - multi-project: 顶层 courses=[{code,name,...}]，一个仓库包含多门课
        """

        # 1) multi-project：为每个子课程建立可查询条目
        repo_type = str(data.get("repo_type") or "").strip()
        courses = data.get("courses")
        if repo_type == "multi-project" and isinstance(courses, list):
            # 先把父仓库本身索引进去：允许 /查 GeneralKnowledge
            parent_code = str(data.get("course_code") or "").strip().upper()
            if parent_code:
                if not (_fallback and parent_code in self.course_map):
                    data["course_code"] = parent_code
                    self.courses_cache.append(data)
                    self.course_map[parent_code] = data

            # 子课程：若提供了 code，则仍可按 code 精确查询；否则只支持按子课程 name 查询。
            for idx, c in enumerate(courses):
                if not isinstance(c, dict):
                    continue
                sub_code = str(c.get("code") or "").strip().upper()
                sub_name = str(c.get("name") or "").strip()
                if sub_code:
                    if _fallback and sub_code in self.course_map:
                        continue
                    entry = {
                        "_schema": "multi-project-item",
                        "_parent": data,
                        "_course_index": idx,
                        "course_code": sub_code,
                        "course_name": sub_name or sub_code,
                    }
                    self.courses_cache.append(entry)
                    self.course_map[sub_code] = entry
            return

        # 2) normal / legacy：必须有 course_code
        if "course_code" in data:
            code = str(data.get("course_code") or "").strip().upper()
            if not code:
                return
            if _fallback and code in self.course_map:
                return
            # 保证 course_code 统一大写，避免后续搜索/展示不一致
            data["course_code"] = code
            self.courses_cache.append(data)
            self.course_map[code] = data

    def _load_nicknames(self):
        if config.NICKNAME_FILE.exists():
            try:
                with open(config.NICKNAME_FILE, 'r', encoding='utf-8') as f:
                    self.nicknames = json.load(f)
            except Exception:
                self.nicknames = {}
        else:
            self.nicknames = {}

    def save_nicknames(self):
        with open(config.NICKNAME_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.nicknames, f, ensure_ascii=False, indent=2)

    def _is_course_repo_name(self, name: str) -> bool:
        # 规则：首字符大写，且不包含 '-'
        if not name:
            return False
        if "-" in name:
            return False
        return bool(re.match(r"^[A-Z]", name))

    async def _list_github_org_repos(self) -> List[str]:
        org = (config.GITHUB_ORG or "").strip()
        if not org:
            return []

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "hitsz_manager",
        }
        if config.GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"

        per_page = 100
        page = 1
        out: List[str] = []
        async with httpx.AsyncClient(base_url=config.GITHUB_API_BASE, headers=headers, timeout=30) as client:
            while True:
                r = await client.get(f"/orgs/{org}/repos", params={"per_page": per_page, "page": page})
                r.raise_for_status()
                items = r.json()
                if not isinstance(items, list) or not items:
                    break
                for it in items:
                    if isinstance(it, dict) and isinstance(it.get("name"), str):
                        out.append(it["name"])
                if len(items) < per_page:
                    break
                page += 1
        return out

    def _sync_one_repo(self, *, repo_url: str, repo_dir) -> Tuple[str, str]:
        """同步单个仓库（阻塞）。返回 (status, message)。"""
        try:
            if repo_dir.exists():
                # 仅当是 git 仓库才 pull，否则认为不可处理
                try:
                    repo = git.Repo(repo_dir)
                    repo.remotes.origin.pull()
                    return ("pulled", "pull")
                except Exception:
                    return ("skipped", "not a git repo")
            depth = int(getattr(config, "GIT_CLONE_DEPTH", 1) or 0)
            if depth > 0:
                git.Repo.clone_from(repo_url, repo_dir, depth=depth, single_branch=True)
            else:
                git.Repo.clone_from(repo_url, repo_dir)
            return ("cloned", "clone")
        except Exception as e:
            return ("failed", str(e))

    async def _fetch_one_repo_toml(self, *, client: httpx.AsyncClient, org: str, name: str) -> Tuple[str, str]:
        """只下载根目录 readme.toml 到 data/courses/<repo>/readme.toml。返回 (status, message)。"""

        # contents API：/repos/{org}/{repo}/contents/{path}
        # 直接用 raw accept 让 GitHub 返回文件内容。
        paths = ["readme.toml", "README.toml"]

        last_err: str = ""
        content: str | None = None
        picked: str | None = None

        # Retry policy: 3 attempts with exponential backoff (0.5s, 1s, 2s)
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

            for p in paths:
                try:
                    r = await client.get(f"/repos/{org}/{name}/contents/{p}")
                    if r.status_code == 404:
                        last_err = f"{p}: 404"
                        continue
                    r.raise_for_status()
                    # raw accept should return plain text
                    content = r.text
                    picked = p
                    break
                except Exception as e:
                    last_err = str(e)
                    continue

            if content and content.strip():
                break

        if not content or not content.strip():
            return ("failed", f"toml not found ({last_err})")

        repo_dir = config.COURSE_DIR / name
        repo_dir.mkdir(parents=True, exist_ok=True)
        out_path = repo_dir / "readme.toml"
        try:
            out_path.write_text(content.rstrip() + "\n", encoding="utf-8")
        except Exception as e:
            return ("failed", f"write failed: {e}")

        return ("pulled", f"download {picked}")

    async def update_repo(self) -> str:
        """更新课程数据来源。

        - 默认：从 GitHub Org 枚举并同步各课程仓库到 data/courses/<repo_name>/
        - 兼容：若 org 同步失败，可回退到单仓库 REPO_URL + REPO_DIR
        """

        # 1) 优先 GitHub Org 同步
        try:
            repo_names = await self._list_github_org_repos()
            filtered = [n for n in repo_names if self._is_course_repo_name(n)]
            if filtered:
                sem = asyncio.Semaphore(max(1, int(config.GIT_SYNC_CONCURRENCY)))
                org = config.GITHUB_ORG.strip()

                mode = (getattr(config, "GIT_SYNC_MODE", "git") or "git").strip().lower()

                results: List[Tuple[str, str]] = []

                async def run_one(name: str) -> None:
                    async with sem:
                        if mode == "toml":
                            # client is created outside and shared
                            status, msg = await self._fetch_one_repo_toml(client=toml_client, org=org, name=name)
                            results.append((status, f"{name}: {msg}"))
                            return

                        url = f"https://github.com/{org}/{name}.git"
                        repo_dir = config.COURSE_DIR / name
                        status, msg = await asyncio.to_thread(self._sync_one_repo, repo_url=url, repo_dir=repo_dir)
                        results.append((status, f"{name}: {msg}"))

                headers = {
                    "Accept": "application/vnd.github.raw",
                    "User-Agent": "hitsz_manager",
                }
                if config.GITHUB_TOKEN:
                    headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"

                timeout = httpx.Timeout(60.0, connect=20.0)
                async with httpx.AsyncClient(base_url=config.GITHUB_API_BASE, headers=headers, timeout=timeout) as toml_client:
                    await asyncio.gather(*(run_one(n) for n in filtered))

                pulled = sum(1 for s, _ in results if s == "pulled")
                cloned = sum(1 for s, _ in results if s == "cloned")
                skipped = sum(1 for s, _ in results if s == "skipped")
                failed = [(s, m) for s, m in results if s == "failed"]

                self.load_data()

                tail = ""
                if failed:
                    sample = "\n".join([m for _, m in failed[:5]])
                    tail = f"\n⚠️ 失败 {len(failed)} 个（示例前 5）：\n{sample}"

                return (
                    f"✅ 已从 GitHub Org 同步课程仓库（mode={mode}）：pull {pulled} / clone {cloned} / skip {skipped}。"
                    f"\n📚 当前共索引 {len(self.course_map)} 门课程。"
                    f"{tail}"
                )
        except Exception as e:
            # 不中断：尝试回退
            org_err = str(e)
        else:
            org_err = ""

        # 2) 回退：单仓库同步（旧模式）
        try:
            if config.REPO_DIR.exists():
                repo = git.Repo(config.REPO_DIR)
                repo.remotes.origin.pull()
                msg = "Git Pull 成功（旧模式）"
            else:
                depth = int(getattr(config, "GIT_CLONE_DEPTH", 1) or 0)
                if depth > 0:
                    git.Repo.clone_from(config.REPO_URL, config.REPO_DIR, depth=depth, single_branch=True)
                else:
                    git.Repo.clone_from(config.REPO_URL, config.REPO_DIR)
                msg = "Git Clone 成功（旧模式）"

            self.load_data()
            prefix = f"⚠️ Org 同步失败，已回退到旧模式：{org_err}\n" if org_err else ""
            return f"{prefix}✅ {msg}，当前共索引 {len(self.course_map)} 门课程。"
        except Exception as e:
            prefix = f"⚠️ Org 同步失败：{org_err}\n" if org_err else ""
            return f"{prefix}❌ 更新仓库失败: {e}"

    def add_nickname(self, nick: str, code: str) -> bool:
        code = code.upper()
        if code in self.course_map:
            self.nicknames[nick] = code
            self.save_nicknames()
            return True
        return False

    def get_course_detail(self, query: str) -> Optional[Dict[str, Any]]:
        """精确查找：支持 代码、全名、昵称"""
        query = query.strip()
        # 兼容：用户从 /搜 结果复制 "CODE name" 过来
        if " " in query:
            first = query.split(" ", 1)[0].strip().upper()
            if first in self.course_map:
                return self.course_map[first]
        
        # 1. 尝试直接匹配 Code
        if query.upper() in self.course_map:
            return self.course_map[query.upper()]
        
        # 2. 尝试匹配昵称 -> Code
        if query in self.nicknames:
            code = self.nicknames[query]
            if code in self.course_map:
                return self.course_map[code]
        
        # 3. 尝试匹配全名
        for c in self.courses_cache:
            if str(c.get("course_name") or "").strip() == query:
                return c

        # 4. multi-project 子课程：允许直接用子课程名字精确查询
        for c in self.courses_cache:
            if not isinstance(c, dict):
                continue
            if str(c.get("repo_type") or "").strip() != "multi-project":
                continue
            courses = c.get("courses")
            if not isinstance(courses, list):
                continue
            for idx, sub in enumerate(courses):
                if not isinstance(sub, dict):
                    continue
                name = str(sub.get("name") or "").strip()
                if name and name == query:
                    return {
                        "_schema": "multi-project-item",
                        "_parent": c,
                        "_course_index": idx,
                        "course_code": str(c.get("course_code") or "").strip().upper(),
                        "course_name": name,
                    }
        return None

    def search_fuzzy(self, keyword: str) -> List[Dict[str, str]]:
        """搜索：仅硬匹配 + 昵称匹配（不做 fuzzy）。

        返回元素结构保持兼容：{"code": <用于 /查 的查询词>, "name": <展示名>}。
        - 普通课程：code 为课程代码
        - multi-project 父仓库：code 为父 course_code
        - multi-project 子课程：code 为子课程 name（因为很多子课程没有 code）
        """
        kw = (keyword or "").strip()
        if not kw:
            return []

        keyword_l = kw.lower()
        out: List[Dict[str, str]] = []
        seen: set[str] = set()

        def _push(code: str, name: str) -> None:
            code = (code or "").strip()
            if not code or code in seen:
                return
            out.append({"code": code, "name": (name or code).strip()})
            seen.add(code)

        # 1) 昵称匹配：支持“完全命中”与“包含命中”
        for nick, code in self.nicknames.items():
            if not nick:
                continue
            if keyword_l in str(nick).lower():
                mapped = str(code or "").strip().upper()
                course = self.course_map.get(mapped)
                if course:
                    _push(mapped, str(course.get("course_name") or mapped))

        # 2) 普通课程/父仓库：按 code/name 子串硬匹配
        for course in self.courses_cache:
            if not isinstance(course, dict):
                continue
            code = str(course.get("course_code") or "").strip().upper()
            name = str(course.get("course_name") or "").strip()
            hay = f"{code} {name}".lower()
            if code and keyword_l in hay:
                _push(code, name or code)

            # 3) multi-project 子课程：按子课程 name/教师名子串硬匹配
            if str(course.get("repo_type") or "").strip() == "multi-project":
                courses = course.get("courses")
                if not isinstance(courses, list):
                    continue
                parent_code = code
                for sub in courses:
                    if not isinstance(sub, dict):
                        continue
                    sub_name = str(sub.get("name") or "").strip()
                    if not sub_name:
                        continue
                    teachers = sub.get("teachers")
                    teacher_names = ""
                    if isinstance(teachers, list):
                        teacher_names = " ".join(
                            [str(t.get("name") or "").strip() for t in teachers if isinstance(t, dict)]
                        )
                    sub_hay = f"{sub_name} {teacher_names} {parent_code}".lower()
                    if keyword_l in sub_hay:
                        # code 字段用于后续 /查，这里用子课程名作为查询词
                        _push(sub_name, f"{sub_name}（{parent_code}）" if parent_code else sub_name)

        return out[:20]

# 全局单例
course_manager = CourseManager()
