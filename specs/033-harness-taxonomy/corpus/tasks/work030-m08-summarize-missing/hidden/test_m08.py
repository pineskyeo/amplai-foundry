import pytest

from demo_app.version_report import InvalidInfo, summarize


def test_complete_input_unchanged() -> None:
    assert summarize({"package_version": "3.0.0.dev3", "stage": "DEV-03"}) == "amplai 3.0.0.dev3 (DEV-03)"
    assert summarize({"package_version": "1.0.0", "stage": "GA", "extra": 1}) == "amplai 1.0.0 (GA)"


def test_missing_stage_is_unknown() -> None:
    assert summarize({"package_version": "1.2.3"}) == "amplai 1.2.3 (unknown stage)"


def test_empty_stage_is_unknown() -> None:
    assert summarize({"package_version": "1.2.3", "stage": ""}) == "amplai 1.2.3 (unknown stage)"


def test_version_text_is_used_verbatim() -> None:
    assert summarize({"package_version": " v2 ", "stage": "X"}) == "amplai  v2  (X)"


def test_invalid_info_is_a_value_error() -> None:
    assert issubclass(InvalidInfo, ValueError)


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"stage": "DEV-03"},
        {"package_version": "", "stage": "DEV-03"},
        {"package_version": "", "stage": ""},
        {"package_version": "", "stage": "DEV-03"},
        {"package_version": 3, "stage": "DEV-03"},
        {"package_version": None, "stage": "DEV-03"},
        {"package_version": None},
        {"package_version": ["1.0.0"], "stage": "X"},
    ],
)
def test_bad_package_version_raises_invalid_info(info: dict) -> None:
    with pytest.raises(InvalidInfo):
        summarize(info)
    with pytest.raises(ValueError):
        summarize(info)
