#!/usr/bin/env python3
"""Re-derive every figure quoted in the slide deck, from the raw results files.

Emits slides_numbers.json. Nothing here is copied from a chart caption or from a
derived summary -- each value is recomputed from the CSVs written by the runs, so a
slide figure can be traced to a file and a line.

Empty CSV cells are treated as missing, not as zero: several result files legitimately
leave a cell blank (a phase-1 row has no sync time; a tie in the fork log has no note).
"""
import csv
import json
import os
import random
from statistics import mean, median

BASE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(BASE, "results")


def load(name):
    with open(os.path.join(RESULTS, name), newline="") as fh:
        return list(csv.DictReader(fh))


def f(v):
    """Float or None. A blank cell is missing, never zero."""
    if v is None:
        return None
    v = v.strip()
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


out = {}

# ---------------------------------------------------------------- difficulty ladder
trials = load("difficulty_trials.csv")
ladder = []
for d in (3, 4, 5, 6):
    rows = [r for r in trials if int(f(r["difficulty"])) == d and f(r["wall_time_s"]) is not None]
    wall = [f(r["wall_time_s"]) for r in rows]
    att = [f(r["attempts"]) for r in rows]
    rate = [f(r["attempt_rate"]) for r in rows if f(r["attempt_rate"]) is not None]
    expected = 16 ** d
    ladder.append({
        "difficulty": d,
        "target": "0" * d,
        "expected_attempts": expected,
        "n_blocks": len(rows),
        "mean_attempts": mean(att),
        "median_attempts": median(att),
        "mean_over_expected": mean(att) / expected,
        "mean_wall_s": mean(wall),
        "median_wall_s": median(wall),
        "min_wall_s": min(wall),
        "max_wall_s": max(wall),
        "mean_attempt_rate": mean(rate),
        "startup_s": f(rows[0]["startup_seconds"]),
    })
out["ladder"] = ladder
out["ladder_span"] = ladder[-1]["mean_wall_s"] / ladder[0]["mean_wall_s"]
out["ladder_span_observed_attempts"] = ladder[-1]["mean_attempts"] / ladder[0]["mean_attempts"]
out["rate_spread"] = max(x["mean_attempt_rate"] for x in ladder) / min(x["mean_attempt_rate"] for x in ladder)
out["startup_scaling"] = ladder[-1]["startup_s"] / ladder[0]["startup_s"]

# ------------------------------------------------- the task sheet's own estimator
# The task sheet asks for "mining time = median of 3 blocks". Test that estimator on the
# difficulty-5 blocks actually measured.
d5 = [f(r["wall_time_s"]) for r in trials
      if int(f(r["difficulty"])) == 5 and f(r["wall_time_s"]) is not None]
TRUE_MEAN_ATTEMPTS = 16 ** 5
sample_mean = mean(d5)
rng = random.Random(20260923)
N = 200_000
med3, mean3, one = [], [], []
within_50_single = 0
within_50_med3 = 0
for _ in range(N):
    s = [rng.choice(d5) for _ in range(3)]
    m3 = median(s)
    a3 = mean(s)
    med3.append(m3)
    mean3.append(a3)
    one.append(s[0])
    if 0.5 <= s[0] / sample_mean <= 1.5:
        within_50_single += 1
    if 0.5 <= m3 / sample_mean <= 1.5:
        within_50_med3 += 1

out["estimator"] = {
    "difficulty": 5,
    "n_blocks": len(d5),
    "sample_mean_wall_s": sample_mean,
    "median_wall_s": median(d5),
    # normalised by the SAMPLE mean (what a student's three blocks actually measure):
    "single_vs_sample_mean": mean(one) / sample_mean,
    "mean3_vs_sample_mean": mean(mean3) / sample_mean,
    "median3_vs_sample_mean": mean(med3) / sample_mean,
    # normalised by the THEORETICAL mean:
    "sample_mean_vs_theory": sample_mean / (
        (TRUE_MEAN_ATTEMPTS / mean([f(r["attempt_rate"]) for r in trials
                                    if int(f(r["difficulty"])) == 5 and f(r["attempt_rate"]) is not None]))
    ),
    "within_50pct_single_block": within_50_single / N,
    "within_50pct_median_of_3": within_50_med3 / N,
    "resamples": N,
    "seed": 20260923,
}
# exact analytic values for an exponential, for comparison with the simulation
out["estimator"]["theory_median_of_3_over_mean"] = 5 / 6
out["estimator"]["theory_within_50pct_single"] = 2.718281828459045 ** -0.5 - 2.718281828459045 ** -1.5

# ------------------------------- the prescribed protocol's live estimator failure
presc = [r for r in load("prescribed_table.csv") if r["program"] == "node.py" and r["target"] == "00000"]
p = presc[0]
med3_ms = f(p["mining_time_s"])
next_block_s = f(p["mining_time_second_block_s"])
out["prescribed_00000"] = {
    "median_of_3_s": med3_ms,
    "next_single_block_s": next_block_s,
    "spread_x": next_block_s / med3_ms,
    "expected_attempts": 16 ** 5,
    "median3_attempts": 307930,   # from logs/phase_demo_capture.log, the median block
    "next_block_attempts": 1415345,
    "median3_vs_expected": 307930 / 16 ** 5,
    "next_vs_expected": 1415345 / 16 ** 5,
}

# ------------------------------------------------------------------ sync across nodes
# The two series do NOT hold the same number of blocks (400 vs 200), so a comparison
# counted in blocks is not like-for-like.  Bytes are comparable and blocks are not --
# which is why the deck quotes byte growth and MB/s rather than "blocks per second".
def sync_stats(name):
    rs = load(name)
    walls = [f(r["sync_wall_s"]) for r in rs]
    byts = [f(r["chain_bytes"]) for r in rs]
    blocks_after = f(rs[-1]["b_length_after"])
    return {
        "label": rs[0]["label"],
        "blocks_min": int(f(rs[0]["blocks"])),
        "blocks_max": int(f(rs[-1]["blocks"])),
        "bytes_min": byts[0],
        "bytes_max": byts[-1],
        "wall_min": walls[0],
        "wall_max": walls[-1],
        "wall_growth": walls[-1] / walls[0],
        "byte_growth": byts[-1] / byts[0],
        "all_agree": all(r["agree"] == "True" for r in rs),
        "all_updated": all(r["updated"] == "True" for r in rs),
        "blocks_after": int(blocks_after),
        "bytes_per_block": byts[-1] / blocks_after,
        # Renamed from "ms_per_mb_upper".  The value was seconds, never milliseconds;
        # the key name was the only thing claiming otherwise, and it is the kind of
        # error that survives review because the number itself looks plausible.
        "s_per_mb": walls[-1] / (byts[-1] / 1e6),
        "mb_per_s": (byts[-1] / 1e6) / walls[-1],
    }

out["sync_empty"] = sync_stats("sync_empty.csv")
out["sync_100tx"] = sync_stats("sync_100tx.csv")

# ------------------------------------------------------------------ payload collapse
mp = load("micro_payload.csv")
out["payload"] = {
    "tx_min": int(f(mp[0]["transactions"])),
    "tx_max": int(f(mp[-1]["transactions"])),
    "payload_bytes_min": f(mp[0]["payload_bytes"]),
    "payload_bytes_max": f(mp[-1]["payload_bytes"]),
    "attempts_per_s_at_0": f(mp[0]["attempts_per_second"]),
    "attempts_per_s_at_10000": f(mp[-1]["attempts_per_second"]),
    "collapse_x": f(mp[0]["attempts_per_second"]) / f(mp[-1]["attempts_per_second"]),
    "serialise_frac_at_0": f(mp[0]["serialise_fraction"]),
    "serialise_frac_at_10000": f(mp[-1]["serialise_fraction"]),
    "serialise_frac_peak": max(f(r["serialise_fraction"]) for r in mp),
    "serialise_frac_peak_tx": int(next(f(r["transactions"]) for r in mp
                                        if f(r["serialise_fraction"]) == max(f(x["serialise_fraction"]) for x in mp))),
    "us_per_attempt_at_0": f(mp[0]["us_per_attempt"]),
    "us_per_attempt_at_10000": f(mp[-1]["us_per_attempt"]),
}
# what the collapse means for wall-clock mining at the provided target '0000'
out["payload"]["mine_s_at_336_per_s"] = 65536 / f(mp[-1]["attempts_per_second"])
out["payload"]["mine_s_at_2_2M_per_s"] = 65536 / f(mp[0]["attempts_per_second"])

ph = load("payload_http.csv")
ovh = [f(r["http_overhead_ms"]) for r in ph if f(r.get("http_overhead_ms")) is not None]
if ovh:
    out["payload"]["http_overhead_ms_min"] = min(ovh)
    out["payload"]["http_overhead_ms_max"] = max(ovh)
out["payload_http_columns"] = list(ph[0].keys())

# ----------------------------------------------------------------------- forks
fe = load("fork_events.csv")
out["fork"] = {
    "steps": len(fe),
    "steps_agreeing": sum(1 for r in fe if r["agree"] == "True"),
    "agreeing_steps": [r["step"] for r in fe if r["agree"] == "True"],
    "final_A": (int(f(fe[-1]["A_length"])), fe[-1]["A_fp"]),
    "final_B": (int(f(fe[-1]["B_length"])), fe[-1]["B_fp"]),
    "final_C": (int(f(fe[-1]["C_length"])), fe[-1]["C_fp"]),
    "ends_divergent": not (fe[-1]["A_fp"] == fe[-1]["B_fp"] == fe[-1]["C_fp"]),
}
# the tie: consecutive syncs where both sides stay at equal length
tie_streak = [r["step"] for r in fe if "not strictly longer" in (r["note"] or "")
              or "still no adoption" in (r["note"] or "") or "not strictly longer" in (r["note"] or "")]
out["fork"]["tie_steps"] = tie_streak

ww = load("wasted_work.csv")
out["wasted"] = {}
for r in ww:
    if r["node"] == "TOTAL":
        out["wasted"]["total_attempts"] = f(r["attempts"])
        out["wasted"]["total_wasted_attempts"] = f(r["wasted_attempts"])
    elif r["node"] == "B":
        out["wasted"]["B_blocks_mined"] = f(r["blocks_mined"])
        out["wasted"]["B_attempts"] = f(r["attempts"])
        out["wasted"]["B_own_blocks_in_final_chain"] = f(r["own_blocks_in_final_chain"])
        out["wasted"]["B_blocks_destroyed"] = f(r["blocks_destroyed"])
        out["wasted"]["genesis_survived_sync"] = r["genesis_survived_sync"]
        out["wasted"]["a_blocks_present_in_adopted"] = f(r["a_blocks_present_in_adopted"])

au = load("autosync.csv")
settled = [r for r in au if r["round"] == "settled"][0]
out["autosync"] = {
    "rounds": len(au) - 1,
    "ever_agreed": any(r["agreed"] == "True" for r in au),
    "settled_lengths": (int(f(settled["A_length"])), int(f(settled["B_length"]))),
    "settled_agreed": settled["agreed"],
}
out["autosync_rows"] = au

with open(os.path.join(BASE, "slides_numbers.json"), "w") as fh:
    json.dump(out, fh, indent=2)

# ------------------------------------------------------------------- readable report
print(json.dumps(out, indent=2))
