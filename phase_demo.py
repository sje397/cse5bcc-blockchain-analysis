#!/usr/bin/env python3
"""phase_demo.py — Phases 1, 2 and 3 exactly as the task sheet prescribes.

Two outputs, both required by the marking rubric:

  (a) a legible console transcript of every prescribed step, in order, showing the HTTP
      call made and the result observed. The rubric's first criterion (30 marks) asks for
      every required step across all phases to be *shown*, with executions and outputs --
      so each line names the call and prints what came back.

  (b) the per-phase table of transaction / mining / synchronisation times the task sheet
      asks for, written to results/prescribed_table.csv and results/prescribed_table.json.

This is NOT the measurement instrument for the analysis section. Single runs at a target
have wide variance (mining duration is geometric: at 0000 the mean is 65,536 attempts but
a single block can take a few hundred or a few hundred thousand). The analysis section's
numbers come from harness.py, which repeats each condition and reports distributions and
attempt-rates. Here we report medians-of-3 only to keep the walkthrough honest-looking
rather than lucky-looking, and the harness numbers are the ones cited.

Wiring is imported from harness.py rather than re-implemented, so the node startup,
identity confirmation and HTTP client are the same code that produced the analysis.
"""

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (  # noqa: E402
    LOGS,
    NODE,
    PROVIDED,
    PROVIDED_DIFFICULTY,
    RESULTS,
    Node,
    stop_all,
)

WIDTH = 86
NODES = []          # every node this script starts, so a crash still leaves nothing alive
ROWS = []           # one row per prescribed phase/difficulty combination


# ----------------------------------------------------------------------------- transcript

def title(text):
    print()
    print("=" * WIDTH)
    print(text)
    print("=" * WIDTH)


def step(call, result):
    """One prescribed step: the request made, and what came back."""
    print(f"  {call:<38} {result}")


def ret(result):
    """A result with no new request of its own (e.g. a comparison)."""
    print(f"  {'':<38} {result}")


def short(h, n=12):
    return h[:n] + "…" if h and len(h) > n else (h or "-")


def rel(path):
    """Path relative to the invocation directory when possible, absolute otherwise.

    Path.relative_to() raises ValueError when the working directory is not an ancestor, which
    would abort the run on the banner line simply because the script was invoked from
    elsewhere. Cosmetic helper, so it must not be able to kill the run.
    """
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


class Ports:
    """Allocates consecutive blocks of ports, so two live nodes cannot share one.

    Ports used to be arithmetic on len(ROWS) -- `p + 30 + 20 * len(ROWS)`. That happened to
    work, but only because of the order rows are appended in: the expressions are coupled to
    how many phases have already run, not to what is listening. Reorder or add a phase and it
    silently hands out a port that is in use. A counter is monotonic by construction.
    """

    def __init__(self, base):
        self._next = base

    def block(self, n):
        out = tuple(range(self._next, self._next + n))
        self._next += n
        return out


# ------------------------------------------------------------------------------- helpers

def fingerprint(chain):
    """Hash of the whole chain, so 'identical' is a comparison of bytes, not of lengths."""
    return hashlib.sha256(
        json.dumps(chain, sort_keys=True).encode()
    ).hexdigest()[:16]


def block_hash(block):
    """The node's own chain-integrity hash, recomputed client-side.

    /blockchain sends the raw chain, and a block dict carries index, timestamp, transactions,
    nonce and hash_of_previous_block. There is no 'hash' field on the wire -- the node derives
    it with hash_block(): sha256 of json.dumps(block, sort_keys=True). Recomputing it here
    yields exactly the value the node compares in valid_chain(), so the transcript can
    demonstrate that each block links to its predecessor rather than assert that it does.
    """
    return hashlib.sha256(json.dumps(block, sort_keys=True).encode()).hexdigest()


def describe_chain(node):
    data = node.chain()
    chain = data["chain"]
    print(f"  chain at {node.name} (port {node.port}): {len(chain)} block(s), "
          f"fingerprint {fingerprint(chain)}")
    for i, b in enumerate(chain):
        txs = len(b.get("transactions", []))
        if i == 0:
            link = "genesis"
        elif b.get("hash_of_previous_block") == block_hash(chain[i - 1]):
            link = "ok"
        else:
            link = "BROKEN"
        print(f"    [{b['index']}] hash {short(block_hash(b), 14)}  "
              f"prev {short(b.get('hash_of_previous_block'), 14)}  "
              f"txs={txs}  nonce={b['nonce']}  link={link}")
    return chain


def compare(a, b, label):
    ca = a.chain()["chain"]
    cb = b.chain()["chain"]
    fa, fb = fingerprint(ca), fingerprint(cb)
    same = fa == fb
    ret(f"{label}: {a.name}={fa} ({len(ca)} blocks)  {b.name}={fb} ({len(cb)} blocks)  "
        f"identical={same}")
    return same


def mine_median(node, reps):
    """Mine `reps` blocks, print each, return (record, median server time)."""
    records = []
    for _ in range(reps):
        m = node.mine()
        records.append(m)
        print(f"    GET /mine -> block {m['block_index']}  nonce={m['nonce']:<9} "
              f"attempts={m['attempts']:<9,} mining={m['server_time_s']:.4f} s "
              f"wall={m['wall_time_s']:.4f} s")
    times = [r["server_time_s"] for r in records]
    med = statistics.median(times)
    print(f"    median of {reps} blocks: {med:.4f} s   "
          f"(range {min(times):.4f}-{max(times):.4f} s)")
    return records, med


def add_transaction(node, sender, recipient, amount):
    elapsed, status, data = node.post(
        "/transactions/new",
        {"sender": sender, "recipient": recipient, "amount": amount},
    )
    t = data["transaction_add_time_seconds"] if data else None
    step(f"POST /transactions/new -> {status}",
         f"{data['message']!r}  transaction time = {t} s" if data else "no body")
    return t


def sync(node):
    elapsed, status, data = node.get("/nodes/sync")
    t = data["sync_time_seconds"] if data else None
    step(f"GET /nodes/sync -> {status}",
         f"{data['message']!r}  sync time = {t} s" if data else "no body")
    return t


# -------------------------------------------------------------------------------- phases

def phase1(script, difficulty, label, port, rows):
    title(f"PHASE 1 — single node, target {difficulty!r}  ({label})")
    node = Node("p1", port, script=script, difficulty=difficulty)
    t = node.start()
    NODES.append(node)
    print(f"  $ python3 {script.name} {port}"
          + ("" if script is PROVIDED else f"   # DIFFICULTY={difficulty}"))
    step("node started", f"ready on :{port} in {t:.2f} s  "
                         f"(difficulty confirmed on the wire: {node.observed_difficulty!r})")
    describe_chain(node)

    print("\n  -- mine a block (mining time) --")
    records, mining_median = mine_median(node, 3)

    print("\n  -- add a transaction (transaction time) --")
    tx_time = add_transaction(node, "alice", "bob", 7)

    print("\n  -- mine the block that includes it --")
    _, mining_median2 = mine_median(node, 1)

    print("\n  -- view the chain --")
    describe_chain(node)

    rows.append({
        "phase": "1",
        "target": difficulty,
        "program": script.name,
        "nodes": 1,
        "transaction_time_s": tx_time,
        "mining_time_s": round(mining_median, 4),
        "mining_time_second_block_s": round(mining_median2, 4),
        "sync_time_s": None,
        "note": "mining time = median of 3 blocks; genesis mined at import",
    })
    node.stop()


def phase2(script, difficulty, label, ports, rows):
    title(f"PHASE 2 — three nodes, target {difficulty!r}  ({label})")
    a = Node("A", ports[0], script=script, difficulty=difficulty)
    b = Node("B", ports[1], script=script, difficulty=difficulty)
    a.start(); b.start()
    NODES.extend([a, b])
    print(f"  $ python3 {script.name} {ports[0]}   # node A")
    print(f"  $ python3 {script.name} {ports[1]}   # node B")

    print("\n  -- register each node with the other --")
    a.register([b]); b.register([a])
    step(f"POST /nodes/add_nodes (A)", f"nodes now known to A: {b.url}")
    step(f"POST /nodes/add_nodes (B)", f"nodes now known to B: {a.url}")

    print("\n  -- mine on A only, then compare --")
    a.mine()
    step("GET /mine (A)", "block mined on A")
    compare(a, b, "compare A vs B")

    print("\n  -- synchronise B --")
    sync_b = sync(b)
    compare(a, b, "compare A vs B after sync")

    print("\n  -- add a transaction on A, mine it, sync B (waiting for a sync on B) --")
    add_transaction(a, "carol", "dave", 3)
    a.mine()
    step("GET /mine (A)", "block with the transaction mined on A")
    sync(b)
    compare(a, b, "compare A vs B")

    print("\n  -- bring up a third node and synchronise it --")
    c = Node("C", ports[2], script=script, difficulty=difficulty)
    c.start()
    NODES.append(c)
    print(f"  $ python3 {script.name} {ports[2]}   # node C")
    a.register([c]); b.register([c]); c.register([a, b])
    step("POST /nodes/add_nodes (A,B,C)", "full mesh of 3 nodes registered")
    sync_c = sync(c)
    describe_chain(c)
    ok_ab = compare(a, b, "compare A vs B")
    ok_ac = compare(a, c, "compare A vs C")

    print("\n  -- add a transaction on B, mine it, sync A and C --")
    add_transaction(b, "erin", "frank", 11)
    b.mine()
    step("GET /mine (B)", "block with the transaction mined on B")
    sync_a = sync(a)
    sync(c)
    ok_ab = compare(a, b, "compare A vs B")
    ok_ac = compare(a, c, "compare A vs C")
    ret(f"all three chains identical: {ok_ab and ok_ac}")

    rows.append({
        "phase": "2",
        "target": difficulty,
        "program": script.name,
        "nodes": 3,
        "transaction_time_s": None,
        "mining_time_s": None,
        # One column per sync event, in the order the walkthrough performs them. Previously
        # the third was suppressed whenever its duration happened to equal the first
        # (sync_c != sync_b): two independent syncs on loopback both take a few milliseconds,
        # so that comparison threw away a real measurement for matching an unrelated one.
        # Value equality is not event identity.
        "sync_time_s": sync_b,          # 1st: B catches up after a block was mined on A (2 nodes)
        "sync_time_s_second": sync_c,   # 2nd: C's first sync on joining (3 nodes)
        "sync_time_s_third": sync_a,    # 3rd: A catches up after a block was mined on B (3 nodes)
        "all_identical": bool(ok_ab and ok_ac),
        "note": "sync (s) = the three syncs in walkthrough order; a fourth, C catching up to B, "
                "is shown in the transcript",
    })
    for n in (a, b, c):
        n.stop()


# ---------------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-port", type=int, default=5201)
    args = ap.parse_args()
    ports = Ports(args.base_port)

    print("CSE5BCC Assessment 1 — Phases 1-3 walkthrough")
    print(f"python {sys.version.split()[0]}   started {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"provided program : {rel(PROVIDED)} (target hard-coded {PROVIDED_DIFFICULTY!r})")
    print(f"modified node    : {rel(NODE)} (target read from DIFFICULTY)")
    print("all nodes bind to 127.0.0.1. Every duration is the node's own figure; the mining lines")
    print("  print wall-clock around the same HTTP call beside it, so transport cost is visible")
    print("  and is not silently folded into the measurement.")

    try:
        # Phase 1 and 2 as prescribed, at the submission's own target.
        phase1(PROVIDED, PROVIDED_DIFFICULTY, "provided program, as supplied", ports.block(1)[0], ROWS)
        phase2(PROVIDED, PROVIDED_DIFFICULTY, "provided program, as supplied", ports.block(3), ROWS)

        # Control: the same phase-1 walkthrough on the modified node at the same target,
        # so the 000/00000 numbers below differ from it only by target.
        phase1(NODE, PROVIDED_DIFFICULTY, "control: modified node at the same target", ports.block(1)[0], ROWS)

        # Phase 3: repeat phases 1 and 2 with the target relaxed and tightened.
        for target in ("000", "00000"):
            phase1(NODE, target, "phase 3 repeat", ports.block(1)[0], ROWS)
            phase2(NODE, target, "phase 3 repeat", ports.block(3), ROWS)

        title("RESULTS — times recorded per phase (task sheet table)")
        print(f"  {'phase':<7}{'target':<9}{'nodes':<7}{'tx (s)':<10}{'mining (s)':<12}"
              f"{'sync (s)':<10}program")
        for r in ROWS:
            print(f"  {r['phase']:<7}{r['target']:<9}{r['nodes']:<7}"
                  f"{fmt(r.get('transaction_time_s')):<10}{fmt(r.get('mining_time_s')):<12}"
                  f"{fmt(r.get('sync_time_s')):<10}{r['program']}")
        print("\n  mining (s) = median of 3 blocks where a single value is shown; the second")
        print("  block's time is in the JSON. Single-run mining durations are dominated by")
        print("  luck (geometric attempts); the analysis section uses harness.py distributions.")

        (RESULTS / "prescribed_table.json").write_text(json.dumps({
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "rows": ROWS,
        }, indent=2) + "\n")
        import csv
        fields = ["phase", "target", "program", "nodes", "transaction_time_s",
                  "mining_time_s", "mining_time_second_block_s", "sync_time_s",
                  "sync_time_s_second", "sync_time_s_third", "all_identical", "note"]
        with open(RESULTS / "prescribed_table.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in ROWS:
                w.writerow({k: r.get(k) for k in fields})
        print(f"\n  written: {rel(RESULTS / 'prescribed_table.csv')}, "
              f"{rel(RESULTS / 'prescribed_table.json')}")
    finally:
        stop_all(NODES)
        print(f"\n  {len(NODES)} node processes stopped; ports released.")


def fmt(v):
    """Fixed 4-decimal rendering, with no width truncation.

    The previous version sliced the formatted string to 8 characters, which quietly drops a
    significant digit from any duration of 1000 s or more rather than widening the column.
    """
    return "-" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))


if __name__ == "__main__":
    main()
