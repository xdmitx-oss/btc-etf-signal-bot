# BTC Telegram Signal Bot v7.1

Bug-fix release for v7.

Fix:
- ETF parser returns (date, flow) tuples.
- Signal engine now correctly uses the numeric flow value instead of dividing the tuple by a number.

Features:
- ETF 1D / 3D / 5D
- BTC momentum
- Coinbase order-book imbalance
- Volume quality
- BUY / WAIT / SELL
- Market bias
- Data confidence (not probability of profit)

Run exactly one Telegram polling instance.
