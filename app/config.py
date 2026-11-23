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
    SECRET_FIELDS: ClassVar[tuple[str, ...]] = (
        "llm_api_key",
        "search_api_key",
        "xhs_cookie",
        "auth_api_keys",
        "auth_jwt_secret",
    )

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

    # ---------- 品牌范围 ----------
    # jp = 线上只识别/检索日牌；all = 全部品牌（含国牌、OTHER）。
    # 国牌的品牌条目、别名和 xhs_account 登记仍留在文件里，只是不参与识别与检索。
    # 需要通过 BRAND_SCOPE=all 才恢复国牌路径，不靠改数据文件。
    brand_scope: str = "jp"

    lolita_rag_root: Path = DEFAULT_RAG_ROOT

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"

    # 前端顶栏展示的产品名（"lolitaagent · 用户名"）。只影响展示，不改实际调用的
    # llm_model、接口入参或日志里的模型标识；换模型时前端不需要跟着改。
    app_display_name: str = "lolitaagent"

    # ---------- 模型分级路由 ----------
    # 按问题类型选写作模型：cheap（默认，全部走便宜档）/ auto（规则判据，
    # 仅洗护/安全与稀薄档案题走强档；可选实验开关）/ strong（强制强档）。
    # 14 题实测（2026-11）：auto 成本 1.91x cheap，句子引用率 73.1% vs 82.6%，
    # 未达到「质量可测提升 + 成本增幅 ≤1.3x」的默认开启标准；auto 因此保留为
    # 显式开关，不默认启用。数据见 eval/forensics/routing-cost/。
    model_routing_mode: str = "cheap"
    # 便宜档 / 强档的具体模型名。留空时回落 LLM_MODEL（只用一家模型也能跑）。
    llm_model_cheap: str = ""
    llm_model_strong: str = ""
    # 规则判不出来时，是否允许用**便宜档**做一次极小的判断（max_tokens 很小）。
    # 失败/超时一律按便宜档兜底，不影响回答。
    model_routing_llm: bool = True

    # 写作单次输出的 token 上限。推理模型（如 deepseek-reasoner）的推理 token
    # 也计入这个预算：600 会在长证据题上先把预算烧在推理上，正文被截断甚至为空。
    # 放宽后模型写完就停，不会为了填满上限而多花钱。
    writer_max_tokens: int = 1800

    # ---------- 成本核算 ----------
    # 每 1K token 的单价，形如：
    #   {"deepseek-chat": {"prompt_per_1k": 0.002, "completion_per_1k": 0.008}}
    # 价格随供应商/时段变化，所以放配置而不写死在代码里。没配价格的模型会被
    # 标成 unpriced，金额只累计能算出来的部分，不会伪造成 0。
    llm_price_table: dict[str, dict[str, float]] = {}
    llm_price_currency: str = "CNY"

    # 检索只取**最相关的一条**。这里是**单通道**的默认值；多路召回在结果层合并，
    # 每通道各取 top_k，最后统一编号给 writer。
    top_k: int = 1

    # 合并后证据条数的上限（去重之后）。六个通道各 1-3 条、再加裙子档案，最坏会到
    # 十几条，编号过长既挤占回答篇幅、也让引用难对。补查通道（xhs/weibo/web）
    # **整体豁免**，不会被核心通道的列表挤掉；因此实际条数可能略超这个值。
    # <=0 表示不限制。
    evidence_max_items: int = 12

    # 单条证据送进 writer 的正文长度。默认 300 字足够概括大部分帖子；但定义类
    # （「A 和 B 有什么区别」）与洗护/安全类问题的答案往往就藏在长文的后半段，
    # 300 字会在关键结论前截断（own-08 的「同系列的 OP…」）。这两类放宽到
    # ``evidence_text_limit_long``，其余保持默认，避免整体 prompt 变贵。
    evidence_text_limit: int = 300
    evidence_text_limit_long: int = 700

    # ---------- 精排（reranker） ----------
    # 公网演示要求保留精排，且默认开启。fp32 torch 版本在本机实测整链峰值
    # 1.54–1.68 GB（batch 4/16），2 GiB 下余量很薄；int8 ONNX 约 1.46 GB，
    # 权重 266 MB + 激活约 150 MB，再配合「首次打分才加载 / 闲置后卸载」。
    # 模型文件或 onnxruntime 缺失时回退 torch；后端不可用只会让精排降级为原序，
    # 不影响检索本身可用。
    reranker_enabled: bool = True
    # auto：有 int8 ONNX 就用 ONNX，否则 torch；onnx：强制 ONNX（缺失仍回退 torch）；
    # torch：走上游的 sentence_transformers.CrossEncoder。
    reranker_backend: str = "auto"
    # 留空 = <LOLITA_RAG_ROOT>/data/models/bge-reranker-base（上游自带的那份）。
    reranker_model: str = ""
    # 留空 = <reranker_model>/onnx/model.int8.onnx。用 scripts/export_reranker_onnx.py 生成。
    reranker_onnx_path: Path | None = None
    # 闲置多少秒后卸载模型（<=0 表示常驻不卸载）。实测加载约 0.8 秒，
    # 闲置后的第一问含加载约 1.2 秒。
    reranker_idle_unload_seconds: float = 120.0
    # 实测 20 条短候选：batch=4 约 275 ms、batch=16 约 420 ms，且峰值更低。
    reranker_batch_size: int = 4

    # 单次问答的**整体**预算（秒）。各通道和模型客户端都有自己的超时，但没有总预算时
    # 最坏会串成两分钟（核心召回 + 30s 规划 + 65s 小红书 + 20s 搜索 + 60s 写作）。
    # 超时返回已查到的证据并注明「部分通道超时」，不报 500。<=0 表示不限制。
    agent_deadline: float = 60.0

    # ---------- 启动预热 ----------
    # 打开后，服务启动即在**后台线程**跑一次检索（不调 LLM），把 BGE 与精排模型
    # 加载进内存，公网第一问就不必再付 20–30 秒的冷加载。代价：开机后约 25 秒
    # 才预热完（期间服务已可访问，只是首问会等加载），空载常驻内存从约 160 MB
    # 升到 1.2 GB 左右。默认关闭，2C2G 演示档在 deploy/env/2c2g.env.example 里打开。
    warmup_on_start: bool = False

    # ---------- 召回通道：小红书 ----------
    # 总开关。关闭时通道状态显示 disabled + 原因，召回直接跳过、不发任何请求。
    # 默认保持关闭：公网服务器没有登录态，打开只会得到登录墙。
    xhs_enabled: bool = False
    xhs_timeout: float = 45.0
    # 单次小红书召回最多取几条；硬上限 10 在 xhs_search 里。关注扫描与实时
    # 交易类问题需要的覆盖面不同，所以做成配置而不是在通道里写死。
    xhs_max_results: int = 5
    # 采集 provider：playwright（浏览器自动化，默认）| spider_xhs（逆向签名，免浏览器）。
    xhs_provider: str = "playwright"
    # Spider_XHS 外部仓库路径（平级目录，不进本仓 git）。
    xhs_spider_root: Path = APP_ROOT.parent / "Spider_XHS"
    # 小红书登录 Cookie（需含 a1 + web_session；运行时注入，不进 git）。
    xhs_cookie: str = ""

    # ---------- 图片文字识别（可选，按需触发） ----------
    # 只在「价格/尺码/预约这类要具体数值」且「正文提示价格标在图上 / 正文过短
    # 带图 / 全篇没有价格数字」时才识别，最多 5 条证据 × 3 张图；其余问题完全
    # 走原路径。依赖 rapidocr-onnxruntime 是可选包，没装时自动跳过（OCR_ENABLED
    # 打开也只在装了依赖后才生效）。
    ocr_enabled: bool = True
    # 单图下载 / 识别的超时（秒）与体积上限；超限只丢这一张图。
    ocr_image_timeout: float = 8.0
    ocr_image_max_bytes: int = 5 * 1024 * 1024
    # 单次召回最多识别几条证据、每条最多几张图。
    ocr_max_items: int = 5
    ocr_images_per_item: int = 3
    # 整段图片识别的总预算（秒）。超了放弃剩余图片，已识别到的照常进证据。
    ocr_total_budget: float = 12.0

    # ---------- 召回通道：网页/微博（博查 / 智谱） ----------
    search_enabled: bool = False
    search_api_key: str = ""
    search_provider: str = "bocha"
    search_base_url: str = ""
    search_engine: str = "search_std"
    search_max_results: int = 5
    search_timeout: float = 20.0
    # 配额失效（智谱 code=1113）后多久内直接按不可用降级，不再反复打接口。
    # 账号资源包耗尽不会秒级恢复；等 TTL 到期再试一次就够，重试只会白等。
    search_quota_ttl: float = 900.0

    # 无 key 网页来源（公网演示）：Bing 浏览器检索为主，360 文本兜底。
    # 只在付费搜索不可用 / 配额失效 / 未配置 key 时启用；Bing 为空或失败才走
    # 360，且 360 结果不带链接，只当文本线索。需要 Playwright + Edge/Chromium。
    web_browser_enabled: bool = True
    web_browser_timeout: float = 25.0
    # 网页作为「最后手段」兜底时的硬超时：它只在官网/微博都没拿到证据时才跑，
    # 再让它按 25 秒拖就没有意义。实际取 min(本值, web_browser_timeout)。
    web_fallback_timeout: float = 6.0
    web_browser_max_results: int = 5

    # 微博应急直抓：付费搜索不可用时作为独立来源；搜索可用时先走搜索，
    # 配额失效（1113）再回退到这里（不替换 provider）。
    # 走 Playwright 打开 m.weibo.cn 搜索，只读第一页；翻页会跳登录，不做。
    weibo_direct_enabled: bool = True
    # 浏览器启动 + 首屏的总预算（秒）。超时按通道 skip 降级，不拖累其他通道。
    weibo_direct_timeout: float = 25.0
    weibo_direct_max_results: int = 5

    # ---------- 关注项扫描 ----------
    # 单个关注项的超时与整轮扫描的总预算（秒）。xhs 最坏要「搜索 + 3 次详情」
    # 约 4×15s，关注项一多就会把「检查更新」拖成几分钟。超时的项只报 timeout，
    # 不写事件、不动基线，下次扫描再比。<=0 表示该项不限制。
    follow_item_timeout: float = 60.0
    follow_check_deadline: float = 120.0

    # ---------- 后台任务队列 ----------
    # 关注扫描、索引重建这类长任务入队执行，接口立刻返回 202 + job_id。
    # QUEUE_MODE=inline 退回同步执行（测试 / worker 起不来时的应急）；任务状态
    # 一律可在 GET /api/jobs 查看。
    queue_enabled: bool = True
    queue_mode: str = "async"

    # HTTP 触发索引重建默认关闭：上游写盘不是**整体**原子（单个文件是临时文件 +
    # os.replace，但 Chroma 增量持久化、manifest 最后直接 write_text），中断可能
    # 留下半成品索引。确认原子性后再打开；日常用 scripts/build_kb_index.py。
    kb_rebuild_api: bool = False
    kb_rebuild_timeout: float = 1800.0

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

    # ---------- 鉴权与配额（可选，默认全关） ----------
    # 本地单用户默认 off：打开就能用。开启后只保护 /api/*（静态资源与首页公开）。
    # off / api_key / jwt 三选一；api_key 用 X-API-Key 或 Bearer，jwt 用 Bearer。
    auth_mode: str = "off"
    # 逗号分隔；支持 "key:展示名"。展示名只用于日志与限流分桶，原始 key 不落日志。
    auth_api_keys: str = ""
    auth_jwt_secret: str = ""
    auth_jwt_ttl: int = 3600
    # 每个 key 在窗口内最多几次；<=0 关闭限流。0 次/0 秒都不启用。
    rate_limit_requests: int = 0
    rate_limit_window: float = 60.0
    # 每日 LLM 成本配额（与 LLM_PRICE_CURRENCY 同币种）；<=0 关闭。
    # 读数来自 trace.cost_summary(days=1)，不另起计量。
    daily_quota: float = 0.0

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
    def weibo_cache_dir(self) -> Path:
        """微博直抓结果的 6 小时缓存目录。"""
        return self.data_dir / "weibo" / "cache"

    @property
    def ocr_cache_dir(self) -> Path:
        """图片文字识别结果缓存（按图片 URL + 引擎版本）。"""
        return self.data_dir / "ocr" / "cache"

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
            self.weibo_cache_dir,
            self.ocr_cache_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


settings = AppSettings()
