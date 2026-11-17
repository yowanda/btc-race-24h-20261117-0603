"""BTC race bot -- v6: oversold bounce + Chandelier 3x ATR(60) trailing exit.

Strategy (from the prose spec, STRATEGY.md section 9.2):
  * Long only, one asset (CBBTC vs USDC), 100% of cash per trade.
  * Entry arm: the close sits at least 2% below the highest close of
    the last 240 one-minute bars (the washout arms the trade).
  * Entry trigger: the close breaks above the highest close of the
    last 60 bars (confirms the turn at the start of the recovery).
    The trigger window is the 60 bars *before* the current bar: a
    close can only break above a high that does not include itself.
  * Exit: Chandelier trail. Sell when the close falls more than
    3x ATR(60) below the highest close seen since entry, where
    ATR is the mean true range of the last 60 one-minute bars,
    recomputed live on every bar. True range of a bar is the
    largest of (high - low), |high - previous close| and
    |low - previous close|.
  * No EMA(240) backstop (price sits below it at entry by
    construction) and no take-profit (any level would be fitted).
  * Warm-up: 240 bars of history before the first trade.

Implementation notes:
  * Integer math only, no floats, no wall clock, no network, no
    files, no randomness -- the run is fully deterministic.
  * Orders decided on a batch fill at the next bar's open, so a buy
    is marked pending until the next batch reports its fill; the
    peak for the Chandelier trail starts at the decision bar's
    close and tracks the highest close seen while the buy is
    pending or the position is held.
  * Max slippage is 100 bps on every order.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

ARM_BARS = 240
ARM_DROP_BPS = 200  # arm when the close is >= 2% below the trailing high
TRIGGER_BARS = 60
ATR_BARS = 60
CHANDELIER_MULT = 3
MAX_SLIPPAGE_BPS = 100
WARMUP_BARS = 240


class V6Bot:
    """State for one run: the bar history plus the open-trade trail."""

    def __init__(self, symbol):
        self.symbol = symbol
        self.closes = []  # closes, oldest first, ints at price_scale
        self.highs = []
        self.lows = []
        self.peak_since_entry = None  # highest close since the buy was decided
        self.pending_buy = False  # a buy was sent, its fill not yet reported

    def on_bar(self, bar):
        self.closes.append(int(bar["c"]))
        self.highs.append(int(bar["h"]))
        self.lows.append(int(bar["l"]))

    def ready(self):
        return len(self.closes) >= WARMUP_BARS

    def highest_close(self, count):
        """Highest close of the last `count` bars, current bar included."""
        top = self.closes[-count]
        for close in self.closes[-count + 1:]:
            if close > top:
                top = close
        return top

    def prior_highest_close(self, count):
        """Highest close of the `count` bars before the current one."""
        window = self.closes[-count - 1:-1]
        top = window[0]
        for close in window[1:]:
            if close > top:
                top = close
        return top

    def true_range(self, index):
        """True range of bar `index` (0-based into the history lists)."""
        high = self.highs[index]
        low = self.lows[index]
        span = high - low
        if index == 0:
            return span
        prev_close = self.closes[index - 1]
        up = high - prev_close
        if up < 0:
            up = -up
        down = low - prev_close
        if down < 0:
            down = -down
        if up > span:
            span = up
        if down > span:
            span = down
        return span

    def atr(self):
        """Mean true range of the last ATR_BARS bars (integer, truncated)."""
        total = 0
        start = len(self.closes) - ATR_BARS
        for index in range(start, len(self.closes)):
            total += self.true_range(index)
        return total // ATR_BARS

    def armed(self):
        """True when the close is >= 2% below the trailing 240-bar high."""
        close = self.closes[-1]
        top = self.highest_close(ARM_BARS)
        return close * 10000 <= top * (10000 - ARM_DROP_BPS)

    def triggered(self):
        """True when the close breaks above the prior 60-bar high close."""
        return self.closes[-1] > self.prior_highest_close(TRIGGER_BARS)


_BOT = None


def _bot_for(state):
    global _BOT
    if _BOT is None:
        symbols = state.hello.get("symbols") or []
        symbol = symbols[0]["symbol"] if symbols else "CBBTC"
        _BOT = V6Bot(symbol)
    return _BOT


def on_batch(state, batch):
    bot = _bot_for(state)
    bars = [bar for bar in batch.get("bars", []) if bar["symbol"] == bot.symbol]
    if not bars:
        return []
    bar = bars[-1]
    bot.on_bar(bar)

    # Settle the pending buy from the fills this batch reports: a
    # rejection clears it (and the trail peak), a fill hands the
    # position over to the holding branch below.
    for fill in batch.get("fills", []):
        if fill.get("side") == "buy" and fill.get("symbol") == bot.symbol:
            bot.pending_buy = False
            if fill.get("status") == "rejected":
                bot.peak_since_entry = None

    if state.warming_up or not bot.ready():
        return []

    close = bot.closes[-1]
    holding = state.positions.get(bot.symbol)
    holding_qty = holding.qty if holding is not None else 0

    if holding_qty > 0:
        if bot.peak_since_entry is None:
            bot.peak_since_entry = close
        elif close > bot.peak_since_entry:
            bot.peak_since_entry = close
        trail = CHANDELIER_MULT * bot.atr()
        if bot.peak_since_entry - close > trail:
            bot.peak_since_entry = None
            return [{
                "side": "sell",
                "symbol": bot.symbol,
                "qty": holding_qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []

    if bot.pending_buy:
        return []

    if bot.armed() and bot.triggered():
        cash = state.cash
        # A buy below the harness minimum notional is rejected outright,
        # so do not spend an order on it.
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if cash < min_notional:
            return []
        bot.pending_buy = True
        bot.peak_since_entry = close
        return [{
            "side": "buy",
            "symbol": bot.symbol,
            "notional_quote_units": cash,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]
    return []


makefun_bars.run(on_batch)
