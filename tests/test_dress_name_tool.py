"""「解析裙名」工具的轨迹：通过 / 未通过 / 未触发三种页面状态。"""

from __future__ import annotations

from app.agent import dress_name_tool


def _query(**overrides) -> dict:
    data = {
        "brand": "Baby, the Stars Shine Bright",
        "series_en": "Sweet Best Wishes JSK",
        "series_cn": "甜心美好祝愿",
        "verified": True,
        "source": "series_alias",
        "name_source": "local",
    }
    data.update(overrides)
    return data


def test_named_tool_constants():
    assert dress_name_tool.TOOL_NAME == "resolve_dress_name"
    assert dress_name_tool.LABEL == "解析裙名"
    assert dress_name_tool.STEP_KIND == "dress_name"


def test_passed_trace_from_local_query():
    trace = dress_name_tool.trace_from_query(_query())
    assert trace is not None
    assert trace["kind"] == "dress_name"
    assert trace["triggered"] is True
    assert trace["result"] == "passed"
    assert trace["candidates"] == [
        {
            "name": "Sweet Best Wishes JSK",
            "brand": "Baby, the Stars Shine Bright",
            "verified": True,
        }
    ]


def test_failed_trace_keeps_candidate_unverified():
    trace = dress_name_tool.trace_from_query(_query(verified=False))
    assert trace is not None
    assert trace["result"] == "failed"
    assert trace["candidates"][0]["verified"] is False


def test_no_series_means_no_row():
    assert dress_name_tool.trace_from_query(None) is None
    assert (
        dress_name_tool.trace_from_query(
            _query(series_en="", series_cn="", brand="Baby")
        )
        is None
    )


def test_rejected_candidates_are_listed():
    trace = dress_name_tool.build_trace(
        candidate="Phantom Ribbon Dream",
        brand="Baby, the Stars Shine Bright",
        verified=False,
        rejected=["Another Phantom"],
        source="model",
        name_source="model",
    )
    assert trace["result"] == "failed"
    assert [row["name"] for row in trace["candidates"]] == [
        "Phantom Ribbon Dream",
        "Another Phantom",
    ]
    assert all(row["verified"] is False for row in trace["candidates"])


def test_merge_hint_without_series_has_no_trace(workdir):
    import json

    from app.agent.channels import RecallContext
    from app.config import AppSettings

    app = AppSettings(data_dir=workdir / "data", content_dir=workdir / "content")
    app.ensure_dirs()
    (app.content_dir / "dress").mkdir(parents=True, exist_ok=True)
    app.brands_path.write_text(
        json.dumps(
            {
                "brands": [
                    {
                        "id": "baby",
                        "name": "Baby, the Stars Shine Bright",
                        "region": "JP",
                        "aliases": ["Baby"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app.dress_aliases_path.write_text('{"aliases": []}', encoding="utf-8")

    ctx = RecallContext(settings=app)
    pending = dress_name_tool.dress.resolve_dress("Baby 的裙子多少钱", ctx)
    assert pending is not None
    merged, trace = dress_name_tool.merge_hint(
        pending.to_dict(),
        {"brand": "Baby", "region": "JP", "series": "", "confidence": "high"},
        ctx,
    )
    assert merged["brand"] == "Baby, the Stars Shine Bright"
    assert trace is None
