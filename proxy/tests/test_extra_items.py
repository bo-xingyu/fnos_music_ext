"""扩展音源条目映射测试。"""
import pytest

from proxy import extra_items


def test_extra_source_names():
    assert "qq" in extra_items.EXTRA_SOURCE_NAMES
    assert "kugou" in extra_items.EXTRA_SOURCE_NAMES
    assert "kuwo" in extra_items.EXTRA_SOURCE_NAMES
    assert "qishui" in extra_items.EXTRA_SOURCE_NAMES


def test_is_extra_source():
    assert extra_items.is_extra_source("qq")
    assert extra_items.is_extra_source("KUGOU")
    assert not extra_items.is_extra_source("netease")
    assert not extra_items.is_extra_source("")
    assert not extra_items.is_extra_source(None)


def test_map_extra_song_basic():
    item = extra_items.map_extra_song({
        "id": "003aAYrm3GE0Ac",
        "source": "qq",
        "title": "晴天",
        "artist": "周杰伦",
        "album": "叶惠美",
        "duration_s": 269,
        "ext": "mp3",
        "cover_url": "https://img.test/c.jpg",
        "playable": True,
    })
    assert item["id"] == "qq:003aAYrm3GE0Ac"
    assert item["source"] == "qq"
    assert item["title"] == "晴天"
    assert item["duration_s"] == 269.0
    assert item["ext"] == "mp3"


def test_map_extra_song_rejects_wrong_source():
    assert extra_items.map_extra_song({"id": "1", "source": "netease", "title": "x"}) is None
    assert extra_items.map_extra_song({"id": "1", "source": "qq", "title": ""}) is None
    assert extra_items.map_extra_song({"id": "", "source": "qq", "title": "x"}) is None
    assert extra_items.map_extra_song({
        "id": "1", "source": "kugou", "title": "x", "playable": False,
    }) is None


def test_map_extra_song_lossless_quality():
    item = extra_items.map_extra_song({
        "id": "HASH123",
        "source": "kugou",
        "title": "t",
        "artist": "a",
        "quality": "SQ 无损",
        "ext": "",
    })
    assert item["ext"] == "flac"


def test_map_extra_song_duration_ms_normalize():
    item = extra_items.map_extra_song({
        "id": "123",
        "source": "kuwo",
        "title": "t",
        "artist": "a",
        "duration_s": 269000,
    })
    assert item["duration_s"] == 269.0


def test_map_extra_search_payload():
    items = extra_items.map_extra_search_payload({
        "ok": True,
        "data": [
            {"id": "a1", "source": "qq", "title": "Song1", "artist": "A"},
            {"id": "b2", "source": "kugou", "title": "Song2", "artist": "B"},
            {"id": "c3", "source": "netease", "title": "Skip", "artist": "C"},
        ],
    })
    assert len(items) == 2
    assert items[0]["id"] == "qq:a1"
    assert items[1]["id"] == "kugou:b2"


def test_online_guid_roundtrip():
    from proxy.app import online_guid_from_item, source_from_online_guid, song_id_from_online_guid
    item = {"id": "qq:003ABC", "source": "qq", "title": "t"}
    guid = online_guid_from_item(item)
    assert guid == "online:qq:003ABC"
    assert source_from_online_guid(guid) == "qq"
    assert song_id_from_online_guid(guid) == "qq:003ABC"
