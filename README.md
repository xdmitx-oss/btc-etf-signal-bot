# BTC ETF + Liquidity Telegram Bot v7

v7 adds ETF 1-day, 3-day and 5-day cumulative flow.
Daily ETF data is treated as a slower institutional-flow signal.

Signal:
- ETF 1D: 20 points
- ETF 3D: 30 points
- ETF 5D: 35 points
- Momentum: 15 points
- Order-book imbalance: 15 points

Volume is used for data quality, not direction.

The bot reports Market bias:
Bullish / Slightly bullish / Neutral / Slightly bearish / Bearish.

Data confidence is a data/indicator-consistency metric, not probability of profit.

Use exactly one Telegram polling instance.
