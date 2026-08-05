# Runbook — what to run, in what order

Two reports come out of this repo and they are **independent**. Only the
price-move report needs the NIFTY 50 file.

```
  SETUP (once)
    config/instances.json ── host/port per kdb instance
    config/cas_isins.txt  ── the CAS ISIN whitelist
    python -m casretro --check-config
         │
         ├──────────────────────────────┐
         │                              │
    PATH A                          PATH B
    retrospective                   price move
         │                              │
         │                         build the NIFTY 50 file
         │                          ├─ Route A: nifty50_from_nse.py   (no Bloomberg)
         │                          └─ Route B: bloomberg_check.py
         │                                     bloomberg_nifty50.py   (Bloomberg box)
         │                                     → copy the file across
         │                                     map_nifty50_syms.py    (kdb box)
         │                              │
    python -m casretro            python cas_price_move.py
         │                              │
    output/cas_retro_<date>_<flow>/   output/cas_price_move_universe_<date>.csv
                                     output/cas_price_move_nifty50_<date>.csv
```

---

## 0. Setup — once

### 0.1 Connection details

Fill in `host` and `port` for each role in `config/instances.json`. No
username/password is sent.

| role | tables |
|---|---|
| `oms` | `target`, `target_state`, `workorder`, `execution`, `alerts` |
| `qatt` | `qatt` on both HT (port 17034) and RT (port 17031) |
| `ref` | `equity`, `fx_last` |

### 0.2 CAS universe

Paste the ISIN whitelist from `temp.q` into `config/cas_isins.txt`. The raw
backtick form is fine — every 12-character ISIN token is picked up, duplicates
dropped, check digits validated.

### 0.3 Optional — snapshot the reference data

```bash
python tools/export_cas_universe.py
```

Writes `config/cas_universe.csv`. Once it exists the report reads the universe
from it instead of querying `equity`; delete it, or pass `--no-universe-file`, to
go back to kdb. The ISIN whitelist is still applied at read time, so
`cas_isins.txt` changes need no re-export — only new reference data does.

### 0.4 Verify

```bash
python -m casretro --check-config
python -m casretro --check-config --mode rt     # if you will use the RT tapes
```

Connects to every configured instance and reports what is reachable and which
tables are missing. **Do not skip this** — every later step assumes it passed.

### 0.5 Optional — see the shape without a database

```bash
python tools/selftest.py
```

Runs the whole analytical layer on synthetic frames. No kdb, no pykx connection.

---

## Path A — the retrospective report

Needs §0.1 and §0.2. **Nothing else. No NIFTY file.**

```bash
python -m casretro                                    # yesterday, both flows
python -m casretro --date 2026-08-04 --flow silk
python -m casretro --mode rt --flow both              # intraday, RT tapes
python -m casretro --no-market-data --formats csv     # skip the qatt queries
python -m casretro --keep-no-close                    # see the misses too
```

By default the report covers **close participants**: parents that executed
something *and* sent a CLOSE-venue child order at or after 17:45 HKT. Both
exclusions are printed and recorded in the run parameters.

`--keep-no-close` puts the orders that never reached the auction back, which is
the only way to get the non-participation waterfall (`NO_CLOSE_INSTRUCTION`,
`FULLY_FILLED_BEFORE_CAS`, …) — those rules can only fire on orders the default
excludes. `--keep-unfilled` does the same for orders that traded nothing.

Output: `output/cas_retro_<date>_<flow>/` — CSVs, an `.xlsx`, and a
self-contained HTML page.

**Read `reconciliation` first.** Any row that is not `OK` means a number
elsewhere in the report is suspect, and it says which.

---

## Path B — the price-move report

### Step 1 — build the NIFTY 50 file

Pick one route. Both write the same columns, so everything after is identical.

#### Route A — NSE's public list (no Bloomberg)

Runs anywhere with internet; can run on the kdb box directly.

```bash
python tools/nifty50_from_nse.py --out config/nifty50.csv --expect 50 --resolve-syms
```

`--resolve-syms` chains step 2 in, so this single command covers steps 1 and 2.

No internet on that machine? Download the list anywhere and feed it in — this
mode never touches the network:

```bash
# on any machine:
#   https://archives.nseindia.com/content/indices/ind_nifty50list.csv
python tools/nifty50_from_nse.py --file ind_nifty50list.csv --resolve-syms
```

#### Route B — Bloomberg

**On the Bloomberg machine** (copy `bloomberg_nifty50.py` and
`bloomberg_check.py` across; they need nothing else from this repo):

```bash
python tools/bloomberg_check.py                   # 8 stages, exit 0 if all pass
python tools/bloomberg_nifty50.py --out config/nifty50.csv
```

Run the check first. It walks the same path one stage at a time, so a failure
names the broken thing instead of just saying Bloomberg could not be reached.

Then **copy `config/nifty50.csv` to the kdb machine** and do step 2.

### Step 2 — resolve the syms

On the **kdb machine**. Skip if you used `--resolve-syms`.

```bash
python tools/map_nifty50_syms.py --file config/nifty50.csv
```

Matches `equity.ID_ISIN` against the member's ISIN, and nothing else. Anything
unmatched is reported with the reason:

| `sym_match_rule` | what to do |
|---|---|
| `isin` | matched, nothing to do |
| `no_isin` | the source gave no ISIN — re-run step 1, or fill the `isin` column by hand |
| `isin_not_in_equity` | check the snapshot date, or whether the name sits under another listing |

### Step 3 — run the report

```bash
python cas_price_move.py --host <host> --port <port>
python cas_price_move.py --scope universe           # skip the subset study
python cas_price_move.py --scope nifty              # subset only
python cas_price_move.py --print-query              # the q text, no connection
```

Two studies by default, from one round of queries:

```
output/cas_price_move_universe_<date>.csv     every CAS-eligible Indian sym
output/cas_price_move_nifty50_<date>.csv      the NIFTY 50 constituents
```

Both files carry the same columns, including `in_nifty50`, so the universe file
alone reproduces the subset. A `side by side` block at the end compares them.

Step 1 and step 2 are only needed for the **subset** study. Without
`config/nifty50.csv` the universe study still runs, and the subset is skipped
with a note on how to build the file.

> **Heads-up.** `cas_price_move.py` predates the rest and takes its own
> `--host` / `--port` (default `localhost:5000`), opening **one** connection for
> **both** `equity` (REF) and `qatt` (QATT-HT). Every other script reads
> `config/instances.json` and connects to each instance separately. So this step
> only works as written if a single kdb process serves both tables. If REF and
> QATT-HT are separate processes, it will fail on whichever table is not there.

---

## Path C — how CAS impacts the NIFTY 50

`casStudy.py`. Same tape, different question: not what our orders did, but what
the auction did to the index. The reasoning is in
[`cas_study_method.md`](cas_study_method.md); the columns are in
[`cas_study_columns.csv`](cas_study_columns.csv).

**Before touching kdb** — the analytical layer runs offline:

```bash
python tools/selftest_casstudy.py
```

**Then, on the kdb box:**

```bash
# 1. look at the query before sending it
python casStudy.py --print-query

# 2. one day, with the official index levels so the reconciliation can pass/fail
python casStudy.py --date 2026-08-04 \
    --index-level 24812.05 --index-level-prev 24735.20
```

Read the output top down. **S1** must show a populated control arm — non-CAS
names printing in the close window — or S5 has nothing to subtract. **S2** must
show the auction prints clustering on one instant; a wide spread means the
17:58–18:00 window is catching ordinary trades. The **check** block at the bottom
is the one that matters on day one: if the rebuilt index return disagrees with
the official move by more than a few bps, stop and fix the weights before quoting
anything above it.

Two inputs it needs beyond the CAS whitelist: `config/nifty50_weights.csv` (index
weights, currently 49 of 50 members) and, for the control arm, a **whole**
universe export — `python tools/export_cas_universe.py --no-isin-filter`. A
CAS-only snapshot is rejected, because the non-CAS names *are* the control.

Add `--append-panel` to accumulate one row per group per day into
`output/casstudy_panel.csv`; a single day is an observation, the panel is what
makes it evidence.

---

## Day to day

Only the final step of each path repeats:

```bash
python -m casretro                    # Path A
python cas_price_move.py              # Path B
python casStudy.py --append-panel     # Path C
```

The NIFTY 50 file is static between index rebalances — NSE reviews
semi-annually, in March and September. Re-run Path B step 1 then, or whenever a
constituent changes.

---

## When something breaks

| symptom | first thing to run |
|---|---|
| any kdb connection problem | `python -m casretro --check-config` |
| you want to see every query sent | `python -m casretro --show-queries 2> queries.log` |
| Bloomberg says a package is missing | `python tools/bloomberg_nifty50.py --diagnose` |
| a Bloomberg pull fails | `python tools/bloomberg_check.py` — it names the failing stage |
| a number looks wrong | the `reconciliation` sheet of the retrospective |
| you want to audit a query | `python tools/dump_queries.py --out docs/queries.md` |
| the price-move query specifically | `python cas_price_move.py --print-query` |
| the index-study query specifically | `python casStudy.py --print-query` |
| you changed classification rules | `python tools/selftest.py` |
| you changed the index study | `python tools/selftest_casstudy.py` |
| the index effect looks too big | the `check` block — reconciliation against the official close-to-close |

`--traceback` on `bloomberg_nifty50.py` prints the full stack instead of the
summary.
