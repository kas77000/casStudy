# `casretro_v2` — what every number on the page is, and where it came from

An audit trail for the trader review. For each element of the HTML: the query
that fed it, and the arithmetic that produced it. Read it with the page open.

Everything below is HKT, because that is what the raw `time` columns carry.
IST = HKT − 02:30. Money is USD unless a column says otherwise.

Companion documents: [`runbook.md`](runbook.md) (what to run),
[`queries.md`](queries.md) (the v1 query reference).

---

## 0. What the report covers

Seven predicates decide the population, and **every** number on the page sits
inside all of them.

This is the definition taken from the desk's own `temp.q`, and it is applied
**server-side** — the frame that arrives is already the population being
measured, so there is no second, quieter definition applied later in pandas.

| filter | rule | why |
|---|---|---|
| **universe** | the CAS ISIN whitelist, NSE `.IN` listings only | `config/cas_isins.txt` |
| **flow** | `--flow silk` / `agency` / `both`; a `basket` containing `SILK` is SILK, anything else is agency | joined from `target` |
| **venue** | `venue like "*CLOSE*"` | the auction, not continuous |
| **traded** | `make > 0` | a close child order that filled nothing never competed; counting it drags every fill rate toward zero and says nothing about execution |
| **sane** | `make <= size` | a child order cannot fill more than it asked for; a row saying otherwise is bad data, not a 300% fill |
| **live into the auction** | `t_off_market > 17:58` | it was still on the market when the auction could freeze |
| **marketable limit** | buy: `price >= avg_fill_price`; sell: `price <= avg_fill_price` | a limit sitting the wrong side of its own average fill price is a row whose `price` and `avg_fill_price` do not describe the same thing. **Market orders are exempt** — they have no limit to test |

Two consequences worth stating plainly, because they decide what every ratio on
the page means:

* **A continuous child order is never counted**, however late it traded. The
  clock has no vote — a close child order is close because of its venue.
* **The fill rate is measured over orders that traded**, not over everything
  sent. It answers *"of the orders that competed in the auction, what fraction of
  their size filled"*, not *"of everything we pointed at the close, what
  landed"*. The second question has a far larger denominator and a far smaller
  answer; it is not what this page reports.

`config.OFF_MARKET_AFTER` and `config.LIMIT_MUST_BE_MARKETABLE` move these.

---

## 1. The queries

Six, and no others. Every one is a q lambda with explicit parameters; nothing
is interpolated into a query except table and column *names*. Symbols and ids go
in chunks of 500 / 2000. Run any report with `--show-queries 2> queries.log` to
see these on the wire with their arguments and timings.

### 1.1 Parent orders — `target`

Called once per day. Used **only** to attach `basket` (and therefore flow) to
each child order — no quantity, price or state is taken from it.

```q
{[d;syms] select date, id_server, time, id_target, trader, basket, portfolio,
   wave, oes_oid, oes_primoid, sym, side, sidesign, size, tif, otype,
   limit_price, t_oes_load, t_gen, t_start, t_end, p_start, p_end, algo, alpha,
   beta, gamma, delta, iwould, stealth, doopen, doclose, docash, cashratio,
   cashrange
 from target where date=d, sym in syms }
```

### 1.2 Close child orders — `workorder` ← **the important one**

This query *is* the population of §0. It carries every predicate, so the report
quotes the numbers `temp.q` returns. Selected by `sym`, not by parent id, which
is why it runs without fetching parents first.

```q
{[d;syms;t]
  select date, id_target, id_work, sym, side, otype, venue,
         size, make, price, avg_fill_price, t_off_market
  from workorder
  where date=d, sym in syms,
        venue like "*CLOSE*",
        make <= size,
        make > 0,
        t_off_market > `time$t,
        (
            (otype<>`limit)
            |
            (
                (otype=`limit)
                &
                (
                    ((side=`sell) & price <= avg_fill_price)
                    |
                    ((side=`buy)  & price >= avg_fill_price)
                )
            )
        ) }
```

`t = 64680000` ms (17:58:00). **`size` is the quantity sent and `make` the
quantity executed**, at `avg_fill_price`.

### 1.3 The `execution` table is not queried

Summing `execution.fillsize` by `id_work` answers "how much traded" a second
way, and where the two disagree the report would be quoting a number the OMS
does not recognise. `workorder.make` is the OMS's own word on the child order and
what the desk reconciles against, so it is the one source used — and named.

### 1.4 Market close **volume** — `qatt`, 17:50 → end of day

```q
{[d;syms;t1;t2]
  0!select vwap:size wavg price, qty:sum size, n:count i,
           pxFirst:first price, pxLast:last price,
           pxHigh:max price, pxLow:min price,
           tFirst:first time, tLast:last time
    by sym from qatt
    where date=d, sym in syms, time >= `time$t1, time < `time$t2,
          not null price, not null size, size > 0 }
```

`t1 = 64200000` ms (17:50:00), `t2 = 86399999` ms (23:59:59.999).
Only `qty` is used, as **`mkt_close_qty`**.

### 1.5 Market close **price** — `qatt`, 17:58 → 18:00

The same lambda, different window: `t1 = 64680000` ms (17:58), `t2 = 64800000`
ms (18:00). Only `pxFirst` is used, as **`mkt_close_px`** — the *first* print
after 17:58, which is where the auction freezes. Same window `casStudy` uses, so
two reports cannot quote different closing prices for the same day.

### 1.6 The day's FX rate — `equity`

```q
{[d;syms] select sym, CRNCY, fx_last from equity where date=d, sym in syms }
```

Run **per day**, because `fx_last` is a daily column. A week converted at one
snapshot's rate would restate every other day at a price nobody traded on.

### 1.7 The universe — `equity`

Only when there is no CSV snapshot in `config/`. The snapshot supplies the sym
list; the FX rate is always query 1.6 regardless.

```q
{[d;isins] select sym, ID_ISIN, TICKER, NAME, CRNCY, COUNTRY, adv, adv_std,
   px_last_prev, fx_last, CUR_MKT_CAP, INDUSTRY_SECTOR, MARKET_STATUS
 from equity where date=d, ((sym like "*.IN")), ID_ISIN in isins }
```

**Not queried at all:** `execution`, `target_state`, `alerts`. v2 needs none of
them — see §1.3.

### 1.8 Which instance each day is read from

The **live day** — the most recent business day on or before today — is read
from the **RT** tapes, falling back to the **HDB** if they no longer hold it.
Every earlier day is read from the HDB, always. A past day is never read from RT,
because a real-time tape holds whatever it holds *now*.

Because the RT tables carry no date predicate server-side, `target` and
`workorder` are filtered **row by row** on their `date` column after loading
(`days.clip_to_date`). A tape that has already rolled therefore comes back empty
— and that empty result is what triggers the handover to the HDB.

> `qatt` aggregates server-side, so its rows cannot be filtered afterwards. If
> the live day's market data comes off an RT tape holding more than one session,
> the market columns for that day cover more than that day. The report warns when
> this applies.

---

## 2. The child-order frame

Everything on the page is an aggregation of one table: **one row per close child
order that traded, per day**. Built by `build.build_children()`, written to
`csv/children.csv` on every run. If a number on the page looks wrong, this is
the file to open.

### 2.1 One row per child order

`workorder` carries one row per *event*, but `t_off_market` is stamped once, so
the query's own predicate leaves one row per child order. That assumption is
**checked, not trusted**: a duplicate `id_work` after filtering is reported as a
warning, because its `size` and `make` would otherwise be counted twice.

### 2.2 Columns, and how each is derived

| column | derivation |
|---|---|
| `date` | the day being loaded |
| `sym`, `side`, `venue`, `t_off_market` | straight from the child order |
| `otype_kind` | `workorder.otype` → `MARKET` / `LIMIT` (`classify.otype_kind`; anything unrecognised passes through upper-cased under its own name) |
| `basket`, `trader`, `portfolio` | from `target`, joined on `id_target` |
| `flow` | `basket` contains `SILK` → `SILK`, else `AGENCY` |
| **`sent_qty`** | **`workorder.size`** |
| **`exec_qty`** | **`workorder.make`** |
| `exec_px` | **`workorder.avg_fill_price`** |
| `exec_notional_local` | `exec_qty × exec_px` |
| **`unfilled_qty`** | `max(sent_qty − exec_qty, 0)` |
| `wo_price` | `workorder.price` — the limit |
| `mkt_close_px` | query 1.5, joined on `sym` |
| **`unfilled_px`** | **LIMIT → `wo_price`. MARKET → `mkt_close_px`.** See below. |
| `unfilled_px_source` | which of the two was used, or the fallback that was |
| `unfilled_notional_local` | `unfilled_qty × unfilled_px` |
| `sent_notional_local` | `exec_notional_local + unfilled_notional_local` |
| `*_usd` | the three notionals × the day's USD factor (§3) |
| `priced` | false when there is unfilled quantity that no price could be found for |

### 2.3 The pricing rule

This is the part most worth checking, because it is a decision rather than a
lookup.

* **Executed quantity is priced at `avg_fill_price`**, off the workorder. Never
  at the close, never at the limit.
* **Unfilled LIMIT quantity is priced at the child order's own price**, off the
  workorder. That is the price we were willing to trade at.
* **Unfilled MARKET quantity is priced at the auction's closing price.** A market
  order carries no price of its own to be unfilled at, so the auction's own print
  is what that quantity would have been worth. This is a **substitution**, and
  the page reports how much of the total rests on it (`substituted_pct`, shown as
  a note under the Market table).

Fallback order when the preferred price is missing: workorder price → auction
close → the order's own `avg_fill_price` → unpriced. An unpriced row keeps its
**quantity** and contributes **no notional** — it is not treated as worth zero.

---

## 3. USD conversion

`equity.fx_last` is a rate whose direction the schema does not document. It is
recovered from its magnitude:

```
factor = 1 / fx_last   if fx_last > 1     (fx_last is local per USD)
factor =     fx_last   if fx_last < 1     (fx_last is USD per local)
usd    = local notional × factor
```

For a currency far from parity the two candidate quotes are reciprocals on
opposite sides of 1, so both readings produce the **same USD number** — INR is
either ~85 or ~0.0117 and nothing else. `--fx divide|multiply` forces it; the
rate actually applied is printed in the page header, worded to match the
direction used.

A sym already quoted in USD gets a factor of 1.0. A sym with no usable rate gets
`NaN`, and every USD figure touching it stays `NaN` — it drops out of a total
rather than joining it at ~85× its true value.

---

## 4. The page, element by element

Two layouts are written from the same frames, so they cannot disagree:

| file | layout |
|---|---|
| `<base>_v1.html` | one column — each chart followed by its own table |
| `<base>_v2.html` | two pages behind a CSS-only tab: **Overview** (KPI row and every chart) and **Data** (every table) |

In `_v2`, page 1 draws market and limit **per flow** — one section each for SILK
and agency on a `--flow both` run, two charts per section — followed by the top
clients of each flow as horizontal bars in notional. Page 2 carries the same
tables as `_v1`. Printing ignores the tab and lays both pages out, page 2
starting on a fresh sheet.

`_v2` is the intended replacement; `_v1` is kept alongside it until it has
earned the job.

### 4.1 Header line

`period · N trading days · flow · NSE closing auction · fx note`

Days listed are the days that **produced data**. A holiday, or a day on neither
tape, is dropped and named in a callout — never counted as a zero.

### 4.2 The KPI tiles

`metrics.headline()`. All period totals; every ratio recomputed from summed
quantities, never averaged across days.

| tile | formula |
|---|---|
| **Notional executed in the close** | Σ `exec_notional_usd`. Note splits it by `otype_kind`. |
| **Fill rate** | Σ `exec_notional_usd` / Σ `sent_notional_usd` × 100 — **by notional**. Note carries Σ `exec_qty` / Σ `sent_qty` × 100, **by shares**. |
| **Market** | Σ `make` / Σ `size` × 100 for market orders — **by shares**. Note: Σ `exec_notional_usd` for market. |
| **Limit** | the same, for limit orders. |
| **Share of the auction** | see §4.6 — matched numerator and denominator. |

Footer line: distinct `sym`; distinct `basket`; the largest basket's share of
Σ `exec_notional_usd`; the largest day's share of the same.

> **Watch the two fill rates.** The Fill rate tile is by **notional**; the
> Market and Limit tiles and every table column called *Fill rate* / *Fill ratio*
> are by **shares**. They differ whenever price and fill probability are
> correlated.

### 4.3 Execution Quality — the charts

One stacked bar per day, per order type, **in shares**:

* blue segment = `exec_qty`
* orange segment = `unfilled_qty`
* total height = `sent_qty`
* the number above the bar = `fill_rate_pct` = `exec_qty / sent_qty × 100`, to
  one decimal

Stacked quantity rather than a bare percentage on purpose: 100% of 500 shares and
60% of five million draw the same height otherwise. Both segments are the same
measure in the same unit, so there is one axis and no second scale.

Day labels shorten as the range grows (`Mon 03 Aug` → `03 Aug` → `03/08`) and
thin to every nth bar past about three weeks. The table always carries every day.

### 4.4 Execution Quality — the tables

`metrics.execution_quality()`, grouped by **(date, otype_kind)**.

| column | formula |
|---|---|
| Day | the trading date |
| Sent | Σ `sent_qty` |
| Executed | Σ `exec_qty` |
| Fill ratio | Σ `make` / Σ `size` × 100, over the orders that traded (§0) |
| Executed notional | Σ `exec_notional_usd` |
| Total notional sent | Σ `sent_notional_usd` |
| **Period** row | `metrics.execution_quality_totals()` — the same sums over every day, with the ratio recomputed, **not** the mean of the daily ratios |

**Executed notional and Total notional sent are equal exactly when nothing went
unfilled.** For market orders, which fill 100%, they will normally match. For
limit orders they should differ, and by a lot.

### 4.5 Flows

`metrics.flows()`, grouped by **(date, flow, otype_kind)**.

| column | formula |
|---|---|
| Day / Flow / Type | the grouping keys |
| Orders | distinct `id_target` |
| Child orders | distinct `id_work` |
| Notional traded in close | Σ `exec_notional_usd` |
| Fill rate | Σ `make` / Σ `size` × 100 — **by shares**, over the orders that traded (§0) |
| Symbols | distinct `sym` |
| Market close volume | Σ `mkt_close_qty` over the row's **distinct (date, sym) pairs** |
| Market notional | Σ `mkt_close_qty × mkt_close_px × fx` over the same pairs |
| % of market notional | see §4.6 |
| **Period** row | recomputed over the whole period's distinct pairs, **not** summed down the column |

### 4.6 Our share of the auction — the one to check carefully

The market denominator covers **only the symbols that row traded**, each counted
once per day however many times we traded it.

Two consequences, both deliberate:

1. **The market columns do not add up down the page.** Two rows that share a name
   both count that name's close, so summing the column double-counts. The Period
   row is recomputed from the distinct pairs of the whole period rather than
   summed. The page says this under the table.

2. **Numerator and denominator are held to the same names.** A symbol that never
   printed between 17:58 and 18:00 carries no closing price and contributes
   nothing to the market notional. Its own executed notional is therefore also
   excluded from the numerator — otherwise the share would be overstated, and can
   exceed 100%. `market_coverage_pct` (in `csv/flows.csv`) reports how much of
   that group's notional survived the test; when it falls below 99.5% the KPI
   tile says so on its face.

```
% of market notional = covered_exec_notional_usd / mkt_close_notional_usd × 100
```

### 4.7 Top 5 clients

`metrics.top_clients()`, grouped by **`basket`**, one table per flow, ranked by
**Σ `exec_notional_usd` over the whole period** — descending, top 5.

A client that traded once, heavily, outranks one that traded a little every day.
That is intended: the ranking is period-wide, not per-day, and the **Days** column
shows how many days each was active so the difference is visible.

| column | formula |
|---|---|
| Basket | the client key (`V.CLIENT_COLUMN`) |
| Days | distinct `date` |
| Orders / Child orders / Symbols | distinct `id_target` / `id_work` / `sym` |
| Notional traded in close | Σ `exec_notional_usd` — **the ranking key** |
| Fill rate | Σ `exec_qty` / Σ `sent_qty` × 100 |
| % of market notional | as §4.6, over that basket's own names |

---

## 5. Conventions that hold everywhere

1. **Ratios come from summed quantities, never from averaged ratios.** A period
   is not the mean of its days unless every day was the same size.
2. **Quantity ratios and notional ratios are different numbers** and are labelled
   as such. Fill rate in the tables is by shares.
3. **A symbol is counted once per day** in any market denominator.
4. **Missing data is excluded, not zeroed.** A day with no orders is dropped and
   named; an unpriced row keeps its quantity and contributes no notional; an
   unconvertible symbol leaves the USD totals.
5. **Only close-venue child orders exist** as far as this report is concerned.

---

## 6. Known limits — read before quoting a number

| limit | consequence |
|---|---|
| The closing price is the first print in 17:58–18:00, a **proxy** for the auction price | if a name's prints in that window are ordinary continuous trades, its market notional is struck on the wrong price. `casStudy` reports the same diagnostic. |
| Close volume is everything from 17:50 to end of day | it includes the last continuous minutes and the post-close session, not the auction alone |
| Unfilled market quantity is priced at the auction close | a substitution, not an observation; reported as `substituted_pct` |
| `fx_last` direction is inferred from magnitude | safe for INR; ambiguous within 0.5–2.0, where the report warns and `--fx` decides |
| RT `qatt` cannot be date-filtered client-side | the live day's market columns depend on that tape holding one session |
| **Price improvement vs the close is not measured** | a call auction clears everyone at one price, so a fill *in* the auction *is* the print. Any such number would measure the 17:58–18:00 proxy, not skill. |

---

## 7. How to verify it yourself

```bash
python tools/selftest_v2.py          # the whole analytical layer, no database
python -m casretro_v2 --show-queries 2> queries.log
```

Every run writes the frames behind the page:

| file | what it is |
|---|---|
| `csv/children.csv` | the base table — one row per close child order that traded, every intermediate column |
| `csv/execution_quality.csv` | section 1, per (date, order type) |
| `csv/flows.csv` | section 2, including `market_coverage_pct` |
| `csv/top_clients_*.csv` | section 3, per flow |
| `csv/market.csv` | per (date, sym): close volume, close price, close notional |

`csv/children.csv` reconciles to every other file by summation — that is the
check to run if a number is disputed.

The selftest asserts that the close query still carries every predicate from
`temp.q`, and then, on a fixture whose every value is known by hand: quantities
taken from `size` and `make`; the pricing rules; ratios from summed quantities; both FX
directions landing on the same USD; a symbol counted once per day in the
denominator; the share of the auction holding numerator and denominator to the
same names; the client ranking being period-wide; and the live day falling back
from RT to the HDB for all four ways the tapes can fail to answer.
