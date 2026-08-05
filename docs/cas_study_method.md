# How does CAS impact the NIFTY 50?

The method behind `casStudy.py` — what the question actually asks, why the
obvious answer is the wrong one, and what each step contributes.

---

## 1. The question, stated precisely

"How does CAS impact the NIFTY 50?" sounds like one question. It is three, and
they have different answers:

1. **What did the index do over the auction window?** Descriptive. One number
   per day, easy, and on its own it proves nothing.
2. **How much of that was the auction rather than the market?** Causal. Needs a
   counterfactual — what would have happened without a closing auction.
3. **Which stocks are responsible?** Attribution. Needs index weights, because a
   200 bps move in a 0.5% name matters less than 12 bps in Reliance.

`casStudy.py` answers all three, in that order, and keeps them visibly separate
so a descriptive number is never quoted as a causal one.

## 2. Why you cannot just recompute the index

The tempting approach is to rebuild the NIFTY 50 level twice — once from prices
before the auction, once from closing prices — and take the difference. It does
not work: the level is

```
Index = Σ(priceᵢ × sharesᵢ × IWFᵢ) / divisor
```

and we hold none of free-float shares, investable weight factors, or the
divisor. Anything reconstructed from the reference data we do have would be full
market cap, wrong by exactly the free-float adjustment.

We do not need it. Over a window where shares and the divisor are constant — and
intraday they always are, divisor changes happen overnight — the index **return**
is exactly the weight-weighted average of constituent returns:

```
R = Σ (wᵢ · rᵢ) / Σ wᵢ
```

That is not an approximation, it is the identity the index is built on. Published
weights are all it needs, and they are in `config/nifty50_weights.csv`. To quote
it in points, anchor on the official close: `points = R × index_level`
(`--index-level`).

## 3. The steps

### S1 — Universe: two arms, not one

`cas_price_move.py` looks only at CAS-eligible names. This study deliberately
pulls **every** Indian listing and splits it on the CAS ISIN whitelist:

- **treated** — CAS-eligible names, which close via the auction
- **control** — everything else, which trades straight through the same clock
  window with no auction

The control arm is the whole reason the study can say anything causal. Both come
from one `equity` query carrying `sym` and `ID_ISIN`, so the index weights attach
directly on ISIN with no symbol-mapping detour.

### S2 — Prices: five windows, one round trip

Per sym, computed server-side:

| what | window (HKT) | why |
|---|---|---|
| `pxOldRule` | 17:15–17:45 VWAP | the close the **old rule** would have produced |
| `pxOldRuleWin` | 17:30–18:00 VWAP | the clock window that rule occupied pre-CAS |
| `pxRef` | 17:30–17:45 VWAP | the exchange's CAS reference price, centre of the ±3% band |
| `pxPre` | last print before 17:45 | end of continuous, the naive anchor |
| `pxClose` | first print 17:58–18:00 | the auction print |
| `volPost`, `dayQty` | 17:45→EOD, whole day | how much volume migrated into the close |

The close window is 17:58–18:00 because order entry stops at a **random instant**
in that window and the close is struck there. That randomisation is not an
inconvenience, it is a gift: the print time is exogenous, so nobody can time it
and no selection story survives.

S2 therefore ends with a **print-time diagnostic** — the distinct `tClose` values
and their clustering. A market-wide freeze should put nearly every name on one
timestamp. A wide spread, or a large `no_close_price` count, means the window is
catching ordinary trades instead of the auction and everything downstream is
measuring the wrong thing.

### S3 — The counterfactual: what CAS changed, name by name

For every stock:

```
effectBps = (pxClose / pxOldRule − 1) × 10 000
```

The auction print against the close the **old rule** would have produced, for the
same stock on the same day, out of the same order flow. This is the heart of the
study, and it is a *within-name* comparison — no size confound, no liquidity
confound, no matching required, because the stock is compared against itself.

It is also reported in **ticks** (`effectTicks`), which is the unit that compares
across names: five paise is one tick on a ₹300 stock and five on a ₹100 one. A
move under one tick is the price grid, not a move — hence `pct_moved_ge_1_tick`.

Two secondary readings ride along because their disagreement is informative:
`moveBps` (against the last continuous print) minus `effectBps` is stale-print
bias, and `closeVsRefBps` is what the exchange's own ±3% band is measured against.

#### Which 30 minutes?

The old rule is that the close is the **VWAP of the last 30 minutes of continuous
trading**. That single sentence resolves to two different clock windows, and the
choice is not cosmetic:

| basis | window | what it means |
|---|---|---|
| `last30-continuous` *(default)* | 17:15–17:45 | the rule applied to today's session, which now ends at 17:45 |
| `clock-1730-1800` | 17:30–18:00 | the window the rule occupied pre-CAS, when continuous ran to 18:00 |

The default is the faithful one. The alternative **cannot** be the headline basis
for a CAS name, because the auction print at ~17:59 falls *inside* 17:30–18:00 —
the counterfactual would contain the very thing being measured, biasing the
effect toward zero. `--old-rule-window clock-1730-1800` exists to inspect that,
and prints a warning when used.

Both VWAPs are computed for every name regardless, because their difference is
worth having:

```
windowShiftBps = (pxOldRuleWin / pxOldRule − 1) × 10 000
```

On a **control** name — no auction in either window — that is pure window
artefact: what moving the 30-minute window 15 minutes later is worth on its own.
It is the yardstick for S4. An index effect smaller than the control's window
shift is not evidence of anything.

### S4 — Attribution: the index effect and who caused it

Weight each name's effect and the contributions sum, exactly, to the index effect:

```
contribBpsᵢ = wᵢ · effectBpsᵢ / Σw          Σ contribBps = index effect
```

Ranked by `|contribBps|`, that is the answer to "who are the major players" — and
it correctly demotes a violent move in a name nobody weights. Three summary
statistics come with it:

- **net vs gross** — `Σ contrib` against `Σ |contrib|`. Gross 40 bps with net 3
  means the names cancelled each other; gross 40 with net 38 means they all
  pushed the same way. Completely different worlds, same index move.
- **top-5 concentration** — if five names carry most of the gross, "CAS impact"
  is really "what five large caps did in the auction".
- **weight coverage** — the study renormalises over the weights it has (49 of 50
  members today; `NESTLEIND` has no published weight). Coverage is printed so a
  gap is never silent.

### S5 — Control: separating the auction from the market

S3 and S4 give a *realised* effect. Part of it is simply the market drifting
between 17:45 and the print — that would have happened with or without an
auction. The control arm measures exactly that drift, over the same clock window,
on names that have no auction:

```
auction effect = index effect (treated) − mean effect (control)
```

Because a raw treated-vs-control comparison confounds the mechanism with size —
CAS-eligible names are the liquid ones — the control is restricted to the
**common support** of day volume: non-CAS names inside the CAS group's p10–p90.
Names outside that range are dropped rather than extrapolated over, and the count
is reported.

The control doubles as a **placebo**, twice over: it has no auction, so both its
own "effect" and its `windowShiftBps` should be small. If either is large, the
finding is in the methodology — a drifting market or a 15-minute window shift —
not in the auction.

## 4. What one day can and cannot tell you

One day gives a number, not evidence. `--append-panel` accumulates the group rows
into `output/casstudy_panel.csv`, one set per day, which is what makes the
following possible:

- is the mean auction effect distinguishable from zero, or is it noise?
- did dispersion around the reference change when CAS started?
- **difference-in-differences**: `[treated after − treated before] − [control
  after − control before]`, which absorbs whatever the market did on those days
  and is the strongest claim this data supports.

Two further extensions, not built:

- **Reversal test** — if the auction move is information it persists overnight;
  if it is pressure it reverses at the next open. `−r(next open) / r(cas)` near 1
  means pressure. This is the standard microstructure test and is the single
  most valuable thing to add next; it needs one T+1 opening-price query.
- **Cross-section** — regress `|effect|` on auction volume share and ADV to test
  whether the movers are the names whose flow concentrated into the auction.

## 5. Reading the output

| file | contents |
|---|---|
| `casstudy_syms_<date>.csv` | one row per sym: every window price, the effect in bps and ticks, and its index contribution |
| `casstudy_index_<date>.csv` | one row per group: `NIFTY50`, `CAS_ALL`, `NONCAS`, `NONCAS_MATCHED` |
| `casstudy_panel.csv` | the same group rows appended across days (`--append-panel`) |

The headline sits in the `NIFTY50` row: `eff_bps_weighted` (and `index_points`
when `--index-level` is supplied) is what the auction did to the index;
`net_of_control_bps` is that number with market drift removed.

## 6. Assumptions to check before quoting a number

1. **Non-CAS names really do still close under the old mechanism.** If everything
   migrated at once there is no control arm, and the study degrades to the
   within-name counterfactual alone — still useful, no longer causal.
2. **`TYP_FILTER` is `None`**, so every record carrying a non-null price counts.
   If `qatt` holds quote updates, prices and counts are polluted. Set it once the
   `typ` domain is known.
3. **Index rebalance days must be excluded** — the weights are wrong across them.
4. **Weight coverage is 49/50.** Everything is renormalised over what is present.
5. **The whole-day cross-check** — recompute the day's index return from
   constituent returns and compare against the official NIFTY 50 close-to-close.
   Agreement to a few bps means weights and prices are sound; disagreement means
   every number above is suspect.
