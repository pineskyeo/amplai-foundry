from __future__ import annotations

import dataclasses
import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from stockroom.errors import ParseError, StockError, StockroomError
from stockroom.inventory import Inventory
from stockroom.lots import Lot, LotStore, read_lots

ROOT = Path(__file__).resolve().parents[3]
LOTS = (
    "lot_id,sku,expires,quantity\n"
    "L-3,TEA-010,2024-06-08,4\n"
    "L-1,MUG-001,2024-06-20,5\n"
    "L-2,MUG-001,2024-06-08,2\n"
    "L-4,MUG-001,2024-05-31,3\n"
    "L-5,MUG-001,2024-06-08,1\n"
    "L-6,TEA-010,2024-06-01,2\n"
)


def d(month: int, day: int) -> date:
    return date(2024, month, day)


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_inventory_remove() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 4)])
    inv.remove("MUG-001", 5)
    assert inv.snapshot() == {"MUG-001": (5, 4)}
    for bad in (2, 0, -1):
        with pytest.raises(StockError):
            inv.remove("MUG-001", bad)
    with pytest.raises(StockError):
        inv.remove("TEA-010", 1)
    assert inv.snapshot() == {"MUG-001": (5, 4)}
    inv.remove("MUG-001", 1)
    assert inv.snapshot() == {"MUG-001": (4, 4)} and inv.available("MUG-001") == 0


def test_receive_and_lot_order() -> None:
    assert [f.name for f in dataclasses.fields(Lot)] == ["lot_id", "sku", "expires", "quantity"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Lot("L-1", "MUG-001", d(6, 1), 1).quantity = 2  # type: ignore[misc]
    inv = Inventory.from_levels([("MUG-001", 4, 1)])
    store = LotStore(inv)
    store.receive(Lot("Z-1", "MUG-001", d(6, 30), 5))
    store.receive(Lot("A-1", "MUG-001", d(6, 30), 2))
    store.receive(Lot("M-1", "MUG-001", d(6, 10), 3))
    store.receive(Lot("T-1", "TEA-010", d(6, 1), 4))
    store.receive(Lot("Z-1", "MUG-001", d(6, 30), 1))
    assert store.lots("MUG-001") == [
        Lot("M-1", "MUG-001", d(6, 10), 3),
        Lot("Z-1", "MUG-001", d(6, 30), 6),
        Lot("A-1", "MUG-001", d(6, 30), 2),
    ]
    assert store.lots("TEA-010") == [Lot("T-1", "TEA-010", d(6, 1), 4)]
    assert store.lots("NB-200") == []
    assert inv.snapshot() == {"MUG-001": (15, 1), "TEA-010": (4, 0)}
    for clash in (Lot("Z-1", "MUG-001", d(7, 1), 1), Lot("Z-1", "TEA-010", d(6, 30), 1)):
        with pytest.raises(StockroomError):
            store.receive(clash)
    with pytest.raises(StockError):
        store.receive(Lot("Q-1", "MUG-001", d(6, 30), 0))
    with pytest.raises(ParseError):
        store.receive(Lot("Q-2", "mug-1", d(6, 30), 1))
    assert inv.snapshot() == {"MUG-001": (15, 1), "TEA-010": (4, 0)}
    assert [lot.quantity for lot in store.lots("MUG-001")] == [3, 6, 2]
    assert store.usable("MUG-001", d(6, 10)) == 11
    assert store.usable("MUG-001", d(6, 11)) == 8
    assert store.usable("MUG-001", d(7, 1)) == 0 and store.usable("NB-200", d(6, 1)) == 0


def test_pick_takes_the_earliest_expiry_first() -> None:
    inv = Inventory()
    store = LotStore(inv)
    store.receive(Lot("Z-1", "MUG-001", d(6, 30), 5))
    store.receive(Lot("A-1", "MUG-001", d(6, 30), 2))
    store.receive(Lot("M-1", "MUG-001", d(6, 10), 3))
    store.receive(Lot("E-1", "MUG-001", d(6, 5), 4))
    assert store.pick("MUG-001", 4, d(6, 6)) == [("M-1", 3), ("Z-1", 1)]
    assert store.lots("MUG-001") == [
        Lot("E-1", "MUG-001", d(6, 5), 4),
        Lot("Z-1", "MUG-001", d(6, 30), 4),
        Lot("A-1", "MUG-001", d(6, 30), 2),
    ]
    # a lot can still be used on its expiry day
    assert store.pick("MUG-001", 5, d(6, 30)) == [("Z-1", 4), ("A-1", 1)]
    assert store.lots("MUG-001") == [
        Lot("E-1", "MUG-001", d(6, 5), 4),
        Lot("A-1", "MUG-001", d(6, 30), 1),
    ]
    # emptied lots are gone: their ids start new lots, received after all others
    store.receive(Lot("M-1", "MUG-001", d(6, 10), 2))
    store.receive(Lot("Z-1", "MUG-001", d(6, 10), 1))
    assert [lot.lot_id for lot in store.lots("MUG-001")] == ["E-1", "M-1", "Z-1", "A-1"]
    assert store.pick("MUG-001", 3, d(6, 5)) == [("E-1", 3)]
    assert store.pick("MUG-001", 3, d(6, 9)) == [("M-1", 2), ("Z-1", 1)]
    assert inv.snapshot() == {"MUG-001": (2, 0)}
    assert store.lots("MUG-001") == [
        Lot("E-1", "MUG-001", d(6, 5), 1),
        Lot("A-1", "MUG-001", d(6, 30), 1),
    ]


def test_pick_limits_change_nothing() -> None:
    inv = Inventory.from_levels([("MUG-001", 2, 0)])
    store = LotStore(inv)
    store.receive(Lot("L-1", "MUG-001", d(6, 30), 5))
    inv.reserve("MUG-001", 4)
    for sku, units, day in (
        ("MUG-001", 4, d(6, 1)),
        ("MUG-001", 6, d(6, 1)),
        ("MUG-001", 0, d(6, 1)),
        ("MUG-001", 1, d(7, 1)),
        ("TEA-010", 1, d(6, 1)),
    ):
        with pytest.raises(StockError):
            store.pick(sku, units, day)
    assert inv.snapshot() == {"MUG-001": (7, 4)}
    assert store.lots("MUG-001") == [Lot("L-1", "MUG-001", d(6, 30), 5)]
    assert store.pick("MUG-001", 3, d(6, 1)) == [("L-1", 3)]
    assert inv.snapshot() == {"MUG-001": (4, 4)}
    with pytest.raises(StockError):
        store.pick("MUG-001", 1, d(6, 1))
    assert store.lots("MUG-001") == [Lot("L-1", "MUG-001", d(6, 30), 2)]


def test_expire_writes_off_expired_lots() -> None:
    inv = Inventory()
    store = LotStore(inv)
    store.receive(Lot("B-2", "TEA-010", d(6, 1), 2))
    store.receive(Lot("A-1", "MUG-001", d(6, 1), 3))
    store.receive(Lot("C-3", "MUG-001", d(5, 20), 1))
    store.receive(Lot("E-5", "TEA-010", d(6, 2), 1))
    store.receive(Lot("D-4", "MUG-001", d(6, 2), 4))
    assert store.expire(d(6, 1)) == [Lot("C-3", "MUG-001", d(5, 20), 1)]
    assert inv.snapshot() == {"MUG-001": (7, 0), "TEA-010": (3, 0)}
    assert store.expire(d(6, 2)) == [
        Lot("A-1", "MUG-001", d(6, 1), 3),
        Lot("B-2", "TEA-010", d(6, 1), 2),
    ]
    assert store.expire(d(6, 2)) == []
    assert inv.snapshot() == {"MUG-001": (4, 0), "TEA-010": (1, 0)}
    inv.reserve("MUG-001", 2)
    with pytest.raises(StockError):
        store.expire(d(6, 3))
    assert inv.snapshot() == {"MUG-001": (4, 2), "TEA-010": (1, 0)}
    assert [lot.lot_id for lot in store.lots("TEA-010")] == ["E-5"]
    inv.release("MUG-001", 2)
    assert store.expire(d(6, 3)) == [
        Lot("D-4", "MUG-001", d(6, 2), 4),
        Lot("E-5", "TEA-010", d(6, 2), 1),
    ]
    assert inv.snapshot() == {"MUG-001": (0, 0), "TEA-010": (0, 0)}
    assert store.lots("MUG-001") == [] and store.lots("TEA-010") == []


def test_read_lots() -> None:
    text = (
        "lot_id, sku ,expires,quantity\n"
        " L-1 , MUG-001 ,2024-06-30, 5 \n"
        ",,,\n"
        "L-2,TEA-010,2024-06-01,2\n"
    )
    assert read_lots(text) == [
        Lot("L-1", "MUG-001", d(6, 30), 5),
        Lot("L-2", "TEA-010", d(6, 1), 2),
    ]
    assert read_lots("lot_id,sku,expires,quantity\n") == []
    header = "lot_id,sku,expires,quantity\n"
    for bad in (
        "",
        "lot,sku,expires,quantity\nL-1,MUG-001,2024-06-30,5\n",
        header + "L-1,MUG-001,2024-06-30\n",
        header + "L-1,MUG-001,2024-06-30,5,x\n",
        header + "L-1,MUG-001,2024-06-30,five\n",
        header + "L-1,MUG-001,2024-06-31,5\n",
        header + " ,MUG-001,2024-06-30,5\n",
    ):
        with pytest.raises(ParseError):
            read_lots(bad)


@dataclasses.dataclass
class _ModelLot:
    lot_id: str
    sku: str
    expires: date
    quantity: int

    def frozen(self) -> Lot:
        return Lot(self.lot_id, self.sku, self.expires, self.quantity)


def _on_hand(loose: dict[str, int], lots: list[_ModelLot], sku: str) -> int:
    return loose[sku] + sum(lot.quantity for lot in lots if lot.sku == sku)


def _ordered(lots: list[_ModelLot], sku: str) -> list[_ModelLot]:
    mine = [(lot.expires, n, lot) for n, lot in enumerate(lots) if lot.sku == sku]
    return [lot for _, _, lot in sorted(mine, key=lambda t: (t[0], t[1]))]


def test_random_operations_follow_the_model() -> None:
    rng = random.Random(7071)
    skus = ["MUG-001", "TEA-010"]
    start = d(6, 1)
    for _ in range(60):
        loose = {sku: rng.randrange(0, 4) for sku in skus}
        inv = Inventory.from_levels([(sku, units, 0) for sku, units in loose.items()])
        store = LotStore(inv)
        lots: list[_ModelLot] = []  # in receipt order
        reserved = dict.fromkeys(skus, 0)

        for _ in range(30):
            op = rng.choice(["receive", "receive", "pick", "pick", "expire", "reserve", "release"])
            day = start + timedelta(days=rng.randrange(0, 10))
            sku = rng.choice(skus)
            if op == "receive":
                lot_id = rng.choice(["L-1", "L-2", "L-3", "L-4", "L-5"])
                lot = Lot(lot_id, sku, day, rng.randrange(1, 5))
                known = next((x for x in lots if x.lot_id == lot_id), None)
                if known is not None and (known.sku, known.expires) != (lot.sku, lot.expires):
                    with pytest.raises(StockroomError):
                        store.receive(lot)
                else:
                    store.receive(lot)
                    if known is None:
                        lots.append(_ModelLot(lot_id, sku, day, lot.quantity))
                    else:
                        known.quantity += lot.quantity
            elif op == "pick":
                units = rng.randrange(1, 7)
                usable = [lot for lot in _ordered(lots, sku) if lot.expires >= day]
                free = _on_hand(loose, lots, sku) - reserved[sku]
                if units > sum(lot.quantity for lot in usable) or units > free:
                    with pytest.raises(StockError):
                        store.pick(sku, units, day)
                    continue
                wanted, expected = units, []
                for lot in usable:
                    take = min(wanted, lot.quantity)
                    if take == 0:
                        break
                    wanted -= take
                    lot.quantity -= take
                    expected.append((lot.lot_id, take))
                lots[:] = [lot for lot in lots if lot.quantity > 0]
                assert store.pick(sku, units, day) == expected
            elif op == "expire":
                gone = [lot for lot in lots if lot.expires < day]
                per_sku = {s: sum(lot.quantity for lot in gone if lot.sku == s) for s in skus}
                if any(per_sku[s] > _on_hand(loose, lots, s) - reserved[s] for s in skus):
                    with pytest.raises(StockError):
                        store.expire(day)
                    continue
                order = {lot.lot_id: n for n, lot in enumerate(lots)}
                gone.sort(key=lambda lot: (lot.expires, lot.sku, order[lot.lot_id]))
                expected_lots = [lot.frozen() for lot in gone]
                lots[:] = [lot for lot in lots if lot.expires >= day]
                assert store.expire(day) == expected_lots
            elif op == "reserve":
                free = _on_hand(loose, lots, sku) - reserved[sku]
                if free > 0:
                    units = rng.randrange(1, free + 1)
                    inv.reserve(sku, units)
                    reserved[sku] += units
            elif reserved[sku] > 0:
                units = rng.randrange(1, reserved[sku] + 1)
                inv.release(sku, units)
                reserved[sku] -= units
            for s in skus:
                assert store.lots(s) == [lot.frozen() for lot in _ordered(lots, s)]
            assert inv.snapshot() == {s: (_on_hand(loose, lots, s), reserved[s]) for s in skus}


def test_cli_lists_lots_with_states(tmp_path: Path) -> None:
    path = tmp_path / "lots.csv"
    path.write_text(LOTS)
    done = run("lots", "--lots", str(path), "--day", "2024-06-01", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "MUG-001   L-4       2024-05-31     3  expired",
        "MUG-001   L-2       2024-06-08     2  expiring",
        "MUG-001   L-5       2024-06-08     1  expiring",
        "MUG-001   L-1       2024-06-20     5  ok",
        "TEA-010   L-6       2024-06-01     2  expiring",
        "TEA-010   L-3       2024-06-08     4  expiring",
        "usable 14  expiring 9  expired 3",
    ]
    done = run("lots", "--lots", str(path), "--day", "2024-06-01", "--within", "6", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert [line.split()[-1] for line in done.stdout.splitlines()[:-1]] == [
        "expired",
        "ok",
        "ok",
        "ok",
        "expiring",
        "ok",
    ]
    assert done.stdout.splitlines()[-1] == "usable 14  expiring 2  expired 3"
    done = run("lots", "--lots", str(path), "--day", "2024-06-08", "--within", "0", cwd=tmp_path)
    assert done.stdout.splitlines() == [
        "MUG-001   L-4       2024-05-31     3  expired",
        "MUG-001   L-2       2024-06-08     2  expiring",
        "MUG-001   L-5       2024-06-08     1  expiring",
        "MUG-001   L-1       2024-06-20     5  ok",
        "TEA-010   L-6       2024-06-01     2  expired",
        "TEA-010   L-3       2024-06-08     4  expiring",
        "usable 12  expiring 7  expired 5",
    ]
    for bad in ("-1", "x", "1.5"):
        done = run(
            "lots", "--lots", str(path), "--day", "2024-06-01", "--within", bad, cwd=tmp_path
        )
        assert done.returncode == 2 and done.stdout == ""


def test_cli_picks_and_errors(tmp_path: Path) -> None:
    path = tmp_path / "lots.csv"
    path.write_text(LOTS)
    args = ("lots", "--lots", str(path), "--day", "2024-06-01")
    done = run(*args, "--pick", "MUG-001=4", "--pick", "TEA-010=1", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "picked 4 MUG-001: L-2 x2, L-5 x1, L-1 x1",
        "picked 1 TEA-010: L-6 x1",
        "MUG-001   L-4       2024-05-31     3  expired",
        "MUG-001   L-1       2024-06-20     4  ok",
        "TEA-010   L-6       2024-06-01     1  expiring",
        "TEA-010   L-3       2024-06-08     4  expiring",
        "usable 9  expiring 5  expired 3",
    ]
    done = run(*args, "--pick", "TEA-010=6", cwd=tmp_path)
    assert done.stdout.splitlines()[:2] == [
        "picked 6 TEA-010: L-6 x2, L-3 x4",
        "MUG-001   L-4       2024-05-31     3  expired",
    ]
    for picks in (
        ["--pick", "MUG-001=9"],
        ["--pick", "TEA-010=1", "--pick", "TEA-010=6"],
        ["--pick", "MUG-001"],
        ["--pick", "MUG-001=0"],
        ["--pick", "MUG-001=x"],
        ["--pick", "mug-001=1"],
        ["--pick", "NB-200=1"],
    ):
        done = run(*args, *picks, cwd=tmp_path)
        assert done.returncode == 1, picks
        assert done.stdout == "" and done.stderr.startswith("stockroom: error: ")
    clash = tmp_path / "clash.csv"
    clash.write_text(LOTS + "L-1,MUG-001,2024-06-21,1\n")
    done = run("lots", "--lots", str(clash), "--day", "2024-06-01", cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: ")
