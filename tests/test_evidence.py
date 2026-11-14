"""合并层的去重与上限（W11）。

这些测试压在 ``graph._normalize`` 的三条边界上：

* 同一篇文章被多个通道抓到时只留一条，且留下**效力更强**的那版；
* 没有稳定标识（URL / ref 都空）的条目不合并——硬合并会误删不同档案；
* 证据总数有上限，但补查通道整体豁免，不能被核心通道的长列表挤掉。
"""

from __future__ import annotations

from app.agent import graph


def _item(channel, source_kind, *, url="", ref="", title=""):
    return {
        "channel": channel,
        "source_kind": source_kind,
        "url": url,
        "ref": ref,
        "title": title,
        "text": title or channel,
    }


# --------------------------------------------------------------------- 去重


def test_same_url_deduped_ignoring_fragment_and_trailing_slash():
    items = [
        _item("web", "realtime", url="https://example.com/a#top"),
        _item("web", "realtime", url="https://example.com/a/"),
    ]

    ordered = graph._normalize(items, [])

    assert len(ordered) == 1


def test_duplicate_url_keeps_stronger_source_kind():
    """自有内容与微博网友抓到同一篇时，留下可作规范的那条。"""
    items = [
        _item("real", "upstream_ugc", url="https://example.com/a", title="网友转发"),
        _item("own", "own_content", url="https://example.com/a/", title="自有条目"),
    ]

    ordered = graph._normalize(items, [])

    assert len(ordered) == 1
    assert ordered[0]["source_kind"] == "own_content"
    assert ordered[0]["title"] == "自有条目"


def test_ref_used_when_url_missing():
    items = [
        _item("own", "own_content", ref="kb-1", title="旧"),
        _item("own", "own_content", ref="kb-1", title="新"),
    ]

    ordered = graph._normalize(items, [])

    assert len(ordered) == 1


def test_items_without_key_are_not_merged():
    """Lolibrary 的聚合条目没有 URL/ref，不能因为都为空就并成一条。"""
    items = [
        _item("lolibrary", "third_party", title="条目一"),
        _item("lolibrary", "third_party", title="条目二"),
    ]

    ordered = graph._normalize(items, [])

    assert len(ordered) == 2


# --------------------------------------------------------------------- 上限


def _many(count, channel="own", kind="own_content"):
    return [_item(channel, kind, ref=f"{channel}-{index}") for index in range(count)]


def test_limit_truncates_after_dedup():
    ordered = graph._normalize(_many(20), [], limit=12)

    assert len(ordered) == 12


def test_limit_zero_means_unlimited():
    assert len(graph._normalize(_many(20), [], limit=0)) == 20


def test_supplementary_channels_are_exempt_from_limit():
    """核心通道条目再多，也不能把本次补查的小红书证据挤掉。"""
    core = _many(30, channel="own", kind="own_content")
    extra = [
        _item("xhs", "realtime", ref="xhs-1"),
        _item("xhs", "realtime", ref="xhs-2"),
    ]
    items = [*core, *extra]

    ordered = graph._normalize(items, ["xhs"], limit=12)

    xhs = [item for item in ordered if item["channel"] == "xhs"]
    assert len(xhs) == 2
    # 排在最前，编号位置不暗示「不重要」。
    assert ordered[0]["channel"] == "xhs"


def test_limit_exemption_allows_exceeding_cap_when_only_supplementary():
    """上限比补查条数还小时，补查证据仍然全留（宁愿超上限也不丢）。"""
    extra = [
        _item("xhs", "realtime", ref="xhs-1"),
        _item("xhs", "realtime", ref="xhs-2"),
        _item("xhs", "realtime", ref="xhs-3"),
    ]

    ordered = graph._normalize(extra, ["xhs"], limit=2)

    assert len(ordered) == 3
