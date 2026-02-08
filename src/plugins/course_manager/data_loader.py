import asyncio
import json
import re
import tomllib
from typing import Any, Dict, List, Optional, Tuple

import git
import httpx
from thefuzz import process
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

        # 优先加载新结构：readme.toml（避免误索引 teachers_reviews.toml 等辅助文件）
        readme_files = list(config.COURSE_DIR.rglob("readme.toml"))
        if readme_files:
            candidates = readme_files
        else:
            # 兼容旧结构：扫描所有 .toml，但排除常见辅助文件
            candidates = [
                p
                for p in config.COURSE_DIR.rglob("*.toml")
                if p.name.lower() not in {"teachers_reviews.toml"}
            ]

        for file in candidates:
            try:
                with open(file, "rb") as f:
                    data = tomllib.load(f)
                if not isinstance(data, dict):
                    continue
                self._index_course_doc(data)
            except Exception as e:
                print(f"❌ 解析文件 {file.name} 失败: {e}")

    def _index_course_doc(self, data: Dict[str, Any]) -> None:
        """将一个 TOML 文档索引到 course_map/courses_cache。

        兼容两类 schema：
        - normal: 顶层 course_code/course_name + sections/lecturers
        - multi-project: 顶层 courses=[{code,name,...}]，一个仓库包含多门课
        - legacy: 顶层 course_code/course_name + course/exam/lab... 等
        """

        # 1) multi-project：为每个子课程建立可查询条目
        repo_type = str(data.get("repo_type") or "").strip()
        courses = data.get("courses")
        if repo_type == "multi-project" and isinstance(courses, list):
            for idx, c in enumerate(courses):
                if not isinstance(c, dict):
                    continue
                sub_code = str(c.get("code") or "").strip().upper()
                sub_name = str(c.get("name") or "").strip() or sub_code
                if not sub_code:
                    continue
                entry = {
                    "_schema": "multi-project-item",
                    "_parent": data,
                    "_course_index": idx,
                    "course_code": sub_code,
                    "course_name": sub_name,
                }
                self.courses_cache.append(entry)
                self.course_map[sub_code] = entry
            return

        # 2) normal / legacy：必须有 course_code
        if "course_code" in data:
            code = str(data.get("course_code") or "").strip().upper()
            if not code:
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
            git.Repo.clone_from(repo_url, repo_dir)
            return ("cloned", "clone")
        except Exception as e:
            return ("failed", str(e))

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

                results: List[Tuple[str, str]] = []

                async def run_one(name: str) -> None:
                    async with sem:
                        url = f"https://github.com/{org}/{name}.git"
                        repo_dir = config.COURSE_DIR / name
                        status, msg = await asyncio.to_thread(self._sync_one_repo, repo_url=url, repo_dir=repo_dir)
                        results.append((status, f"{name}: {msg}"))

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
                    f"✅ 已从 GitHub Org 同步课程仓库：pull {pulled} / clone {cloned} / skip {skipped}。"
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
        return None

    def search_fuzzy(self, keyword: str) -> List[Dict[str, str]]:
        """模糊搜索，返回匹配的课程条目（含 code+name）。"""
        keyword = keyword.lower()
        
        # 构建搜索语料库: {展示文本: 匹配分数字符串}
        # 匹配分数字符串包含: name + code + all_nicknames
        # 这样搜昵称也能搜到
        
        # 先反向整理 code -> nicknames
        code_nicks = {}
        for n, c in self.nicknames.items():
            code_nicks.setdefault(c, []).append(n)
            
        choices: Dict[str, str] = {}
        for course in self.courses_cache:
            name = str(course.get("course_name") or "").strip()
            code = str(course.get("course_code") or "").strip().upper()
            if not code:
                continue
            nicks = " ".join(code_nicks.get(code, []))
            display = f"{code} {name}".strip()
            choices[display] = f"{name} {code} {nicks}".strip()

        results = process.extract(keyword, choices, limit=10)

        out: List[Dict[str, str]] = []
        for display, score in results:
            if score <= 50:
                continue
            parts = str(display).split(" ", 1)
            code = parts[0].strip().upper()
            name = parts[1].strip() if len(parts) > 1 else code
            out.append({"code": code, "name": name})
        return out

# 全局单例
course_manager = CourseManager()
