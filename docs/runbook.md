# Runbook — what to run, in what order

Three reports come out of this repo and they are **independent**, answering
different questions off the same tapes:

| path | question | needs |
|---|---|---|
| A — `casretro` | what did **our orders** do in the auction? | §0.1, §0.2 |
| B — `cas_price_move` | how far did prices move into the close? | + the NIFTY 50 file |
| C — `casStudy` | what did the auction do to the **index**? | + index weights, whole-book universe |

```
  SETUP (once)
    config/instances.json ── host/port per kdb instance
    config/cas_isins.txt  ── the CAS ISIN whitelist
    python -m casretro --check-config
         │
         ├──────────────────────────────┬───────────────────────────┐
         │                              │                           │
    PATH A                          PATH B                      PATH C
    retrospective                   price move                  index impact
         │                              │                           │
         │                         build the NIFTY 50 file     config/nifty50_weights.csv
         │                          ├─ Route A: nifty50_from_nse.py    (index weights)
         │                          └─ Route B: bloomberg_check.py     export_cas_universe.py
         │                                     bloomberg_nifty50.py      --scope all
         │                                     → copy the file across  (the control arm)
         │                                     map_nifty50_syms.py          │
         │                              │                                   │
    python -m casretro            python cas_price_move.py          python casStudy.py
         │                              │                                   │
    output/cas_retro_<date>_<flow>/   output/cas_price_move_universe_<date>.csv
                                     output/cas_price_move_nifty50_<date>.csv
                                                              output/casstudy_syms_<date>.csv
                                                              output/casstudy_index_<date>.csv
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
python tools/export_cas_universe.py                 # both files
python tools/export_cas_universe.py --scope all     # the one Path C needs
```

Two files, because the consumers want different things:

| file | contents | used by |
|---|---|---|
| `config/cas_universe.csv` | the CAS-eligible subset | `casretro` |
| `config/india_universe.csv` | the whole Indian book | `casretro` **and** `casStudy` |

Once a file exists the universe is read from it instead of querying `equity`;
delete it, or pass `--no-universe-file`, to go back to kdb. The ISIN whitelist is
applied again at read time, so `cas_isins.txt` changes need no re-export — only
new reference data does.

**Path C needs the whole-book file.** Its control arm *is* the non-CAS names, so
a CAS-only snapshot leaves it with nothing to control against, and it refuses to
run on one rather than quietly reporting an unadjusted number.

The universe is **NSE `.IN` listings only**. `--suffixes .IN,.IS,.IB` widens it
to every Indian listing line if you need them; whichever was used is recorded in
the file's `universe_suffixes` column. Check the count the exporter prints — if
the CAS-eligible number comes back below the number of ISINs in your whitelist,
some names live on a listing line the filter is excluding.

### 0.4 Verify

```bash
python -m casretro --check-config
python -m casretro --check-config --mode rt     # if you will use the RT tapes
```

Connects to every configured instance and reports what is reachable and which
tables are missing. **Do not skip this** — every later step assumes it passed.

### 0.5 Optional — see the shape without a database

```bash
python tools/selftest.py             # Path A: the retrospective
python tools/selftest_casstudy.py    # Path C: the index study
```

Both run the whole analytical layer on synthetic frames — no kdb, no pykx
connection. Run the matching one after any change to the rules it guards.

---

## Path A — the retrospective report

Needs §0.1 and §0.2. **Nothing else. No NIFTY file.**

```bash
python -m casretro                                    # yesterday, both flows
python -m casretro --weekly                           # Monday to today
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

Output: `output/cas_retro_<date>_<flow>/` — CSVs, an `.xlsx`, and **two**
self-contained HTML pages:

| file | for | what is on it |
|---|---|---|
| `cas_retro_<date>_<flow>.html` | the desk, quant | every section, every column, every row, plus the reconciliation checks |
| `cas_retro_<date>_<flow>_trader.html` | traders, clients | four questions and then it stops — plain English, IST clocks, ₹ in crore/lakh, no order ids and no reason codes |

Drop either with `--formats`: `html` is the quant page, `trader` the client one.

**Read `reconciliation` first.** Any row that is not `OK` means a number
elsewhere in the report is suspect, and it says which. The trader page says so
too, at the top, in a sentence — so a page that goes out never hides a failed
check.

### A — the Friday review

Run at the end of the week, after Friday's close:

```bash
python -m casretro --weekly
```

That runs the same pipeline once per business day from Monday up to today, folds
the days into one report, and writes both HTML pages for the week. The anchor is
**today**, not the last business day, precisely so a Friday-evening run includes
Friday. Reviewing last week on the Monday after, name the Friday:

```bash
python -m casretro --weekly --date 2026-08-07
python -m casretro --from 2026-07-27 --to 2026-08-07 --per-day   # any range
```

Each day comes off the tape that actually has it:

| run on | days | source |
|---|---|---|
| Thursday | Mon–Wed / **Thu** | HT / **RT** |
| Friday, after the close | Mon–Thu / **Fri** | HT / **RT** |
| Saturday or Sunday | Mon–Thu / **Fri** | HT / **RT** |
| the Monday after | Mon–Fri | HT — RT is never opened |

The **live day** — the most recent business day — is read from the RT tapes,
because the HDB has usually not written it down yet. If the tapes have already
handed it over, the run falls back to HT and everything comes from there, so the
same command works on either side of the write-down. A past day is never read
from RT: the tape holds whatever it holds *now*, and would be stamped with a date
it does not belong to.

The `by_day` table's `source` column says which tape served each day, and the
report's mode reads `ht+rt` when a week mixes them. `--rt-today off` keeps it on
the HDB entirely; `--rt-today force` disables the fallback.

> The RT order tables get no date predicate server-side, so the day is enforced
> row by row after loading. The `qatt` queries aggregate server-side and cannot
> be filtered afterwards — when the live day's market data comes off an RT tape,
> both HTML pages say so.

What to know before quoting a weekly number:

* a day with no parent order — a holiday, nothing traded, or a day that is on
  neither tape — is **dropped, not counted as a zero**, and named in the
  warnings. Check that line first: five days in the range does not mean five days
  in the numbers;
* quantities and counts are summed; **every percentage is recomputed from the
  summed quantities**, so a quiet Monday does not weigh the same as a heavy
  Friday;
* close capture is weighted by the size that actually traded in the auction;
* the new `by_day` section, and the charts above it, are the point of the
  exercise — one bad print and a habit look identical on a single day;
* `--per-day` also writes each day's own report under `<out>/days/<date>/`;
* `--mode rt` is refused here: it would put *every* day on the tapes, which carry
  no date, so every day would return the same rows. The live day already comes
  from RT without it.

Output: `output/cas_retro_week_<start>_<end>_<flow>/`.

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

### Inputs

Beyond §0.1 and §0.2:

| input | why | if missing |
|---|---|---|
| `config/nifty50_weights.csv` | the index weights every weighted number rests on (49 of 50 members today) | no index effect, no attribution |
| `config/india_universe.csv` — `--scope all` | the non-CAS names **are** the control arm | falls back to kdb; a CAS-only snapshot is rejected |
| official NIFTY 50 closes, today and previous | turns the reconciliation into pass/fail, and bps into points | the check reports itself unavailable |

### Run

```bash
# 1. look at the query before sending it
python casStudy.py --print-query

# 2. one day, with the official index levels so the reconciliation can pass/fail
python casStudy.py --date 2026-08-04 \
    --index-level 24812.05 --index-level-prev 24735.20
```

### Read it top down — each block gates the next

**S1 — is there a control arm?** It prints how many non-CAS names actually print
inside the close window. Below 20 the drift adjustment is withheld and S5 says
so; the effect then stands as a *realised* move, not an isolated one. If none of
them print, they have either migrated to CAS as well or they do not trade that
late, and no amount of arithmetic downstream will fix it.

**S2 — is the window catching the auction?** The prints should cluster on
essentially one instant, because the freeze is market-wide. A wide spread, or a
large `no_close_price` count, means 17:58–18:00 is picking up ordinary trades and
everything below is measuring the wrong thing.

**S3/S4 — the answer.** The effect against the old rule, the index effect, and
the names that caused it. A t-statistic under 2 is printed as *not
distinguishable from zero* — read it before quoting the number.

**check — the one number from outside this codebase.** If the rebuilt index
return disagrees with the official close-to-close by more than a few bps, stop:
the weights are stale or a constituent's close was not read, and every number
above it is suspect.

### Knobs you may need

| situation | flag |
|---|---|
| the reconciliation says no previous close was found | `--prev-close-col <name>` |
| the market-data table is not called `qatt` | `--qatt-table <name>` |
| you want the old 17:30–18:00 window (contaminated for CAS names — controls only) | `--old-rule-window clock-1730-1800` |
| a snapshot exists but you want to hit kdb | `--no-universe-file` |

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

And once a week, on Friday after the close:

```bash
python -m casretro --weekly           # Path A, Monday to Friday in one report
```

The NIFTY 50 file is static between index rebalances — NSE reviews
semi-annually, in March and September. Re-run Path B step 1 then, or whenever a
constituent changes, and refresh `config/nifty50_weights.csv` at the same time:
`casStudy` warns when its `asof` is more than 45 days behind the study date,
because a missed rebalance corrupts every weighted number silently.

---

## When something breaks

| symptom | first thing to run |
|---|---|
| any kdb connection problem | `python -m casretro --check-config` |
| you want to see every query sent | `python -m casretro --show-queries 2> queries.log` |
| Bloomberg says a package is missing | `python tools/bloomberg_nifty50.py --diagnose` |
| a Bloomberg pull fails | `python tools/bloomberg_check.py` — it names the failing stage |
| a number looks wrong | the `reconciliation` sheet of the retrospective |
| a weekly number looks wrong | the `by_day` sheet — which day moved it, which tape it came off, and is a day missing |
| the live day is missing from a weekly run | is an `oms` `rt` instance configured and reachable? `--check-config --mode rt` |
| you changed the weekly roll-up or either HTML page | `python tools/selftest.py` |
| you want to audit a query | `python tools/dump_queries.py --out docs/queries.md` |
| the price-move query specifically | `python cas_price_move.py --print-query` |
| the index-study query specifically | `python casStudy.py --print-query` |
| you changed classification rules | `python tools/selftest.py` |
| you changed the index study | `python tools/selftest_casstudy.py` |
| the index effect looks too big | the `check` block — reconciliation against the official close-to-close |
| the universe looks too short | the exporter's count line; `--suffixes .IN,.IS,.IB` to widen |
| `casStudy` refuses the universe file | it is the CAS-only snapshot — export `--scope all` |

`--traceback` on `bloomberg_nifty50.py` prints the full stack instead of the
summary.
