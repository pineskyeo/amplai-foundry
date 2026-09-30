import pytest

from demo_app.formatting import human_bytes


def test_bytes_have_no_decimals() -> None:
    assert human_bytes(0) == "0 B"
    assert human_bytes(1) == "1 B"
    assert human_bytes(512) == "512 B"
    assert human_bytes(1023) == "1023 B"


def test_kibibytes() -> None:
    assert human_bytes(1024) == "1.0 KiB"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(1100) == "1.1 KiB"
    assert human_bytes(1025) == "1.0 KiB"


def test_mebibytes_and_gibibytes() -> None:
    assert human_bytes(1048576) == "1.0 MiB"
    assert human_bytes(10 * 1024**2 + 512 * 1024) == "10.5 MiB"
    assert human_bytes(1024**3) == "1.0 GiB"
    assert human_bytes(3 * 1024**3) == "3.0 GiB"


def test_tebibytes_are_the_largest_unit() -> None:
    assert human_bytes(1024**4) == "1.0 TiB"
    assert human_bytes(1024**5) == "1024.0 TiB"


def test_unit_is_chosen_before_rounding() -> None:
    assert human_bytes(1048575) == "1024.0 KiB"


def test_returns_str() -> None:
    assert isinstance(human_bytes(2048), str)


@pytest.mark.parametrize("n", [-1, -1024, -(1024**3)])
def test_negative_raises(n: int) -> None:
    with pytest.raises(ValueError):
        human_bytes(n)
