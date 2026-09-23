#!/usr/bin/env python3
"""Two independent findings, demonstrated rather than asserted.

FINDING 1 (measurement).  The task sheet's Phase 1 protocol reports "mining time =
median of 3 blocks".  That estimator is biased low BY CONSTRUCTION (E = 5/6 of the
mean) and at n = 3 the sampling noise dominates.  This is a property of the
protocol, not a defect in any code, and it is why the prescribed run shows
0.2403 s and then 1.5543 s on the same target minutes apart.  Demonstrated here by
simulating the estimator on the 127 difficulty-5 blocks actually measured.

FINDING 2 (correctness).  In the provided scaffold the proof-of-work preimage
excludes the timestamp while the block hash includes it:

    valid_proof  (line 37):  sha256(f"{index}{prev}{transactions}{nonce}")   -> no timestamp
    hash_block   (line 17):  sha256(json.dumps(block, sort_keys=True))       -> includes timestamp

So two nodes that perform identical work (same target, same previous hash, same
transactions) find the SAME nonce and still produce DIFFERENT block hashes, because
each stamped its own time().  Genesis is the clean case: all four PoW inputs are
identical across nodes by construction, so there is no content difference to blame.

This is shown twice:
  (a) forensics over the real capture log: group genesis records by nonce, count
      distinct block hashes per nonce;
  (b) a live reproduction against the provided class, including a positive control
      that copies one node's timestamp onto another's block and watches the two
      hashes become equal -- which identifies the cause, not just the symptom.

FALSIFICATION.  Every claim here is paired with an input that must make it fail:
  * the nonce must be identical across the three live nodes (if not, the cause
    is content, not the timestamp);
  * rewriting only the timestamp must leave valid_proof True;
  * changing any field the preimage DOES cover must make valid_proof False
    (otherwise valid_proof is vacuous and the timestamp result means nothing);
  * the log parser's genesis count must equal an independent grep count.

Run:  ./.venv/bin/python rootcause_demo.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import statistics
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
RESULTS = BASE / "results"
LOG = BASE / "logs" / "phase_demo_capture.log"

# ---------------------------------------------------------------- load provided
spec = importlib.util.spec_from_file_location(
    "provided_blockchain", BASE / "provided" / "blockchain_assessment.py")
provided = importlib.util.module_from_spec(spec)
sys.modules["provided_blockchain"] = provided
spec.loader.exec_module(provided)

report: list[str] = []
out: dict = {}


def say(line: str = "") -> None:
    report.append(line)
    print(line)


def head(title: str) -> None:
    say()
    say("=" * 78)
    say(title)
    say("=" * 78)


# =========================================================== FINDING 2, part (b)
head("FINDING 2b  live reproduction against the provided class")

Blockchain = provided.Blockchain
target = Blockchain.difficulty_target
say(f"difficulty_target = {target!r}  (so 16**4 = {16**4:,} attempts expected per block)")
say("")

# Three nodes, as the Phase 2 walkthrough starts them, but staggered so their
# genesis timestamps differ.  A sleep is used because time() has microsecond
# resolution and three constructions in a tight loop can share a timestamp --
# which would make this demonstration silently prove nothing.
nodes = []
for i in range(3):
    bc = Blockchain()
    genesis = bc.chain[0]
    nodes.append((f"node {'ABC'[i]}", genesis))
    time.sleep(0.05)

say("  node    index  nonce    timestamp             hash_block(genesis)")
for name, g in nodes:
    say(f"  {name}     {g['index']}      {g['nonce']:<8} {g['timestamp']:.6f}   "
        f"{Blockchain.hash_block(Blockchain, g)}")

nonces = {g["nonce"] for _, g in nodes}
hashes = {Blockchain.hash_block(Blockchain, g) for _, g in nodes}
prevs = {g["hash_of_previous_block"] for _, g in nodes}
txss = {json.dumps(g["transactions"]) for _, g in nodes}

say("")
say(f"  distinct nonces        : {len(nonces)}  {sorted(nonces)}")
say(f"  distinct prev hashes   : {len(prevs)}")
say(f"  distinct tx lists      : {len(txss)}")
say(f"  distinct block hashes  : {len(hashes)}")

assert len(prevs) == 1, "prev hashes must be identical for genesis"
assert len(txss) == 1, "transaction lists must be identical for genesis"
claim_nonce_identical = len(nonces) == 1
claim_hash_divergent = len(hashes) == 3
say("")
say(f"  CLAIM identical work      (1 nonce for 3 nodes) : {claim_nonce_identical}")
say(f"  CLAIM different identity  (3 hashes for 3 nodes): {claim_hash_divergent}")
assert claim_nonce_identical, "FALSIFIED: nodes disagreed on the nonce - content differs"
assert claim_hash_divergent, "FALSIFIED: hashes agreed - timestamps must be equal"

out["live"] = {
    "difficulty_target": target,
    "nonce_identical_across_nodes": claim_nonce_identical,
    "distinct_genesis_hashes": len(hashes),
    "genesis_nonce": sorted(nonces)[0],
}

# ---------------------------------------------- positive control: it IS the time
say("")
say("  POSITIVE CONTROL - is the timestamp the whole difference?")
say("  Copy node A's genesis **timestamp** onto node B's genesis, change nothing")
say("  else, and re-hash.")
a_name, a_g = nodes[0]
b_name, b_g = nodes[1]
h_a = Blockchain.hash_block(Blockchain, a_g)
h_b_before = Blockchain.hash_block(Blockchain, b_g)
b_copy = dict(b_g)
b_copy["timestamp"] = a_g["timestamp"]
h_b_after = Blockchain.hash_block(Blockchain, b_copy)
say(f"    A hash                    : {h_a}")
say(f"    B hash (own timestamp)    : {h_b_before}")
say(f"    B hash (A's timestamp)    : {h_b_after}")
say(f"    B now equals A            : {h_b_after == h_a}")
assert h_b_after == h_a, "FALSIFIED: the timestamp alone does not explain the divergence"
say("    -> the divergence is fully explained by the timestamp field, nothing else.")
out["live"]["timestamp_copy_reproduces_hash"] = (h_b_after == h_a)

# ------------------------------------- the timestamp is not bound by the proof
say("")
say("  CONSEQUENCE - is the timestamp authenticated by the proof-of-work?")
g = nodes[0][1]
fields = (g["index"], g["hash_of_previous_block"], g["transactions"], g["nonce"])
base_ok = Blockchain.valid_proof(Blockchain, *fields)
say(f"    valid_proof on the block as mined                 : {base_ok}")

for fake in (0.0, 1.0, 2_000_000_000.0, -1.0):
    tampered = dict(g)
    tampered["timestamp"] = fake
    ok = Blockchain.valid_proof(Blockchain, *fields)  # timestamp is not an input
    changed = Blockchain.hash_block(Blockchain, tampered) != Blockchain.hash_block(Blockchain, g)
    say(f"    timestamp := {fake:>14.1f}   valid_proof still {ok!s:<5} "
        f"and hash_block changed: {changed}")
    assert ok, "FALSIFIED: timestamp reached the preimage"
say("    -> a block's recorded time can be set to any value at all and the")
say("       proof-of-work still validates.  The PoW covers the ledger's contents")
say("       but not its clock; only the *link* into the next block notices.")

# ------------------------------- control: valid_proof must bind what it covers
say("")
say("  CONTROL - valid_proof must fail when a field it DOES cover is changed.")
say("  Without this, the result above would only show the check is vacuous.")
mutations = {
    "nonce + 1":      (g["index"], g["hash_of_previous_block"], g["transactions"], g["nonce"] + 1),
    "index + 1":      (g["index"] + 1, g["hash_of_previous_block"], g["transactions"], g["nonce"]),
    "prev hash + 'a'": (g["index"], g["hash_of_previous_block"] + "a", g["transactions"], g["nonce"]),
    "txs + [1]":      (g["index"], g["hash_of_previous_block"], g["transactions"] + [1], g["nonce"]),
}
all_rejected = True
for label, vals in mutations.items():
    ok = Blockchain.valid_proof(Blockchain, *vals)
    all_rejected &= not ok
    say(f"    {label:<16} -> valid_proof = {ok}")
assert all_rejected, "FALSIFIED: valid_proof accepted a mutated covered field"
say("    -> the check is not vacuous; it binds every field in the preimage.")
say("       The timestamp is absent from the preimage, not merely unchecked.")
out["live"]["valid_proof_binds_covered_fields"] = all_rejected

# =========================================================== FINDING 2, part (a)
head("FINDING 2a  forensics over the real capture log")

LINE = re.compile(
    r"\[(\d+)\]\s+hash\s+([0-9a-f]+)\u2026\s+prev\s+([0-9a-f]+)\u2026\s+"
    r"txs=(\d+)\s+nonce=(\d+)\s+link=(\w+)")

records = []
for raw in LOG.read_text(errors="replace").splitlines():
    m = LINE.search(raw)
    if m:
        records.append({
            "index": int(m.group(1)), "hash": m.group(2), "prev": m.group(3),
            "txs": int(m.group(4)), "nonce": int(m.group(5)), "link": m.group(6),
        })

genesis = [r for r in records if r["link"] == "genesis"]
say(f"  block listings parsed from the log : {len(records)}")
say(f"  of which genesis                  : {len(genesis)}")

# Independent count of the same thing, by a different method.  If the parser and
# grep disagree, the parser is the thing under test and its result is void.
grep_count = sum(1 for raw in LOG.read_text(errors="replace").splitlines()
                 if "link=genesis" in raw)
say(f"  independent grep count of the same: {grep_count}")
say(f"  parser agrees with grep           : {len(genesis) == grep_count}")
assert len(genesis) == grep_count, "FALSIFIED: parser under/over-counts genesis rows"

by_nonce: dict[int, set[str]] = {}
prev_by_nonce: dict[int, set[str]] = {}
for r in genesis:
    by_nonce.setdefault(r["nonce"], set()).add(r["hash"])
    prev_by_nonce.setdefault(r["nonce"], set()).add(r["prev"])

say("")
say("  genesis records, grouped by the nonce the node found:")
say("    nonce      distinct hashes   block hashes")
for nonce in sorted(by_nonce):
    hs = sorted(by_nonce[nonce])
    say(f"    {nonce:<10} {len(hs):<17} {', '.join(h[:12] + '…' for h in hs)}")

divergent = {n: h for n, h in by_nonce.items() if len(h) > 1}
say("")
say(f"  nonces producing more than one block hash : {len(divergent)} of {len(by_nonce)}")
say(f"  maximum hashes from a single nonce        : {max(len(h) for h in by_nonce.values())}")

# The same nonce appearing twice is only meaningful if the INPUTS were identical.
# For genesis they are: index 0, empty transaction list, and one fixed previous
# hash (the sha256 of the literal "genesis_block").  Verified, not assumed.
say("")
say("  why genesis is the clean case - are the inputs really identical?")
say(f"    distinct index values        : {len({r['index'] for r in genesis})}")
say(f"    distinct transaction counts  : {len({r['txs'] for r in genesis})}")
say(f"    distinct previous hashes     : {len({r['prev'] for r in genesis})}")
inputs_identical = (len({r["index"] for r in genesis}) == 1
                    and len({r["txs"] for r in genesis}) == 1
                    and len({r["prev"] for r in genesis}) == 1)
assert inputs_identical, "FALSIFIED: genesis inputs differ, so a hash difference is expected"
say("    -> every input to valid_proof is identical, so identical nonces are")
say("       the SAME work on the SAME data.  Only the block hash differs.")
say("")
say("  LIMIT OF THIS EVIDENCE, stated plainly: for a non-genesis block the inputs")
say("  legitimately differ between nodes, so a repeated nonce there would prove")
say("  nothing.  Genesis is the only class where the inputs are identical by")
say("  construction, which is why the claim rests on it.")

out["log"] = {
    "listings_parsed": len(records),
    "genesis_listings": len(genesis),
    "genesis_listings_by_grep": grep_count,
    "distinct_genesis_nonces": len(by_nonce),
    "nonces_with_multiple_hashes": len(divergent),
    "max_hashes_from_one_nonce": max(len(h) for h in by_nonce.values()),
    "genesis_distinct_hashes_total": len({r["hash"] for r in genesis}),
    "genesis_inputs_identical": inputs_identical,
}

# =========================================================== FINDING 1
head("FINDING 1  the prescribed estimator, simulated on real blocks")

import csv
from statistics import mean, median

with open(RESULTS / "difficulty_trials.csv", newline="") as fh:
    trials = list(csv.DictReader(fh))
d5 = [float(r["wall_time_s"]) for r in trials if r["difficulty"] == "5"]
rate5 = [float(r["attempt_rate"]) for r in trials
         if r["difficulty"] == "5" and r["attempt_rate"]]

n = len(d5)
sample_mean = mean(d5)
theory_mean = 16 ** 5 / mean(rate5)
say(f"  difficulty-5 blocks measured        : {n}")
say(f"  sample mean                         : {sample_mean:.4f} s")
say(f"  theoretical mean (16^5 / rate)       : {theory_mean:.4f} s")
say(f"  sample mean / theory                 : {sample_mean / theory_mean:.3f}  (1.000 would be exact)")

# The estimator the task sheet prescribes, on the block times actually observed.
import random
rng = random.Random(20260923)
N = 200_000
singles, med3s, mean3s = [], [], []
within_single = within_med3 = 0
for _ in range(N):
    s = [rng.choice(d5) for _ in range(3)]
    m3 = median(s)
    singles.append(s[0])
    med3s.append(m3)
    mean3s.append(mean(s))
    within_single += abs(s[0] / sample_mean - 1) <= 0.5
    within_med3 += abs(m3 / sample_mean - 1) <= 0.5

exp_med3 = 3 / (2 - 1 / 16 ** 5) - 2 / (3 - 3 / 16 ** 5 + (1 / 16 ** 5) ** 2)

say("")
say(f"  E[median of 3] / E[X], closed form (p -> 0) : {exp_med3:.4f}  (= 5/6)")
say(f"  mean simulated single block / sample mean   : {mean(singles) / sample_mean:.3f}")
say(f"  mean simulated median of 3 / sample mean    : {mean(med3s) / sample_mean:.3f}")
say(f"  mean simulated mean of 3   / sample mean    : {mean(mean3s) / sample_mean:.3f}")
say("")
say(f"  P(one block within +-50% of the mean)       : {within_single / N:.3f}")
say(f"  P(median of 3 within +-50% of the mean)     : {within_med3 / N:.3f}")

srt = sorted(d5)
p5 = srt[int(0.05 * n)]
p95 = srt[int(0.95 * n)]
say("")
say(f"  observed p5 .. p95 span at n={n}             : {p5:.4f} .. {p95:.4f} s "
    f"= {p95 / p5:.1f}x")
say("  exponential population expectation        : 58.4x")
say("  -> the spread is what a geometric search at this target is supposed to do.")

# The prescribed run itself, for comparison.
presc = [r for r in csv.DictReader(open(RESULTS / "prescribed_table.csv"))
         if r["program"] == "node.py" and r["target"] == "00000"][0]
med_reported = float(presc["mining_time_s"])
next_block = float(presc["mining_time_second_block_s"])
say("")
say("  my Phase 1 walkthrough at target 00000 (prescribed procedure):")
say(f"    reported 'mining time' (median of 3)  : {med_reported:.4f} s")
say(f"    the very next block, same target      : {next_block:.4f} s")
say(f"    ratio                                 : {next_block / med_reported:.2f}x")
say(f"    each against the battery mean {sample_mean:.4f} s : "
    f"{med_reported / sample_mean:.3f}x and {next_block / sample_mean:.3f}x")

out["estimator"] = {
    "n_d5_blocks": n,
    "sample_mean_s": sample_mean,
    "theory_mean_s": theory_mean,
    "sample_mean_over_theory": sample_mean / theory_mean,
    "closed_form_median3_over_mean": exp_med3,
    "sim_median3_over_sample_mean": mean(med3s) / sample_mean,
    "sim_mean3_over_sample_mean": mean(mean3s) / sample_mean,
    "p_within_50pct_single": within_single / N,
    "p_within_50pct_median3": within_med3 / N,
    "span_p5_p95": p95 / p5,
    "p5_s": p5, "p95_s": p95,
    "prescribed_median3_s": med_reported,
    "prescribed_next_block_s": next_block,
    "prescribed_spread_x": next_block / med_reported,
}

# ------------------------------------------------- the link between the findings
head("WHAT THIS DOES AND DOES NOT EXPLAIN")

say("  FINDING 1 is a defect in the measurement protocol, not in the code. The")
say("  spread between 0.2403 s and 1.5543 s is ordinary geometric variance that the")
say("  prescribed estimator cannot resolve at n = 3. It is NOT caused by FINDING 2,")
say("  and this report does not claim that it is.")
say("")
say("  FINDING 2 is a defect in the provided scaffold, visible only in multi-node")
say("  behaviour: identical work, divergent identity, no canonical genesis. It does")
say("  not affect a single node's timing, and the 6.47x spread is not evidence of it.")
say("")
say("  They are independent, and they are reported as two findings.")

(RESULTS / "rootcause.json").write_text(json.dumps(out, indent=2))
(BASE / "rootcause_report.txt").write_text("\n".join(report) + "\n")
say("")
say(f"  wrote {RESULTS / 'rootcause.json'}")
say(f"  wrote {BASE / 'rootcause_report.txt'}")
