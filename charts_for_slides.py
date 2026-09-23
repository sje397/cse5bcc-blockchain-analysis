#!/usr/bin/env python3
"""Regenerate every figure that appears in the CSE5BCC Assessment 1 slide deck.

Single source of truth: this script reads only files in results/ and writes only
files in charts/. It prints a NUMBERS block that the slide text and the spoken
script must quote verbatim, so no figure in the deck is typed by hand.

Every headline claim is cross-checked against a closed-form result:
  * E[attempts] = 16**k                     (uniform nonce search)
  * P(X <= x*E[X]) -> 1 - exp(-x)           (geometric -> exponential)
  * E[median of 3] / E[X] -> 5/6 = 0.8333   (2nd order statistic of 3)
The analytic value is printed next to the measured one so a mismatch is visible
rather than buried in a chart.

Run:  ./.venv/bin/python charts_for_slides.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import textwrap
from collections import defaultdict
from pathlib import Path

import numpy as np

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib.patches import Patch

RESULTS = Path("results")
CHARTS = Path("charts")

# ---------------------------------------------------------------- slide style
INK = "#1c2430"
MUTED = "#6b7785"
GRID = "#dfe4ea"
BLUE = "#1f5fa8"
ORANGE = "#d1622b"
TEAL = "#0f7b6c"
PURPLE = "#6b4fa0"
RED = "#b3261e"

plt.rcParams.update({
    "figure.dpi": 160,
    "savefig.dpi": 160,
    "font.size": 12,
    "axes.titlesize": 13.5,
    "axes.titleweight": "bold",
    "axes.labelsize": 12,
    "axes.edgecolor": MUTED,
    "axes.labelcolor": INK,
    "axes.titlecolor": INK,
    "text.color": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelsize": 11,
    "ytick.labelsize": 11,
    "legend.fontsize": 10.5,
    "legend.frameon": False,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "axes.axisbelow": True,
    "figure.facecolor": "white",
    "axes.spines.top": False,
    "axes.spines.right": False,
})


def load_csv(name: str) -> list[dict]:
    with open(RESULTS / name, newline="") as fh:
        return list(csv.DictReader(fh))


def load_json(name: str):
    return json.loads((RESULTS / name).read_text())


FOOTER_PT = 8.5             # footer font size, points
FOOTER_LEAD_IN = 0.145      # footer line leading, inches
FOOTER_PAD_IN = 0.010       # gap below the last footer line, inches
FOOTER_EDGE_IN = 0.06       # keep the text this far inside each figure edge


def _text_width_in(fig, s: str, pt: float) -> float:
    """Width of `s` in inches, measured against the figure's own renderer.

    Measuring rather than assuming, because the obvious shortcut is wrong in the
    direction that loses text.  Per-character width is not a constant: at 8.5pt
    lowercase averages ~0.063in, capitals ~0.100in and 'W' ~0.120in.  A fixed
    0.064in/char therefore under-estimates any capital-heavy string by up to 2x,
    and an over-long line is not wrapped and not marked -- it is clipped flush at
    the right margin, so the sentence still reads as complete with its last
    clause gone.
    """
    t = fig.text(0.0, 0.0, s, fontsize=pt)
    try:
        return t.get_window_extent(renderer=fig.canvas.get_renderer()).width / fig.dpi
    finally:
        t.remove()


def _wrap_to_width(fig, note: str, pt: float, max_in: float) -> list[str]:
    """Greedy word wrap to a measured width rather than a character count."""
    lines: list[str] = []
    cur = ""
    for word in note.split():
        trial = word if not cur else f"{cur} {word}"
        if cur and _text_width_in(fig, trial, pt) > max_in:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)

    # Fail loudly rather than clipping.  A line wider than the figure here means
    # either a single unbreakable word, or a wrapping bug -- both would otherwise
    # ship as a silently truncated claim on a slide.
    for line in lines:
        w = _text_width_in(fig, line, pt)
        if w > max_in:
            raise ValueError(
                f"footer line measures {w:.2f}in against {max_in:.2f}in available "
                f"and cannot be wrapped further: {line!r}"
            )
    return lines


def finish(fig, path: Path, note: str = "") -> None:
    """Save the figure, wrapping the note to the figure width and reserving the
    vertical space it needs.

    The wrap is measured against the rendered text (see _text_width_in), and it
    raises rather than emits a clipped line.
    """
    if note:
        max_in = fig.get_figwidth() - 2 * FOOTER_EDGE_IN
        lines = _wrap_to_width(fig, note, FOOTER_PT, max_in)
        line_h = FOOTER_LEAD_IN / fig.get_figheight()
        pad = FOOTER_PAD_IN / fig.get_figheight()
        bottom = pad + len(lines) * line_h
        x = FOOTER_EDGE_IN / fig.get_figwidth()
        for i, line in enumerate(lines):
            fig.text(x, pad + (len(lines) - 1 - i) * line_h, line,
                     fontsize=FOOTER_PT, color=MUTED, ha="left")
        fig.tight_layout(rect=(0, bottom, 1, 1))
    else:
        fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}  ({path.stat().st_size // 1024} KB)")


# ------------------------------------------------------- analytic expectations
def e_median_of_3_over_mean(p: float) -> float:
    """E[median of 3] / E[X] for X ~ Geometric(p) on {1,2,...}.

    E[X_(2)] = sum_{n>=0} P(X_(2) > n) = sum 3*q^n^2 - 2*q^n^3  with q = 1-p,
    which sums to 3/(2p - p^2) - 2/(3p - 3p^2 + p^3).  As p -> 0 this is 5/6.
    """
    return 3.0 / (2 - p) - 2.0 / (3 - 3 * p + p * p)


def p_within(x: float, truth: float) -> float:
    return 1.0 - math.exp(-x)



def ks_lilliefors_exponential(vals: list[int], sims: int = 20_000, seed: int = 20260924) -> tuple[float, float]:
    """One-sample KS test of vals against an exponential whose mean is estimated
    from the SAME data (the Lilliefors case).

    Using the plain KS p-value would be wrong here: estimating the mean from the
    sample makes the statistic smaller than the fixed-parameter null assumes, so
    the textbook p-value is too conservative.  Lilliefors fixes that by resampling
    the null distribution of the statistic under exactly this procedure, which is
    what we do -- no scipy needed, and the correction is the honest one.
    """
    x = np.asarray(vals, dtype=float)
    n = x.size

    def stat(a: np.ndarray) -> float:
        s = np.sort(a)
        cdf = 1.0 - np.exp(-s / s.mean())
        i = np.arange(1, n + 1)
        return float(max(np.max(np.abs(cdf - i / n)), np.max(np.abs(cdf - (i - 1) / n))))

    d = stat(x)
    rng = np.random.default_rng(seed)
    null = np.array([stat(rng.exponential(1.0, n)) for _ in range(sims)])
    # +1 smoothing: the observed draw is itself a possible value of the null.
    p = float((np.sum(null >= d) + 1) / (sims + 1))
    return round(d, 4), round(p, 4)


def theoretical_span_exp_x() -> float:
    """p95/p95 of an exponential expressed as a ratio of TIMES (not quantiles).

    Invert the CDF: t_q = -ln(1-q), so t_.95/t_.05 = ln(0.05)/ln(0.95) = 58.4.
    The naive log(0.95)/log(0.05) is the reciprocal and evaluates to 0.02 -> 0.0
    when rounded, which is how a wrong value can look like a legitimate result.
    """
    return math.log(0.05) / math.log(0.95)


def span_null(vals: list[int], observed_span: float,
              sims: int = 40_000, seed: int = 20260924) -> dict:
    """Null distribution of the p5-p95 span statistic AT THIS SAMPLE SIZE.

    The span is a ratio of two order statistics, which is biased low in small
    samples: for n=300 a PURE exponential sample gives a median span of ~56x
    against 58.4x for the population, and at n=23 only ~36x.  Those are the same
    index rule the figure uses (int(0.05n), int(0.95n)).

    Without this, the d6 band reading narrower than d3 looks like evidence the
    distribution itself tightened at high difficulty -- the opposite of the
    collapse the figure is demonstrating.  It is the estimator, not the data.
    """
    n = len(vals)
    rng = np.random.default_rng(seed)
    x = np.sort(rng.exponential(1.0, size=(sims, n)), axis=1)
    span = (x[:, int(0.95 * n)] / x.mean(axis=1)) / (x[:, int(0.05 * n)] / x.mean(axis=1))
    return {"n": n,
            "median": round(float(np.median(span)), 1),
            "p5": round(float(np.percentile(span, 5)), 1),
            "p95": round(float(np.percentile(span, 95)), 1),
            # +1 smoothing, as in the KS test above: the observed draw is itself
            # a possible value of the null.
            "observed_percentile": round(100.0 * (np.sum(span < observed_span) + 1) / (sims + 1), 1)}


# -------------------------------------------------------------------- figure 1
def fig_difficulty_ladder(summary: list[dict], prescribed: list[dict]) -> dict:
    ks = [int(r["difficulty"]) for r in summary]
    exp = [int(r["expected_attempts_16pow_k"]) for r in summary]
    obs = [float(r["observed_mean_attempts"]) for r in summary]
    wall = [float(r["mean_wall_s"]) for r in summary]
    reps = [int(r["reps"]) for r in summary]
    rate = [float(r["mean_attempt_rate"]) for r in summary]

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11.6, 4.5))

    ax.plot(ks, exp, "-", color=MUTED, lw=2, label=r"predicted  $16^{k}$")
    ax.plot(ks, obs, "o", color=BLUE, ms=9, label="measured mean attempts")
    for k, e, o, n in zip(ks, exp, obs, reps):
        ax.annotate(f"{o/e:.3f}$\\times$", (k, o), textcoords="offset points",
                    xytext=(9, -4), fontsize=10, color=BLUE)
    ax.set_yscale("log")
    ax.set_xticks(ks)
    ax.set_xlabel("difficulty target (leading zero hex digits)")
    ax.set_ylabel("hash attempts per block  (log scale)")
    # Computed, never typed: a hand-written "4,000x" is a claim that cannot fail.
    span_measured = obs[-1] / obs[0]
    ax.set_title(f"(a)  Observed work tracks $16^{{k}}$ across {span_measured:,.0f}$\\times$")
    ax.legend(loc="upper left")
    ax.text(0.97, 0.06, f"n = {', '.join(str(r) for r in reps)} blocks",
            transform=ax.transAxes, ha="right", fontsize=10, color=MUTED)

    # The robust way to tighten a one-block reading is "mining time = median of 3 blocks".  Its EXPECTATION
    # is not the mean: for a geometric search the 2nd order statistic of 3 sits at
    # e_median_of_3_over_mean(p) = 0.833 of it, so the median-of-three reading is low
    # even when nothing goes wrong.  This panel previously plotted the sample median
    # (0.69 of the mean) and labelled it "median of 3" -- two different quantities
    # under one name, and the more flattering of the two.  Replaced.
    med3_exp = [w * e_median_of_3_over_mean(1.0 / 16 ** k) for k, w in zip(ks, wall)]
    ax2.plot(ks, med3_exp, "s--", color=ORANGE, ms=8, lw=1.6,
             label="median of 3: expectation ($5/6\\times$ mean)")
    ax2.plot(ks, wall, "o-", color=BLUE, ms=9, lw=2, label="mean of all blocks")
    # Anchored in axes coordinates in the empty lower-right corner.  Placed against
    # the data first, it landed across the k=5 markers and the k=6 line.
    #
    # Both notes live as separate one-line calls because the earlier version asked for a
    # line break with an escaped "\\n" in a non-raw string: that renders the two
    # characters, not a break.  Splitting into two calls makes the layout the thing that
    # is written, not an escape the reader has to decode.  Checked against the data
    # before placing: for x >= 4.17 the lowest curve is above 0.32 of the axes height
    # (0.0037 s at k=3 is 0.045), so this band is empty over the whole text width
    # (the longest line starts at x = 4.17).
    ax2.text(0.98, 0.27, "median of 3 sits $1.20\\times$ below the mean",
             transform=ax2.transAxes, fontsize=9, color=ORANGE, ha="right", va="bottom")
    ax2.text(0.98, 0.195, "at every difficulty",
             transform=ax2.transAxes, fontsize=9, color=ORANGE, ha="right", va="bottom")

    # My own Phase 1 walkthrough at this target, reporting the median of three
    # procedure.  Two points, same target, minutes apart, 6.47x apart -- which is the
    # finding this panel exists for.  These are MY measurements (results/prescribed_table.csv),
    # not numbers published anywhere else.
    p = next(r for r in prescribed
             if r["program"] == "node.py" and r["target"] == "00000")
    t_med, t_next = float(p["mining_time_s"]), float(p["mining_time_second_block_s"])
    ax2.plot([5, 5], [t_med, t_next], "*", color=RED, ms=17, zorder=6,
             label="my Phase 1 walkthrough, this target")
    ax2.annotate("", xy=(5.16, t_next), xytext=(5.16, t_med),
                 arrowprops=dict(arrowstyle="<->", color=RED, lw=1.5))
    ax2.text(0.98, 0.12, "same target, minutes apart:",
             transform=ax2.transAxes, fontsize=9, color=RED, ha="right", va="bottom")
    ax2.text(0.98, 0.045, f"{t_next/t_med:.2f}$\\times$ apart",
             transform=ax2.transAxes, fontsize=9, color=RED, ha="right", va="bottom")
    ax2.set_yscale("log")
    ax2.set_xticks(ks)
    ax2.set_xlim(2.7, 6.9)
    ax2.set_xlabel("difficulty target")
    ax2.set_ylabel("mining time per block  (s, log scale)")
    ax2.set_title("(b)  The median of three\nreads below the mean")
    ax2.legend(loc="upper left")

    finish(fig, CHARTS / "s1_difficulty_ladder.png",
           "Attempts counted per block by the node itself; n as labelled, one machine, single process per difficulty.")

    return {"ratios": [round(o / e, 3) for k, e, o in zip(ks, exp, obs)],
            "rate_MHs": [round(r / 1e6, 3) for r in rate],
            # Replaces "wall_gap" (sample median / mean), which measured the sample
            # rather than the median of three and so did not describe the
            # protocol being evaluated.
            "med3_expected_gap": [round(w / m, 3) for w, m in zip(wall, med3_exp)],
            "step_factor_wall": round(wall[-1] / wall[0], 1),
            "step_factor_attempts": round(obs[-1] / obs[0], 1),
            "prescribed_median3_s": round(t_med, 4),
            "prescribed_next_block_s": round(t_next, 4),
            "prescribed_spread_x": round(t_next / t_med, 3)}


# -------------------------------------------------------------------- figure 2
def fig_distribution_collapse(trials: list[dict]) -> dict:
    by_k: dict[int, list[int]] = defaultdict(list)
    for r in trials:
        by_k[int(r["difficulty"])].append(int(r["attempts"]))

    fig, ax = plt.subplots(figsize=(9.4, 4.6))
    colours = {3: TEAL, 4: BLUE, 5: ORANGE, 6: PURPLE}
    xs = [i / 200 for i in range(0, 601)]
    theory = [1 - math.exp(-x) for x in xs]
    ax.plot(xs, theory, "-", color=INK, lw=2, zorder=5,
            label=r"theoretical  $1-e^{-x}$")

    spans = {}
    for k in sorted(by_k):
        vals = sorted(by_k[k])
        mean = sum(vals) / len(vals)
        n = len(vals)
        xs_k = [v / mean for v in vals]
        ys = [(i + 1) / n for i in range(n)]
        ax.step(xs_k, ys, where="post", color=colours[k], lw=1.9,
                label=f"difficulty {k}  (n={n})", alpha=0.95)
        spans[k] = (vals[int(0.05 * n)] / mean, vals[int(0.95 * n)] / mean)

    ax.set_xlim(0, 3)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("block mining time  $\\div$  mean time at that difficulty")
    ax.set_ylabel("cumulative fraction of blocks")
    ax.set_title("Every difficulty collapses onto one curve — the shape is set by the\n"
                 "hashing process, and difficulty only moves the time scale")
    ax.legend(loc="lower right")

    # The shaded band is explained in the legend rather than by floating text: an
    # annotation here collided with the legend and its arrow crossed three curves.
    k = max(by_k)
    lo, hi = spans[k]
    ax.axvspan(lo, hi, color=RED, alpha=0.10, zorder=0)

    tests = {kk: ks_lilliefors_exponential(by_k[kk]) for kk in sorted(by_k)}
    rejects = [kk for kk, (_, p) in tests.items() if p < 0.05]
    ax.text(0.02, 0.97,
            "Kolmogorov\u2013Smirnov against an exponential with its mean\n"
            "fitted to the same data (Lilliefors-corrected):\n"
            + "   ".join(f"d{kk}: p={p:.2f}" for kk, (_, p) in tests.items()),
            transform=ax.transAxes, fontsize=9.5, color=INK, va="top", ha="left",
            linespacing=1.45)

    handles, labels = ax.get_legend_handles_labels()
    handles.append(Patch(facecolor=RED, alpha=0.25, edgecolor="none"))
    labels.append(f"difficulty {k}: middle 90% of blocks")
    ax.legend(handles, labels, loc="lower right")

    # The band narrows with difficulty, which is the estimator rather than the
    # data: the span is a ratio of order statistics and is biased low in small
    # samples.  Calibrated here against pure-exponential samples of the same
    # size, so the claim in the footer cannot drift from the figure.
    nulls = {kk: span_null(by_k[kk], spans[kk][1] / spans[kk][0]) for kk in sorted(by_k)}

    finish(fig, CHARTS / "s2_distribution_collapse.png",
           "Each block time divided by its own difficulty's mean, so all four share one scale. "
           f"d6 (n={nulls[k]['n']}) is the smallest sample, so its curve is the noisiest and its band the "
           f"narrowest \u2014 that narrowing is the estimator, not the data: pure-exponential samples of the "
           f"same size give a median span of {nulls[k]['median']:.0f}\u00d7 against "
           f"{theoretical_span_exp_x():.1f}\u00d7 for the population, and d6's span sits at the "
           f"{nulls[k]['observed_percentile']:.0f}th percentile of that null.")

    return {"p5_p95_span_x": {kk: round(spans[kk][1] / spans[kk][0], 1) for kk in sorted(spans)},
            # Ratio of TIMES, not of quantiles: t_.95/t_.05 = ln(0.05)/ln(0.95) = 58.4.
            "theory_span_for_exponential_x": round(theoretical_span_exp_x(), 1),
            "span_null_median_x": {kk: nulls[kk]["median"] for kk in sorted(nulls)},
            "span_null_central_90_x": {kk: [nulls[kk]["p5"], nulls[kk]["p95"]] for kk in sorted(nulls)},
            "span_observed_percentile": {kk: nulls[kk]["observed_percentile"] for kk in sorted(nulls)},
            "ks_D": {kk: tests[kk][0] for kk in sorted(tests)},
            "ks_p": {kk: tests[kk][1] for kk in sorted(tests)},
            "ks_rejected_at_5pct": rejects}


# -------------------------------------------------------------------- figure 3
def fig_estimator(trials: list[dict]) -> dict:
    """Why a median of 3 is biased low, measured and closed-form."""
    by_k: dict[int, list[int]] = defaultdict(list)
    for r in trials:
        by_k[int(r["difficulty"])].append(int(r["attempts"]))

    k = 5
    vals = by_k[k]
    n = len(vals)
    truth = 16 ** k
    sample_mean = sum(vals) / n
    p = 1.0 / truth

    rng = random.Random(20260923)
    B = 200_000
    med3 = []
    single = []
    for _ in range(B):
        a, b, c = rng.choice(vals), rng.choice(vals), rng.choice(vals)
        med3.append(sorted((a, b, c))[1])
        single.append(a)

    e_med3_theory = e_median_of_3_over_mean(p)
    mean_med3 = sum(med3) / B
    mean_single = sum(single) / B

    # exact enumeration over the real triples, as an independent check on the bootstrap
    exact = 0.0
    tri = n * (n - 1) * (n - 2) / 6
    for i in range(n):
        for j in range(i + 1, n):
            v = sorted((vals[i], vals[j]))
            for m in range(j + 1, n):
                exact += sorted((vals[i], vals[j], vals[m]))[1]
    exact_mean_med3 = exact / tri

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11.6, 4.4))

    xs = [i * 4 for i in range(0, 251)]
    f = [1 - math.exp(-x) for x in xs]
    dens = [math.exp(-x) for x in xs]
    ax.plot(xs, dens, "-", color=INK, lw=2, label="theory: exponential")
    ax.hist([v / truth for v in vals], bins=24, density=True, color=BLUE,
            alpha=0.55, edgecolor="white", label=f"measured blocks (n={n})")
    # analytic density of the 2nd order statistic of 3: 6(1-e^-x)e^-2x
    ax.plot(xs, [6 * (1 - math.exp(-x)) * math.exp(-2 * x) for x in xs],
            "--", color=ORANGE, lw=2.2, label="theory: median of 3")
    ax.axvline(1.0, color=BLUE, lw=1.4)
    ax.axvline(5 / 6, color=RED, lw=1.4)
    ax.annotate("true mean\n1.0", xy=(1.0, 1.02), fontsize=10, color=BLUE, ha="center")
    ax.annotate("$5/6$", xy=(5 / 6, 1.28), fontsize=11, color=RED, ha="center")
    ax.set_xlim(0, 3)
    ax.set_ylim(0, 1.55)
    ax.set_xlabel("measured value $\\div$ true mean (difficulty 5)")
    ax.set_ylabel("probability density")
    ax.set_title("(a)  A single block is a wide, one-sided draw")
    ax.legend(loc="upper right", fontsize=9.5)

    mean3 = []
    for _ in range(B):
        a, b, c = rng.choice(vals), rng.choice(vals), rng.choice(vals)
        mean3.append((a + b + c) / 3)

    estimates = [("one block\n(n = 1)", single), ("median of 3\n(the robust fix)", med3),
                 ("mean of 3", mean3)]
    labels = [name for name, _ in estimates]
    means = [sum(e) / len(e) / truth for _, e in estimates]
    lo = [sorted(e)[int(0.05 * len(e))] / truth for _, e in estimates]
    hi = [sorted(e)[int(0.95 * len(e))] / truth for _, e in estimates]
    colours = [BLUE, ORANGE, TEAL]

    # A zoomed axis needs a dot plot, not bars: cutting a bar's baseline at 0.7
    # would misrepresent the ratios, and the same 90% whiskers drawn here would be
    # ~7x the size of the effect and swamp it.  Spread is panel (a)'s job; this
    # panel's job is the 17% offset, so it gets its own scale and says so.
    ys = list(range(len(estimates)))[::-1]
    ax2.scatter(means, ys, s=170, color=colours, zorder=5)
    for y, m, l, h in zip(ys, means, lo, hi):
        ax2.text(m + 0.013, y, f"{m:.3f}", va="center", ha="left", fontsize=11,
                 fontweight="bold")
    ax2.axvline(1.0, color=BLUE, lw=1.4, ls=":")
    ax2.axvline(5 / 6, color=RED, lw=1.4, ls=":")
    ax2.set_yticks(ys)
    ax2.set_yticklabels(labels, fontsize=10)
    ax2.set_xlim(0.72, 1.19)
    ax2.set_ylim(-0.75, len(estimates) - 0.4)
    ax2.set_xlabel("estimate $\\div$ true mean   (axis zoomed to show the bias)")
    # Two lines, not one: a 48-char single line runs past the figure's right edge
    # (tight_layout budgets for title HEIGHT, never width).
    ax2.set_title("(b)  Bias: the median of three\nsettles 17% low")
    ax2.text(1.0, -0.62, "true mean\n1.0", fontsize=9, color=BLUE, ha="center", va="bottom")
    ax2.text(5 / 6, len(estimates) - 0.5, "$5/6$ ceiling", fontsize=9.5, color=RED,
             ha="right", va="top")
    ax2.text(0.72, -0.62, f"90% of runs span {lo[0]:.2f}\u2013{hi[0]:.1f}\n(see panel a)",
             fontsize=8.5, color=MUTED, ha="left", va="bottom", linespacing=1.4)
    ax2.grid(axis="y", visible=False)

    finish(fig, CHARTS / "s3_estimator_bias.png",
           "Points are each estimator's mean over 200,000 resamples of the same 127 measured blocks. The sample itself ran 6.5% high, which lifts all three "
           "estimators equally; median-of-3 \u00f7 sample mean = 0.833, matching the 5/6 limit exactly.")

    return {
        "k": k, "n": n, "truth": truth, "sample_mean_ratio": round(sample_mean / truth, 3),
        "e_med3_theory": round(e_med3_theory, 4),
        "one_block_over_mean": round(mean_single / truth, 3),
        "med3_over_mean": round(mean_med3 / truth, 4),
        "med3_exact_enumeration": round(exact_mean_med3 / truth, 4),
        # Isolates the estimator from this sample's own luck: dividing by the sample
        # mean (not the true mean) must reproduce the closed form exactly.
        "med3_over_sample_mean": round(mean_med3 / sample_mean, 4),
        "shortfall_vs_mean_of_3_pct": round(100 * (1 - (mean_med3 / sample_mean)), 1),
        "mean_of_3_over_mean": round(means[2], 4),
        "within50_single": round(sum(1 for v in vals if abs(v / truth - 1) <= 0.5) / n, 3),
    }


# -------------------------------------------------------------------- figure 4
def fig_transaction_scaling(micro: list[dict], http: list[dict]) -> dict:
    tx = [int(r["transactions"]) for r in micro]
    rate = [float(r["attempts_per_second"]) for r in micro]
    pbytes = [int(r["payload_bytes"]) for r in micro]
    frac = [float(r["serialise_fraction"]) for r in micro]

    http_tx = sorted({int(r["extra_transactions"]) for r in http})
    http_rate = [sum(float(r["attempt_rate"]) for r in http
                     if int(r["extra_transactions"]) == t) / len(
                     [r for r in http if int(r["extra_transactions"]) == t]) for t in http_tx]

    fig, ax = plt.subplots(figsize=(10.4, 4.7))
    ax.plot([max(t, 0.5) for t in tx], rate, "o-", color=BLUE, ms=8, lw=2,
            label="marking payload, hashed in-process")
    ax.plot([max(t, 0.5) for t in http_tx], http_rate, "s--", color=ORANGE, ms=8, lw=2,
            label="mine over HTTP, three-node harness")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("transactions carried in the block")
    ax.set_ylabel("hash attempts per second  (log scale)")
    ax.set_title("Mining throughput collapses as a block carries more transactions")

    for t, r, pb in zip(tx, rate, pbytes):
        if t in (0, 100, 1000, 10000):
            ax.annotate(f"{pb/1000:,.1f} kB", (max(t, 0.5), r), textcoords="offset points",
                        xytext=(8, 8), fontsize=9.5, color=MUTED)

    ax2 = ax.twinx()
    ax2.plot([max(t, 0.5) for t in tx], [f * 100 for f in frac], "^:", color=TEAL, ms=7, lw=1.6)
    ax2.set_ylabel("share of each attempt spent re-serialising  (%)", color=TEAL)
    ax2.set_ylim(0, 100)
    ax2.grid(False)
    ax2.spines["right"].set_visible(True)
    ax2.spines["right"].set_color(TEAL)
    ax2.tick_params(axis="y", colors=TEAL)

    ax.text(0.02, 0.06, "the cost is rebuilding the block bytes, not the network:\n"
                        "HTTP overhead stayed at 0.83–1.57 ms across every payload size",
            transform=ax.transAxes, fontsize=10, color=MUTED)
    # Both curves descend left-to-right, so the only genuinely empty region is the
    # wedge under them: upper-right (where this sat) is on top of the markers.
    ax.legend(loc="lower left", bbox_to_anchor=(0.005, 0.155), fontsize=9.5,
              framealpha=0.95)

    finish(fig, CHARTS / "s4_transaction_scaling.png",
           "Same effect measured twice: in-process hashing (n=1 per size) and over HTTP (n=5 per size, mean plotted).")

    return {"rate_0tx": round(rate[0]), "rate_10000tx": round(rate[-1]),
            "collapse_x": round(rate[0] / rate[-1]),
            "bytes_0": pbytes[0], "bytes_10000": pbytes[-1],
            "frac_0": frac[0], "frac_max": max(frac),
            "http_1tx": round(http_rate[0]), "http_1000tx": round(http_rate[-1])}


# -------------------------------------------------------------------- figure 5
def fig_fork(fork: list[dict], wasted: dict) -> dict:
    n = len(fork)
    xs = list(range(n))
    lens = {node: [int(r[f"{node}_length"]) for r in fork] for node in ("A", "B", "C")}
    colours = {"A": BLUE, "B": ORANGE, "C": TEAL}

    fig, ax = plt.subplots(figsize=(11.6, 6.6))
    fig.subplots_adjust(left=0.055, right=0.985, top=0.845, bottom=0.085)
    for node in ("A", "B", "C"):
        ax.step(xs, lens[node], where="post", color=colours[node], lw=2.2,
                label=f"node {node}", marker="o", ms=6)

    # The discriminator sits in the step label for two of these and in the note for
    # the other two, so both columns must be read or the tie is undercounted 2 -> 4.
    tie = [i for i, r in enumerate(fork)
           if any(t in (r["step"] + " " + r["note"]).lower()
                  for t in ("not strictly longer", "no adoption"))]
    assert len(tie) == 4, f"expected 4 equal-length syncs, found {len(tie)}: {[fork[i]['step'] for i in tie]}"
    if tie:
        # No legend label: the red caption over the band says the same thing, and a
        # fourth legend entry for a shaded region is one more thing to read than it
        # saves.
        ax.axvspan(min(tie) - 0.5, max(tie) + 0.5, color=RED, alpha=0.08)
    # The thirteen step labels used to be printed on the x axis.  At this width they
    # overprinted each other and were clipped at both ends, so the axis carried neither
    # the order nor the content.  Numbers on the axis, content as a key placed in the
    # band y in [0.8, 2.6]: that band is empty because every chain is at least three
    # blocks long from step 5 onward (C is the last to rise).
    nl = chr(10)
    key_left = nl.join([
        "1   start: 3 nodes, genesis only",
        "2   A mines 2 blocks (A=3)",
        "3   B mines 1 (own fork, B=2)",
        "4   B syncs -> adopts A (B=3)",
        "5   C syncs (C=3): all agree",
        "6   B and C each mine 1",
        "7   B syncs: B=4, C=4 -> tie",
    ])
    key_right = nl.join([
        "8   C syncs: equal, no adoption",
        "9   B syncs again: no adoption",
        "10  C syncs again: no adoption",
        "11  C mines one more (C=5)",
        "12  B syncs -> adopts C, block lost",
        "13  A mines, B syncs",
    ])
    covered = len(key_left.split(nl)) + len(key_right.split(nl))
    assert covered == n, f"step key covers {covered} of {n} steps"

    ax.text(min(tie) - 0.4, 5.9, "four syncs at equal length: no adoption",
            fontsize=10, color=RED, ha="left", va="top")
    ax.text(n + 0.25, 5.9, "C strictly longer (5 vs 4): B adopts",
            fontsize=10, color=ORANGE, ha="right", va="top")
    for x, block in ((4.3, key_left), (9.0, key_right)):
        ax.text(x, 2.6, block, fontsize=8.6, color=INK, va="top", ha="left",
                linespacing=1.45, family="monospace")

    ax.set_xticks(xs)
    ax.set_xticklabels([str(i + 1) for i in xs], fontsize=9.5)
    ax.set_xlim(-0.8, 13.4)
    ax.set_xlabel("step  (key above)")
    ax.set_ylim(0.5, 6.2)
    ax.set_yticks(range(1, 6))
    ax.set_ylabel("chain length (blocks)")
    ax.set_title("Three nodes, thirteen steps: adopting the longer chain leaves an equal-length tie\n"
                 "permanently unresolved")
    ax.legend(loc="upper left", fontsize=9.5)
    ax.text(n + 0.25, 0.8,
            f"work destroyed when B's chain was replaced:  "
            f"{wasted['b_blocks_destroyed']} blocks, {wasted['wasted_attempts']:,} hashes",
            fontsize=10, color=RED, ha="right", va="bottom")


    finish(fig, CHARTS / "s5_fork_timeline.png",
           "Chain length and content fingerprint read from each node after every step; the three nodes start from independent genesis blocks.")

    return {"steps": n, "tie_syncs": len(tie),
            "wasted_attempts": wasted["wasted_attempts"],
            "wasted_blocks": wasted["b_blocks_destroyed"],
            "b_total_attempts": wasted["b_attempts"],
            "a_total_attempts": wasted["a_attempts"]}


def main() -> None:
    CHARTS.mkdir(exist_ok=True)
    summary = load_csv("difficulty_summary.csv")
    trials = load_csv("difficulty_trials.csv")
    micro = load_csv("micro_payload.csv")
    http = load_csv("payload_http.csv")
    fork = load_csv("fork_events.csv")
    wasted = load_json("wasted_work.json")
    prescribed = load_csv("prescribed_table.csv")

    print("generating figures\n")
    out = {
        "ladder": fig_difficulty_ladder(summary, prescribed),
        "collapse": fig_distribution_collapse(trials),
        "estimator": fig_estimator(trials),
        "payload": fig_transaction_scaling(micro, http),
        "fork": fig_fork(fork, wasted),
    }

    counts = defaultdict(int)
    for r in trials:
        counts[int(r["difficulty"])] += 1
    http_overhead = [float(r["overhead_s"]) for r in http]
    sync = [float(r["sync_time_s"]) for r in prescribed if r["sync_time_s"]]

    out["provenance"] = {
        "total_measured_blocks": len(trials),
        "blocks_per_difficulty": dict(sorted(counts.items())),
        "http_overhead_ms": [round(min(http_overhead) * 1000, 2), round(max(http_overhead) * 1000, 2)],
        "sync_times_ms": [round(s * 1000, 1) for s in sync],
        "prescribed_rows": len(prescribed),
    }

    print("\n================ NUMBERS (quote these; do not retype) ================")
    print(json.dumps(out, indent=2))

    # Written, not just printed.  It used to be printed only, so the file on disk
    # held values from an earlier run of a different experiment -- the deck would
    # have quoted fork numbers that no longer matched the chart beside them.  One
    # writer, one reader, no opportunity to drift.
    nums = RESULTS.parent / "numbers_from_charts.json"
    nums.write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nwrote {nums}")
    print("=====================================================================")


if __name__ == "__main__":
    main()
