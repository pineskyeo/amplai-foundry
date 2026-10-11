from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from stockroom.errors import ParseError
from stockroom.models import Item
from stockroom.query import And, Not, Or, QueryError, Term, format_query, matches, parse_query

ROOT = Path(__file__).resolve().parents[3]


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=50,
        check=False,
    )


def T(value: str, field: str | None = None, op: str = ":") -> Term:
    return Term(field, op, value)


def test_parse_examples() -> None:
    assert parse_query("") == And(())
    assert parse_query(" \t\n") == And(())
    assert parse_query("mug") == T("mug")
    assert parse_query("  mug  ") == T("mug")
    assert parse_query("blue mug") == And((T("blue"), T("mug")))
    assert parse_query("tag:gift price<20") == And((T("gift", "tag"), T("20", "price", "<")))
    assert parse_query("price<=12.5 price>=1 price>0.05 reorder:3 reorder<10") == And(
        (
            T("12.5", "price", "<="),
            T("1", "price", ">="),
            T("0.05", "price", ">"),
            T("3", "reorder"),
            T("10", "reorder", "<"),
        )
    )
    assert parse_query('name:"blue mug"') == T("blue mug", "name")
    assert parse_query('"say ""hi"""') == T('say "hi"')
    assert parse_query('""') == T("")
    assert parse_query('name:""') == T("", "name")
    assert parse_query('name:"a:b<c>"') == T("a:b<c>", "name")
    assert parse_query("sku:MUG-* -tag:gift") == And((T("MUG-*", "sku"), Not(T("gift", "tag"))))
    assert parse_query('-"blue mug"') == Not(T("blue mug"))
    assert parse_query("a OR b") == Or((T("a"), T("b")))
    assert parse_query("a b OR c") == Or((And((T("a"), T("b"))), T("c")))
    assert parse_query("a (b OR c)") == And((T("a"), Or((T("b"), T("c")))))
    assert parse_query("a OR b OR c") == Or((T("a"), T("b"), T("c")))
    assert parse_query("(a OR b) OR c") == Or((T("a"), T("b"), T("c")))
    assert parse_query("a OR (b OR c)") == Or((T("a"), T("b"), T("c")))
    assert parse_query("(a b) c") == And((T("a"), T("b"), T("c")))
    assert parse_query("a (b (c d))") == And((T("a"), T("b"), T("c"), T("d")))
    assert parse_query("((a))") == T("a")
    assert parse_query("-(a b) c") == And((Not(And((T("a"), T("b")))), T("c")))
    assert parse_query("-(-a)") == Not(Not(T("a")))
    assert parse_query("-(a OR b)") == Not(Or((T("a"), T("b"))))
    assert parse_query("(a)b") == And((T("a"), T("b")))
    assert parse_query("a(b)") == And((T("a"), T("b")))
    assert parse_query('a OR"b"') == Or((T("a"), T("b")))
    assert parse_query("a OR(b c)") == Or((T("a"), And((T("b"), T("c")))))
    assert parse_query('(a)"b c"') == And((T("a"), T("b c")))
    assert parse_query("or and OR ORx") == Or((And((T("or"), T("and"))), T("ORx")))
    assert parse_query('"OR" x') == And((T("OR"), T("x")))
    assert parse_query("a-b x=1 name:-x") == And((T("a-b"), T("x=1"), T("-x", "name")))
    assert parse_query("a\xa0b\u2028c") == And((T("a"), T("b"), T("c")))
    assert parse_query("café 日本") == And((T("café"), T("日本")))
    assert parse_query("(a OR b) (c OR d)") == And((Or((T("a"), T("b"))), Or((T("c"), T("d")))))


def column(text: str) -> int:
    with pytest.raises(QueryError) as caught:
        parse_query(text)
    error = caught.value
    assert isinstance(error, ParseError)
    assert str(error).startswith(f"column {error.column}: ")
    return error.column


def test_errors_report_their_column() -> None:
    cases = {
        '"abc': 1,
        'a "abc': 3,
        'name:"abc': 6,
        '"a"b': 4,
        '"a"(b)': 4,
        'name:"x"y': 9,
        'ab"c"': 3,
        'name:x"y"': 7,
        "- a": 1,
        "a -": 3,
        "--a": 1,
        "-)": 1,
        "a -OR b": 4,
        "OR a": 1,
        "a OR": 5,
        "a OR OR b": 6,
        "a OR ) b": 6,
        "(OR a)": 2,
        "()": 2,
        "(a": 3,
        "((a) b": 7,
        "a)": 2,
        "a) b": 2,
        "(a))": 4,
        "colour:red": 1,
        ":red": 1,
        "Name:x": 1,
        "price=>5": 1,
        "name<x": 5,
        "tag>=x": 4,
        "sku<a": 4,
        "price<<5": 7,
        "name:a:b": 7,
        "x name:": 8,
        "price:": 7,
        "price: 5": 7,
        "price:12.345": 7,
        "price:1,000": 7,
        "price:-5": 7,
        "price:.5": 7,
        "price:5.": 7,
        "price:\u0665": 7,
        "price:\u00b2": 7,
        "price:1_000": 7,
        "reorder<1_0": 9,
        'price:"12 "': 7,
        "reorder>=1.5": 10,
        "reorder:x": 9,
        "price:1:2": 8,
        'price:12.345"x"': 13,
        'reorder:"x"y': 12,
        'colour:"x': 1,
        'name<"x': 5,
        "a b  (c OR )": 12,
    }
    for text, expected in cases.items():
        assert column(text) == expected, text


def test_format_query() -> None:
    assert format_query(And(())) == ""
    assert format_query(T("mug")) == "mug"
    assert format_query(T("blue mug")) == '"blue mug"'
    assert format_query(T('say "hi"')) == '"say ""hi"""'
    assert format_query(T("")) == '""'
    assert format_query(T("OR")) == '"OR"'
    assert format_query(T("ORx")) == "ORx"
    assert format_query(T("-x")) == '"-x"'
    assert format_query(T("a-b")) == "a-b"
    assert format_query(T("a:b")) == '"a:b"'
    assert format_query(T("(a)")) == '"(a)"'
    assert format_query(T("a\xa0b")) == '"a\xa0b"'
    assert format_query(T("12.5", "price", "<=")) == "price<=12.5"
    assert format_query(T("blue mug", "name")) == 'name:"blue mug"'
    assert format_query(T("-x", "name")) == 'name:"-x"'
    assert format_query(T("", "tag")) == 'tag:""'
    assert format_query(Not(T("gift", "tag"))) == "-tag:gift"
    assert format_query(Not(T("a b"))) == '-"a b"'
    assert format_query(Not(And((T("a"), T("b"))))) == "-(a b)"
    assert format_query(Not(Not(T("a")))) == "-(-a)"
    assert format_query(And((T("a"), Or((T("b"), T("c"))), Not(T("d"))))) == "a (b OR c) -d"
    assert format_query(Or((And((T("a"), T("b"))), T("c")))) == "a b OR c"
    assert format_query(Or((Not(Or((T("a"), T("b")))), T("c")))) == "-(a OR b) OR c"
    assert format_query(parse_query("  tag:gift   ( a OR\tb )  -( c )")) == "tag:gift (a OR b) -c"


VALUE_CHARS = [
    "a",
    "B",
    "z",
    "1",
    " ",
    '"',
    "(",
    ")",
    ":",
    "<",
    ">",
    "-",
    "=",
    "*",
    "?",
    "é",
    "ß",
    "\t",
    "\xa0",
    "OR",
    "or",
]


def random_value(rng: random.Random, field: str | None) -> str:
    if field == "price":
        return rng.choice(["0", "7", "12", "12.5", "12.50", "007.1", "1000.99", "0.05"])
    if field == "reorder":
        return rng.choice(["0", "3", "10", "007", "120"])
    return "".join(rng.choice(VALUE_CHARS) for _ in range(rng.choice([0, 1, 1, 2, 3, 5])))


def random_term(rng: random.Random) -> Term:
    field = rng.choice([None, None, "sku", "name", "tag", "price", "reorder"])
    op = rng.choice([":", "<", "<=", ">", ">="]) if field in ("price", "reorder") else ":"
    return Term(field, op, random_value(rng, field))


def random_node(rng: random.Random, depth: int, forbid: type | None = None):
    kinds = ["term", "term", "not"] + (["and", "or"] if depth > 0 else [])
    kind = rng.choice(
        [
            k
            for k in kinds
            if not (forbid is And and k == "and") and not (forbid is Or and k == "or")
        ]
    )
    if kind == "term" or (depth <= 0 and kind != "not"):
        return random_term(rng)
    if kind == "not":
        return Not(random_node(rng, depth - 1) if depth > 0 else random_term(rng))
    cls = And if kind == "and" else Or
    return cls(tuple(random_node(rng, depth - 1, cls) for _ in range(rng.randint(2, 3))))


def test_generated_queries_round_trip() -> None:
    rng = random.Random(1987)
    for _ in range(3000):
        node = random_node(rng, rng.randint(0, 3))
        text = format_query(node)
        assert parse_query(text) == node, text
        assert format_query(parse_query(text)) == text
        spaced = text.replace(" OR ", "  \t OR\n ").replace("(", "( ").replace(")", " )")
        if '"' not in text:
            assert parse_query(" " + spaced + " ") == node, spaced


def glob_match(pattern: str, text: str) -> bool:
    if not pattern:
        return not text
    if pattern[0] == "*":
        return any(glob_match(pattern[1:], text[k:]) for k in range(len(text) + 1))
    if not text:
        return False
    if pattern[0] == "?" or pattern[0] == text[0]:
        return glob_match(pattern[1:], text[1:])
    return False


def model(node, item: Item) -> bool:
    if isinstance(node, Not):
        return not model(node.child, item)
    if isinstance(node, And):
        return all(model(c, item) for c in node.children)
    if isinstance(node, Or):
        return any(model(c, item) for c in node.children)
    value = node.value.casefold()
    if node.field is None:
        return value in item.name.casefold() or value in item.sku.casefold()
    if node.field == "name":
        return value in item.name.casefold()
    if node.field == "sku":
        return glob_match(value, item.sku.casefold())
    if node.field == "tag":
        return value in [t.casefold() for t in item.tags]
    whole, _, frac = node.value.partition(".")
    number = int(whole) * 100 + int((frac + "00")[:2]) if node.field == "price" else int(node.value)
    have = item.price_cents if node.field == "price" else item.reorder_level
    return {
        ":": have == number,
        "<": have < number,
        "<=": have <= number,
        ">": have > number,
        ">=": have >= number,
    }[node.op]


ITEMS = [
    Item("MUG-001", "Blue Mug", 1250, ("kitchen", "gift"), 5),
    Item("MUG-002", "Straße Mug", 1250, ("Kitchen",), 3),
    Item("TEA-010", "Green Tea 100g", 895, ("food", "tea"), 10),
    Item("PEN-100", 'Pen "Deluxe" (blue)', 3400, ("office", "gift"), 2),
    Item("NB-200", "Notebook A5", 625, (), 20),
    Item("LMP-030", "DESK LAMP", 100099, ("home",), 0),
    Item("CND-007", "Café Candle", 5, ("home", "gift"), 8),
    Item("MUG-0100", "Mug [big]", 1200, ("kitchen",), 7),
]


def test_matching_examples() -> None:
    def skus(text: str) -> list[str]:
        return [item.sku for item in ITEMS if matches(parse_query(text), item)]

    assert skus("") == [item.sku for item in ITEMS]
    assert skus("mug") == ["MUG-001", "MUG-002", "MUG-0100"]
    assert skus("MUG") == ["MUG-001", "MUG-002", "MUG-0100"]
    assert skus("strasse") == ["MUG-002"]
    assert skus("name:STRASSE") == ["MUG-002"]
    assert skus("name:straße") == ["MUG-002"]
    assert skus("tea-0") == ["TEA-010"]
    assert skus("name:tea-0") == []
    assert skus("tag:gift") == ["MUG-001", "PEN-100", "CND-007"]
    assert skus("tag:KITCHEN") == ["MUG-001", "MUG-002", "MUG-0100"]
    assert skus("tag:kit") == []
    assert skus("sku:MUG-*") == ["MUG-001", "MUG-002", "MUG-0100"]
    assert skus("sku:mug-00?") == ["MUG-001", "MUG-002"]
    assert skus("sku:*1*") == ["MUG-001", "TEA-010", "PEN-100", "MUG-0100"]
    assert skus("sku:MUG") == []
    assert skus("sku:[M]UG-001") == []
    assert skus('name:"[big]"') == ["MUG-0100"]
    assert skus("price:12.5") == ["MUG-001", "MUG-002"]
    assert skus("price:12.50 -tag:gift") == ["MUG-002"]
    assert skus("price<12.50") == ["TEA-010", "NB-200", "CND-007", "MUG-0100"]
    assert skus("price<=12.5 price>=12") == ["MUG-001", "MUG-002", "MUG-0100"]
    assert skus("price>1000.98") == ["LMP-030"]
    assert skus("price:0.05") == ["CND-007"]
    assert skus("reorder:0 OR reorder>=10") == ["TEA-010", "NB-200", "LMP-030"]
    assert skus("reorder<3") == ["PEN-100", "LMP-030"]
    assert skus('"deluxe" (blue)') == ["PEN-100"]
    assert skus('"pen ""deluxe"""') == ["PEN-100"]
    assert skus("-(tag:gift OR tag:kitchen)") == ["TEA-010", "NB-200", "LMP-030"]
    assert skus("-(-mug)") == ["MUG-001", "MUG-002", "MUG-0100"]
    assert skus("cafÉ") == ["CND-007"]
    assert skus('""') == [item.sku for item in ITEMS]
    assert skus('tag:""') == []


NAME_CHARS = ["a", "B", " ", "ß", "SS", "s", "é", "É", "-", "1", "(", '"', "*", "?"]


def test_generated_matching_follows_the_rules() -> None:
    rng = random.Random(55)
    skus = ["MUG-001", "MUG-002", "TEA-010", "AB-1234", "XYZ-99999", "PEN-100"]
    items = []
    for _ in range(40):
        name = "".join(rng.choice(NAME_CHARS) for _ in range(rng.randint(1, 8))).strip() or "x"
        tags = tuple(
            rng.choice(["gift", "Gift", "kitchen", "straße", "a b", "-x"])
            for _ in range(rng.randint(0, 3))
        )
        items.append(
            Item(
                rng.choice(skus),
                name,
                rng.choice([0, 5, 1250, 1999, 100000]),
                tags,
                rng.randint(0, 12),
            )
        )
    for _ in range(1500):
        node = random_node(rng, rng.randint(0, 3))
        if rng.random() < 0.3:
            node = Term(
                "sku",
                ":",
                "".join(
                    rng.choice(["M", "u", "g", "-", "0", "1", "*", "?", "A"])
                    for _ in range(rng.randint(0, 8))
                ),
            )
        for item in items:
            assert matches(node, item) == model(node, item), (format_query(node), item)
        reparsed = parse_query(format_query(node))
        assert [matches(reparsed, item) for item in items] == [model(node, item) for item in items]


CATALOGUE = (
    "sku,name,price,tags,reorder_level\n"
    "MUG-001,Blue Mug,12.50,kitchen;gift,5\n"
    "MUG-002,Straße Mug,12.50,kitchen,5\n"
    "TEA-010,Green Tea 100g,8.95,food;tea,10\n"
    "PEN-100,Fountain Pen,34.00,office;gift,2\n"
    "NB-200,Notebook A5,6.25,office,20\n"
)


def test_items_where_command(tmp_path: Path) -> None:
    (tmp_path / "items.csv").write_text(CATALOGUE, encoding="utf-8")

    def listed(*args: str) -> list[str]:
        done = run("items", "--file", "items.csv", *args, cwd=tmp_path)
        assert done.returncode == 0, done.stderr
        return [line.split()[0] for line in done.stdout.splitlines()[2:]]

    assert listed("--where", "mug") == ["MUG-001", "MUG-002"]
    assert listed("--where", "name:strasse") == ["MUG-002"]
    assert listed("--where", "tag:gift OR price<7") == ["MUG-001", "PEN-100", "NB-200"]
    assert listed("--where", "-(tag:kitchen OR tag:food) reorder>=5") == ["NB-200"]
    assert listed("--where", 'name:"green tea"') == ["TEA-010"]
    assert listed("--where", "sku:mug-* -price:12.5") == []
    assert listed("--where", "") == ["MUG-001", "MUG-002", "TEA-010", "PEN-100", "NB-200"]
    assert listed("--where", "sku:*0", "--tag", "office") == ["PEN-100", "NB-200"]
    for query, col in (("tag:gift (", 11), ("colour:red", 1), ('name:"x', 6), ("price<12.345", 7)):
        done = run("items", "--file", "items.csv", "--where", query, cwd=tmp_path)
        assert done.returncode == 1 and done.stdout == ""
        assert done.stderr.startswith(f"stockroom: error: column {col}: "), (query, done.stderr)
