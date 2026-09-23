# Measuring a toy blockchain — and what its specification gets wrong

A measurement study of a ~200-line teaching blockchain, run across four prescribed phases with
**750 timed mining trials**. The point was not to build a blockchain. It was to test whether the
numbers the exercise asks you to report actually mean anything — and, where they don't, to *prove*
it rather than assert it.

**The headline:** the prescribed mining-time estimator is biased **16.7% low by construction**, and
the block's identity is **not bound to its proof of work** — so two nodes that do identical work
produce different blocks, and the network has no common root from which to agree.

## The four findings

### 1. The prescribed estimator is biased, and the bias is exactly 1/6

The exercise asks for mining time as *the median of three blocks*. Block time is a one-sided draw
from a **geometric** distribution, and the expected median of three such draws is **5/6 of the true
mean** — 0.8333, not 1.0. That is a property of the estimator, not of the miner, and it is not noise
that more care could remove.

At n = 3 the sampling noise also swamps the signal. On the prescribed protocol at target `00000`:
three blocks at 0.2403, 0.2080 and 0.4441 s — median 0.2403 s — then **1.5543 s on the very next
block**, same node, same target, minutes apart. That is **6.47×** the median.

Over 200,000 resamples of the 127 measured difficulty-5 blocks: the mean of three recovers 0.999 of
the sample mean, the median of three settles at **0.833**. A single block lands within ±50% of the
true mean 38.6% of the time; the median of three manages 53.8%. The observed p5–p95 span of a single
block is **22× to 57×**, widening as the target gets harder.

*Proven by:* `rootcause_demo.py`, `results/rootcause.json`, `charts/s3_estimator_bias.png`

### 2. Block identity is not bound to the work

Two different preimages over overlapping fields:

| | |
|---|---|
| `valid_proof` — the acceptance criterion | `sha256(f'{index}{prev}{transactions}{nonce}')` — **no timestamp** |
| `hash_block` — the block's identity | `sha256(json.dumps(block, sort_keys=True))` — **includes the timestamp** |

So the proof covers the ledger's contents but not its clock. Three nodes, same target, each mining
its own genesis block: **all three found the same nonce, 61,093** — same index, same previous hash,
same transactions — and produced **three different block hashes**, because each stamped its own
`time()`. Set a block's recorded timestamp to 0, −1 or 2×10⁹ and `valid_proof` stays true while the
block hash changes.

There is no per-node nonce-space separation — Bitcoin gets that from a unique coinbase — and the
nonce is ground deterministically upward from 0, so identical work reliably yields identical nonces.

*Proven by:* `rootcause_demo.py`, `results/rootcause.json`, `screenshots/p4_same_nonce.png`

### 3. Equal-length forks never resolve

The adoption rule is *"adopt only a strictly longer chain"*, and length is a **block count** — not
accumulated work. So at equal length nothing resolves. In the 13-step fork script two nodes at four
blocks each sync, re-sync, sync again and change nothing. Autosync settles the same way: A on four
blocks, B on four, `agreed = False`.

Convergence returns only when one side pulls ahead: C mines one more block, B adopts — and discards
both of its own. **19,755 hashes of B's own work destroyed**, against zero of the three adopted
blocks.

*Proven by:* `phase_demo.py`, `harness.py:837` (`exp_fork`), `results/fork_events.{csv,json}`,
`results/wasted_work.{json,csv}`, `results/autosync.csv`, `charts/s5_fork_timeline.png`

### 4. Payload growth collapses the attempt rate

Attempts per second is roughly constant across difficulty targets — **1.10–1.26 M/s** — because
changing the target changes *how many* attempts are needed, not what each attempt costs. But that
cost is itself a function of how much data gets re-serialised every attempt:

**End to end, mining over HTTP at target `0000`:**

| pending transactions | attempts/s |
|---|---|
| 1 | 919,273 |
| 1,000 | 3,269 |

A **281× collapse**: the same block now takes ~20 s instead of 0.07 s.

**In process, cost per attempt** (no HTTP in the path, payload measured exactly):

| transactions | payload | attempts/s | time spent serialising |
|---|---|---|---|
| 0 | 68 B | 2,208,328 | 31.5% |
| 10,000 | 586,740 B | 336 | 92.8% |

A **6,580× collapse**, with the serialisation share rising from under a third to over nine tenths.
The bytes are identical between attempts; they are rebuilt anyway.

Those two series do not sit on the same x-axis, and it is worth being explicit about why: the HTTP
node always has at least **two** transactions in a block, because `/mine` mints a reward transaction
before it does any hashing. So the 919,273/s row is a two-transaction payload, not a one-transaction
one — which places it between the in-process 1-transaction (1,265,853/s) and 10-transaction
(284,900/s) rows, where it belongs.

*Proven by:* `harness.py:607` (`exp_payload_http`), `harness.py:732` (`exp_micro`),
`results/payload_http.csv`, `results/micro_payload.csv`, `charts/s4_transaction_scaling.png`

### Why 16^k is the right unit

A hex digit is 4 bits with 16 equally likely values, so the probability a SHA-256 digest begins with a
given k-character prefix is 16⁻ᵏ, and the expected number of independent nonce trials is **16ᵏ**.
Bitcoin's difficulty-1 target is `00000000FFFF…`, i.e. k = 8, so work is 2^(4k) and this study's
hardest target, k = 6, is 1/65,536 of Bitcoin difficulty 1.

| k | target | expected attempts | measured mean | obs/exp | blocks |
|---|---|---|---|---|---|
| 3 | `000` | 4,096 | 4,086 | 0.998 | 300 |
| 4 | `0000` | 65,536 | 66,149 | 1.009 | 300 |
| 5 | `00000` | 1,048,576 | 1,116,839 | 1.065 | 127 |
| 6 | `000000` | 16,777,216 | 18,387,442 | 1.096 | 23 |

The model was tested, not assumed: a Kolmogorov–Smirnov test against the geometric distribution
**fails to reject at every target** (p = 0.19–0.66; weakest at k = 6, where there are only 23
blocks). The observed means run 0–10% above 16ᵏ, consistent with a small fixed per-attempt overhead.

That matters to finding 1 rather than being a result of its own. If the underlying distribution were
not geometric — if the miner sped up over the run, or the machine drifted, or the harness leaked time
— the 5/6 result would not apply, and the shortfall would be an artefact of these particular runs
rather than a structural property of the prescribed estimator. The shape is right, so the bias is
structural.

*Proven by:* `results/difficulty_summary.json`, `results/difficulty_trials.csv`,
`charts/s1_difficulty_ladder.png`, `charts/s2_distribution_collapse.png`

## Why you can trust these numbers

An instrument is worth exactly what its failure modes are worth, so the harness treats its own
validity as a claim to be tested rather than assumed:

- **`assert_provided_shape`** (`harness.py:148`) verifies at start-up that the supplied file really
  does ignore the environment, rather than assuming it.
- **`assert_format_matches_provided`** (`harness.py:655`) proves the harness's byte model matches the
  real `valid_proof` by mining nonces where the two formats must **disagree** — and confirming the
  supplied code rejects exactly the ones only the wrong format satisfies.
- **`genesis_predicate`** (`harness.py:116`) plus identity assertions stop the harness accepting *any*
  HTTP 200 on its port as proof that its own node is up. Without that guard, a leftover node from an
  earlier experiment can be measured while the intended parameters are written into the result file:
  real numbers describing a different system than the row claims, and undetectable downstream.
- **`test_node_isolation.py`** asserts that guard *fires*, against real decoy nodes, with one control
  asserting it does **not** fire on a healthy node. A guard that fires on everything is as useless as
  one that never fires.
- **Falsification discipline.** Every fix was verified by reverting it, confirming the new test fails
  and the old ones still pass, restoring, and checking the file is byte-identical to the tested state.
  A test that has never failed proves nothing.

Raw evidence is kept in `logs/`, and every figure above traces to a stored result file rather than to
the presentation it came from.

## Layout

```
harness.py              the measurement instrument (experiments, guards, CSV/JSON writers)
node.py                 the node under test
phase_demo.py           the prescribed walkthrough, verbatim, with a transcript
probe_verify.py         two independent checks on the reported nonces
rootcause_demo.py       reproduces findings 1 and 2
test_node_isolation.py  falsification suite for the node-identity guard
verify_format_check.py  byte-format verification
results/                every stored result file the figures come from
charts/                 figures, generated by charts_for_slides.py
screenshots/            terminal captures of the prescribed phases
logs/                   raw run logs
```

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install flask requests
.venv/bin/python harness.py all         # the full battery, ~30 min
.venv/bin/python harness.py difficulty  # one experiment by name
.venv/bin/python phase_demo.py          # the prescribed walkthrough
.venv/bin/python test_node_isolation.py
```

`harness.py` writes to `results/`; `charts_for_slides.py` reads those and writes `charts/`.

### The supplied scaffold is included — here is its provenance

`provided/blockchain_assessment.py` is the unit's supplied program, reproduced **unmodified**. It is
included because the assignment recommends publishing the work; it is not my code and no licence is
asserted over it. The file this study was built against is:

```
sha256 f3d754bcbc9bd08018ae118e110f09ab25fdf29aacc142a50bf212dd849ee392
```

That check is not decoration. Every "provided" row in `results/` asserts something *about that file*
— that its mining target is hard-coded to `"0000"` and that it ignores the environment entirely — and
a condition reported from an unverified file is not evidence. `assert_provided_shape`
(`harness.py:148`) reads the file at start-up and fails loudly if the assumption no longer holds,
rather than measuring a different program than the row claims.

Because the file is present at the path those four scripts expect, the repository runs as cloned —
`verify_format_check.py` and `harness.py` both work immediately after `pip install`.

## Provenance and authorship

This is coursework — **CSE5BCC Assessment 1, Option A** — and should be read as such. Two things
about that are worth stating plainly:

- **The node under test is not my design.** `node.py` is the unit's supplied scaffold with three
  documented modifications, listed at the top of that file:

  1. **Difficulty read from the environment** (`DIFFICULTY`, default `"0000"`), so that Phase 3 — which
     asks you to vary the target — never edits the code it is measuring. Every condition then runs the
     *same* code path, so a difference between conditions cannot be an artefact of the file having been
     edited differently for each one. Unset, the behaviour is exactly the supplied behaviour.
  2. **Optional background synchronisation** (`AUTO_SYNC`, default off), which the brief anticipates
     ("if the provided program doesn't automatically handle chain sync, you may need to modify it").
     Because mining and chain-replacement now genuinely run concurrently, this also adds a re-entrant
     lock around the three operations that mutate chain state. It carries two further fixes: a timeout
     and an explicit exception guard on the peer request, so an unreachable peer cannot hang or crash a
     sync; and a latent `NameError` in the supplied `update_blockchain()`, where `length` and `chain`
     are assigned inside `if response.status_code == 200:` but read outside it — so a non-200 reply
     from the first peer raises, and a non-200 from a later peer silently re-tests the *previous*
     peer's chain instead.
  3. **`GET /blockchain` reports the difficulty in force** (one added field). The supplied endpoint
     returns only `chain` and `length`, which have the same shape whatever the target happens to be —
     so a reply from a process configured for difficulty 4 is indistinguishable from one configured for
     difficulty 6, and a measurement taken against the wrong process would be filed under the wrong
     condition with no error raised anywhere. The field makes the varied parameter observable, so the
     harness can assert it measured the process it configured.

  The inherited code was also given a whitespace/formatting pass; that part is behaviour-neutral, but it
  does mean `node.py` is not a byte-for-byte copy of the supplied file. The supplied file itself is
  reproduced unmodified in `provided/`. The original proof-of-work construction, the chain structure
  and the adoption rule are the unit's work; the measurement instrument, the experiments and the
  analysis are mine. All quoted line numbers refer to the supplied file.

- **This was built with AI assistance, and I am not going to obscure that.** I directed a
  locally-hosted AI assistant to help design the measurement harness, write the experiment and
  plotting scripts, and compile the presentation. What to measure, the instrument's self-checks, the
  falsification discipline and every interpretation of the results were decisions I set and reviewed.
  Every experiment ran on my own machine, and every figure was re-derived from the stored result
  files rather than copied out of the presentation.

If you are taking this unit now, close the tab — the point is to measure something yourself.

## Known limitations

- **Scale.** 23 blocks at the hardest target, one machine, loopback only. The network is three
  processes on one host, so nothing here tests real latency, partition, or adversarial behaviour.
- **One experiment is inconclusive.** The `race_fixed` arm of `harness.py:921` tests a synchronisation
  arriving *during* proof of work. It reported no corruption — but for a reason unrelated to the lock:
  it creates its long mining window by running the two nodes at *different* difficulty targets, and a
  node validates a peer's chain against **its own** target, so the slower node rejects the faster
  node's chain outright and the chain is never replaced mid-mine. The "no corruption" result does not
  carry, and the corruption the experiment exists to provoke remains undemonstrated.
  `results/race_fixed_result.json`.
- **No licence file.** The repository carries the unit's supplied scaffold in `provided/` alongside a
  derivative of it in `node.py`, so a blanket licence over the whole repository would be presumptuous. Treat it as coursework, not as
  redistributable code.
