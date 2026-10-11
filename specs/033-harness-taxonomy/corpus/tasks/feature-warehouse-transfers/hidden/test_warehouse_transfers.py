from __future__ import annotations

import dataclasses
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from stockroom.errors import ParseError, StockError, StockroomError
from stockroom.inventory import Inventory
from stockroom.transfers import Network, Transfer, parse_events, read_network

ROOT = Path(__file__).resolve().parents[3]
NETWORK = (
    "location,sku,on_hand,reserved\n"
    "MAIN,MUG-001,10,2\n"
    "MAIN,TEA-010,5,0\n"
    "SHOP,MUG-001,1,0\n"
    "BACK,NB-200,7,7\n"
)


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


def test_inventory_issue() -> None:
    inv = Inventory.from_levels([("MUG-001", 10, 4)])
    inv.issue("MUG-001", 6)
    assert inv.snapshot() == {"MUG-001": (4, 4)}
    for sku, bad in (("MUG-001", 1), ("MUG-001", 0), ("MUG-001", -2), ("TEA-010", 1)):
        with pytest.raises(StockError):
            inv.issue(sku, bad)
    assert inv.snapshot() == {"MUG-001": (4, 4)}


def test_dispatch_receive_and_cancel() -> None:
    assert [f.name for f in dataclasses.fields(Transfer)] == [
        "transfer_id",
        "sku",
        "quantity",
        "source",
        "target",
    ]
    main = Inventory.from_levels([("MUG-001", 10, 2), ("TEA-010", 5, 0)])
    shop = Inventory.from_levels([("MUG-001", 1, 0)])
    net = Network({"SHOP": shop, "MAIN": main})
    assert net.locations() == ["MAIN", "SHOP"]
    assert net.dispatch(Transfer("T-1", "MUG-001", 3, "MAIN", "SHOP")) is True
    assert main.snapshot() == {"MUG-001": (7, 2), "TEA-010": (5, 0)}
    assert net.state("T-1") == "in_transit" and net.in_transit("MUG-001") == 3
    assert net.total("MUG-001") == 11 and net.in_transit("TEA-010") == 0
    assert net.dispatch(Transfer("T-2", "TEA-010", 5, "MAIN", "SHOP")) is True
    assert net.receive("T-1") is True
    assert shop.snapshot() == {"MUG-001": (4, 0)}
    assert net.state("T-1") == "received" and net.in_transit("MUG-001") == 0
    assert net.total("MUG-001") == 11
    assert net.cancel("T-2") is True
    assert main.snapshot() == {"MUG-001": (7, 2), "TEA-010": (5, 0)}
    assert net.state("T-2") == "cancelled" and net.total("TEA-010") == 5
    assert net.dispatch(Transfer("T-3", "TEA-010", 2, "MAIN", "SHOP")) is True
    assert net.receive("T-3") is True
    assert shop.snapshot() == {"MUG-001": (4, 0), "TEA-010": (2, 0)}
    assert net.transfer_ids() == ["T-1", "T-2", "T-3"]
    assert net.total("NB-200") == 0


def test_replays_and_conflicts() -> None:
    main = Inventory.from_levels([("MUG-001", 10, 0)])
    shop = Inventory()
    net = Network({"MAIN": main, "SHOP": shop})
    first = Transfer("T-1", "MUG-001", 4, "MAIN", "SHOP")
    assert net.dispatch(first) is True
    assert net.dispatch(Transfer("T-1", "MUG-001", 4, "MAIN", "SHOP")) is False
    assert main.snapshot() == {"MUG-001": (6, 0)} and net.in_transit("MUG-001") == 4
    for other in (
        Transfer("T-1", "MUG-001", 5, "MAIN", "SHOP"),
        Transfer("T-1", "MUG-001", 4, "SHOP", "MAIN"),
        Transfer("T-1", "TEA-010", 4, "MAIN", "SHOP"),
    ):
        with pytest.raises(StockroomError):
            net.dispatch(other)
    assert net.receive("T-1") is True and net.receive("T-1") is False
    with pytest.raises(StockroomError):
        net.cancel("T-1")
    assert net.dispatch(first) is False
    assert shop.snapshot() == {"MUG-001": (4, 0)} and main.snapshot() == {"MUG-001": (6, 0)}
    second = Transfer("T-2", "MUG-001", 6, "MAIN", "SHOP")
    assert net.dispatch(second) is True and main.available("MUG-001") == 0
    # an equal replay is answered even though the stock for it is gone now
    assert net.dispatch(second) is False
    assert net.cancel("T-2") is True and net.cancel("T-2") is False
    with pytest.raises(StockroomError):
        net.receive("T-2")
    assert net.dispatch(second) is False
    assert main.snapshot() == {"MUG-001": (6, 0)} and net.state("T-2") == "cancelled"
    for call in (net.receive, net.cancel, net.state):
        with pytest.raises(StockroomError):
            call("T-9")


def test_dispatch_refusals_change_nothing() -> None:
    main = Inventory.from_levels([("MUG-001", 10, 7)])
    shop = Inventory()
    net = Network({"MAIN": main, "SHOP": shop})
    refused: list[tuple[Transfer, type[Exception]]] = [
        (Transfer("T-1", "MUG-001", 0, "MAIN", "SHOP"), StockError),
        (Transfer("T-1", "MUG-001", 1, "MAIN", "BACK"), StockroomError),
        (Transfer("T-1", "MUG-001", 1, "BACK", "MAIN"), StockroomError),
        (Transfer("T-1", "MUG-001", 1, "MAIN", "MAIN"), StockroomError),
        (Transfer("T-1", "mug-001", 1, "MAIN", "SHOP"), ParseError),
        (Transfer("T-1", "MUG-001", 4, "MAIN", "SHOP"), StockError),
        (Transfer("T-1", "TEA-010", 1, "SHOP", "MAIN"), StockError),
    ]
    for transfer, error in refused:
        with pytest.raises(error):
            net.dispatch(transfer)
    assert main.snapshot() == {"MUG-001": (10, 7)} and shop.snapshot() == {}
    assert net.transfer_ids() == [] and net.in_transit("MUG-001") == 0
    assert net.dispatch(Transfer("T-1", "MUG-001", 3, "MAIN", "SHOP")) is True
    assert main.snapshot() == {"MUG-001": (7, 7)}


def test_apply_is_all_or_nothing() -> None:
    main = Inventory.from_levels([("MUG-001", 10, 0), ("TEA-010", 3, 0)])
    shop = Inventory()
    net = Network({"MAIN": main, "SHOP": shop})
    one = Transfer("T-1", "MUG-001", 4, "MAIN", "SHOP")
    two = Transfer("T-2", "TEA-010", 3, "MAIN", "SHOP")
    assert net.apply([one, ("receive", "T-1"), two]) == [True, True, True]
    before = (main.snapshot(), shop.snapshot())
    failing = [
        ("cancel", "T-2"),
        one,
        Transfer("T-3", "MUG-001", 2, "SHOP", "MAIN"),
        ("receive", "T-3"),
        ("receive", "T-1"),
        ("cancel", "T-1"),
    ]
    with pytest.raises(StockroomError):
        net.apply(iter(failing))
    assert (main.snapshot(), shop.snapshot()) == before
    assert net.transfer_ids() == ["T-1", "T-2"]
    assert net.state("T-1") == "received" and net.state("T-2") == "in_transit"
    assert net.in_transit("TEA-010") == 3
    with pytest.raises(ParseError):
        net.apply([("cancel", "T-2"), ("ship", "T-2")])
    assert net.state("T-2") == "in_transit" and main.snapshot() == before[0]
    assert net.apply([("cancel", "T-2"), ("cancel", "T-2"), one]) == [True, False, False]
    assert main.snapshot() == {"MUG-001": (6, 0), "TEA-010": (3, 0)}
    assert net.apply([]) == []


def test_parse_events() -> None:
    text = (
        "# moves\n"
        "\n"
        "dispatch T-1 MUG-001 4 MAIN SHOP\r\n"
        "  receive\tT-1  \n"
        "   # indented comment\n"
        "cancel T-2\n"
        "dispatch T-3 mug-1 0 A A\n"
    )
    assert parse_events(text) == [
        Transfer("T-1", "MUG-001", 4, "MAIN", "SHOP"),
        ("receive", "T-1"),
        ("cancel", "T-2"),
        Transfer("T-3", "mug-1", 0, "A", "A"),
    ]
    assert parse_events("") == [] and parse_events("# nothing\n\n") == []
    for bad, number in (
        ("dispatch T-1 MUG-001 4 MAIN", 1),
        ("receive T-1\r\n\r\nreceive", 3),
        ("Dispatch T-1 MUG-001 4 MAIN SHOP", 1),
        ("cancel T-1\ndispatch T-1 MUG-001 4.0 MAIN SHOP", 2),
        ("dispatch T-1 MUG-001 -4 MAIN SHOP", 1),
        ("dispatch T-1 MUG-001 ٤ MAIN SHOP", 1),
        ("# a\n# b\nreceive T-1 T-2", 3),
        ("move T-1", 1),
    ):
        with pytest.raises(ParseError, match=rf"^line {number}:"):
            parse_events(bad)


def test_read_network() -> None:
    text = (
        "location,sku,on_hand,reserved\n"
        "MAIN,MUG-001,10,2\n"
        " SHOP , MUG-001 , 1 , 0 \n"
        ",,,\n"
        "MAIN,TEA-010,5,0\n"
    )
    found = read_network(text)
    assert sorted(found) == ["MAIN", "SHOP"]
    assert found["MAIN"].snapshot() == {"MUG-001": (10, 2), "TEA-010": (5, 0)}
    assert found["SHOP"].snapshot() == {"MUG-001": (1, 0)}
    assert read_network("location,sku,on_hand,reserved\n") == {}
    header = "location,sku,on_hand,reserved\n"
    for bad in (
        "",
        "place,sku,on_hand,reserved\nMAIN,MUG-001,1,0\n",
        header + "MAIN,MUG-001,1\n",
        header + "MAIN,MUG-001,x,0\n",
        header + "MAIN,MUG-001,1,0\nSHOP,MUG-001,1,0\nMAIN,MUG-001,2,0\n",
        header + ",MUG-001,1,0\n",
        header + "MAIN,MUG-001,1,2\n",
        header + "MAIN,mug-001,1,0\n",
    ):
        with pytest.raises(StockroomError):
            read_network(bad)


def test_random_events_keep_stock_together() -> None:
    rng = random.Random(9090)
    skus = ["MUG-001", "TEA-010"]
    for _ in range(50):
        names = ["MAIN", "SHOP", "BACK"][: rng.randrange(2, 4)]
        levels: dict[tuple[str, str], list[int]] = {}
        for name in names:
            for sku in skus:
                on_hand = rng.randrange(0, 9)
                levels[(name, sku)] = [on_hand, rng.randrange(0, on_hand + 1)]
        invs = {
            name: Inventory.from_levels([(sku, *levels[(name, sku)]) for sku in skus])
            for name in names
        }
        net = Network(invs)
        start = {sku: sum(levels[(name, sku)][0] for name in names) for sku in skus}
        known: dict[str, tuple[Transfer, str]] = {}
        for _ in range(25):
            batch: list[Transfer | tuple[str, str]] = []
            for _ in range(rng.choice([1, 1, 2, 3])):
                pick = rng.random()
                tid = f"T-{rng.randrange(1, 9)}"
                if pick < 0.5:
                    if tid in known and rng.random() < 0.5:
                        batch.append(known[tid][0])
                        continue
                    source = rng.choice(names)
                    target = rng.choice([*names, "NOWHERE"] if rng.random() < 0.1 else names)
                    batch.append(
                        Transfer(tid, rng.choice(skus), rng.randrange(1, 6), source, target)
                    )
                else:
                    batch.append(("receive" if pick < 0.8 else "cancel", tid))
            t_levels = {k: list(v) for k, v in levels.items()}
            t_known = dict(known)
            results: list[bool] = []
            error: type[Exception] | None = None
            for event in batch:
                if isinstance(event, Transfer):
                    old = t_known.get(event.transfer_id)
                    if old is not None:
                        if old[0] == event:
                            results.append(False)
                            continue
                        error = StockroomError
                        break
                    if (
                        event.target not in names
                        or event.source not in names
                        or event.source == event.target
                    ):
                        error = StockroomError
                        break
                    on_hand, reserved = t_levels[(event.source, event.sku)]
                    if event.quantity > on_hand - reserved:
                        error = StockError
                        break
                    t_levels[(event.source, event.sku)][0] -= event.quantity
                    t_known[event.transfer_id] = (event, "in_transit")
                    results.append(True)
                    continue
                kind, tid = event
                if tid not in t_known:
                    error = StockroomError
                    break
                transfer, state = t_known[tid]
                done = "received" if kind == "receive" else "cancelled"
                if state == done:
                    results.append(False)
                elif state != "in_transit":
                    error = StockroomError
                    break
                else:
                    place = transfer.target if kind == "receive" else transfer.source
                    t_levels[(place, transfer.sku)][0] += transfer.quantity
                    t_known[tid] = (transfer, done)
                    results.append(True)
            single = len(batch) == 1 and rng.random() < 0.5
            if error is not None:
                with pytest.raises(error):
                    if single:
                        event = batch[0]
                        if isinstance(event, Transfer):
                            net.dispatch(event)
                        elif event[0] == "receive":
                            net.receive(event[1])
                        else:
                            net.cancel(event[1])
                    else:
                        net.apply(batch)
            else:
                if single:
                    event = batch[0]
                    if isinstance(event, Transfer):
                        got = [net.dispatch(event)]
                    elif event[0] == "receive":
                        got = [net.receive(event[1])]
                    else:
                        got = [net.cancel(event[1])]
                else:
                    got = net.apply(batch)
                assert got == results
                levels, known = t_levels, t_known
            for name in names:
                assert invs[name].snapshot() == {
                    sku: (levels[(name, sku)][0], levels[(name, sku)][1]) for sku in skus
                }
            assert net.transfer_ids() == sorted(known)
            for tid, (_, state) in known.items():
                assert net.state(tid) == state
            for sku in skus:
                moving = sum(
                    t.quantity
                    for t, state in known.values()
                    if t.sku == sku and state == "in_transit"
                )
                assert net.in_transit(sku) == moving
                assert net.total(sku) == start[sku]
                assert all(invs[name].available(sku) >= 0 for name in names)


def test_cli_transfers_table(tmp_path: Path) -> None:
    stock = tmp_path / "network.csv"
    stock.write_text(NETWORK)
    events = tmp_path / "events.txt"
    events.write_text(
        "# morning moves\n"
        "dispatch T-1 MUG-001 4 MAIN SHOP\n"
        "dispatch T-2 TEA-010 5 MAIN SHOP\n"
        "receive T-1\n"
        "dispatch T-3 MUG-001 2 SHOP BACK\n"
        "cancel T-2\n"
        "dispatch T-4 MUG-001 1 MAIN BACK\n"
        "receive T-1\n"
    )
    done = run("transfers", "--stock", str(stock), "--events", str(events), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "SKU           BACK    MAIN    SHOP TRANSIT   TOTAL",
        "MUG-001          0       5       3       3      11",
        "NB-200           7       0       0       0       7",
        "TEA-010          0       5       0       0       5",
        "TOTAL            7      10       3       3      23",
        "1 received, 1 cancelled, 2 in transit",
    ]
    events.write_text("")
    done = run("transfers", "--stock", str(stock), "--events", str(events), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "SKU           BACK    MAIN    SHOP TRANSIT   TOTAL",
        "MUG-001          0      10       1       0      11",
        "NB-200           7       0       0       0       7",
        "TEA-010          0       5       0       0       5",
        "TOTAL            7      15       1       0      23",
        "0 received, 0 cancelled, 0 in transit",
    ]


@pytest.mark.parametrize(
    ("text", "start"),
    [
        (
            "dispatch T-1 MUG-001 4 MAIN SHOP\ndispatch T-9 NB-200 1 BACK MAIN\n",
            "stockroom: error: ",
        ),
        ("dispatch T-1 MUG-001 4 MAIN SHOP\nreceive T-1\ncancel T-1\n", "stockroom: error: "),
        ("dispatch T-1 MUG-001 9 MAIN SHOP\n", "stockroom: error: "),
        ("receive T-1\n", "stockroom: error: "),
        ("dispatch T-1 MUG-001 4 MAIN SHOP\nship T-1\n", "stockroom: error: line 2:"),
    ],
)
def test_cli_errors(tmp_path: Path, text: str, start: str) -> None:
    stock = tmp_path / "network.csv"
    stock.write_text(NETWORK)
    events = tmp_path / "events.txt"
    events.write_text(text)
    done = run("transfers", "--stock", str(stock), "--events", str(events), cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith(start)
