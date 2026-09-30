import pytest

from demo_app.stages import stage_rank


def test_dev_stages() -> None:
    assert stage_rank("DEV-01") == 1
    assert stage_rank("DEV-07") == 7
    assert stage_rank("DEV-10") == 10
    assert stage_rank("DEV-99") == 99


def test_rc_stages() -> None:
    assert stage_rank("RC-1") == 101
    assert stage_rank("RC-3") == 103
    assert stage_rank("RC-10") == 110
    assert stage_rank("RC-99") == 199


def test_final_stage() -> None:
    assert stage_rank("FINAL") == 1000


def test_stages_are_ordered() -> None:
    assert stage_rank("DEV-99") < stage_rank("RC-1")
    assert stage_rank("RC-99") < stage_rank("FINAL")
    assert stage_rank("DEV-02") > stage_rank("DEV-01")


@pytest.mark.parametrize("bad", ["DEV-00", "DEV-1", "DEV-100", "DEV-123", "DEV-", "DEV-AB"])
def test_bad_dev_numbers_raise(bad: str) -> None:
    with pytest.raises(ValueError):
        stage_rank(bad)


@pytest.mark.parametrize("bad", ["RC-0", "RC-01", "RC-100", "RC-", "RC", "RC-A"])
def test_bad_rc_numbers_raise(bad: str) -> None:
    with pytest.raises(ValueError):
        stage_rank(bad)


@pytest.mark.parametrize(
    "bad",
    ["", "dev-01", "Final", "final", " FINAL", "FINAL ", "DEV-01 ", "DEV-01\n", "FINAL\n", "DEV", "BETA-1"],
)
def test_case_whitespace_and_unknown_raise(bad: str) -> None:
    with pytest.raises(ValueError):
        stage_rank(bad)


def test_non_ascii_digits_raise() -> None:
    with pytest.raises(ValueError):
        stage_rank("DEV-０１")
    with pytest.raises(ValueError):
        stage_rank("RC-１")
