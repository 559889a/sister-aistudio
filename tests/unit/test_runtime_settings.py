"""运行期设置的 schema / 校验 / 持久化用例。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aistudio_api.application import runtime_settings as rs


def _store(tmp_path: Path) -> rs.RuntimeSettings:
    return rs.RuntimeSettings(path=tmp_path / "settings.json")


def test_schema_covers_three_groups_and_core_keys():
    groups = {item["group"] for item in rs.SCHEMA}
    assert {"override", "prefill", "bypass"} <= groups
    keys = {item["key"] for item in rs.SCHEMA}
    for key in (
        "thinkingMode",
        "thinkingLevel",
        "includeThoughts",
        "dropMaxTokens",
        "prefillEnabled",
        "prefillCotGuard",
        "bypassEnabled",
        "sigSentinelThought",
        "sigSentinelTool",
    ):
        assert key in keys


def test_defaults_and_update_are_persisted(tmp_path: Path):
    path = tmp_path / "settings.json"
    store = rs.RuntimeSettings(path=path)
    assert store.get("bypassEnabled") is False
    assert store.get("sigSentinelThought") is True

    values = store.update({"bypassEnabled": True, "thinkingLevel": "high"})
    assert values["bypassEnabled"] is True
    assert values["thinkingLevel"] == "high"

    # 新实例从同一份文件读回来（进程重启后仍然生效）
    again = rs.RuntimeSettings(path=path)
    assert again.get("bypassEnabled") is True
    assert again.get("thinkingLevel") == "high"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["bypassEnabled"] is True


def test_invalid_patch_is_rejected_atomically(tmp_path: Path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.update({"bypassMode": "whatever"})
    with pytest.raises(ValueError):
        store.update({"notAKey": 1})
    # 非法 patch 不落地：一个键都不该被写进去
    assert store.get("bypassEnabled") is False


def test_numbers_are_clamped_to_schema_range(tmp_path: Path):
    store = _store(tmp_path)
    values = store.update({"topK": 999, "temperature": -5})
    assert values["topK"] == 63
    assert values["temperature"] == 0.0
    # 清空 = 未设置
    cleared = store.update({"maxOutputTokens": None})
    assert cleared["maxOutputTokens"] is None


def test_text_is_truncated_to_max_length(tmp_path: Path):
    store = _store(tmp_path)
    item = rs.SCHEMA_BY_KEY["prefillNudge"]
    too_long = "九" * (int(item["max_length"]) + 10)
    values = store.update({"prefillNudge": too_long})
    assert len(values["prefillNudge"]) == int(item["max_length"])
