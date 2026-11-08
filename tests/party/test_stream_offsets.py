import json

from nyoanime.party.stream_offsets import (
    MAX_OFFSET_SECONDS,
    clamp_offset,
    load_stream_offset,
    offset_key,
    save_stream_offset,
    stream_offsets_path,
)


def test_offsets_are_keyed_by_canonical_provider_and_language(tmp_path):
    path = tmp_path / "stream_offsets.json"
    save_stream_offset("hianime_dub", "en_dub", 5.0, path=path)
    save_stream_offset("hianime_dub", "en_sub", -2.0, path=path)

    assert load_stream_offset("hianime_dub", "en_dub", path=path) == 5.0
    assert load_stream_offset("hianime_dub", "en_sub", path=path) == -2.0
    assert load_stream_offset("hianime", "en_dub", path=path) == 0.0
    assert load_stream_offset("stardima", "ar_dub", path=path) == 0.0

    assert offset_key("hianime_sub", "en-sub") == "hianime:en_sub"


def test_saving_zero_removes_the_stored_offset(tmp_path):
    path = tmp_path / "stream_offsets.json"
    save_stream_offset("hianime_dub", "en_dub", 4.5, path=path)
    save_stream_offset("hianime_dub", "en_dub", 0, path=path)

    assert load_stream_offset("hianime_dub", "en_dub", path=path) == 0.0
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["offsets"] == {}


def test_offsets_are_clamped_and_rounded(tmp_path):
    path = tmp_path / "stream_offsets.json"
    assert clamp_offset(float("nan")) == 0.0
    assert clamp_offset("nope") == 0.0
    assert save_stream_offset("hianime_dub", "en_dub", 999, path=path) == (
        MAX_OFFSET_SECONDS
    )
    assert save_stream_offset("hianime_dub", "en_dub", 2.345, path=path) == 2.35


def test_a_corrupt_store_reads_as_zero_and_is_replaced(tmp_path):
    path = tmp_path / "stream_offsets.json"
    path.write_text("{not json", encoding="utf-8")

    assert load_stream_offset("hianime_dub", "en_dub", path=path) == 0.0

    save_stream_offset("hianime_dub", "en_dub", 3.0, path=path)
    assert load_stream_offset("hianime_dub", "en_dub", path=path) == 3.0


def test_default_store_lives_outside_the_repository(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert stream_offsets_path() == (
        tmp_path / ".nyoanime" / "database" / "stream_offsets.json"
    )
