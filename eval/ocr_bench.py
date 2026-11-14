"""OCR 准确率实测：从真实召回的小红书笔记取图，跑本地 rapidocr，落盘结果。

只读调用：不修改 app/，不写业务数据；图片与结果都放 eval/forensics/（gitignore）。
用法：
    python eval/ocr_bench.py download
    python eval/ocr_bench.py run
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.services import imagefetch, xhs_spider  # noqa: E402

OUT = ROOT / "eval" / "forensics" / "ocr-corpus"
RESULTS = ROOT / "eval" / "forensics" / "ocr-results.json"

# 三条真实召回笔记：价格表、多图存档（尺码/参数/公告混杂）、实拍出物帖。
NOTES = {
    "p1-存档价格表": (
        "https://www.xiaohongshu.com/explore/6ad4eae10000000029015800"
        "?xsec_token=ABeX6tEqPjdnmPbS4PoyjB_oPMoe9nqBGt-TzYDoq3y7U=&xsec_source=pc_search",
        4,
    ),
    "p2-存一下心头好": (
        "https://www.xiaohongshu.com/explore/6ad271ef0000000018013898"
        "?xsec_token=ABqYy-J8K-BbpAI-F1VWrhoN88SP06EMCFc30AYRs4m0c=&xsec_source=pc_search",
        10,
    ),
    "p3-收m码实拍": (
        "https://www.xiaohongshu.com/explore/6af2ff96000000002501ebee"
        "?xsec_token=ABD8qm5-Rd7VaMdyuIgsS8ISEMQ1cRannS-9UA97kgE8Q=&xsec_source=pc_search",
        4,
    ),
}


def _api():
    XHS_Apis, XHSPcAuth = xhs_spider._load_api(settings)
    return XHS_Apis(XHSPcAuth.from_cookie(str(settings.xhs_cookie).strip()))


def download() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    api = _api()
    manifest = []
    for label, (url, limit) in NOTES.items():
        ok, msg, payload = api.get_note_info(url)
        if not ok or not isinstance(payload, dict):
            print(f"{label}: 取详情失败 {msg}")
            continue
        items = (payload.get("data") or {}).get("items") or []
        card = (items[0] or {}).get("note_card") or {}
        images = card.get("image_list") or []
        print(f"{label}: {len(images)} 张，取前 {min(limit, len(images))} 张")
        for index, image in enumerate(images[:limit], start=1):
            info = image.get("info_list") or []
            urls = [entry.get("url") for entry in info if isinstance(entry, dict)]
            if not urls:
                continue
            target = OUT / f"{label}-{index:02d}.webp"
            try:
                request = Request(urls[0], headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(request, timeout=20, context=imagefetch.ssl_context()) as resp:
                    target.write_bytes(resp.read())
                manifest.append(
                    {
                        "file": str(target.relative_to(ROOT)),
                        "label": label,
                        "index": index,
                        "bytes": target.stat().st_size,
                        "title": card.get("title"),
                        "desc_len": len(str(card.get("desc") or "")),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 诊断脚本
                print(f"  图 {index} 下载失败：{type(exc).__name__} {exc}")
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"共 {len(manifest)} 张 -> {OUT}")
    return 0


def _group_rows(items: list[tuple[list, str, float]], tolerance: float) -> list[list[str]]:
    """按 y 中心聚类成行，行内按 x 排序；用于检查「哪一行对应哪个 SKU」。"""
    rows: list[dict] = []
    for box, text, _score in items:
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        center = (min(ys) + max(ys)) / 2
        height = max(ys) - min(ys)
        placed = False
        for row in rows:
            if abs(row["center"] - center) <= max(tolerance, height * 0.5):
                row["items"].append((min(xs), text))
                row["center"] = mean([row["center"], center])
                placed = True
                break
        if not placed:
            rows.append({"center": center, "items": [(min(xs), text)]})
    rows.sort(key=lambda row: row["center"])
    return [[text for _x, text in sorted(row["items"], key=lambda pair: pair[0])] for row in rows]


def run() -> int:
    from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415 - 可选依赖

    engine = RapidOCR()
    files = sorted(OUT.glob("*.webp"))
    if not files:
        print("没有图片，先跑 download")
        return 1

    results = []
    for path in files:
        started = time.perf_counter()
        output, _elapse = engine(str(path))
        elapsed = time.perf_counter() - started
        items = [
            (box, text, float(score))
            for box, text, score in (output or [])
        ]
        heights = [max(p[1] for p in box) - min(p[1] for p in box) for box, _t, _s in items]
        tolerance = mean(heights) * 0.6 if heights else 10
        results.append(
            {
                "file": str(path.relative_to(ROOT)),
                "seconds": round(elapsed, 2),
                "lines": len(items),
                "mean_score": round(mean([score for _b, _t, score in items]), 3) if items else 0,
                "raw_lines": [text for _b, text, _s in items],
                "rows": _group_rows(items, tolerance),
            }
        )
        print(f"{path.name}: {elapsed:.2f}s, {len(items)} 行, 平均置信度 {results[-1]['mean_score']}")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "engine": "rapidocr_onnxruntime",
        "images": results,
    }
    RESULTS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 逐图文本（按行聚类）===")
    for item in results:
        print("=" * 70)
        print(item["file"], f"{item['seconds']}s", f"lines={item['lines']}")
        for row in item["rows"]:
            print("  | " + " | ".join(row))
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    raise SystemExit(download() if command == "download" else run())
