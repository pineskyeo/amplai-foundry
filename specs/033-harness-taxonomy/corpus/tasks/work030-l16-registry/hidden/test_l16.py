from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo_app.registry import Registry, RegistryError


def disk(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def expected(data: dict) -> str:
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def test_add_versions_latest_names(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "reg.json")
    reg.add("beta", "0.1.0")
    reg.add("alpha", "1.10.0")
    reg.add("alpha", "1.9.0")
    reg.add("alpha", "1.2.10")
    reg.add("alpha", "1.2.9")
    assert reg.versions("alpha") == ["1.2.9", "1.2.10", "1.9.0", "1.10.0"]
    assert reg.latest("alpha") == "1.10.0"
    assert reg.versions("beta") == ["0.1.0"]
    assert reg.latest("beta") == "0.1.0"
    assert reg.names() == ["alpha", "beta"]


def test_unknown_names(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "reg.json")
    assert reg.versions("nope") == []
    assert reg.latest("nope") is None
    assert reg.names() == []


def test_names_use_default_string_order(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "reg.json")
    for name in ["b", "B", "a", "", "aa", "10", "9"]:
        reg.add(name, "1.0.0")
    assert reg.names() == sorted(["b", "B", "a", "", "aa", "10", "9"])


def test_versions_returns_a_fresh_list(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "reg.json")
    reg.add("a", "1.0.0")
    got = reg.versions("a")
    got.append("9.9.9")
    got.clear()
    assert reg.versions("a") == ["1.0.0"]
    names = reg.names()
    names.clear()
    assert reg.names() == ["a"]


@pytest.mark.parametrize(
    "bad",
    ["1.2", "1.2.3.4", "v1.2.3", "1.02.3", "01.2.3", "1.2.3\n", " 1.2.3", "1.2.3 ", "-1.2.3",
     "+1.2.3", "1.2.x", "", "1..3", "a.b.c", "1.2.3-rc1", "١.2.3"],
)
def test_invalid_versions_raise_value_error(tmp_path: Path, bad: str) -> None:
    path = tmp_path / "reg.json"
    reg = Registry(path)
    with pytest.raises(ValueError):
        reg.add("pkg", bad)
    assert reg.versions("pkg") == []
    assert reg.names() == []
    assert not path.exists()


def test_zero_parts_are_valid(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "reg.json")
    reg.add("z", "0.0.0")
    reg.add("z", "10.20.30")
    assert reg.versions("z") == ["0.0.0", "10.20.30"]


def test_duplicate_raises_but_other_name_is_fine(tmp_path: Path) -> None:
    path = tmp_path / "reg.json"
    reg = Registry(path)
    reg.add("a", "1.0.0")
    before = disk(path)
    with pytest.raises(ValueError):
        reg.add("a", "1.0.0")
    assert disk(path) == before
    assert reg.versions("a") == ["1.0.0"]
    reg.add("b", "1.0.0")
    assert reg.names() == ["a", "b"]


def test_remove(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "reg.json")
    reg.add("a", "1.0.0")
    reg.add("a", "2.0.0")
    reg.remove("a", "2.0.0")
    assert reg.versions("a") == ["1.0.0"]
    assert reg.latest("a") == "1.0.0"
    reg.remove("a", "1.0.0")
    assert reg.versions("a") == []
    assert reg.latest("a") is None
    assert reg.names() == []


def test_remove_missing_raises_key_error_and_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "reg.json"
    reg = Registry(path)
    with pytest.raises(KeyError):
        reg.remove("a", "1.0.0")
    assert not path.exists()
    reg.add("a", "1.0.0")
    before = disk(path)
    with pytest.raises(KeyError):
        reg.remove("a", "2.0.0")
    with pytest.raises(KeyError):
        reg.remove("b", "1.0.0")
    assert disk(path) == before
    assert reg.versions("a") == ["1.0.0"]


def test_constructor_does_not_create_file(tmp_path: Path) -> None:
    path = tmp_path / "reg.json"
    Registry(path)
    assert not path.exists()


def test_every_mutation_is_written_exactly(tmp_path: Path) -> None:
    path = tmp_path / "reg.json"
    reg = Registry(path)
    reg.add("beta", "0.1.0")
    assert disk(path) == expected({"beta": ["0.1.0"]})
    reg.add("alpha", "1.10.0")
    reg.add("alpha", "1.2.0")
    assert disk(path) == expected({"alpha": ["1.2.0", "1.10.0"], "beta": ["0.1.0"]})
    assert disk(path) == (
        '{\n  "alpha": [\n    "1.2.0",\n    "1.10.0"\n  ],\n  "beta": [\n    "0.1.0"\n  ]\n}\n'
    )
    reg.remove("alpha", "1.2.0")
    assert json.loads(disk(path)) == {"alpha": ["1.10.0"], "beta": ["0.1.0"]}
    reg.remove("alpha", "1.10.0")
    assert disk(path) == expected({"beta": ["0.1.0"]})
    reg.remove("beta", "0.1.0")
    assert disk(path) == "{}\n"


def test_non_ascii_names_are_written_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "reg.json"
    reg = Registry(path)
    reg.add("café", "1.0.0")
    raw = path.read_bytes()
    assert "café".encode("utf-8") in raw
    assert b"\\u" not in raw
    assert raw.decode("utf-8") == expected({"café": ["1.0.0"]})
    assert Registry(path).versions("café") == ["1.0.0"]


def test_reload_sees_what_was_written(tmp_path: Path) -> None:
    path = tmp_path / "reg.json"
    first = Registry(path)
    first.add("a", "1.9.0")
    first.add("a", "1.10.0")
    first.add("b", "0.0.1")
    second = Registry(str(path))
    assert second.names() == ["a", "b"]
    assert second.versions("a") == ["1.9.0", "1.10.0"]
    assert second.latest("b") == "0.0.1"
    second.add("a", "2.0.0")
    third = Registry(path)
    assert third.versions("a") == ["1.9.0", "1.10.0", "2.0.0"]


def test_loading_hand_written_file_and_normalizing_on_mutation(tmp_path: Path) -> None:
    path = tmp_path / "reg.json"
    path.write_text('{"b":["1.0.0"],"a":["1.2.0","1.10.0"]}', encoding="utf-8")
    reg = Registry(path)
    assert reg.names() == ["a", "b"]
    assert reg.versions("a") == ["1.2.0", "1.10.0"]
    assert path.read_text(encoding="utf-8") == '{"b":["1.0.0"],"a":["1.2.0","1.10.0"]}'
    reg.add("c", "3.0.0")
    assert disk(path) == expected(
        {"a": ["1.2.0", "1.10.0"], "b": ["1.0.0"], "c": ["3.0.0"]}
    )


def test_empty_object_file_loads(tmp_path: Path) -> None:
    path = tmp_path / "reg.json"
    path.write_text("{}", encoding="utf-8")
    assert Registry(path).names() == []


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"   \n",
        b"{not json",
        b"[]",
        b'["a"]',
        b"42",
        b"null",
        b'"text"',
        b'{"a": "1.0.0"}',
        b'{"a": {"1.0.0": 1}}',
        b'{"a": null}',
        b'{"a": []}',
        b'{"a": [1]}',
        b'{"a": [null]}',
        b'{"a": ["1.2"]}',
        b'{"a": ["v1.0.0"]}',
        b'{"a": ["1.02.0"]}',
        b'{"a": ["1.10.0", "1.9.0"]}',
        b'{"a": ["1.0.0", "1.0.0"]}',
        b'{"a": ["1.0.0"], "b": []}',
        b"\xff\xfe\x00{}",
        b'{"a": ["\xff"]}',
    ],
)
def test_malformed_files_raise_registry_error(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "reg.json"
    path.write_bytes(content)
    with pytest.raises(RegistryError):
        Registry(path)
    assert path.read_bytes() == content


def test_registry_error_is_a_value_error() -> None:
    assert issubclass(RegistryError, ValueError)
