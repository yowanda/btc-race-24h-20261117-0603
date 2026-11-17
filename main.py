# SPDX-License-Identifier: MIT
"""v5 — oversold bounce: deep 4-hour drawdown + 60-bar breakout.

Long-only, one position, full size. Pure function of bar closes,
integer math only — deterministic, no clock, no network, no files,
no randomness.

Entry — arm AND trigger must hold on the SAME bar (per-bar arm,
no latch: a washout printed hours ago does not keep the trade
armed once price has recovered):
  * Arm: close at least 2% below the highest close of the last
    240 bars (4 hours). 2% is ~8x the ~26 bps round-trip cost and
    ~1/4 of a volatile day's range — a genuine washout, not noise.
  * Trigger: close breaks above the highest close of the previous
    60 bars (1-hour Donchian high). Confirms the turn at the START
    of the recovery.
Exit — trailing stop only:
  * Sell when the close falls 0.3% below the highest close since
    entry. Locks the bounce's gain as it develops, cuts promptly
    when it fades.
  * NO trend-break backstop: the entry is justified by the washout
    low, not by a 4-hour uptrend — price sits BELOW EMA(240) at
    entry by construction, so such a backstop would fire at once.
  * NO take-profit: any TP level that "catches" these bounces
    would be chosen from the data — fitting, forbidden.
Size: 100% of cash. Max slippage 100 bps. Warm-up 240 bars.

Re-entry: this version has NO guard against re-entering after a
trailing-stop exit — if the arm and the breakout still hold on a
later bar, it buys again. That is the spec as written (§8.2 lists
no guard); any one-trade-per-washout rule belongs to a later
version, not to this one.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

# --- v5 parameters (set from the cost / range structure, not tuned) ---
ARM_BARS = 240          # washout lookback, bars
ARM_DRAWDOWN_BPS = 200  # close must be >= 2% below the 240-bar high
BREAKOUT_BARS = 60      # Donchian breakout lookback, bars
TRAIL_BPS = 30          # trailing stop: 0.3% below the peak since entry
MAX_SLIPPAGE_BPS = 100  # max slippage on every order
WARMUP_BARS = 240       # bars of history needed before trading

_BPS = 10_000

# Close history (ints, oldest first) and the running peak since entry.
_closes = []
_peak_since_entry = None


def _highest_last(values, count):
    """Highest value among the last `count` entries of `values`."""
    best = values[-count]
    for value in values[-count + 1:]:
        if value > best:
            best = value
    return best


def on_batch(state, batch):
    global _peak_since_entry

    bars = batch.get("bars") or []
    if not bars:
        return []
    symbol = bars[0]["symbol"]
    close = int(bars[0]["c"])
    if close <= 0:
        return []
    _closes.append(close)

    # No trading during warm-up, and never before 240 bars of history.
    if state.warming_up or len(_closes) < WARMUP_BARS:
        return []

    holding = state.positions.get(symbol)

    if holding is not None:
        # Track the highest close since entry, then check the trail.
        if _peak_since_entry is None or close > _peak_since_entry:
            _peak_since_entry = close
        if close * _BPS <= _peak_since_entry * (_BPS - TRAIL_BPS):
            _peak_since_entry = None
            return [{
                "side": "sell",
                "symbol": symbol,
                "qty": holding.qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []

    # Flat: reset the peak tracker, then test arm + trigger on this bar.
    _peak_since_entry = None
    if len(_closes) < BREAKOUT_BARS + 1:
        return []

    high_240 = _highest_last(_closes, ARM_BARS)
    armed = close * _BPS <= high_240 * (_BPS - ARM_DRAWDOWN_BPS)
    if not armed:
        return []

    high_60 = _highest_last(_closes[:-1], BREAKOUT_BARS)
    if close <= high_60:
        return []

    amount = state.cash
    room = state.weight_room(symbol)
    if amount > room:
        amount = room
    if amount <= 0:
        return []
    return [{
        "side": "buy",
        "symbol": symbol,
        "notional_quote_units": amount,
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }]


makefun_bars.run(on_batch)
