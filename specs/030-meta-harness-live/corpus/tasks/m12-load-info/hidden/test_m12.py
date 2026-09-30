import json

import pytest

from demo_app.loader import InfoError, load_info


def test_valid_info_is_returned() -> None:
    text = '{"package_version": "3.0.0.dev3", "stage": "DEV-03"}'
    assert load_info(text) == {"package_version": "3.0.0.dev3", "stage": "DEV-03"}


def test_empty_stage_and_blank_version_string_are_accepted() -> None:
    assert load_info('{"package_version": "1.0.0", "stage": ""}') == {"package_version": "1.0.0", "stage": ""}
    assert load_info('{"package_version": " ", "stage": "x"}') == {"package_version": " ", "stage": "x"}


def test_extra_keys_are_kept() -> None:
    data = {"package_version": "1.0.0", "stage": "S", "n": 1, "tags": [1, 2], "nested": {"a": None}}
    assert load_info(json.dumps(data)) == data


def test_info_error_is_a_value_error() -> None:
    assert issubclass(InfoError, ValueError)


BAD_TEXTS = [
    "",
    "{",
    "not json",
    '{"package_version": "1.0.0", "stage": "S"',
    "[]",
    '"x"',
    "3",
    "true",
    "null",
    '[{"package_version": "1.0.0", "stage": "S"}]',
    "{}",
    '{"stage": "S"}',
    '{"package_version": "1.0.0"}',
    '{"package_version": "", "stage": "S"}',
    '{"package_version": 3, "stage": "S"}',
    '{"package_version": null, "stage": "S"}',
    '{"package_version": true, "stage": "S"}',
    '{"package_version": ["1.0.0"], "stage": "S"}',
    '{"package_version": {"v": "1"}, "stage": "S"}',
    '{"package_version": "1.0.0", "stage": 3}',
    '{"package_version": "1.0.0", "stage": null}',
    '{"package_version": "1.0.0", "stage": false}',
    '{"package_version": "1.0.0", "stage": []}',
    '{"package_version": "1.0.0", "stage": {}}',
]


@pytest.mark.parametrize("text", BAD_TEXTS)
def test_failures_raise_info_error_with_prefix(text: str) -> None:
    with pytest.raises(InfoError) as excinfo:
        load_info(text)
    assert str(excinfo.value).startswith("invalid info:")
