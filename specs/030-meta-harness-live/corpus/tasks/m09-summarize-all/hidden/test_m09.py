import pytest

from demo_app.version_report import summarize, summarize_all


def info(version: str, stage: str = "S") -> dict:
    return {"package_version": version, "stage": stage}


def test_numeric_not_textual_ordering() -> None:
    lines = summarize_all([info("1.10.0", "B"), info("1.2.0", "A")])
    assert lines == ["amplai 1.2.0 (A)", "amplai 1.10.0 (B)"]


def test_major_minor_patch_ordering() -> None:
    versions = ["2.0.0", "1.9.9", "1.10.0", "1.9.10", "10.0.0", "0.0.1"]
    lines = summarize_all([info(v) for v in versions])
    assert lines == [summarize(info(v)) for v in ["0.0.1", "1.9.9", "1.9.10", "1.10.0", "2.0.0", "10.0.0"]]


def test_dev_builds_sort_before_release_and_by_number() -> None:
    versions = ["3.0.0", "3.0.0.dev10", "2.9.9", "3.0.0.dev3", "3.0.0.dev9"]
    lines = summarize_all([info(v) for v in versions])
    assert lines == [
        "amplai 2.9.9 (S)",
        "amplai 3.0.0.dev3 (S)",
        "amplai 3.0.0.dev9 (S)",
        "amplai 3.0.0.dev10 (S)",
        "amplai 3.0.0 (S)",
    ]


def test_dev_of_next_version_is_after_previous_release() -> None:
    lines = summarize_all([info("1.1.0.dev1"), info("1.0.5")])
    assert lines == ["amplai 1.0.5 (S)", "amplai 1.1.0.dev1 (S)"]


def test_equal_versions_keep_input_order() -> None:
    lines = summarize_all([info("1.0.0", "first"), info("0.9.0", "old"), info("1.0.0", "second"), info("1.0.0", "third")])
    assert lines == [
        "amplai 0.9.0 (old)",
        "amplai 1.0.0 (first)",
        "amplai 1.0.0 (second)",
        "amplai 1.0.0 (third)",
    ]


def test_leading_zeros_compare_as_integers_and_text_is_kept() -> None:
    lines = summarize_all([info("1.2.0", "plain"), info("1.02.0", "zero"), info("1.1.9", "low")])
    assert lines == ["amplai 1.1.9 (low)", "amplai 1.2.0 (plain)", "amplai 1.02.0 (zero)"]
    lines = summarize_all([info("1.02.0", "zero"), info("1.2.0", "plain")])
    assert lines == ["amplai 1.02.0 (zero)", "amplai 1.2.0 (plain)"]


def test_empty_list_and_input_unchanged() -> None:
    assert summarize_all([]) == []
    data = [info("2.0.0"), info("1.0.0")]
    snapshot = [dict(item) for item in data]
    summarize_all(data)
    assert data == snapshot


@pytest.mark.parametrize(
    "version",
    ["", " 1.0.0", "1.0.0 ", "v1.0.0", "1.0", "1", "1.0.0.1", "1.0.0.dev", "1.0.0.rc1", "1.0.-1", "x.y.z", "1..0", "1.0.0.dev-1", "1.0.0.dev1.dev2"],
)
def test_unparsable_version_raises_even_alone(version: str) -> None:
    with pytest.raises(ValueError):
        summarize_all([info(version)])
    with pytest.raises(ValueError):
        summarize_all([info("1.0.0"), info(version), info("2.0.0")])
