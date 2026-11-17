"""BTC race bot -- Elder Triple Screen, 720 / 240 / 60 (v4).

Long only, one position, full size. A long is opened only when all three
screens agree; once in, a trailing stop and a trend-break backstop guard
the trade. Pure function of bar closes, integer math only: deterministic,
no clock, no network, no randomness, no files.

Screens (all three must pass on the same bar):
  1. Tide, 720 min (12 h): the 720-minute return is positive AND the
     close sits above EMA(720). The day-scale trend itself must be up;
     a bounce cannot flip a 12-hour return unless it retraces the whole
     12-hour drop, so this screen keeps the bot out of downtrend days.
  2. Wave, 240 min (4 h): the 240-minute return is positive AND the
     close sits above EMA(240). The middle screen, spaced ~3x from the
     tide and ~4x from the ripple (Elder spacing).
  3. Ripple, 60 min (1 h): the close breaks above the highest close of
     the last 60 bars (one-hour Donchian high). The entry trigger: it
     fires at the start of a leg, not minutes into it.

Exits (whichever comes first):
  - Trailing stop: sell when the close falls 0.3% below the highest
    close printed since entry (about one round-trip cost).
  - Trend-break backstop: sell when the close falls below EMA(240).
    The 4-hour trend that justified the entry is gone, so the premise
    is gone. The trail usually fires first; the backstop covers slow
    rollovers where price drifts under the trend without a sharp
    pullback from the peak.

Deliberately absent: no take-profit (any level that "catches" the
bounces would be chosen from the data itself), no minimum hold (the
trailing exit does that job), no trend filter besides the screens.

Sizing and costs: 100% of cash on entry, one position max, no
leverage, max slippage 100 bps. Warm-up is 720 bars: the tide screen
needs 720 bars of history, so the bot cannot trade the first 12 hours
of a 24-hour window. On a genuine uptrend day the tide gate opens in
the second half and the breakout fires on new highs; on a downtrend
or chop day the bot takes zero trades and stands aside at $0 instead
of bleeding on bull traps.

Replay safety: the bot keeps no state beyond the close list, the two
EMAs and the peak since entry, all of them rebuilt deterministically
from the bars themselves. Two runs over the same window therefore
produce byte-identical action logs. At most one order is ever sent
per reply, and a sell always empties the whole position.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# Screen horizons, in one-minute bars.
TIDE_BARS = 720
WAVE_BARS = 240
BREAKOUT_BARS = 60

# Exit: sell 0.3% (30 bps) below the highest close since entry.
TRAIL_BPS = 30

MAX_SLIPPAGE_BPS = 100

# No trading until this many closes have been seen: the tide screen
# needs a close 720 bars back, so the first 12 hours are history only.
WARMUP_BARS = 720

# Close history, oldest first, ints (the protocol delivers prices as
# decimal strings; cast once on arrival so every comparison below is
# integer math).
_closes = []

# Integer EMAs, seeded with the first close seen and stepped once per
# bar. Seeding on the first close keeps the EMAs defined from bar one
# while the 720-bar warm-up builds the history the screens need.
_ema_tide = None
_ema_wave = None

# Highest close printed since the current entry filled; None while flat.
# It is only cleared when the position is actually gone, so a rejected
# exit order never loses the true peak.
_peak_since_entry = None


def _ema_step(ema, close, period):
    """One integer EMA step: ema += (close - ema) * 2 // (period + 1)."""
    return ema + (close - ema) * 2 // (period + 1)


def _tide_ok(close):
    """Screen 1: 720-min return > 0 and close above EMA(720)."""
    ret_tide = close - _closes[-1 - TIDE_BARS]
    return ret_tide > 0 and close > _ema_tide


def _wave_ok(close):
    """Screen 2: 240-min return > 0 and close above EMA(240)."""
    ret_wave = close - _closes[-1 - WAVE_BARS]
    return ret_wave > 0 and close > _ema_wave


def _ripple_ok(close):
    """Screen 3: close breaks above the highest close of the last 60 bars."""
    prior_high = max(_closes[-1 - BREAKOUT_BARS:-1])
    return close > prior_high


def on_batch(state, batch):
    global _ema_tide, _ema_wave, _peak_since_entry

    bar = None
    for candidate in batch.get("bars", []):
        if candidate.get("symbol") == SYMBOL:
            bar = candidate
            break
    if bar is None:
        # No bar for our symbol this minute: nothing to decide on.
        return []

    close = int(bar["c"])
    _closes.append(close)
    if _ema_tide is None:
        _ema_tide = close
        _ema_wave = close
    else:
        _ema_tide = _ema_step(_ema_tide, close, TIDE_BARS)
        _ema_wave = _ema_step(_ema_wave, close, WAVE_BARS)

    holding = state.positions.get(SYMBOL)
    if holding is not None and holding.qty > 0:
        if _peak_since_entry is None or close > _peak_since_entry:
            _peak_since_entry = close
        trail_hit = close * 10000 <= _peak_since_entry * (10000 - TRAIL_BPS)
        backstop_hit = close < _ema_wave
        if trail_hit or backstop_hit:
            return [
                {
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": holding.qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }
            ]
        return []

    _peak_since_entry = None

    if state.warming_up:
        return []
    if len(_closes) <= WARMUP_BARS:
        return []
    if state.cash <= 0:
        return []

    if _tide_ok(close) and _wave_ok(close) and _ripple_ok(close):
        return [
            {
                "side": "buy",
                "symbol": SYMBOL,
                "notional_quote_units": state.cash,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }
        ]
    return []


makefun_bars.run(on_batch)
