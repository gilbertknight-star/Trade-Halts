# Research — LULD Halt Resumption Strategy

Complete backtesting framework and analysis for the LULD halt resumption strategy.
All data needed to reproduce every result is included in `data/`.

## Quick Start

```bash
git clone https://github.com/gilbertknight-star/Trade-Halts
cd Trade-Halts
pip install pandas numpy matplotlib scipy pyarrow
```

Then run any script directly:
```bash
python research/backtest/realistic_backtest.py
python research/backtest/sizing_comparison.py
python research/backtest/pyramid_vs_single.py
```

All scripts run from the **repo root** and load data from `research/data/` automatically.

---

## Directory Structure

```
research/
├── data/               ← All datasets needed to reproduce results
│   ├── halt_trades_rvol.csv           ← 550 filtered trades (core dataset)
│   ├── exit_liquidity.csv             ← Exit window (3:20-3:50 PM) liquidity
│   ├── halt_events.parquet            ← Raw halt event metadata
│   ├── realistic_backtest_trades.csv  ← Per-trade log from main backtest
│   └── README.md                      ← Full data dictionary
│
├── backtest/           ← Simulation scripts
│   ├── realistic_backtest.py    ← Main backtest ($900 start, 10% sizing, commissions)
│   ├── sizing_comparison.py     ← 5% vs 10% vs 25% vs 50% position sizing
│   ├── pyramid_vs_single.py     ← Pyramiding vs first-halt-only comparison
│   └── backtest_rvol.py         ← RVOL filter sweep
│
├── analysis/           ← Edge analysis scripts
│   ├── exit_liquidity.py        ← Measures exit window dollar volume per trade
│   ├── exit_volume_analysis.py  ← Does exit volume predict returns?
│   └── prehalt_volume_analysis.py ← Does pre-halt volume predict returns?
│
└── results/            ← Pre-generated charts (run scripts to regenerate)
    ├── realistic_backtest.png
    ├── sizing_comparison.png
    ├── pyramid_vs_single.png
    └── ...
```

---

## Key Findings

| Analysis | Result |
|----------|--------|
| **Win rate (net of commissions)** | 69.6% |
| **Median trade return** | +30.3% |
| **Max drawdown** | -13.0% |
| **CAGR from $900** | +717% (driven by compounding to $100k cap) |
| **Pre-halt volume vs returns** | r = -0.032, no predictive value |
| **Exit volume vs returns** | r = +0.040, weak positive |
| **Optionable stocks win rate** | 81.7% vs 68.6% for non-optionable |
| **Pyramid vs single-entry** | Pyramid +30.3% median vs Single +18.2% |

---

## Strategy Logic

1. **Signal:** NASDAQ LULD halt with reason code `LUDP` (Limit Up)
2. **Filter:** Stock up ≥2% from session open + RVOL ≥ 1.0
3. **Entry:** Market order at halt resumption
4. **Sizing:** 10% of beginning-of-day equity, max $100k/trade
5. **Exit:** Market order at 3:50 PM ET

---

## Using This Data for Your Own Strategy Research

`research/data/halt_trades_rvol.csv` is the complete filtered trade dataset.
Load it with:

```python
import pandas as pd
df = pd.read_csv('research/data/halt_trades_rvol.csv', parse_dates=['entry_ts', 'exit_ts'])
print(df.columns.tolist())
# ['date', 'symbol', 'entry_ts', 'exit_ts', 'entry_px', 'exit_px',
#  'ret_pct', 'rvol', 'pre_halt_close', 'session_open', ...]
```

Feel free to fork this repo and build on top of it.
