"""
Multi-strategy crypto trading bot — ccxt-based, async, with risk management
and a per-symbol position state machine.

Install: pip install ccxt numpy

⚠️ DRY_RUN = True by default. Flip to False only once you've backtested and
reviewed every threshold below — this places real orders on a real exchange
when False and API keys are supplied.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import ccxt.async_support as ccxt
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tradingbot")


# ============================================================
# CONFIG
# ============================================================


@dataclass
class Config:
    exchange_id: str = "binance"
    api_key: str = ""  # fill in for live trading
    api_secret: str = ""
    dry_run: bool = True  # ⚠️ set False only when ready to place real orders

    symbols: tuple = ("BTC/USDT", "ETH/USDT")
    timeframe: str = "15m"
    candle_limit: int = 200  # bars of history to pull each cycle
    poll_interval_seconds: int = 60  # how often to check for new candles

    # Indicator periods
    sma_fast: int = 9
    sma_slow: int = 21
    ema_trend: int = 50
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14

    # Signal combination — each indicator votes -1 (bearish) / 0 (neutral) / +1 (bullish),
    # weighted and summed; entry requires the combined score to clear a threshold.
    weight_trend: float = 1.5  # SMA/EMA crossover + trend filter
    weight_rsi: float = 1.0
    weight_bbands: float = 1.0
    entry_score_threshold: float = (
        2.0  # combined weighted score needed to open a position
    )

    # Risk management
    account_equity_usd: float = 10_000.0
    risk_per_trade_pct: float = 1.0  # % of equity risked per trade (via stop distance)
    max_concurrent_positions: int = 3
    max_position_pct_of_equity: float = 25.0  # hard cap regardless of stop distance
    atr_stop_multiplier: float = 2.0  # stop = entry -/+ (ATR * multiplier)
    atr_take_profit_multiplier: float = 3.5
    trailing_stop_atr_multiplier: float = 1.5  # once in profit, trail by this many ATRs
    max_daily_loss_pct: float = 5.0  # circuit breaker: halt new entries for the day
    max_drawdown_pct: float = 15.0  # circuit breaker: halt everything, close positions

    # Order execution
    order_retry_attempts: int = 4
    order_retry_backoff_seconds: float = 2.0
    slippage_tolerance_pct: float = (
        0.5  # reject fills worse than this vs expected price
    )


CFG = Config()


# ============================================================
# INDICATORS — implemented directly (no ta-lib dependency)
# ============================================================


def sma(values: np.ndarray, period: int) -> np.ndarray:
    if len(values) < period:
        return np.full(len(values), np.nan)
    kernel = np.ones(period) / period
    result = np.convolve(values, kernel, mode="valid")
    return np.concatenate([np.full(period - 1, np.nan), result])


def ema(values: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1)
    result = np.empty_like(values, dtype=float)
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = alpha * values[i] + (1 - alpha) * result[i - 1]
    return result


def rsi(values: np.ndarray, period: int) -> np.ndarray:
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.zeros(len(values))
    avg_loss = np.zeros(len(values))

    if len(values) <= period:
        return np.full(len(values), np.nan)

    avg_gain[period] = gains[:period].mean()
    avg_loss[period] = losses[:period].mean()

    for i in range(period + 1, len(values)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i - 1]) / period

    rs = np.divide(
        avg_gain, avg_loss, out=np.full(len(values), np.inf), where=avg_loss != 0
    )
    result = 100 - (100 / (1 + rs))
    result[:period] = np.nan
    return result


def bollinger_bands(values: np.ndarray, period: int, num_std: float):
    mid = sma(values, period)
    std = np.array(
        [
            np.std(values[max(0, i - period + 1) : i + 1])
            if i >= period - 1
            else np.nan
            for i in range(len(values))
        ]
    )
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def atr(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int
) -> np.ndarray:
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum.reduce(
        [
            highs - lows,
            np.abs(highs - prev_close),
            np.abs(lows - prev_close),
        ]
    )
    result = np.full(len(closes), np.nan)
    if len(closes) <= period:
        return result
    result[period] = tr[1 : period + 1].mean()
    for i in range(period + 1, len(closes)):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period
    return result


# ============================================================
# SIGNAL GENERATION
# ============================================================


class Signal(Enum):
    STRONG_BUY = auto()
    BUY = auto()
    NEUTRAL = auto()
    SELL = auto()
    STRONG_SELL = auto()


@dataclass
class MarketSnapshot:
    symbol: str
    closes: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    last_price: float
    sma_fast: np.ndarray = field(default_factory=lambda: np.array([]))
    sma_slow: np.ndarray = field(default_factory=lambda: np.array([]))
    ema_trend: np.ndarray = field(default_factory=lambda: np.array([]))
    rsi: np.ndarray = field(default_factory=lambda: np.array([]))
    bb_upper: np.ndarray = field(default_factory=lambda: np.array([]))
    bb_mid: np.ndarray = field(default_factory=lambda: np.array([]))
    bb_lower: np.ndarray = field(default_factory=lambda: np.array([]))
    atr: np.ndarray = field(default_factory=lambda: np.array([]))


def compute_indicators(snapshot: MarketSnapshot) -> MarketSnapshot:
    snapshot.sma_fast = sma(snapshot.closes, CFG.sma_fast)
    snapshot.sma_slow = sma(snapshot.closes, CFG.sma_slow)
    snapshot.ema_trend = ema(snapshot.closes, CFG.ema_trend)
    snapshot.rsi = rsi(snapshot.closes, CFG.rsi_period)
    snapshot.bb_upper, snapshot.bb_mid, snapshot.bb_lower = bollinger_bands(
        snapshot.closes, CFG.bb_period, CFG.bb_std
    )
    snapshot.atr = atr(snapshot.highs, snapshot.lows, snapshot.closes, CFG.atr_period)
    return snapshot


def score_trend(snapshot: MarketSnapshot) -> float:
    """SMA fast/slow crossover, filtered by long-term EMA trend direction."""
    if np.isnan(snapshot.sma_fast[-1]) or np.isnan(snapshot.sma_slow[-1]):
        return 0.0

    fast, slow = snapshot.sma_fast[-1], snapshot.sma_slow[-1]
    prev_fast, prev_slow = snapshot.sma_fast[-2], snapshot.sma_slow[-2]
    price = snapshot.last_price
    trend_ema = snapshot.ema_trend[-1]

    crossed_up = prev_fast <= prev_slow and fast > slow
    crossed_down = prev_fast >= prev_slow and fast < slow
    above_trend = price > trend_ema
    below_trend = price < trend_ema

    if crossed_up and above_trend:
        return 1.0
    if crossed_down and below_trend:
        return -1.0
    if fast > slow and above_trend:
        return 0.5
    if fast < slow and below_trend:
        return -0.5
    return 0.0


def score_rsi(snapshot: MarketSnapshot) -> float:
    val = snapshot.rsi[-1]
    if np.isnan(val):
        return 0.0
    if val < CFG.rsi_oversold:
        return 1.0  # oversold -> bullish mean-reversion signal
    if val > CFG.rsi_overbought:
        return -1.0  # overbought -> bearish
    # mild pull toward center as a secondary signal
    midpoint = 50.0
    return (midpoint - val) / midpoint * 0.3


def score_bbands(snapshot: MarketSnapshot) -> float:
    price = snapshot.last_price
    upper, lower = snapshot.bb_upper[-1], snapshot.bb_lower[-1]
    if np.isnan(upper) or np.isnan(lower):
        return 0.0
    if price <= lower:
        return 1.0  # at/below lower band -> bullish reversion
    if price >= upper:
        return -1.0  # at/above upper band -> bearish reversion
    return 0.0


def generate_signal(snapshot: MarketSnapshot) -> tuple[Signal, float]:
    """Returns (signal, combined_weighted_score) for logging/diagnostics."""
    trend = score_trend(snapshot) * CFG.weight_trend
    rsi_score = score_rsi(snapshot) * CFG.weight_rsi
    bb_score = score_bbands(snapshot) * CFG.weight_bbands
    combined = trend + rsi_score + bb_score

    if combined >= CFG.entry_score_threshold:
        signal = (
            Signal.STRONG_BUY
            if combined >= CFG.entry_score_threshold * 1.5
            else Signal.BUY
        )
    elif combined <= -CFG.entry_score_threshold:
        signal = (
            Signal.STRONG_SELL
            if combined <= -CFG.entry_score_threshold * 1.5
            else Signal.SELL
        )
    else:
        signal = Signal.NEUTRAL

    return signal, combined


# ============================================================
# POSITION STATE MACHINE
# ============================================================


class PositionState(Enum):
    FLAT = auto()
    ENTERING = auto()
    OPEN = auto()
    EXITING = auto()


@dataclass
class Position:
    symbol: str
    state: PositionState = PositionState.FLAT
    side: Optional[str] = None  # "long" | "short"
    entry_price: float = 0.0
    size: float = 0.0
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    trailing_stop_price: float = 0.0
    entry_atr: float = 0.0
    opened_at: float = 0.0
    highest_price_since_entry: float = 0.0  # for long trailing stop
    lowest_price_since_entry: float = 0.0  # for short trailing stop


# ============================================================
# RISK MANAGER
# ============================================================


class RiskManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.starting_equity = cfg.account_equity_usd
        self.current_equity = cfg.account_equity_usd
        self.daily_start_equity = cfg.account_equity_usd
        self.daily_reset_ts = time.time()
        self.halted_for_day = False
        self.halted_permanently = False

    def maybe_reset_daily(self):
        now = time.time()
        if now - self.daily_reset_ts >= 86400:
            self.daily_start_equity = self.current_equity
            self.daily_reset_ts = now
            self.halted_for_day = False
            log.info("Risk manager: daily loss counter reset.")

    def record_equity(self, new_equity: float):
        self.current_equity = new_equity
        self.maybe_reset_daily()

        daily_loss_pct = (
            (self.daily_start_equity - new_equity) / self.daily_start_equity * 100
        )
        if daily_loss_pct >= self.cfg.max_daily_loss_pct and not self.halted_for_day:
            self.halted_for_day = True
            log.warning(
                f"⛔ Daily loss limit hit ({daily_loss_pct:.2f}%) — halting new entries until reset."
            )

        drawdown_pct = (self.starting_equity - new_equity) / self.starting_equity * 100
        if drawdown_pct >= self.cfg.max_drawdown_pct and not self.halted_permanently:
            self.halted_permanently = True
            log.critical(
                f"🛑 Max drawdown hit ({drawdown_pct:.2f}%) — halting bot entirely. Manual review required."
            )

    def can_open_new_position(self, open_position_count: int) -> bool:
        if self.halted_permanently:
            return False
        if self.halted_for_day:
            return False
        if open_position_count >= self.cfg.max_concurrent_positions:
            return False
        return True

    def calculate_position_size(self, entry_price: float, stop_price: float) -> float:
        """
        Sizes the position so that if the stop is hit, the loss equals
        risk_per_trade_pct of current equity — then caps it by a hard
        max-position-pct-of-equity ceiling regardless of stop distance.
        """
        risk_amount_usd = self.current_equity * (self.cfg.risk_per_trade_pct / 100)
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return 0.0

        size_by_risk = risk_amount_usd / stop_distance
        max_position_usd = self.current_equity * (
            self.cfg.max_position_pct_of_equity / 100
        )
        size_by_cap = max_position_usd / entry_price

        return min(size_by_risk, size_by_cap)


# ============================================================
# ORDER EXECUTION (with retries)
# ============================================================


class OrderExecutionError(Exception):
    pass


async def place_order_with_retry(
    exchange, symbol: str, side: str, amount: float, cfg: Config
):
    """
    Places a market order with retry/backoff on transient failures.
    Distinguishes retryable (network/exchange-busy) errors from ones
    that should fail fast (insufficient balance, invalid params).
    """
    if cfg.dry_run:
        log.info(
            f"🧪 [DRY RUN] Would place {side.upper()} order: {amount:.6f} {symbol}"
        )
        return {
            "id": "dry-run",
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "status": "closed",
            "price": None,
            "filled": amount,
        }

    last_exc = None
    for attempt in range(cfg.order_retry_attempts):
        try:
            order = await exchange.create_order(symbol, "market", side, amount)
            log.info(
                f"✅ Order placed: {side.upper()} {amount:.6f} {symbol} -> id={order.get('id')}"
            )
            return order
        except ccxt.InsufficientFunds as e:
            raise OrderExecutionError(
                f"Insufficient funds for {symbol}: {e}"
            )  # don't retry
        except ccxt.InvalidOrder as e:
            raise OrderExecutionError(
                f"Invalid order params for {symbol}: {e}"
            )  # don't retry
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as e:
            last_exc = e
            wait = cfg.order_retry_backoff_seconds * (2**attempt)
            log.warning(
                f"⚠️ Order attempt {attempt + 1}/{cfg.order_retry_attempts} failed "
                f"for {symbol}: {e} — retrying in {wait:.1f}s"
            )
            await asyncio.sleep(wait)
        except ccxt.ExchangeError as e:
            last_exc = e
            wait = cfg.order_retry_backoff_seconds * (2**attempt)
            log.warning(
                f"⚠️ Exchange error on attempt {attempt + 1}: {e} — retrying in {wait:.1f}s"
            )
            await asyncio.sleep(wait)

    raise OrderExecutionError(
        f"Order failed for {symbol} after {cfg.order_retry_attempts} attempts: {last_exc}"
    )


def check_slippage(
    expected_price: float, filled_price: Optional[float], side: str, cfg: Config
) -> bool:
    """Returns True if the fill is within tolerance, False if it should be flagged."""
    if filled_price is None:
        return True  # dry run / no fill price available
    diff_pct = abs(filled_price - expected_price) / expected_price * 100
    if diff_pct > cfg.slippage_tolerance_pct:
        log.warning(
            f"⚠️ Slippage {diff_pct:.2f}% exceeds tolerance "
            f"({cfg.slippage_tolerance_pct}%): expected {expected_price}, filled {filled_price}"
        )
        return False
    return True


# ============================================================
# TRADING ENGINE — per-symbol state machine + orchestration
# ============================================================


class TradingEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exchange = getattr(ccxt, cfg.exchange_id)(
            {
                "apiKey": cfg.api_key,
                "secret": cfg.api_secret,
                "enableRateLimit": True,
            }
        )
        self.risk_manager = RiskManager(cfg)
        self.positions: dict[str, Position] = {
            symbol: Position(symbol=symbol) for symbol in cfg.symbols
        }

    async def fetch_market_snapshot(self, symbol: str) -> Optional[MarketSnapshot]:
        try:
            ohlcv = await self.exchange.fetch_ohlcv(
                symbol, timeframe=self.cfg.timeframe, limit=self.cfg.candle_limit
            )
        except Exception as e:
            log.error(f"Failed to fetch OHLCV for {symbol}: {e}")
            return None

        if (
            len(ohlcv)
            < max(self.cfg.ema_trend, self.cfg.bb_period, self.cfg.atr_period) + 5
        ):
            log.warning(
                f"Not enough candle history for {symbol} yet ({len(ohlcv)} bars) — skipping this cycle."
            )
            return None

        closes = np.array([c[4] for c in ohlcv], dtype=float)
        highs = np.array([c[2] for c in ohlcv], dtype=float)
        lows = np.array([c[3] for c in ohlcv], dtype=float)

        snapshot = MarketSnapshot(
            symbol=symbol, closes=closes, highs=highs, lows=lows, last_price=closes[-1]
        )
        return compute_indicators(snapshot)

    def open_position_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.state == PositionState.OPEN)

    async def try_enter_position(
        self, snapshot: MarketSnapshot, signal: Signal, score: float
    ):
        symbol = snapshot.symbol
        position = self.positions[symbol]

        if position.state != PositionState.FLAT:
            return  # already in/entering a position for this symbol

        if not self.risk_manager.can_open_new_position(self.open_position_count()):
            return

        if signal not in (Signal.BUY, Signal.STRONG_BUY):
            # This example only takes long entries — extend with short logic
            # (and margin/futures API calls) if your exchange/account supports it.
            return

        entry_price = snapshot.last_price
        current_atr = snapshot.atr[-1]
        if np.isnan(current_atr) or current_atr <= 0:
            log.warning(f"{symbol}: ATR unavailable, skipping entry this cycle.")
            return

        stop_price = entry_price - (current_atr * self.cfg.atr_stop_multiplier)
        take_profit_price = entry_price + (
            current_atr * self.cfg.atr_take_profit_multiplier
        )

        size = self.risk_manager.calculate_position_size(entry_price, stop_price)
        if size <= 0:
            log.warning(f"{symbol}: calculated position size is zero — skipping entry.")
            return

        position.state = PositionState.ENTERING
        log.info(
            f"📈 {symbol}: entering LONG — score={score:.2f} signal={signal.name} "
            f"entry~{entry_price:.4f} stop={stop_price:.4f} tp={take_profit_price:.4f} size={size:.6f}"
        )

        try:
            order = await place_order_with_retry(
                self.exchange, symbol, "buy", size, self.cfg
            )
        except OrderExecutionError as e:
            log.error(f"{symbol}: entry order failed — {e}")
            position.state = PositionState.FLAT
            return

        filled_price = order.get("price") or entry_price
        check_slippage(entry_price, order.get("price"), "buy", self.cfg)

        position.state = PositionState.OPEN
        position.side = "long"
        position.entry_price = filled_price
        position.size = order.get("filled", size)
        position.stop_price = stop_price
        position.take_profit_price = take_profit_price
        position.trailing_stop_price = stop_price
        position.entry_atr = current_atr
        position.opened_at = time.time()
        position.highest_price_since_entry = filled_price

    async def manage_open_position(self, snapshot: MarketSnapshot):
        symbol = snapshot.symbol
        position = self.positions[symbol]
        if position.state != PositionState.OPEN or position.side != "long":
            return

        price = snapshot.last_price
        position.highest_price_since_entry = max(
            position.highest_price_since_entry, price
        )

        # Trail the stop upward once price has moved favorably.
        trailing_candidate = position.highest_price_since_entry - (
            position.entry_atr * self.cfg.trailing_stop_atr_multiplier
        )
        if trailing_candidate > position.trailing_stop_price:
            position.trailing_stop_price = trailing_candidate

        effective_stop = max(position.stop_price, position.trailing_stop_price)

        exit_reason = None
        if price <= effective_stop:
            exit_reason = (
                "stop_loss"
                if effective_stop == position.stop_price
                else "trailing_stop"
            )
        elif price >= position.take_profit_price:
            exit_reason = "take_profit"

        if exit_reason:
            await self.exit_position(position, price, exit_reason)

    async def exit_position(self, position: Position, exit_price: float, reason: str):
        symbol = position.symbol
        position.state = PositionState.EXITING
        log.info(
            f"📉 {symbol}: exiting position ({reason}) at ~{exit_price:.4f} "
            f"(entry was {position.entry_price:.4f})"
        )

        try:
            order = await place_order_with_retry(
                self.exchange, symbol, "sell", position.size, self.cfg
            )
        except OrderExecutionError as e:
            log.error(
                f"{symbol}: exit order FAILED — {e}. Position may still be open on the exchange; "
                f"manual intervention may be required."
            )
            position.state = (
                PositionState.OPEN
            )  # revert — don't silently lose track of an open position
            return

        filled_price = order.get("price") or exit_price
        pnl_usd = (filled_price - position.entry_price) * position.size
        pnl_pct = (filled_price - position.entry_price) / position.entry_price * 100

        log.info(
            f"{'🟢' if pnl_usd >= 0 else '🔴'} {symbol}: closed — "
            f"PnL: {pnl_usd:+.2f} USD ({pnl_pct:+.2f}%) — reason={reason}"
        )

        self.risk_manager.record_equity(self.risk_manager.current_equity + pnl_usd)

        self.positions[symbol] = Position(symbol=symbol)  # reset to flat

    async def run_cycle(self):
        if self.risk_manager.halted_permanently:
            log.critical("Bot halted (max drawdown). Skipping cycle entirely.")
            return

        for symbol in self.cfg.symbols:
            snapshot = await self.fetch_market_snapshot(symbol)
            if snapshot is None:
                continue

            position = self.positions[symbol]

            if position.state == PositionState.OPEN:
                await self.manage_open_position(snapshot)
                continue  # don't also evaluate new entries while holding a position

            signal, score = generate_signal(snapshot)
            log.info(
                f"{symbol}: price={snapshot.last_price:.4f} signal={signal.name} score={score:.2f}"
            )

            await self.try_enter_position(snapshot, signal, score)

    async def run_forever(self):
        log.info(
            f"🚀 Trading engine starting — symbols={self.cfg.symbols} "
            f"dry_run={self.cfg.dry_run} exchange={self.cfg.exchange_id}"
        )
        try:
            while True:
                cycle_start = time.monotonic()
                try:
                    await self.run_cycle()
                except Exception as e:
                    log.exception(f"Unhandled error in trading cycle: {e}")

                elapsed = time.monotonic() - cycle_start
                await asyncio.sleep(max(0, self.cfg.poll_interval_seconds - elapsed))
        finally:
            await self.exchange.close()


# ============================================================
# ENTRY POINT
# ============================================================


async def main():
    engine = TradingEngine(CFG)
    try:
        await engine.run_forever()
    except KeyboardInterrupt:
        log.info("🛑 Stopped by user.")
    finally:
        await engine.exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
