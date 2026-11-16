"""品牌 → 小红书官方账号：先白名单，再搜一次并缓存，查不到就回退。

为什么需要这一层：小红书搜索结果里能看到作者，但「是不是品牌官方发的」不能靠
标题猜——实测同名的「古典玩偶种草小天使」「古典玩偶一生黑」都不是官方号。只有
用户搜索接口带 ``red_official_verified``，所以判定要单独走一次用户搜索，并且
**必须缓存**，否则每次提问都多一次请求。

边界：

* 只采信 ``red_official_verified`` 为真的账号，且账号名要能对上品牌名/别名；
* 账号表里没登记 ≠ 品牌没有账号，只是我们没核实过——运行时搜不到就记空结果
  缓存住，避免反复请求；
* 整条链路是增强：任何失败、超时都返回 ``None``，调用方照旧走现有 UGC 召回。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from app.agent import dress
from app.config import AppSettings

logger = logging.getLogger(__name__)

# 账号归属变化很慢：确认过的长期有效，没搜到的 7 天后再试一次就够。
POSITIVE_TTL_SECONDS = 30 * 24 * 3600
NEGATIVE_TTL_SECONDS = 7 * 24 * 3600
ACCOUNT_CACHE_VERSION = 1
# 单次用户搜索的预算。实测一次 0.3～0.5s；给到 4s 是为了兜住签名进程冷启动，
# 再久就不值得等——没有官方号时用户不该为这条链路多等。
SEARCH_TIMEOUT = 4.0
# 官方帖：一页主页 + 至多 3 条详情，实测约 1s；超了就整路放弃。
NOTES_TIMEOUT = 8.0
NOTES_LIMIT = 2
NOTES_PROBE = 3

_cache_lock = threading.Lock()


def _accounts_path(app: AppSettings) -> Path:
    return app.xhs_cache_dir / "accounts.json"


def _read_store(app: AppSettings) -> dict:
    try:
        payload = json.loads(_accounts_path(app).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("v") != ACCOUNT_CACHE_VERSION:
        return {}
    brands = payload.get("brands")
    return brands if isinstance(brands, dict) else {}


def _entry_fresh(entry: dict) -> bool:
    ttl = POSITIVE_TTL_SECONDS if entry.get("user_id") else NEGATIVE_TTL_SECONDS
    try:
        age = time.time() - float(entry.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= ttl


def _write_store(app: AppSettings, brand_id: str, entry: dict) -> None:
    """读改写整个小文件；账号表只有几十条，不值得为它引第三方存储。"""
    path = _accounts_path(app)
    with _cache_lock:
        store = _read_store(app)
        store[brand_id] = entry
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"v": ACCOUNT_CACHE_VERSION, "brands": store}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.info("小红书账号缓存写入失败：%s", exc)


def _normalize(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _name_matches(name: str, brand: dress.Brand) -> bool:
    """账号名要能对上品牌名或别名：官方号常写成「古典玩偶ClassicalPuppets」。"""
    target = _normalize(name)
    if not target:
        return False
    for candidate in (brand.name, *brand.aliases):
        value = _normalize(candidate)
        if len(value) >= 2 and (value in target or target in value):
            return True
    return False


def _within(func, seconds: float, *args, **kwargs) -> tuple[bool, object]:
    """在预算内跑 func，返回 ``(是否跑完, 结果)``。

    超时和「搜到了、但没有认证账号」必须分开：前者不该写缓存（下次还该再试），
    后者要记成空结果，否则每次提问都为同一个没有官方号的品牌多打一次请求。
    """
    box: dict = {}

    def worker() -> None:
        try:
            box["value"] = func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 原样带回主线程
            box["error"] = exc

    thread = threading.Thread(target=worker, name="xhs-account", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        logger.info("小红书账号链路超时（%ss），按没有官方号处理", seconds)
        return False, None
    error = box.get("error")
    if error is not None:
        logger.info("小红书账号链路失败：%s: %s", type(error).__name__, error)
        return False, None
    return True, box.get("value")


def brand_for_question(question: str, app: AppSettings) -> tuple[dress.Brand | None, tuple[str, ...]]:
    """问题里点名的品牌（或裙子所属品牌）与该系列名。识别不到就返回 ``(None, ())``。"""
    try:
        brands = dress.load_brands(app)
        brand, _note = dress._match_brand(question, brands)
        if brand is not None:
            return brand, ()
        alias, _note = dress._match_series(
            question, dress.load_series_aliases(app)
        )
        if alias is None:
            return None, ()
        return dress._brand_by_id(brands, alias.brand_id), (alias.cn, alias.en)
    except Exception as exc:  # noqa: BLE001 - 识别失败只是没有官方号
        logger.info("品牌识别失败，跳过官方账号：%s", exc)
        return None, ()


def _search_verified(brand: dress.Brand, app: AppSettings) -> dict | None:
    from app.services import xhs_spider

    XHS_Apis, XHSPcAuth = xhs_spider._load_api(app)
    api = XHS_Apis(XHSPcAuth.from_cookie(str(app.xhs_cookie).strip()))
    success, msg, users = api.search_some_user(brand.name, 5)
    if not success:
        raise RuntimeError(f"用户搜索失败：{msg}")
    for user in users or []:
        if not isinstance(user, dict) or not user.get("red_official_verified"):
            continue
        if not _name_matches(str(user.get("name") or ""), brand):
            continue
        return {
            "user_id": str(user.get("id") or "").strip(),
            "nickname": str(user.get("name") or "").strip(),
        }
    return None


def resolve(brand: dress.Brand, app: AppSettings) -> dict | None:
    """取品牌的小红书官方账号：白名单 → 本地缓存 → 搜一次（带预算）。"""
    if brand is None:
        return None
    if brand.xhs_account_id:
        return {
            "user_id": brand.xhs_account_id,
            "nickname": brand.xhs_account_name or brand.name,
            "source": "brands",
        }

    entry = _read_store(app).get(brand.id)
    if isinstance(entry, dict) and _entry_fresh(entry):
        if not entry.get("user_id"):
            return None
        return {
            "user_id": str(entry["user_id"]),
            "nickname": str(entry.get("nickname") or ""),
            "source": "cache",
        }

    from app.services import xhs_search

    try:
        usable = xhs_search.available(app) and xhs_search._provider(app) == "spider_xhs"
    except Exception:  # noqa: BLE001 - 配置写错只是没有官方号，不该影响召回
        usable = False
    if not usable:
        # Playwright 方案没有用户搜索，只能吃白名单与已有缓存。
        return None

    done, found = _within(_search_verified, SEARCH_TIMEOUT, brand, app)
    if not done:
        # 超时/失败不写缓存：下次还该再试，别把偶发故障记成「这个品牌没有官方号」。
        return None
    if not isinstance(found, dict):
        # 搜到了但没有通过认证的账号：记空结果，7 天内不再为它发请求。
        _write_store(app, brand.id, {"user_id": "", "ts": time.time(), "source": "search"})
        return None
    _write_store(
        app,
        brand.id,
        {
            "user_id": found["user_id"],
            "nickname": found["nickname"],
            "ts": time.time(),
            "source": "search",
        },
    )
    return {**found, "source": "search"}


def official_notes(
    account: dict, app: AppSettings, *, series: tuple[str, ...] = ()
) -> list[dict]:
    """官方账号第一页里挑出的情报帖；失败/超时返回空表，绝不抛。"""
    user_id = str((account or {}).get("user_id") or "")
    if not user_id:
        return []
    from app.services import xhs_spider

    done, notes = _within(
        xhs_spider.fetch_account_notes,
        NOTES_TIMEOUT,
        user_id,
        app,
        series=series,
        limit=NOTES_LIMIT,
        probe=NOTES_PROBE,
    )
    if not done:
        return []
    return [note for note in (notes or []) if isinstance(note, dict)]
