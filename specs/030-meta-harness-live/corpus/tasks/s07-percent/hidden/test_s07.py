import pytest

from demo_app.formatting import percent


def test_simple_fractions() -> None:
    assert percent(1, 4) == "25.0%"
    assert percent(1, 8) == "12.5%"
    assert percent(1, 2) == "50.0%"


def test_repeating_fractions_round_to_one_decimal() -> None:
    assert percent(1, 3) == "33.3%"
    assert percent(2, 3) == "66.7%"


def test_zero_and_full() -> None:
    assert percent(0, 5) == "0.0%"
    assert percent(5, 5) == "100.0%"


def test_values_above_hundred() -> None:
    assert percent(3, 2) == "150.0%"
    assert percent(2000, 1) == "200000.0%"


def test_small_values_keep_one_decimal() -> None:
    assert percent(1, 1000) == "0.1%"
    assert percent(1, 10000) == "0.0%"


@pytest.mark.parametrize("part", [0, 5, 100])
def test_zero_total_returns_na(part: int) -> None:
    assert percent(part, 0) == "n/a"


@pytest.mark.parametrize("part,total", [(-1, 5), (1, -5), (-1, -1), (-1, 0), (0, -1)])
def test_negative_input_raises(part: int, total: int) -> None:
    with pytest.raises(ValueError):
        percent(part, total)
