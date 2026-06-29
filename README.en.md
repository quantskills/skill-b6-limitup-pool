# skill-b6-limitup-pool · Limit-Up Pool Dynamic Management

> **Project status: Community Project.** Created by a community member; **not reviewed, certified, verified, or
> endorsed by QuantSkills**, and not a production-certified project. The `quantskills/` namespace only indicates the
> hosting organization and implies no official status.

> 中文说明见 [README.md](README.md) ｜ Chinese README is the primary version.

A pre-close maintenance tool for the A-share limit-up pool. Each trading day it maintains the limit-up pool and tags
**first board / consecutive-board count / blow-up count / re-seal time**, plus theme grouping, special patterns
(地天板/天地板/一字/秒板/烂板), and sentiment metrics (tiered promotion rate, blow-up rate, money-making effect).
Outputs a multi-dimensional table and an interactive dark HTML dashboard. Data source: **PandaData**.

---

## What it does
Compresses whole-market daily bars (+ minute bars for limit-up stocks) into a "limit-up pool board view":
consecutive-board ladder, blow-up & re-seal, theme grouping, and a market-sentiment row.

## How to use
- Natural language (after wiring into an agent): "run today's limit-up pool".
- CLI: `python 开发产物/scripts/build.py --mode daily`, then `render_html.py` for the dashboard.
- Full call rules and field tables: **[开发产物/SKILL.md](开发产物/SKILL.md)**.

## Scenarios
Post-close review agents · consecutive-board sentiment research factors · manual review.

## Maintainer
Community member [@ZLHad](https://github.com/ZLHad). Issues / PRs welcome.

## Limitations
- Blow-up count / re-seal time need minute bars; if unavailable they **fall back to a daily-bar proxy**
  (lower precision, `seal_metric_source=daily_proxy`).
- Theme grouping depends on the concept API; missing → that dimension is left blank.
- Depends on a PandaData account and traffic quota; over-quota interrupts the daily run.

---

## Quant boundaries (Community Rule §8)
- **Data source**: PandaData (`panda_data` ≥ 0.0.9); `get_stock_daily` / `get_concept_*` / `get_stock_min`.
- **Assumptions**: limit-up judged by the API `limit_up`; fallback to per-board 10/20/30% caps when missing;
  streak accumulates over trading days (halts do not break it).
- **Parameters**: lookback window, universe (All-A / CSI300 / CSI1000 / CN2000), minute precision — all configurable.
- **Known limitations**: see "Limitations" above; theme labels are coarse API-level; special patterns are rule-based.
- **Risk boundary**: output is **objective statistics for research/review**, containing no buy/sell advice.
- **Nature**: **research / educational example only**. Not investment advice. No promised returns. No implication
  that any strategy is safe or guaranteed to be profitable.

## Attribution & License (Community Rules §3 / §6)
- License: **GPL-3.0-only** (see [LICENSE](LICENSE)).
- Limit-up judgment / streak state machine / minute-level first-seal & blow-up logic are kept consistent with the
  same author's alpha-A3 consecutive-board factor to avoid drift.
- Third-party deps: `panda_data`, `pandas`, `numpy`, `pyarrow`, each under their own licenses.
