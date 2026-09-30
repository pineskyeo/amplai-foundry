import pytest

from demo_app.version_report import latest_per_stage


def info(stage: str, version: str, **extra: object) -> dict:
    return {"stage": stage, "package_version": version, **extra}


def test_numeric_ordering_picks_highest() -> None:
    result = latest_per_stage([info("A", "1.2.0"), info("A", "1.10.0"), info("A", "1.9.9")])
    assert result == {"A": info("A", "1.10.0")}


def test_one_entry_per_stage() -> None:
    result = latest_per_stage(
        [info("A", "1.0.0"), info("B", "2.0.0"), info("A", "1.1.0"), info("B", "1.5.0")]
    )
    assert result == {"A": info("A", "1.1.0"), "B": info("B", "2.0.0")}


def test_dev_builds_are_lower_than_release() -> None:
    result = latest_per_stage([info("S", "3.0.0"), info("S", "3.0.0.dev10")])
    assert result["S"]["package_version"] == "3.0.0"
    result = latest_per_stage([info("S", "3.0.0.dev9"), info("S", "3.0.0.dev10")])
    assert result["S"]["package_version"] == "3.0.0.dev10"
    result = latest_per_stage([info("S", "3.0.1.dev1"), info("S", "3.0.0")])
    assert result["S"]["package_version"] == "3.0.1.dev1"
    result = latest_per_stage([info("S", "3.0.0.dev10"), info("S", "3.0.0.dev9"), info("S", "2.9.9")])
    assert result["S"]["package_version"] == "3.0.0.dev10"


def test_tie_goes_to_later_item_and_values_are_original_objects() -> None:
    first = info("S", "1.2.0", tag="first")
    second = info("S", "1.02.0", tag="second")
    third = info("S", "1.2.0", tag="third")
    assert latest_per_stage([first, second])["S"] is second
    assert latest_per_stage([second, first])["S"] is first
    assert latest_per_stage([first, second, third])["S"] is third
    older = info("S", "1.0.0", tag="older")
    assert latest_per_stage([first, older])["S"] is first


def test_infos_without_stage_are_skipped() -> None:
    result = latest_per_stage(
        [{"package_version": "9.9.9"}, info("A", "1.0.0"), {"package_version": "not a version"}]
    )
    assert result == {"A": info("A", "1.0.0")}
    assert latest_per_stage([{"package_version": "1.0.0"}]) == {}


def test_empty_stage_is_a_normal_key() -> None:
    result = latest_per_stage([info("", "1.0.0"), info("A", "1.0.0"), info("", "2.0.0")])
    assert result == {"": info("", "2.0.0"), "A": info("A", "1.0.0")}
    assert list(result) == ["", "A"]


def test_keys_ordered_by_stage_name() -> None:
    result = latest_per_stage([info("DEV-10", "1.0.0"), info("DEV-02", "1.0.0"), info("a", "1.0.0"), info("Z", "1.0.0")])
    assert list(result) == ["DEV-02", "DEV-10", "Z", "a"]
    result = latest_per_stage([info("b", "1.0.0"), info("a", "1.0.0"), info("b", "2.0.0")])
    assert list(result) == ["a", "b"]


def test_empty_list() -> None:
    assert latest_per_stage([]) == {}


@pytest.mark.parametrize("version", ["", "v1.0.0", "1.0", "1.0.0.dev", "1.0.0 ", "1.0.0.1", "x.y.z", "1.0.0.rc1"])
def test_unparsable_version_raises(version: str) -> None:
    with pytest.raises(ValueError):
        latest_per_stage([info("A", version)])
    with pytest.raises(ValueError):
        latest_per_stage([info("A", "1.0.0"), info("B", version)])
