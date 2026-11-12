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
    # 也允许把运行时数据（索引、xhs 会话、偏好库）放到另一块盘。
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

    # ---------- 召回通道：小红书 ----------
    xhs_enabled: bool = False
    xhs_timeout: float = 45.0
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

    # ---------- 派生路径 ----------
    @property
    def kb_dir(self) -> Path:
        """自有知识库源文件（纯文本 .md，进 git）。"""
        return self.content_dir / "kb"

    @property
    def kb_index_dir(self) -> Path:
        """自有知识库索引的家（`own` 通道）。与用户数据分开：删索引不会碰偏好。"""
        return self.data_dir / "kbindex"

    @property
    def db_path(self) -> Path:
        """偏好 / 关注项 / 会话记忆的 SQLite 库。唯一不可再生的东西。"""
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
        for path in (self.data_dir, self.kb_index_dir, self.lolibrary_dir, self.xhs_cache_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = AppSettings()
