import tomllib
import json
import git
import shutil
from typing import List, Dict, Any, Optional
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
        
        # 遍历所有 TOML 文件 (包括子文件夹)
        for file in config.COURSE_DIR.rglob("*.toml"):
            try:
                with open(file, 'rb') as f:
                    data = tomllib.load(f)
                    if isinstance(data, dict) and 'course_code' in data:
                        # 统一转大写作为 Key
                        code = data['course_code'].strip().upper()
                        self.courses_cache.append(data)
                        self.course_map[code] = data
            except Exception as e:
                print(f"❌ 解析文件 {file.name} 失败: {e}")

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

    async def update_repo(self) -> str:
        """执行 Git Pull"""
        try:
            if config.REPO_DIR.exists():
                repo = git.Repo(config.REPO_DIR)
                repo.remotes.origin.pull()
                msg = "Git Pull 成功"
            else:
                git.Repo.clone_from(config.REPO_URL, config.REPO_DIR)
                msg = "Git Clone 成功"
            
            # 更新后重载内存数据
            self.load_data()
            return f"✅ {msg}，当前共索引 {len(self.course_map)} 门课程。"
        except Exception as e:
            return f"❌ 更新仓库失败: {e}"

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
            if c['course_name'] == query:
                return c
        return None

    def search_fuzzy(self, keyword: str) -> List[str]:
        """模糊搜索，返回匹配的课程全名列表"""
        keyword = keyword.lower()
        
        # 构建搜索语料库: {展示文本: 匹配分数字符串}
        # 匹配分数字符串包含: name + code + all_nicknames
        # 这样搜昵称也能搜到
        
        # 先反向整理 code -> nicknames
        code_nicks = {}
        for n, c in self.nicknames.items():
            code_nicks.setdefault(c, []).append(n)
            
        choices = {}
        for course in self.courses_cache:
            name = course['course_name']
            code = course['course_code']
            nicks = " ".join(code_nicks.get(code, []))
            
            # Key是原本的名字，Value是用来做模糊匹配的长字符串
            full_str = f"{name} {code} {nicks}"
            choices[name] = full_str

        # 使用 thefuzz 提取前 10 个匹配
        results = process.extract(keyword, list(choices.values()), limit=10)
        
        # 过滤低分并还原回名字
        matches = []
        for match_str, score in results:
            if score > 50: # 分数阈值
                # 反查 name
                for name, s_str in choices.items():
                    if s_str == match_str:
                        matches.append(name)
                        break
        
        return list(dict.fromkeys(matches)) # 去重并保持顺序

# 全局单例
course_manager = CourseManager()
