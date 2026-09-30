import ast
import hashlib
import importlib.util
import inspect
import random
import subprocess
import sys
from pathlib import Path

import pytest

import stockroom
from stockroom import csvio

ROOT = Path(stockroom.__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make_sample_data.py"

DIGESTS = {
    (7, 20): {
        "items.csv": "1eb8b9a547ef855651a80c8ccff7d44ec14cf47e0a82acde9407b0eeda104be6",
        "orders.csv": "6d44cad1d322b12d90a388670123d89a22820eca017f2b109856852f95615f7e",
        "stock.csv": "792768bfa9e664aa9c28e412b612f70d38fd864e8f575a88bdd0cdb953f6ad74",
    },
    (3, 9): {
        "items.csv": "4579ad5d1eecf5de9c185e173fdcf4d1a70a4215a8dc0fd5d06643a971ff4238",
        "orders.csv": "fe606b981b7291c8c96fdc8145f945420daf23d12a45ad4c4014642239da30b3",
        "stock.csv": "60459fea289ccb7faff78209e821afc7094ea699e9470bb93125afb4ab7c4014",
    },
    (11, 0): {
        "items.csv": "9c962e8767fa2f3ab1e566adf1a65f8e154c6a6423aeeda3284e9f49b4ce976f",
        "orders.csv": "8390daebc9454b09c2e2ae99196c6cd5faec144527896b4249699532ad957232",
        "stock.csv": "9928dfbad9a8faaeae8be3d2f2c5b403e5b45356b4d3ed6b5fd0a829becbbd58",
    },
}


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("make_sample_data_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_write_stock_writes_what_read_stock_reads() -> None:
    levels = [("MUG-001", 14, 2), ("TEA-010", 0, 0), ("PEN-100", 3, 3)]
    text = csvio.write_stock(levels)
    assert text == "sku,on_hand,reserved\nMUG-001,14,2\nTEA-010,0,0\nPEN-100,3,3\n"
    assert csvio.read_stock(text) == levels
    assert csvio.write_stock([]) == "sku,on_hand,reserved\n"
    assert csvio.write_stock(iter([("NB-200", 40, 4)])) == "sku,on_hand,reserved\nNB-200,40,4\n"
    assert csvio.read_stock(csvio.write_stock([])) == []


def test_the_script_has_one_builder_per_file(script) -> None:
    assert list(inspect.signature(script.make_items).parameters) == ["rng"]
    assert list(inspect.signature(script.make_stock).parameters) == ["rng", "items"]
    assert list(inspect.signature(script.make_orders).parameters) == ["rng", "items", "count"]
    assert list(inspect.signature(script.build).parameters) == ["seed", "orders"]


def test_builders_chained_on_one_generator_give_the_files_of_build(script) -> None:
    for seed, count in ((7, 20), (3, 9), (11, 0), (1, 5)):
        rng = random.Random(seed)
        items = script.make_items(rng)
        stock = script.make_stock(rng, items)
        orders = script.make_orders(rng, items, count)
        assert len(items) == 8 and len(stock) == 8 and len(orders) == count
        assert [i.sku for i in items] == [f"SMP-{100 + n}" for n in range(8)]
        assert [s[0] for s in stock] == [i.sku for i in items]
        assert all(0 <= reserved <= on_hand < 40 for _, on_hand, reserved in stock)
        files = script.build(seed, count)
        assert files["items.csv"] == csvio.write_items(items)
        assert files["stock.csv"] == csvio.write_stock(stock)
        assert files["orders.csv"] == csvio.write_orders(orders)


def test_the_builders_use_only_the_generator_they_are_given(script) -> None:
    random.seed(99)
    before = random.getstate()
    rng = random.Random(5)
    items = script.make_items(rng)
    script.make_stock(rng, items)
    script.make_orders(rng, items, 6)
    assert random.getstate() == before


def test_the_generator_is_drawn_from_in_the_same_order_as_before(script) -> None:
    rng = random.Random(7)
    items = script.make_items(rng)
    after_items = rng.getstate()
    script.make_stock(rng, items)
    after_stock = rng.getstate()
    script.make_orders(rng, items, 20)
    final = rng.getstate()
    assert len({after_items, after_stock, final}) == 3
    first = random.Random(7)
    script.make_items(first)
    assert first.getstate() == after_items
    # the same state is reached by a fresh generator that only ran the two earlier builders
    second = random.Random(7)
    items2 = script.make_items(second)
    script.make_stock(second, items2)
    assert second.getstate() == after_stock


def test_build_output_is_byte_identical_to_before(script) -> None:
    for (seed, count), expected in DIGESTS.items():
        files = script.build(seed, count)
        assert {name: digest(text) for name, text in files.items()} == expected


def test_the_command_line_still_writes_the_same_files(tmp_path: Path) -> None:
    out = tmp_path / "out"
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), "--seed", "3", "--orders", "9"],
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"wrote 8 items and 9 orders to {out}\n"
    got = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir()}
    assert got == DIGESTS[(3, 9)]


def test_the_script_no_longer_spells_out_the_stock_file_itself() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "on_hand" in node.value
    ]
    assert offenders == []
    used = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    assert "write_stock" in used
