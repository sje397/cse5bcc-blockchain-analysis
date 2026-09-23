#!/usr/bin/env python3
"""Probe: what a mid-PoW sync actually does, and why the node refused the longer chain.

WHY THIS EXISTS
---------------
The submitted arm `race_fixed` (exp_race) recorded:
  * a sync issued 3.0 s into a 3.83 s proof of work, returning after 0.778 s with
    "Our blockchain is the latest" (the node REFUSED the peer's 5-block chain while
    holding 1, then 2, of its own), and
  * a post-race chain that passes the independent link check (2 blocks).
That single number carries every possible reading, so the arm as published cannot say
which is true:
  (a) DEFERRAL   - mine_block() holds blockchain.lock across PoW + append_block
                   (node.py:216) and update_blockchain() takes the same lock
                   (node.py:156): the sync waited out the remainder of the PoW, then
                   evaluated the FINISHED chain, so nothing interleaved.
  (b) INTERLEAVE - the sync ran immediately against the half-built chain and 0.778 s is
                   the operation's own cost.
and two candidate causes for the refusal:
  (i) TARGET     - the incoming blocks fail valid_chain against the receiver's own
                   (harder) target: Finding 3's asymmetry.
  (ii) RACE      - the concurrent sync itself was mishandled.

WHAT IS MEASURED HERE, AND THE CONTROLS THAT DO THE WORK
--------------------------------------------------------
1. Timing. The same arm at three sleeps. If (a), sync_wall tracks the residual PoW;
   if (b), sync_wall is flat. Both are compared against a control sync with NO mining
   in flight - the operation's own cost. Without that control a large sync_wall could
   be attributed to either reading, so the control is what makes the number mean
   anything.
2. The refusal. A sync with NO mining in flight, from a node holding fewer blocks than
   the peer (negative control: slower target on the subject). If that ALSO refuses, the
   race is not the cause - (i) is.
3. The positive control that stops the negative one being vacuous: a node at the SAME
   target as the peer, holding fewer blocks, must ADOPT that peer's chain. Without it,
   "refused" could mean the node never adopts anything (broken gossip, unreachable
   peer), and the refusal would explain nothing.

Writes probe_lock_deferral.json beside itself. Read-only with respect to node.py,
harness.py and the submitted results/.
"""
import json
import math
import threading
import time
from pathlib import Path

from harness import NODE, Node, stop_all

HERE = Path(__file__).resolve().parent
SLEEPS = (0.5, 1.5, 2.5)
DIFF_SLOW = "000000"    # ~4M attempts: buys the mid-PoW window
DIFF_FAST = "00"


def _pair(subject_diff, peer_diff, port, label):
    """Start a subject node and a peer, register them both ways, peer mines 4 blocks."""
    subject = Node(f"lockprobe_subject_{label}", port, script=NODE,
                   difficulty=subject_diff)
    peer = Node(f"lockprobe_peer_{label}", port + 1, script=NODE, difficulty=peer_diff)
    for n in (subject, peer):
        n.start(timeout=900)
    for n in (subject, peer):
        n.register([o for o in (subject, peer) if o is not n])
    for _ in range(4):
        peer.mine()
    return subject, peer


def no_mining_case(port, subject_diff, peer_diff, label, expect):
    """The two controls: a sync with nothing racing. expect is what SHOULD happen."""
    subject, peer = _pair(subject_diff, peer_diff, port, label)
    try:
        peer_chain = peer.chain()["length"]
        before = subject.chain()["length"]
        wall, _status, data = subject.raw_get("/nodes/sync", timeout=300)
        after = subject.chain()["length"]
        adopted = after == peer_chain and before < peer_chain
        return {
            "case": f"{label} (no mining in flight; expect {expect})",
            "sleep_s": None, "subject_difficulty": subject_diff,
            "peer_difficulty": peer_diff, "peer_chain": peer_chain,
            "sync_wall_s": round(wall, 4), "message": (data or {}).get("message"),
            "chain_len_at_issue": before, "chain_len_after": after,
            "adopted": adopted, "mine_wall_s": None, "residual_pow_s": None,
            "window_open": None,
        }
    finally:
        stop_all((subject, peer))


def mining_case(sleep_s, port, label):
    """A sync issued while the subject is inside proof of work."""
    subject, peer = _pair(DIFF_SLOW, DIFF_FAST, port, label)
    try:
        peer_chain = peer.chain()["length"]
        rec = {}
        th = threading.Thread(target=lambda: rec.update(mine=subject.mine(timeout=900)),
                              daemon=True)
        t_thread = time.time()
        th.start()
        time.sleep(sleep_s)
        t_issue = time.time()
        len_at_issue = subject.chain()["length"]   # one loopback read, ~2 ms
        wall, _status, data = subject.raw_get("/nodes/sync", timeout=300)
        len_after = subject.chain()["length"]
        th.join(timeout=900)
        mine_wall = rec.get("mine", {}).get("wall_time_s")
        return {
            "case": f"mining in flight, sync issued {sleep_s}s in",
            "sleep_s": sleep_s, "subject_difficulty": DIFF_SLOW,
            "peer_difficulty": DIFF_FAST, "peer_chain": peer_chain,
            "sync_wall_s": round(wall, 4), "message": (data or {}).get("message"),
            "chain_len_at_issue": len_at_issue, "chain_len_after": len_after,
            "adopted": len_after == peer_chain,
            "mine_wall_s": mine_wall,
            "residual_pow_s": (round(mine_wall - (t_issue - t_thread), 4)
                               if mine_wall is not None else None),
            # the append must not have happened yet and the mine must still be running,
            # or there was no window and the row is not evidence about the race
            "window_open": bool(mine_wall is not None and len_at_issue == 1),
        }
    finally:
        stop_all((subject, peer))


def main():
    rows, port = [], 5300
    rows.append(no_mining_case(port, DIFF_FAST, DIFF_FAST, "positive control",
                               "ADOPT the longer chain")); port += 2
    rows.append(no_mining_case(port, DIFF_SLOW, DIFF_FAST, "negative control",
                               "REFUSE it (target mismatch)")); port += 2
    for sleep_s in SLEEPS:
        rows.append(mining_case(sleep_s, port, f"sleep{sleep_s}")); port += 2

    pos, neg = rows[0], rows[1]
    control = neg["sync_wall_s"]
    treat = [r for r in rows[2:] if r["window_open"]]

    print("\n=== probe: lock deferral, and what the refusal is ===")
    print(f"  positive control  same target, shorter chain -> len "
          f"{pos['chain_len_at_issue']}->{pos['chain_len_after']}  "
          f"{'ADOPTED' if pos['adopted'] else 'REFUSED'}  {pos['message']!r}")
    print(f"  negative control  harder target, shorter chain -> len "
          f"{neg['chain_len_at_issue']}->{neg['chain_len_after']}  "
          f"{'ADOPTED' if neg['adopted'] else 'REFUSED'}  {neg['message']!r}")
    print(f"  control sync cost (no mining): {control:.4f}s")
    for r in rows[2:]:
        print(f"  sleep {r['sleep_s']:>4}s  mine {r['mine_wall_s']}s  "
              f"residual {r['residual_pow_s']}s  sync_wall {r['sync_wall_s']}s  "
              f"len {r['chain_len_at_issue']}->{r['chain_len_after']}  "
              f"{'window open' if r['window_open'] else 'NO WINDOW'}")

    notes = []
    # the refusal: decided by the controls, not by the in-flight rows
    if not pos["adopted"]:
        refusal = "CONTROLS DISAGREE - positive control also refused; refusal unexplained"
    elif neg["adopted"]:
        refusal = "NEGATIVE CONTROL ADOPTED - the target is not what blocks adoption"
    else:
        refusal = "TARGET MISMATCH - same target adopts, harder target refuses, no mining"

    if not treat:
        timing = "no row had a window open - the mine finished before the sync landed"
    else:
        ratios = [r["sync_wall_s"] / control for r in treat]
        rel = [abs(r["sync_wall_s"] - r["residual_pow_s"]) / r["residual_pow_s"]
               for r in treat]
        # relative, because the absolute gap includes thread-start -> handler-entry
        # latency, which is load-dependent (0.06 s in the submitted run, 0.3 s under
        # this probe's load) and must not be mistaken for a failure to track
        tracks = all(x <= 0.05 for x in rel)
        if tracks and all(x > 100 for x in ratios):
            # Reported as a BOUND, so both figures round away from the measurement:
            # rounding a bound to nearest can state a tighter bound than was measured
            # (max(rel) = 1.919% printed as "1.9%"; min(ratios) = 1,435.6 printed as
            # "1,436x"). Ceil the upper, floor the lower.
            timing = (f"DEFERRAL - sync_wall tracks the residual PoW "
                      f"(within {math.ceil(max(rel) * 10000) / 100:.2f}%) at "
                      f"{math.floor(min(ratios)):,}-{math.ceil(max(ratios)):,}x "
                      f"the no-mining control")
        elif all(abs(r["sync_wall_s"] - control) <= max(0.05, 10 * control)
                 for r in treat):
            timing = "INTERLEAVE - sync_wall flat and equal to the control"
        else:
            timing = "UNRESOLVED"
            notes = [f"tracks_within_5pct={tracks}",
                     f"ratios={[round(x, 1) for x in ratios]}",
                     f"rel_gaps={[round(x, 4) for x in rel]}"]

    out = {"positive_control": pos, "negative_control": neg, "control_sync_wall_s": control,
           "rows": rows, "timing_verdict": timing, "refusal_verdict": refusal,
           "notes": notes}
    (HERE / "probe_lock_deferral.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"\n  TIMING : {timing}")
    print(f"  REFUSAL: {refusal}")
    for n in notes:
        print(f"  ({n})")
    print("  wrote probe_lock_deferral.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
