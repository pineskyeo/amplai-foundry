from __future__ import annotations

import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stockroom.csvio import write_items
from stockroom.models import Item
from stockroom.textutil import slugify, unique_slugs

ROOT = Path(__file__).resolve().parents[3]
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
ALPHABET = list(
    "abcxyzABCXYZ0129 \t-_.,;:!?'’&éÉèüñçøØæÆœŒßẞłŁđÐþÞ½Ⅻ²ﬁＡ日本̧́"
)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_accents_are_folded() -> None:
    assert slugify("Café Au Lait") == "cafe-au-lait"
    assert slugify("Crème Brûlée") == "creme-brulee"
    assert slugify("Straße") == "strasse"
    assert slugify("STRAẞE") == "strasse"
    assert slugify("Ærø Ølsen") == "aero-olsen"
    assert slugify("Łódź Œuvre") == "lodz-oeuvre"
    assert slugify("Þór Ðe") == "thor-de"
    assert slugify("Đà Nẵng") == "da-nang"
    assert slugify("Ｈｅｌｌｏ １２") == "hello-12"
    assert slugify("ﬁne") == "fine"
    assert slugify("Ⅻ") == "xii"
    assert slugify("x²") == "x2"
    assert slugify("éȩ") == "ee"
    assert slugify("Blue Mug, Large") == "blue-mug-large"
    assert slugify("  --Desk   Lamp--  ") == "desk-lamp"
    assert slugify("日本語") == ""
    assert slugify("") == ""
    assert slugify("Zürich 日本 Bern") == "zurich-bern"


def test_ampersands_and_apostrophes() -> None:
    assert slugify("Salt & Pepper") == "salt-and-pepper"
    assert slugify("R&D") == "r-and-d"
    assert slugify("a&&b") == "a-and-and-b"
    assert slugify("&") == "and"
    assert slugify("Bob's Mug") == "bobs-mug"
    assert slugify("Bob’s Mug") == "bobs-mug"
    assert slugify("rock 'n' roll") == "rock-n-roll"
    assert slugify("'Tis") == "tis"
    assert slugify("ab''cd") == "ab-cd"
    assert slugify("x'’y") == "x-y"
    assert slugify("a-'b") == "a-b"
    assert slugify("5'10\"") == "510"
    assert slugify("é'a") == "ea"
    assert slugify("a'b'c") == "abc"
    assert slugify("Ｏ＇Neil") == "oneil"


def test_max_length_keeps_whole_words() -> None:
    text = "Blue Mug, Large"
    assert slugify(text, max_length=14) == "blue-mug-large"
    assert slugify(text, max_length=100) == "blue-mug-large"
    assert slugify(text, max_length=13) == "blue-mug"
    assert slugify(text, max_length=9) == "blue-mug"
    assert slugify(text, max_length=8) == "blue-mug"
    assert slugify(text, max_length=7) == "blue"
    assert slugify(text, max_length=4) == "blue"
    assert slugify(text, max_length=3) == "blu"
    assert slugify(text, max_length=1) == "b"
    assert slugify("Extraordinarily Long", max_length=5) == "extra"
    assert slugify("ab cd ef", max_length=5) == "ab-cd"
    assert slugify("ab cd ef", max_length=4) == "ab"
    assert slugify("Ærø Ølsen", max_length=5) == "aero"
    assert slugify("日本", max_length=3) == ""
    for bad in (0, -1):
        with pytest.raises(ValueError):
            slugify("x", max_length=bad)


def test_unique_slugs_numbering() -> None:
    assert unique_slugs(["Mug", "mug", "MUG!", "Mug 2"]) == ["mug", "mug-2", "mug-3", "mug-2-2"]
    assert unique_slugs(["a", "a", "a-2"]) == ["a", "a-2", "a-2-2"]
    assert unique_slugs(["a-2", "a", "a"]) == ["a-2", "a", "a-3"]
    assert unique_slugs(["!!!", "???", "é!"]) == ["n-a", "n-a-2", "e"]
    assert unique_slugs(["Café", "Cafe", "CAFÉ"]) == ["cafe", "cafe-2", "cafe-3"]
    assert unique_slugs([]) == []
    assert unique_slugs(name for name in ("x", "x")) == ["x", "x-2"]
    assert unique_slugs(["One", "Two"]) == ["one", "two"]
    assert unique_slugs(["日本", "日本"]) == ["n-a", "n-a-2"]


def test_unique_slugs_with_a_length_limit() -> None:
    assert unique_slugs(["Blue Mug"] * 4, max_length=6) == ["blue", "blue-2", "blue-3", "blue-4"]
    ten = unique_slugs(["Blue Mug"] * 10, max_length=6)
    assert ten[:3] == ["blue", "blue-2", "blue-3"]
    assert ten[8] == "blue-9" and ten[9] == "blu-10"
    assert unique_slugs(["abcdef", "abcdeg"], max_length=5) == ["abcde", "abc-2"]
    assert unique_slugs(["a", "a"], max_length=3) == ["a", "a-2"]
    assert unique_slugs(["ab", "ab"], max_length=3) == ["ab", "a-2"]
    assert unique_slugs(["!!!", "!!!"], max_length=3) == ["n-a", "n-2"]
    assert unique_slugs(["!!!"], max_length=2) == ["n"]
    with pytest.raises(ValueError):
        unique_slugs(["a", "a"], max_length=2)
    for bad in (0, -3):
        with pytest.raises(ValueError):
            unique_slugs([], max_length=bad)
        with pytest.raises(ValueError):
            unique_slugs(["x"], max_length=bad)


def _random_text(rng: random.Random) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randrange(0, 24)))


def test_slugs_are_well_formed_and_stable() -> None:
    rng = random.Random(20261001)
    for _ in range(1500):
        text = _random_text(rng)
        slug = slugify(text)
        assert slug == "" or SLUG.fullmatch(slug), (text, slug)
        assert slugify(slug) == slug, (text, slug)
        limit = rng.randrange(1, 20)
        short = slugify(text, max_length=limit)
        assert len(short) <= limit, (text, limit, short)
        assert slug.startswith(short), (text, limit, short)
        assert short == "" or SLUG.fullmatch(short), (text, limit, short)
        assert slugify(short, max_length=limit) == short
        if short != slug:
            first = slug.split("-")[0]
            if len(first) > limit:
                assert short == first[:limit]
            else:
                # whole words, and the next word would not have fitted
                assert slug[len(short)] == "-"
                following = slug[len(short) + 1 :].split("-")[0]
                assert len(short) + 1 + len(following) > limit, (text, limit, short)
        else:
            assert len(slug) <= limit


def _model(names: list[str], limit: int | None) -> list[str]:
    used: set[str] = set()
    out = []
    for name in names:
        base = slugify(name) or "n-a"
        n = 1
        candidate = slugify(base, max_length=limit)
        while candidate in used:
            n += 1
            suffix = f"-{n}"
            room = None if limit is None else limit - len(suffix)
            candidate = slugify(base, max_length=room) + suffix
        used.add(candidate)
        out.append(candidate)
    return out


def test_generated_name_lists_get_distinct_slugs() -> None:
    rng = random.Random(4242)
    pool = ["Mug", "mug", "Mug 2", "Café", "cafe", "Tea & Co", "tea and co", "日本", "!", "a", "a 2"]
    for _ in range(400):
        names = [
            rng.choice(pool) if rng.random() < 0.6 else _random_text(rng)
            for _ in range(rng.randrange(0, 25))
        ]
        limit = rng.choice([None, 6, 8, 12, 20])
        try:
            slugs = unique_slugs(names, max_length=limit)
        except ValueError:
            assert limit is not None
            continue
        assert len(slugs) == len(names)
        assert len(set(slugs)) == len(slugs), (names, slugs)
        for slug in slugs:
            assert SLUG.fullmatch(slug), (names, slugs)
            assert limit is None or len(slug) <= limit, (names, slugs)
        assert slugs == _model(names, limit), (names, slugs)
        if len({slugify(n) or "n-a" for n in names}) == len(names) and (
            limit is None or all(len(slugify(n) or "n-a") <= limit for n in names)
        ):
            assert slugs == [slugify(n) or "n-a" for n in names]


def test_slugs_command(tmp_path: Path) -> None:
    items = [
        Item("MUG-001", "Blue Mug", 1250),
        Item("MUG-002", "Blue Mug!", 1250),
        Item("TEA-010", "Café Crème", 895),
        Item("TEA-011", "Bob's Tea", 740),
    ]
    path = tmp_path / "items.csv"
    path.write_text(write_items(items), encoding="utf-8")
    done = run("slugs", "--file", str(path))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "MUG-001 blue-mug",
        "MUG-002 blue-mug-2",
        "TEA-010 cafe-creme",
        "TEA-011 bobs-tea",
    ]
    done = run("slugs", "--file", str(path), "--max-length", "6")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "MUG-001 blue",
        "MUG-002 blue-2",
        "TEA-010 cafe",
        "TEA-011 bobs",
    ]
    for bad in ("0", "-4", "2"):
        refused = run("slugs", "--file", str(path), "--max-length", bad)
        assert refused.returncode == 1, (bad, refused.stdout, refused.stderr)
        assert refused.stdout == ""
        assert refused.stderr.startswith("stockroom: error:")
