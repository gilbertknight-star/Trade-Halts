# Data Dictionary

All datasets needed to reproduce the LULD halt resumption research.
Covers NASDAQ-listed stocks, April 2021 – May 2026.

---

## halt_trades_rvol.csv — Core Trade Dataset

**550 rows.** One row per trade, filtered to RVOL ≥ 1.0 halt-up signals.
This is the primary dataset used by all backtest and analysis scripts.

| Column | Type | Description |
|--------|------|-------------|
| `date` | date | Trading date (ET) |
| `symbol` | str | Stock ticker |
| `entry_ts` | datetime | Entry timestamp (UTC) — halt resumption time |
| `exit_ts` | datetime | Exit timestamp (UTC) — approximately 3:50 PM ET |
| `entry_px` | float | Entry price including 50bps slippage |
| `exit_px` | float | Exit price including 25bps slippage |
| `ret_pct` | float | Gross return % = (exit_px / entry_px - 1) × 100 |
| `rvol` | float | Relative volume at halt (surge rate / baseline rate) |
| `pre_halt_close` | float | Last price before halt |
| `session_open` | float | Day's opening price |

**Note on slippage:** `entry_px` and `exit_px` already include realistic slippage
(50bps on entry, 25bps on exit). These are not mid-prices — they reflect
what you would likely have paid/received in practice.

---

## exit_liquidity.csv — Exit Window Liquidity

**545 rows** (5 trades missing bar data). Measures dollar volume in the
3:20–3:50 PM ET window — the period when the strategy exits.

| Column | Type | Description |
|--------|------|-------------|
| `date` | date | Trading date |
| `symbol` | str | Stock ticker |
| `ret_pct` | float | Trade return % |
| `exit_px` | float | Exit price |
| `window_dolvol_30m` | float | Dollar volume in 3:20-3:50 PM window |
| `last_bar_dolvol` | float | Dollar volume in final 1-min bar |
| `window_shares` | int | Total shares traded in exit window |
| `pct_vol_10k` | float | $10k position as % of exit window volume |
| `pct_vol_25k` | float | $25k position as % of exit window volume |
| `pct_vol_50k` | float | $50k position as % of exit window volume |
| `pct_vol_100k` | float | $100k position as % of exit window volume |
| `pct_vol_250k` | float | $250k position as % of exit window volume |

**Key stat:** Median exit window volume = $7.1M. At $100k position,
the median trade uses only 1.4% of exit volume — no market impact concern.

---

## halt_events.parquet — Raw Halt Events

**Full halt event metadata** used to build the trade dataset.
Includes all LULD halt events before filtering.

| Column | Description |
|--------|-------------|
| `symbol_resolved` | Ticker |
| `event_date` | Date of halt |
| `anchor_ts_utc` | Halt resumption time (UTC) |
| `halt_code` | NASDAQ halt reason code (LUDP = LULD up) |
| `bars_file` | Path to corresponding bar data file |

Load with: `pd.read_parquet('research/data/halt_events.parquet')`

---

## realistic_backtest_trades.csv — Backtest Trade Log

**550 rows.** Per-trade log from `realistic_backtest.py` with full
position sizing, commission, and equity calculations.

| Column | Description |
|--------|-------------|
| `trade_num` | Sequential trade number |
| `date` | Trading date |
| `symbol` | Ticker |
| `entry_ts` / `exit_ts` | Timestamps |
| `entry_px` / `exit_px` | Prices |
| `ret_pct` | Gross return % |
| `BOD_equity` | Beginning-of-day equity when trade was taken |
| `pos_size` | Dollar position size |
| `gross_pnl` | Gross profit/loss |
| `commission` | IBKR commission (both legs) |
| `net_pnl` | Net profit/loss after commission |
| `equity_after` | Running equity after this trade |

---

## Raw Bar Data (not in repo)

The minute-by-minute OHLCV bar data (~5 GB of parquet files) is **not included**
in this repo due to size. It is required only if you want to:
- Run `research/analysis/exit_liquidity.py` (measures exit window volume)
- Re-export or rebuild `halt_trades_rvol.csv` from scratch

To rebuild: you need IBKR historical data access and the data pipeline scripts
(contact repo owner). All backtesting scripts work with the included CSVs.
