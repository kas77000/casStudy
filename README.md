# CAS India — execution retrospective

Post-trade report on **CAS-eligible Indian stocks** traded on the platform: which
parent orders made it into the closing auction, which ones did not and **why**,
what got rejected — split between continuous trading and the CAS window — and how
our volume sat against the desk's benchmark closing-bin shares.

Reads the algo kdb+ stack through **pykx**. All times are **HKT** (IST = HKT − 02:30),
matching the raw `time` columns of the tables.

---

## 1. Install

```bash
pip install pykx pandas numpy openpyxl
```

`openpyxl` is only needed for the `.xlsx` output; without it the workbook is
skipped and CSV/HTML still run.

## 2. Configure

### 2.1 kdb instances — `config/instances.json`

Fill in `host` and `port` for each role. No username/password is sent.

| role | instance | tables |
|---|---|---|
| `oms` | `OMS-HT` / `OMS-RT` | `target`, `target_state`, `workorder`, `execution`, `alerts` |
| `qatt` | `QATT-HT` (`qatt_17034`) / `QATT-RT` (`qatt_17031`) | market data |
| `ref` | `REF` | `equity`, `fx_last` |

`"partitioned": true` makes every query emit `where date=d, …`; `false` drops the
date predicate entirely, which is what the RT tapes need (`qatt_17031` has no
`date` column). Pick the set with `--mode ht` (default) or `--mode rt`.

Check the wiring before you trust a report:

```bash
python -m casretro --check-config
python -m casretro --check-config --mode rt
```

### 2.2 CAS universe — `config/cas_isins.txt`

Paste the ISIN whitelist from `temp.q` into this file. The raw backtick form is
fine — the loader picks up every 12-character ISIN token, ignores `#` / `/`
comment lines, drops duplicates and validates the check digit, so a mangled paste
is reported rather than silently shrinking the universe.

```
`INE180A01020`INE935A01035`INE451A01017`…
```

The universe query is `temp.q` verbatim: `equity`, last business day resolved
server-side, `sym like "*.IN" | "*.IS" | "*.IB"`, `ID_ISIN in <whitelist>`.
`--no-isin-filter` takes every `.IN/.IS/.IB` listing instead — useful to check
whether the whitelist is dropping names it should not.

## 3. Run

```bash
# yesterday, both flows, CSV + Excel + HTML into output/
python -m casretro

# one flow, one date
python -m casretro --date 2026-08-03 --flow silk

# intraday, against the real-time tapes (no date predicate)
python -m casretro --mode rt --flow both

# skip the market-data section (no reference price, band check, volume share)
python -m casretro --no-market-data --formats csv
```

`--flow silk | agency | both`. A `basket` containing `SILK` is SILK flow,
everything else is Agency. With `both`, `flow` is a column and every aggregate is
broken down side by side plus a `TOTAL` row.

The report covers **orders that executed — partially or completely**. A parent
that put nothing away at all is dropped before anything is counted, whatever the
reason it went nowhere: it describes intent rather than execution, and it only
inflates `NOT_SENT` and depresses the participation rate.

The test is on quantity alone, so **a rejected order that still completed a
percentage stays in** — as does one that was cancelled, arrived late, or was
priced out, as long as some quantity traded. Only `exec_qty = 0` goes.

The count and the quantity removed are printed and land in the run parameters of
every output. `--keep-unfilled` puts them back (`config.DROP_UNFILLED_ORDERS` is
the default).

Output lands in `output/cas_retro_<date>_<flow>/`.

### Running without a database

```bash
python tools/selftest.py
```

Builds synthetic frames matching the real schemas, runs the whole analytical
layer and the three writers, and asserts that every branch of the
non-participation waterfall fires. Use it to see the report shape before pointing
at kdb, and as a regression test after changing the classification rules.

---

## 4. The CAS session calendar

Straight from the India CAS deck. Everything in the code references
`casretro/config.py`, so a rule change is a one-line edit.

| # | Session | HKT | IST | What the exchange allows |
|---|---|---|---|---|
| — | Continuous — final 15 min | 17:30–17:45 | 15:00–15:15 | feeds the reference-price VWAP |
| 1 | Ref price calc / CTS→CAS | 17:45–17:50 | 15:15–15:20 | **no order action at all** |
| 2 | Order entry — limit **and** market | 17:50–17:55 | 15:20–15:25 | within ±3% of the reference price |
| 3 | Order entry — limit **only** | 17:55–18:00 | 15:25–15:30 | market orders refused |
| 3A | Random close | 17:58–18:00 | 15:28–15:30 | the auction can freeze any time |
| 4 | Order matching | 18:00–18:05 | 15:30–15:35 | **the close prints here** |
| 5 | Buffer | 18:05–18:20 | 15:35–15:50 | no trading |
| 6 | Post close / trading-at-last | 18:20–18:30 | 15:50–16:00 | at the closing price |

Execution priority in the auction: **market orders → carried-over limit orders
(unmodified from CTS) → new limit orders placed during CAS.**

### Reference price and the ±3% band

```
VWAP over 15:00–15:15 IST (17:30–17:45 HKT)
  └─ no trades in that window?  → last traded price earlier today
       └─ no trades at all today? → previous adjusted close
```

Every CAS order must be priced within **±3%** of that reference; outside the band
the exchange rejects it. The report rebuilds the reference price per sym and
records which rule produced it (`ref_source`), so the band check can be audited
rather than trusted.

---

## 5. What the report answers

### 5.1 Close participation

Each parent order lands in exactly one bucket:

| bucket | meaning |
|---|---|
| `FILLED_IN_CLOSE` | traded in the auction |
| `SENT_NOT_FILLED` | a child order reached the auction but never traded |
| `NOT_SENT` | no child order was ever sent to the auction |

A child order counts as "of the close" when its **`venue`** contains `CLOSE` —
`venuetype` is not consulted, since it can read `CLOSE` on a child order that was
only close-*eligible*. The parents that traded in the auction are then exactly
those reached by tracing those child orders' `id_work` back to `id_target` on the
execution tape.

**The clock has no say — anywhere.** A fill printing inside 17:45–18:05 on a
continuous venue is continuous; a close-venue fill stays close however late it
reports. That applies to the `phase` column too, which is what the close
rejection and cancellation counts key off: `phase` is `CLOSE` if and only if the
child order's venue says `CLOSE`, otherwise `CONTINUOUS`.

The time is still recorded, just never consulted for this decision — every
rejection, cancellation and child order carries `bucket` / `event_bucket` (the
CAS session it happened in) and `event_phase` / `time_phase` (the clock-only
view) beside `phase`. So a continuous child order refused at 17:52 is still
visible as such; it simply no longer counts as a close rejection.

A fill whose `id_work` matches no child order cannot be credited to the close at
all — `reconciliation` counts those under *"every fill traces back to a child
order"*, and a non-zero count there means close quantity is being understated.

Three reconciliation checks defend the invariant, and the selftest fails if any
of them reports:

- *close quantity only ever comes from a close-venue child order*
- *only parents with a close-venue child order leave the `NOT_SENT` bucket*
- *each child order's venue is consistent across its rows*

### 5.2 Why an order missed the close

A **waterfall** — the first condition that fires wins, and any later condition
that also held is still recorded in `reason_detail`, so a second cause is never
hidden behind the first.

**Nothing was ever sent (`NOT_SENT`)**

| order | code | evidence |
|---|---|---|
| 1 | `NOT_CAS_ELIGIBLE` | sym outside the CAS universe (only reachable with `--no-isin-filter`) |
| 2 | `NO_CLOSE_INSTRUCTION` | `target.doclose = 0` |
| 3 | `FULLY_FILLED_BEFORE_CAS` | `target_state.open` was already 0 at 17:45 |
| 4 | `PARENT_CANCELLED_BEFORE_CAS` | parent hit `cxl:*` before 17:45 |
| 5 | `PARENT_DONE_BEFORE_CAS` | parent hit any terminal state before 17:45 |
| 6 | `ORDER_END_BEFORE_CAS` | `t_end ≤ 17:40` — the "participate in close = N" profile |
| 7 | `ORDER_ARRIVED_AFTER_ENTRY_CLOSED` | first state / `t_start` at or after 18:00 |
| 8 | `LIMIT_OUTSIDE_PRICE_BAND` | client limit below the band on a buy (above on a sell) — nothing legal to send |
| 9 | `NO_MARKET_DATA` | no prints at all on the day; likely halted |
| 10 | `ALGO_NEVER_COMMITTED_TO_CLOSE` | residual live but `make_close`/`commit_close` stayed at 0 |
| 11 | `BLOCKING_ALERT` | an alert fired on the parent inside the CAS window |
| 12 | `UNEXPLAINED` | nothing matched — **investigate**; the reconciliation sheet counts these |

**Sent but never traded (`SENT_NOT_FILLED`)**

| order | code |
|---|---|
| 1 | `CLOSE_ORDER_REJECTED` — with the reject text |
| 2 | `CLOSE_ORDER_CANCELLED` — with the `cxl:<reason>` decoded |
| 3 | `SENT_AFTER_ENTRY_CLOSED` — left after 18:00 |
| 4 | `MARKET_ORDER_IN_LIMIT_ONLY_PHASE` — market child between 17:55 and 18:00 |
| 5 | `PRICE_OUTSIDE_PRICE_BAND` |
| 6 | `NOT_MATCHED_IN_AUCTION` — stood in the book, the clearing price never reached it |

### 5.3 Rejections and cancellations

Rejections are read from **both** `workorder.state` and `execution.ostat`, then
de-duplicated per child order — the same refusal usually lands on both tapes, and
counting it twice would double the numbers. The exchange's free text
(`execution.comment`, typically FIX tag 58) is carried onto the surviving row as
`exchange_text`.

Split into `CONTINUOUS` / `CLOSE` **by venue**: a close-venue child order is
`CLOSE`, everything else is `CONTINUOUS`. The session it actually happened in is
in the `bucket` column beside it, so "refused during the CAS window on a
continuous venue" is still a visible combination — it is just not counted as a
close rejection. Cancellations get the same treatment, with `cxl:<reason>`
decoded into a `reason` column so the taxonomy can be counted.

---

## 6. Output

CSV (one file per section), a multi-sheet `.xlsx`, and a self-contained
theme-aware HTML page.

| sheet | contents |
|---|---|
| `summary` | headline numbers per flow, plus TOTAL |
| `benchmark` | volume share vs the desk benchmarks |
| `orders` | one row per parent order — every derived column |
| `non_participation` | the orders that did not trade in the close, with the diagnosed cause |
| `rejections` | rejected child orders, `CONTINUOUS` vs `CLOSE` |
| `cancellations` | cancelled child orders with the reason decoded |
| `mix_otype_basket` | size / make / fill rate by order type (market vs limit) and basket |
| `mix_flow_venue_otype` | size / make / fill rate by flow, venue and order type |
| `timing` | CAS deadline and order-type compliance flags |
| `sym_stats` | per-symbol volume profile and our participation |
| `ref_price_band` | reference price, its source, and the ±3% band |
| `alerts` | alerts on parent orders |
| `workorders` | child orders |
| `reconciliation` | data-quality checks |
| `session_calendar` | the calendar above, HKT and IST |

### The two mix tables

Both live at the **child-order** level: `venue` exists nowhere else, and
market-vs-limit is a child-order property — which is the distinction the exchange
enforces during the limit-only phase (17:55–18:00 HKT). `flow` and `basket` are
carried down from the parent.

| column | meaning |
|---|---|
| `n_child_orders` / `n_parents` / `n_syms` | how much the row is built on |
| `size` | quantity ordered — what we sent |
| `make` | quantity executed, summed off the execution tape by `id_work` |
| `fill_rate_pct` | `make / size × 100` |

The OMS `workorder.make` column is **not** what `make` means here: on the desk
`make` is the executed quantity, which is what divides into a fill rate.

`otype` is normalised to `MARKET` / `LIMIT`; anything else is passed through
upper-cased rather than bucketed, so an unexpected order type is visible instead
of hidden. Each table ends with a `TOTAL` row.

### Metrics worth knowing about

- `fill_pct`, `residual`, `close_pct_of_order`, `close_pct_of_executed`
- `close_capture_bps` — our auction fill price vs the close print. **Positive =
  we beat the print**, on either side.
- `perf_vs_close_bps`, `perf_vs_strike_bps`, `perf_vs_vwap_bps` — whole-parent
  performance against the close, the arrival strike and the target VWAP.
- `adverse_move_bps` — the move from the last continuous print to the close,
  signed by the order's side. Positive = the market went against us.
- `residual_notional_at_close`, `missed_close_pnl` — what the unexecuted residual
  was worth at the close, and what completing it there would have cost.
- `flag_*` — market child order in the limit-only phase, order action inside the
  17:45–17:50 no-action window, sent after the 17:58 random close, sent after
  18:00.

### Benchmarks

From the desk mail, in `config.Benchmarks`:

- historical CAS-eligible closing-bin average **17.29%** (6–30 Jul 2026, 19 days,
  range 13.61–22.01%)
- day 1: CTS **9.90%**, close auction **2.09%**, combined **11.99%**

---

## 7. Assumptions to check against your data

These are the places where the code makes a judgement call. Each is a one-line
change if your data says otherwise.

1. **`config.QATT_TRADE_FILTER`** — which `qatt` records count as trades. It
   currently defaults to `not null price, not null size, size > 0`, which will
   also pick up quote records if your feed carries a price on them. Once you know
   the `typ` domain, tighten it to e.g. `typ in \`trade\`auction, size > 0`. The
   `reconciliation` sheet compares the summed prints against the feed's own
   `totalVolume`; a large gap means this filter needs work.

2. **The day-1 "CTS window".** The mail calls **15:15–15:30 IST** the *final
   15-minute continuous trading session*, but the deck puts the CTS→CAS switch at
   15:15 — so for a CAS name that window is the auction call, not continuous
   trading. The report does not pick a side: `sym_stats` carries the volume for
   **every** session bucket, and `benchmark` reports the mail's window
   (17:45–18:00 HKT), the auction print (18:00–18:05 HKT) **and** the full
   30-minute historical closing bin (17:30–18:00 HKT) separately. Compare the
   three against your own read before quoting a single number.

3. **Rejection / cancellation state strings.** `workorder.state` is matched with
   `cxl*` → cancelled, `*rej*` → rejected, `fill*`/`done*` → filled. If your
   states use other spellings, adjust `classify.workorder_state_kind`.

4. **`t_end` as a participation signal.** The deck's close-participation table
   maps `Y`/blank → 18:05 HKT, `N` → 17:45 HKT for CAS names (18:00 for non-CAS).
   Our own orders do not follow it: most parents that *do* trade in the close
   carry a `t_end` between 17:40 and 17:45 HKT, so `t_end ≤ 17:45` would flag the
   participating majority. The waterfall therefore uses
   `t_end ≤ config.TEND_NO_CLOSE_CUTOFF` (17:40 HKT) as the "N" signal, ranked
   *below* `doclose`, so an explicit `doclose = 0` always wins.

5. **`RESIDUAL_ABS_TOL` / `RESIDUAL_PCT_TOL`** — how small a residual has to be
   before the order counts as done rather than as a genuine miss. Currently 0
   shares / 5 bps of the parent.

---

## 8. Ideas not built yet

Worth a conversation before adding — each is cheap on top of what is already
loaded:

- **Auction imbalance context.** If the feed publishes the indicative equilibrium
  price and imbalance during 17:50–18:00, comparing our limit against the
  indicative price explains `NOT_MATCHED_IN_AUCTION` precisely instead of by
  elimination.
- **Order size vs the auction.** Close quantity as a share of the printed auction
  volume and of `equity.adv` — flags where we were large enough to move the print.
- **Multi-day trend.** Run the report over a date range and track participation
  rate, reason mix and the closing-bin share day by day; the day-1 numbers only
  mean something as a series.
- **Trader / algo / client league table.** Repeat offenders for
  `ALGO_NEVER_COMMITTED_TO_CLOSE` or late arrivals are usually a workflow problem,
  not a market one.
- **Reverse check on the universe.** CAS-eligible names where we had *no* order at
  all — the opportunity cost of not being in the auction.
- **Post-close (trading-at-last) usage.** Residual that missed the auction could
  still have traded 18:20–18:30 at the closing price; the session bucket is
  already computed, the analysis is not.
- **USD notionals** via `REF.fx_last`, so SILK and Agency totals are comparable
  across currencies. Left out because the direction of the `fx_last` quote is not
  documented in the schema — confirm it and it is a two-line change.

---

## 9. Layout

```
casretro/
  config.py      session calendar, thresholds, benchmarks, instance wiring
  kdbio.py       pykx connections, date-predicate handling, pandas normalisation
  universe.py    ISIN whitelist + the temp.q universe query
  loaders.py     one function per table; all aggregation pushed server-side
  sessions.py    timestamp -> CAS session bucket / phase
  classify.py    state parsing, close participation, the "why not" waterfall
  metrics.py     reference price, price band, volume share, slippage, timing
  build.py       load_frames() talks to kdb; assemble() is pure pandas
  report.py      console, CSV, Excel, HTML writers
  cli.py         argument parsing and orchestration
tools/
  selftest.py          synthetic end-to-end run, no database needed
  dump_queries.py      print every q query without touching a database
  nifty50_from_nse.py  NIFTY 50 members from NSE's public list (no Bloomberg)
  bloomberg_nifty50.py NIFTY 50 members via blpapi           (Bloomberg machine)
  bloomberg_check.py   8-stage check that Bloomberg works    (Bloomberg machine)
  map_nifty50_syms.py  resolve each member to a kdb sym      (kdb machine)
config/
  instances.json host/port per instance
  cas_isins.txt  the CAS ISIN whitelist
  nifty50.csv    NIFTY 50 members, weights and syms (generated, gitignored)
docs/
  queries.md     the generated query reference
cas_price_move.py  standalone: price move between end of continuous and the close
```

Queries are q lambdas with explicit parameters — nothing is interpolated into a
query except table and column *names*, and syms/ids are pushed in chunks so a
1500-name universe never builds a monster IPC message.

---

## 10. NIFTY 50 subset — `cas_price_move.py`

`cas_price_move.py` measures the move between the end of continuous trading and
the close. It now runs on the **NIFTY 50 subset** of the CAS universe and reports
the reference price and the volume either side of the auction.

### 10.1 Building the NIFTY 50 file

**Step 1 — get the members.** Two routes. Both write the same columns, so
everything downstream is identical.

#### Route A — NSE's public list (no Bloomberg)

```bash
python tools/nifty50_from_nse.py --out config/nifty50.csv --expect 50
python tools/nifty50_from_nse.py --file ind_nifty50list.csv    # offline
python tools/nifty50_from_nse.py --resolve-syms                # steps 1 and 2 in one go
```

NSE publishes the constituent list at
`https://archives.nseindia.com/content/indices/ind_nifty50list.csv` as

```
Company Name, Industry, Symbol, Series, ISIN Code
```

It carries **ISIN**, which is the only key `map_nifty50_syms.py` matches on — so
the universe half of the job is fully covered by a free, public file. Verified:
50 members, 50 with an ISIN.

**Index weights are not collected.** NSE publishes them only in the monthly
factsheet PDF, and anything derivable from the reference data we hold is full
market cap rather than free float — an approximation dressed up as a fact. The
report does not carry a weight column, so nothing needs one. `weight_pct` stays
in the file schema and is passed through if a source supplies it; the column is
simply written empty here.

`--list-name` takes any NSE index file (`ind_nifty500list`,
`ind_niftynext50list`, …). If the kdb box has no internet, download the CSV
anywhere and pass `--file`; that mode never touches the network.

#### Route B — Bloomberg (`blpapi`)

Equivalent for the report's purposes — Route A is simpler and needs nothing
licensed. Use this one if you already have a Terminal and would rather source the
basket from Bloomberg, or if you want the published free-float weights in the
file for something else. Needs a Terminal or B-PIPE session and `blpapi`. Copy
`bloomberg_nifty50.py` and `bloomberg_check.py` across; they depend on nothing
else in this project.

The weights come along for free here: `INDX_MWEIGHT` *is* the member-list field,
so the pull cannot drop them. They land in `weight_pct` and go unread.

Check the setup first — it walks the same path one stage at a time, so a failure
says *which* thing is broken:

```bash
python tools/bloomberg_check.py                     # 8 stages, exit 0 if all pass
python tools/bloomberg_check.py --date 2026-08-04   # also exercises END_DATE_OVERRIDE
python tools/bloomberg_check.py --verbose           # show the data each stage got
```

```
  [1/8] environment                             PASS   python 3.11.4 at C:\...\python.exe
  [2/8] import blpapi                           PASS   blpapi 3.24.6 loaded
  [3/8] session / //blp/refdata                 PASS   connected to localhost:8194
  [4/8] reference data on 'NIFTY Index'         PASS   NAME = NIFTY 50 Index
  [5/8] INDX_MWEIGHT (current basket)           PASS   50 members, weights sum to 100.00%
  [6/8] INDX_MWEIGHT_HIST + END_DATE_OVERRIDE   PASS   50 members as of 2026-08-04
  [7/8] ID_ISIN on the members                  PASS   all 5 sampled members have an ISIN
  [8/8] end-to-end fetch (the real code path)   PASS   50 rows, 50 with an ISIN
```

Stage 7 is not decoration: `map_nifty50_syms.py` matches on ISIN and nothing
else, so a member without one cannot be mapped at all.

Then the pull itself:

```bash
python tools/bloomberg_nifty50.py --out config/nifty50.csv
python tools/bloomberg_nifty50.py --date 2026-08-04       # that day's basket
python tools/bloomberg_nifty50.py --index "NIFTY Index"   # or any other index
python tools/bloomberg_nifty50.py --diagnose              # is blpapi importable, and from where
python tools/bloomberg_nifty50.py --traceback             # full stack on failure
```

Weights come from `INDX_MWEIGHT_HIST` with an `END_DATE_OVERRIDE`. If that field
returns nothing the script falls back to `INDX_MWEIGHT` — the *current* basket —
and says so loudly, because a file labelled with a past date that quietly holds
today's weights is worse than no file.

> **If it reports blpapi as not installed when you know it is:** that was a bug,
> fixed. `blpapi` raises a plain `ImportError` — not `ModuleNotFoundError` — when
> the wheel is installed but its **C++ SDK library** cannot be loaded, and that
> was being reported as a missing package. The two are now told apart, and
> `--diagnose` prints the interpreter in use, `BLPAPI_ROOT`, and the real import
> error.

**Step 2 — on the kdb machine.** Copy the file across, then:

```bash
python tools/map_nifty50_syms.py                        # config/nifty50.csv, in place
python tools/map_nifty50_syms.py --equity-csv dump/equity.csv   # no kdb needed
python tools/map_nifty50_syms.py --fail-on-unmatched    # for a scheduled run
```

It fills in `sym` and `sym_match_rule` by matching **ISIN and nothing else**:
`equity.ID_ISIN` == the member's Bloomberg `ID_ISIN`. Ticker strings drift
between vendors and exchanges, so a `TICKER` or `sym_blp` fallback would quietly
map the wrong instrument the day one of them changes — and a wrong sym costs far
more than a missing one.

Everything that does not match is reported, split by what you would have to do
about it:

| `sym_match_rule` | meaning | fix |
|---|---|---|
| `isin` | matched | — |
| `no_isin` | Bloomberg returned no `ID_ISIN` for the member | re-run step 1, or fill the `isin` column by hand |
| `isin_not_in_equity` | the ISIN is absent from `equity` on that date | check the snapshot date, or whether the name sits under another listing |

One ISIN can legitimately hit several listings — a dual `.IN`/`.IB` line is the
same security twice. That is resolved by `SYM_PREFERENCE` (`.IN` first, since the
NIFTY 50 is an NSE index) and every candidate is written to `sym_candidates`, so
the choice stays visible.

Because ISIN is the only key, step 1 warns loudly if any member came back without
one: those rows cannot be mapped at all.

`config/nifty50*.csv` is gitignored: index weights are licensed Bloomberg data.

### 10.2 What the price-move report now carries

```bash
python cas_price_move.py                      # NIFTY 50 subset of the CAS universe
python cas_price_move.py --no-nifty-filter    # the whole CAS universe, as before
python cas_price_move.py --print-query        # the q text, no connection needed
```

New columns, all from one query per sym chunk:

| column | meaning |
|---|---|
| `closeRefPrice` | `size wavg price` over **17:30–17:45 HKT** (15:00–15:15 IST) — the exchange's CAS reference price |
| `volRef`, `nRef` | volume and print count in that same window |
| `volPost`, `nPost` | volume and print count from **17:50 HKT** to the end of the day |
| `vwapPost` | VWAP over that post window |
| `closeVsRefBps` | `pxClose` vs `closeRefPrice`, in bps — the ±3% band is ±300 bps, so this reads directly against it |
| `bbg_ticker`, `name` | carried across from the NIFTY 50 file |

No index weight is carried — see Route A above for why.

The summary adds total volume either side of the auction, their ratio, and a
count of names whose close printed outside the ±3% band.

The window for `closeRefPrice` and `volRef` is half-open — `time >= 17:30:00.000,
time < 17:45:00.000` — so a print exactly at 17:45:00.000 belongs to the close
rather than to the reference VWAP. `volPost` has no upper bound.

> **Fixed along the way:** the joins are now folded left one `lj` per statement.
> Written `base lj pre lj cls`, q reads it right-to-left as `base lj (pre lj cls)`,
> which drops `pxClose` for any sym that had a close print but no pre print — those
> names were reported as `no_data` instead of `no_pre_price`.
