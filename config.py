"""
Configuration for the BingX trend-pullback futures bot.

All values are read from environment variables so no secrets ever live in
the code itself. Copy .env.example to .env, fill it in, and make sure your
run environment loads it (the included run.sh does this, or you can
`export $(cat .env | xargs)` before running).
"""
import os
from dataclasses import dataclass, field


def _env(env_key: str, default: str) -> str:
    """Like os.getenv, but also falls back to `default` when the variable
    is set to an empty string. GitHub Actions' `${{ vars.X }}` syntax
    always defines the env var, substituting "" when the repo Variable
    was never created -- plain os.getenv(key, default) would miss that
    and pass an empty string straight through to int()/float()."""
    val = os.getenv(env_key)
    if val is None or val.strip() == "":
        return default
    return val


def _bool(env_key: str, default: bool) -> bool:
    val = os.getenv(env_key)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_list(env_key: str, default_csv: str) -> list[str]:
    """Comma-separated list env var -> list of stripped strings."""
    val = _env(env_key, default_csv)
    return [s.strip() for s in val.split(",") if s.strip()]


# Same curated ~15-symbol basket of liquid BingX USDT-M perpetuals used by
# the order-block bot -- not "every symbol BingX lists" on purpose, since
# thin/illiquid pairs carry much higher slippage and manipulation risk.
# Override with the SYMBOLS repo variable (comma-separated).
DEFAULT_SYMBOLS = (
    "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,BNB/USDT:USDT,XRP/USDT:USDT,"
    "DOGE/USDT:USDT,ADA/USDT:USDT,AVAX/USDT:USDT,LINK/USDT:USDT,LTC/USDT:USDT,"
    "DOT/USDT:USDT,TRX/USDT:USDT,NEAR/USDT:USDT,ATOM/USDT:USDT,APT/USDT:USDT"
)


@dataclass
class Config:
    # --- credentials ---
    api_key: str = field(default_factory=lambda: _env("BINGX_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: _env("BINGX_API_SECRET", ""))

    # --- market ---
    symbols: list = field(default_factory=lambda: _env_list("SYMBOLS", DEFAULT_SYMBOLS))

    # --- the three timeframes this strategy looks at, and only these
    # three: weekly (primary trend), daily (primary trend + pullback
    # zone), 4h (entry trigger). There is deliberately no 15m/1h "noise"
    # level -- this bot only ever analyzes weekly, daily and 4h charts. ---
    weekly_timeframe: str = field(default_factory=lambda: _env("WEEKLY_TIMEFRAME", "1w"))
    daily_timeframe: str = field(default_factory=lambda: _env("DAILY_TIMEFRAME", "1d"))
    entry_timeframe: str = field(default_factory=lambda: _env("ENTRY_TIMEFRAME", "4h"))

    weekly_candle_limit: int = field(default_factory=lambda: int(_env("WEEKLY_CANDLE_LIMIT", "150")))
    daily_candle_limit: int = field(default_factory=lambda: int(_env("DAILY_CANDLE_LIMIT", "200")))
    entry_candle_limit: int = field(default_factory=lambda: int(_env("ENTRY_CANDLE_LIMIT", "300")))

    # --- trend detection (weekly + daily) ---
    # Same EMA-slope trend read used by the order-block bot: bullish if
    # price is above a rising EMA, bearish if below a falling EMA,
    # neutral otherwise. Applied to both the weekly and daily closes.
    trend_ema_period: int = field(default_factory=lambda: int(_env("TREND_EMA_PERIOD", "50")))

    # Weekly trend must be clearly bullish or bearish for this strategy to
    # look for a trade at all -- a pullback needs a trend to pull back
    # from, so a neutral weekly reading skips the symbol outright (unlike
    # the order-block bot, where a neutral primary reading just removes
    # the directional filter). Daily trend must not directly contradict
    # the weekly trend (same conflict rule as combine_htf_bias); if it
    # does, the symbol is skipped for this cycle -- there is no
    # weekly/daily override here, unlike the order-block bot, to keep
    # this strategy's rules simple and easy to reason about.
    require_weekly_trend: bool = field(default_factory=lambda: _bool("REQUIRE_WEEKLY_TREND", True))

    # --- pullback zone (daily) ---
    # A daily EMA that acts as the "value zone" trend-following pullbacks
    # are typically bought (in an uptrend) or sold (in a downtrend) into.
    # Separate from trend_ema_period on purpose: trend_ema_period should
    # stay slow/stable (default 50) so the trend read doesn't flicker,
    # while pullback_ema_period can be faster (default 20) so the zone
    # actually gets tagged periodically instead of only in deep pullbacks.
    pullback_ema_period: int = field(default_factory=lambda: int(_env("PULLBACK_EMA_PERIOD", "20")))
    # How close (%) price needs to get to the pullback EMA to count as
    # "tagged" -- the daily candle's low (uptrend) or high (downtrend)
    # must come within this percent of the EMA, or trade through it.
    # Stops a pullback that "almost" reaches the zone from being ignored,
    # since candles rarely close exactly on an EMA.
    pullback_touch_tolerance_pct: float = field(
        default_factory=lambda: float(_env("PULLBACK_TOUCH_TOLERANCE_PCT", "0.5"))
    )
    # N-bar fractal window (see strategy.find_fractals) used to find the
    # most recently confirmed daily swing low/high. A pullback that has
    # already closed beyond that level is treated as a possible trend
    # reversal, not a healthy pullback, and is skipped -- see
    # strategy.daily_pullback_zone().
    fractal_n: int = field(default_factory=lambda: int(_env("FRACTAL_N", "2")))

    # --- entry trigger (4h) ---
    # Fast EMA on the 4h chart. The entry trigger is a 4h close crossing
    # back to the trend side of this EMA (from the wrong side on the
    # previous 4h candle) while the daily pullback zone is active -- see
    # strategy.entry_trigger(). Kept separate from pullback_ema_period
    # since they run on different timeframes.
    trigger_ema_period: int = field(default_factory=lambda: int(_env("TRIGGER_EMA_PERIOD", "9")))
    # Extra room (%) beyond the nearest confirmed 4h swing low/high used
    # for the stop-loss, so the stop sits just past structure rather than
    # exactly on it (a exact-on-structure stop gets clipped by normal
    # wicking noise far too often).
    stop_buffer_pct: float = field(default_factory=lambda: float(_env("STOP_BUFFER_PCT", "0.15")))

    # --- reward:risk ---
    # Hard rule, not just a default: take-profit is always exactly this
    # multiple of the stop-loss distance (mirrors the order-block bot).
    # Since partial-TP + trailing (below) manage the runner after the
    # partial exit, this fixed target is really only ever hit as a full
    # exit if PARTIAL_TP_FRACTION is set to 0.
    required_reward_risk_ratio: float = 2.0
    reward_risk_ratio: float = field(default_factory=lambda: float(_env("REWARD_RISK_RATIO", "2")))

    # --- risk management: fixed margin per trade, leverage flexes to cap risk ---
    risk_pct_per_trade: float = field(
        default_factory=lambda: float(_env("RISK_PCT_PER_TRADE", "0.5")) / 100
    )
    max_risk_pct_per_trade_hard_cap: float = 0.03
    margin_per_trade_usdt: float = field(
        default_factory=lambda: float(_env("MARGIN_PER_TRADE_USDT", "50"))
    )
    max_leverage_hard_cap: int = 10
    max_margin_utilization_pct: float = field(
        default_factory=lambda: float(_env("MAX_MARGIN_UTILIZATION_PCT", "50")) / 100
    )
    max_daily_loss_pct: float = field(
        default_factory=lambda: float(_env("MAX_DAILY_LOSS_PCT", "3")) / 100
    )
    max_concurrent_positions: int = field(
        default_factory=lambda: int(_env("MAX_CONCURRENT_POSITIONS", "1"))
    )
    min_notional_usdt: float = field(
        default_factory=lambda: float(_env("MIN_NOTIONAL_USDT", "10"))
    )

    # --- partial take-profit + trailing stop (identical to the order-block bot) ---
    partial_tp_r_multiple: float = field(
        default_factory=lambda: float(_env("PARTIAL_TP_R_MULTIPLE", "1.0"))
    )
    partial_tp_fraction: float = field(
        default_factory=lambda: float(_env("PARTIAL_TP_FRACTION", "0.5"))
    )
    trail_r_multiple: float = field(
        default_factory=lambda: float(_env("TRAIL_R_MULTIPLE", "1.0"))
    )

    # --- cost modeling ---
    dry_run_fee_pct_per_side: float = field(
        default_factory=lambda: float(_env("DRY_RUN_FEE_PCT_PER_SIDE", "0.05")) / 100
    )

    # --- safety ---
    # Defaults to True on purpose. You must consciously set DRY_RUN=false
    # to let the bot place real orders.
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))

    # --- paths ---
    state_path: str = field(default_factory=lambda: _env("STATE_PATH", "data/state.json"))
    log_path: str = field(default_factory=lambda: _env("LOG_PATH", "data/trades.log"))
    trades_csv_path: str = field(default_factory=lambda: _env("TRADES_CSV_PATH", "data/trades.csv"))


def load_config() -> Config:
    cfg = Config()
    if cfg.margin_per_trade_usdt <= 0:
        print(
            f"[config] MARGIN_PER_TRADE_USDT={cfg.margin_per_trade_usdt} is not positive; "
            f"clamping to 50."
        )
        cfg.margin_per_trade_usdt = 50.0
if cfg.risk_pct_per_trade > cfg.max_risk_pct_per_trade_hard_cap:
        print(
            f"[config] RISK_PCT_PER_TRADE={cfg.risk_pct_per_trade * 100:.2f}% exceeds hard cap "
            f"({cfg.max_risk_pct_per_trade_hard_cap * 100:.0f}%); clamping."
        )
        cfg.risk_pct_per_trade = cfg.max_risk_pct_per_trade_hard_cap
    if cfg.reward_risk_ratio != cfg.required_reward_risk_ratio:
        print(
            f"[config] REWARD_RISK_RATIO={cfg.reward_risk_ratio} ignored -- this strategy always "
            f"uses a fixed {cfg.required_reward_risk_ratio}:1 reward:risk ratio."
        )
        cfg.reward_risk_ratio = cfg.required_reward_risk_ratio
    if not (0.0 < cfg.max_margin_utilization_pct <= 1.0):
        print(
            f"[config] MAX_MARGIN_UTILIZATION_PCT={cfg.max_margin_utilization_pct * 100:.0f}% "
            f"is out of range (0-100]; clamping to 50%."
        )
        cfg.max_margin_utilization_pct = 0.5
    if cfg.min_notional_usdt < 0:
        print(f"[config] MIN_NOTIONAL_USDT={cfg.min_notional_usdt} is negative; clamping to 0.")
        cfg.min_notional_usdt = 0.0
    if cfg.stop_buffer_pct < 0:
        print(f"[config] STOP_BUFFER_PCT={cfg.stop_buffer_pct} is negative; clamping to 0.")
        cfg.stop_buffer_pct = 0.0
    if cfg.pullback_touch_tolerance_pct < 0:
        print(
            f"[config] PULLBACK_TOUCH_TOLERANCE_PCT={cfg.pullback_touch_tolerance_pct} "
            f"is negative; clamping to 0."
        )
        cfg.pullback_touch_tolerance_pct = 0.0
    return cfg
