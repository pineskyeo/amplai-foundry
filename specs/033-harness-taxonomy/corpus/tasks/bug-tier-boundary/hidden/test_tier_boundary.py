from collections.abc import Iterator

from stockroom.pricing import Tier, unit_price


def test_a_tier_applies_at_exactly_its_minimum_quantity() -> None:
    tiers = [Tier(10, 900)]
    assert unit_price(1000, 9, tiers) == 1000
    assert unit_price(1000, 10, tiers) == 900
    assert unit_price(1000, 11, tiers) == 900
    assert unit_price(1000, 1000, tiers) == 900


def test_each_of_several_tiers_applies_from_its_own_minimum() -> None:
    tiers = [Tier(10, 900), Tier(50, 800), Tier(200, 700)]
    expected = {
        1: 1000,
        9: 1000,
        10: 900,
        11: 900,
        49: 900,
        50: 800,
        51: 800,
        199: 800,
        200: 700,
        201: 700,
    }
    for quantity, price in expected.items():
        assert unit_price(1000, quantity, tiers) == price, quantity


def test_the_order_and_the_kind_of_the_tier_collection_do_not_matter() -> None:
    tiers = [Tier(10, 900), Tier(50, 800), Tier(200, 700)]
    reordered = [tiers[2], tiers[0], tiers[1]]

    def generated() -> Iterator[Tier]:
        yield from reordered

    for quantity, price in ((9, 1000), (10, 900), (50, 800), (199, 800), (200, 700)):
        assert unit_price(1000, quantity, reordered) == price
        assert unit_price(1000, quantity, tuple(tiers)) == price
        assert unit_price(1000, quantity, generated()) == price


def test_the_largest_minimum_reached_wins_even_when_its_price_is_higher() -> None:
    tiers = [Tier(10, 900), Tier(20, 950)]
    assert unit_price(1000, 10, tiers) == 900
    assert unit_price(1000, 19, tiers) == 900
    assert unit_price(1000, 20, tiers) == 950
    assert unit_price(1000, 25, tiers) == 950


def test_a_tier_that_starts_at_one_unit_applies_to_a_single_unit() -> None:
    assert unit_price(1000, 1, [Tier(1, 950)]) == 950
    assert unit_price(1000, 2, [Tier(1, 950), Tier(5, 900)]) == 950
    assert unit_price(1000, 5, [Tier(1, 950), Tier(5, 900)]) == 900


def test_without_a_reached_tier_the_base_price_is_used() -> None:
    assert unit_price(1000, 5) == 1000
    assert unit_price(1000, 5, []) == 1000
    assert unit_price(1000, 9, [Tier(10, 900), Tier(50, 800)]) == 1000
    assert unit_price(0, 10, [Tier(10, 0)]) == 0
