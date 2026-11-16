"""按需图片文字识别（可选依赖，本机 CPU）。

## 为什么是「只读字、不下结论」

``eval/ocr_accuracy.md`` 实测：rapidocr 在真实召回图上的单元格正确率 86.8%，
但行完整率只有 46.8%，存在静默错值（99→66）与同图不同编码结果不一致，
置信度也不能用来兜底。所以这一层**只负责把字读出来**：

* 不判断哪一行是 SKU、哪一列是定金，不整理成表格；
* 识别结果是否可用由上层决定，进证据时必须标「OCR，未校验」。

## 降级

依赖懒加载：没装、引擎加载失败、单图超时、图片超过上限、下载失败都只让
这一张图缺席并记日志，绝不抛给调用方，也不重试。

## 缓存

按图片 URL + 引擎版本缓存识别结果。同一张图只识别一次；引擎升级后版本
不匹配自动重识别，不会拿旧引擎的结果冒充新引擎。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import AppSettings
from app.services import imagefetch

logger = logging.getLogger(__name__)

ENGINE = "rapidocr_onnxruntime"
# 缓存结构版本。识别文本的形态变了就加一；引擎版本另算进缓存键。
# v2：改为按检测框 y 中心分组还原行，旧缓存是按引擎返回顺序直接拼接的错位文本，
# 不兼容——否则修好行序后仍会读到旧的错位结果。
CACHE_VERSION = 2

_engine_lock = threading.Lock()
_engine_box: dict[str, object] = {}


class OcrError(RuntimeError):
    """单张图识别失败（下载 / 超时 / 引擎异常）。调用方跳过，不重试。"""


def engine_version() -> str:
    """识别引擎版本，写进缓存键：换引擎后旧结果不再命中。"""
    try:
        from importlib.metadata import version

        return version(ENGINE)
    except Exception:  # noqa: BLE001 - 未安装 / 元数据缺失
        return "unknown"


def readiness(app: AppSettings) -> tuple[bool, str]:
    """是否可用，并给出具体原因（状态接口与召回共用这一份判断）。"""
    if not app.ocr_enabled:
        return False, "未启用（OCR_ENABLED=false）"
    if importlib.util.find_spec(ENGINE) is None:
        return False, f"未安装 {ENGINE}（可选依赖，装好后自动生效）"
    return True, ""


def _cache_path(app: AppSettings, url: str) -> Path:
    raw = f"{ENGINE}|{engine_version()}|{url}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return app.ocr_cache_dir / f"{digest}.json"


def _read_cache(path: Path) -> str | None:
    """命中返回文本（可能是空串，表示这张图确实没读出字）；未命中返回 None。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("v") != CACHE_VERSION or payload.get("engine") != engine_version():
        return None
    text = payload.get("text")
    return text if isinstance(text, str) else None


def _write_cache(path: Path, url: str, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "v": CACHE_VERSION,
                    "engine": engine_version(),
                    "url": url,
                    "ts": time.time(),
                    "text": text,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.info("图片文字缓存写入失败：%s", exc)


def _download(url: str, *, max_bytes: int, timeout: float) -> bytes:
    """下载一张图。超过 ``max_bytes`` 或超时都抛 :class:`OcrError`。"""
    headers = {"User-Agent": imagefetch.FETCH_UA}
    referer = imagefetch.referer_for(url)
    if referer:
        # 微博图床防盗链：不带 Referer 会 403，图片永远进不了 OCR。
        headers["Referer"] = referer
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout, context=imagefetch.ssl_context()) as response:  # noqa: S310 - 只读 GET
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise OcrError(f"图片 {int(length)} 字节，超过上限 {max_bytes}")
            data = response.read(max_bytes + 1)
    except HTTPError as exc:
        raise OcrError(f"图片 HTTP {exc.code}") from exc
    except URLError as exc:
        raise OcrError(f"图片连不上：{exc.reason}") from exc
    except TimeoutError as exc:
        raise OcrError("图片下载超时") from exc
    if len(data) > max_bytes:
        raise OcrError(f"图片超过上限 {max_bytes} 字节")
    if not data:
        raise OcrError("图片为空")
    return data


def _engine():
    """懒加载 + 进程内单例。第一次调用约 0.2s，之后复用。"""
    with _engine_lock:
        engine = _engine_box.get("engine")
        if engine is None:
            from rapidocr_onnxruntime import RapidOCR

            engine = RapidOCR()
            _engine_box["engine"] = engine
        return engine


def _group_lines(output: list) -> str:
    """按检测框还原行：先按 y 中心聚成行，行内按 x 从左到右拼接。

    为什么不能直接按引擎返回顺序拼：实测同一张价格表，引擎顺序可能是
    「定金 / 棉OP短袖 / 尾款 / 预约价 / 现货价 / 188 / 长款本体 / 390…」，
    列头和数据行全错开，模型会把尾款当预约价。这里只做**几何还原**，
    不判断列含义——列归属仍然由「原样引用整行 + 对照原图」约束。
    """
    items: list[dict] = []
    for box, text, _score in output or []:
        value = str(text).strip()
        if not value:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        items.append(
            {
                "text": value,
                "cy": sum(ys) / len(ys),
                "x0": min(xs),
                "height": max(ys) - min(ys),
            }
        )
    if not items:
        return ""

    heights = sorted(item["height"] for item in items if item["height"] > 0)
    median_height = heights[len(heights) // 2] if heights else 10.0
    # 同一行的框会有几像素的基线差；按中位字高的一半做容差，避免把两行并成一行。
    tolerance = max(6.0, median_height * 0.6)

    items.sort(key=lambda item: (item["cy"], item["x0"]))
    rows: list[dict] = []
    for item in items:
        if rows and abs(item["cy"] - rows[-1]["cy"]) <= tolerance:
            rows[-1]["items"].append(item)
            rows[-1]["cy"] = sum(entry["cy"] for entry in rows[-1]["items"]) / len(
                rows[-1]["items"]
            )
        else:
            rows.append({"cy": item["cy"], "items": [item]})

    lines = []
    for row in rows:
        ordered = sorted(row["items"], key=lambda item: item["x0"])
        lines.append(" ".join(item["text"] for item in ordered))
    return "\n".join(lines)


def _recognize_bytes(data: bytes) -> str:
    """把一张图的文字读出来：保持行序，行内保持从左到右，不做列对齐。"""
    engine = _engine()
    # RapidOCR 收路径最稳（内部按内容解码，不依赖扩展名）；临时文件用完即删。
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        output, _elapsed = engine(path)
    finally:
        Path(path).unlink(missing_ok=True)
    return _group_lines(output)


def _recognize_with_timeout(data: bytes, timeout: float) -> str | None:
    """单图识别限时。超时放弃这张图（线程泄漏在本地单机可接受），不重试。"""
    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["text"] = _recognize_bytes(data)
        except BaseException as exc:  # noqa: BLE001 - 原样带回主线程后只记日志
            box["error"] = exc

    thread = threading.Thread(target=worker, name="ocr-recognize", daemon=True)
    thread.start()
    thread.join(max(0.0, timeout))
    if thread.is_alive():
        logger.info("图片识别超时（%.1fs），跳过这张图", timeout)
        return None
    error = box.get("error")
    if isinstance(error, Exception):
        logger.info("图片识别失败：%s: %s", type(error).__name__, error)
        return None
    text = box.get("text")
    return text if isinstance(text, str) else None


def text_from_url(url: str, app: AppSettings) -> str | None:
    """识别一张图的文字；任何失败都返回 ``None``（不抛、不重试）。

    返回空串表示识别成功但图里没有可用文字——调用方据此让这条证据缺席。
    """
    url = (url or "").strip()
    if not url:
        return None
    cached = _read_cache(_cache_path(app, url))
    if cached is not None:
        return cached

    timeout = max(0.1, float(app.ocr_image_timeout))
    try:
        data = _download(url, max_bytes=int(app.ocr_image_max_bytes), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 下载失败只影响这一张
        logger.info("图片下载失败，跳过 %s：%s", url, exc)
        return None

    text = _recognize_with_timeout(data, timeout)
    if text is None:
        return None
    _write_cache(_cache_path(app, url), url, text)
    return text
