"""应用配置。

与上游（lolita-rag）的分工：
* 本文件只管**应用层**：绑定地址、数据目录、知识库索引目录、LLM 凭据、召回通道开关；
* 检索/生成的全部参数（阈值、闸门、融合方式）留在上游 `Settings` 里，
  由 bridge.py 显式覆盖少数几项（data_dir / 密钥），其余一个不动——
  那些数字是上游在真实数据上标定出来的，这里没有资格改。

多路召回（ADR-0001）的通道开关与运行时目录也在这里：小红书（Playwright 或
Spider_XHS，可切换）、微博/网页（博查/智谱）都是**可选**通道，未配置时对应
通道召回会跳过而不是猜。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_ROOT = Path(__file__).resolve().parents[1]

# 上游仓库的位置。默认取本仓同级的 lolita-rag（README 的推荐布局）；
# 放在别的盘/目录时用 .env 的 LOLITA_RAG_ROOT 覆盖，不用改代码。
DEFAULT_RAG_ROOT = APP_ROOT.parent / "lolita-rag"


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=APP_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 这些项一旦进日志就等于泄露账号会话 / 账单，上游踩过（pytest 打印 fixture）。
    SECRET_FIELDS: ClassVar[tuple[str, ...]] = ("llm_api_key", "search_api_key", "xhs_cookie")

    def __repr__(self) -> str:
        parts: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name, None)
            if name in self.SECRET_FIELDS:
                length = len(str(value or "").strip())
                parts.append(f"{name}={'<未设置>' if not length else f'<已设置 {length} 字符>'}")
            else:
                parts.append(f"{name}={value!r}")
        return f"{type(self).__name__}({', '.join(parts)})"

    bind_host: str = "127.0.0.1"
    bind_port: int = 8787

    # 数据目录。做成**字段**而不是硬编码属性，是为了测试能把数据指到临时目录，
    # 也允许把运行时数据（索引、xhs 会话、用户数据库）放到另一块盘。
    data_dir: Path = APP_ROOT / "data"

    # 内容目录（自有知识库 content/kb，进 git）。做成字段同样是为了测试隔离。
    content_dir: Path = APP_ROOT / "content"

    lolita_rag_root: Path = DEFAULT_RAG_ROOT

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"

    # 检索只取**最相关的一条**。这里是**单通道**的默认值；多路召回在结果层合并，
    # 每通道各取 top_k，最后统一编号给 writer。
    top_k: int = 1

    # 单次问答的**整体**预算（秒）。各通道和模型客户端都有自己的超时，但没有总预算时
    # 最坏会串成两分钟（核心召回 + 30s 规划 + 65s 小红书 + 20s 搜索 + 60s 写作）。
    # 超时返回已查到的证据并注明「部分通道超时」，不报 500。<=0 表示不限制。
    agent_deadline: float = 60.0

    # ---------- 召回通道：小红书 ----------
    xhs_enabled: bool = False
    xhs_timeout: float = 45.0
    # 单次小红书召回最多取几条；硬上限 10 在 xhs_search 里。关注扫描与实时
    # 交易类问题需要的覆盖面不同，所以做成配置而不是在通道里写死。
    xhs_max_results: int = 3
    # 采集 provider：playwright（浏览器自动化，默认）| spider_xhs（逆向签名，免浏览器）。
    xhs_provider: str = "playwright"
    # Spider_XHS 外部仓库路径（平级目录，不进本仓 git）。
    xhs_spider_root: Path = APP_ROOT.parent / "Spider_XHS"
    # 小红书登录 Cookie（需含 a1 + web_session；运行时注入，不进 git）。
    xhs_cookie: str = ""

    # ---------- 召回通道：网页/微博（博查 / 智谱） ----------
    search_enabled: bool = False
    search_api_key: str = ""
    search_provider: str = "bocha"
    search_base_url: str = ""
    search_engine: str = "search_std"
    search_max_results: int = 5
    search_timeout: float = 20.0

    # ---------- 关注项扫描 ----------
    # 单个关注项的超时与整轮扫描的总预算（秒）。xhs 最坏要「搜索 + 3 次详情」
    # 约 4×15s，关注项一多就会把「检查更新」拖成几分钟。超时的项只报 timeout，
    # 不写事件、不动基线，下次扫描再比。<=0 表示该项不限制。
    follow_item_timeout: float = 60.0
    follow_check_deadline: float = 120.0

    # ---------- 裙子档案 ----------
    # 日牌本地档案缺少发售年/原价/配色时，是否在线查 Lolibrary 补详情。
    # 离线部署或测试可以关掉；关掉后只返回本地档案已有的字段。
    lolibrary_online_enabled: bool = True

    # ---------- 会话记忆 ----------
    # 服务端持久化的对话保留最近多少条消息（一问一答算 2 条）。<=0 表示不自动
    # 清理。默认 100 条 ≈ 50 轮，足够接住追问，又不会让 app.db 无限增长。
    conversation_retention_messages: int = 100

    # ---------- 可观测性 ----------
    # 轨迹 JSONL 按文件大小轮转，避免 data/logs/chat_runs.jsonl 随使用无限增长。
    # 只保留最近 N 个归档；<=0 关闭轮转（排查时可以临时关掉）。
    trace_max_bytes: int = 5 * 1024 * 1024
    trace_keep_files: int = 3

    # ---------- 派生路径 ----------
    @property
    def kb_dir(self) -> Path:
        """自有知识库源文件（纯文本 .md，进 git）。"""
        return self.content_dir / "kb"

    @property
    def kb_index_dir(self) -> Path:
        """自有知识库索引的家（`own` 通道）。与用户数据分开：删索引不会碰关注项。"""
        return self.data_dir / "kbindex"

    @property
    def db_path(self) -> Path:
        """关注项 / 会话记忆的 SQLite 库。唯一不可再生的东西。"""
        return self.data_dir / "app.db"

    @property
    def lolibrary_dir(self) -> Path:
        """Lolibrary 档案缓存的家（第三方档案，不进 git、可重建）。"""
        return self.data_dir / "lolibrary"

    @property
    def lolibrary_catalog_path(self) -> Path:
        """档案索引（JSONL，一行一个条目），`lolibrary` 通道读它。"""
        return self.lolibrary_dir / "catalog.jsonl"

    @property
    def brands_path(self) -> Path:
        """品牌/地区注册表（`content/brands.json`，进 git）。"""
        return self.content_dir / "brands.json"

    @property
    def dress_aliases_path(self) -> Path:
        """系列级中文译名表（`content/dress/aliases.json`，进 git）。"""
        return self.content_dir / "dress" / "aliases.json"

    @property
    def dress_glossary_path(self) -> Path:
        """中文意象词 → 英文词（`content/dress/glossary.json`，进 git）。"""
        return self.content_dir / "dress" / "glossary.json"

    @property
    def learned_aliases_path(self) -> Path:
        """模型识别并经 Lolibrary 校验后学到的译名（运行时缓存，不进 git）。"""
        return self.data_dir / "dress" / "learned_aliases.json"

    @property
    def xhs_profile_dir(self) -> Path:
        """小红书登录会话目录（浏览器持久化 profile，运行时数据，不进 git）。"""
        return self.data_dir / "xhs" / "profile"

    @property
    def xhs_cache_dir(self) -> Path:
        """小红书搜索结果的 6 小时缓存目录。"""
        return self.data_dir / "xhs" / "cache"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key.strip())

    def ensure_dirs(self) -> None:
        """只建运行时目录。content/ 不自动创建——那样会把「路径写错了」变成空目录。"""
        for path in (
            self.data_dir,
            self.kb_index_dir,
            self.lolibrary_dir,
            self.learned_aliases_path.parent,
            self.xhs_cache_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


settings = AppSettings()
