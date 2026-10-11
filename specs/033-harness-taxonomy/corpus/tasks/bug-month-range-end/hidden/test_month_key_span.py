import random
from datetime import date

from stockroom.dates import months_between


def test_ranges_that_end_in_the_last_month_of_the_calendar() -> None:
    assert months_between(date(9999, 11, 5), date(9999, 12, 31)) == ["9999-11", "9999-12"]
    assert months_between(date(9999, 12, 1), date(9999, 12, 31)) == ["9999-12"]
    assert months_between(date(9999, 12, 31), date(9999, 12, 31)) == ["9999-12"]
    assert months_between(date(9999, 12, 17), date(9999, 12, 18)) == ["9999-12"]
    assert months_between(date(9998, 12, 15), date(9999, 1, 1)) == ["9998-12", "9999-01"]
    assert months_between(date(9999, 1, 31), date(9999, 12, 1))[-3:] == [
        "9999-10",
        "9999-11",
        "9999-12",
    ]


def test_the_whole_calendar_is_one_key_per_month() -> None:
    keys = months_between(date.min, date.max)
    assert len(keys) == 9999 * 12
    assert keys[:3] == ["0001-01", "0001-02", "0001-03"]
    assert keys[-2:] == ["9999-11", "9999-12"]
    assert keys == sorted(set(keys))
    assert months_between(date(1, 1, 1), date(1, 1, 1)) == ["0001-01"]
    assert months_between(date(999, 12, 31), date(1000, 1, 1)) == ["0999-12", "1000-01"]


def model(start: date, end: date) -> list[str]:
    if end < start:
        return []
    first = start.year * 12 + start.month - 1
    last = end.year * 12 + end.month - 1
    return [f"{n // 12:04d}-{n % 12 + 1:02d}" for n in range(first, last + 1)]


def test_generated_ranges_match_the_month_count() -> None:
    rng = random.Random(9999)
    edge = date.max.toordinal()

    def clamp(n: int) -> int:
        return min(max(n, 1), edge)

    for _ in range(3000):
        kind = rng.randrange(3)
        if kind == 0:
            first, last = rng.randint(1, edge), rng.randint(1, edge)
        elif kind == 1:
            last = edge - rng.randint(0, 800)
            first = clamp(last - rng.randint(-40, 900))
        else:
            first = 1 + rng.randint(0, 800)
            last = clamp(first + rng.randint(-40, 900))
        start, end = date.fromordinal(first), date.fromordinal(last)
        assert months_between(start, end) == model(start, end), (start, end)


def test_empty_and_ordinary_ranges_are_unchanged() -> None:
    assert months_between(date(2024, 3, 20), date(2024, 3, 5)) == []
    assert months_between(date(2024, 2, 1), date(2024, 1, 31)) == []
    assert months_between(date(9999, 12, 31), date(9999, 12, 30)) == []
    assert months_between(date(2023, 11, 20), date(2024, 2, 1)) == [
        "2023-11",
        "2023-12",
        "2024-01",
        "2024-02",
    ]
    assert months_between(date(2024, 1, 31), date(2024, 3, 1)) == ["2024-01", "2024-02", "2024-03"]
