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

    # --- timeframes: daily (trend filter) and 4h (breakout entry + stops) ---
    daily_timeframe: str = field(default_factory=lambda: _env("DAILY_TIMEFRAME", "1d"))
    entry_timeframe: str = field(default_factory=lambda: _env("ENTRY_TIMEFRAME", "4h"))
    daily_candle_limit: int = field(default_factory=lambda: int(_env("DAILY_CANDLE_LIMIT", "200")))
    entry_candle_limit: int = field(default_factory=lambda: int(_env("ENTRY_CANDLE_LIMIT", "300")))

    # --- daily trend filter (strategy.detect_trend) ---
    # Longs only when the daily close is above a rising EMA of this
    # period, shorts only below a falling one; neutral = no trade.
    trend_ema_period: int = field(default_factory=lambda: int(_env("TREND_EMA_PERIOD", "50")))

    # --- 4h breakout entry (strategy.donchian_breakout) ---
    # The entry fires on the first 4h close beyond the highest high /
    # lowest low of this many previous 4h candles (20 = about 3.3 days).
    breakout_lookback: int = field(default_factory=lambda: int(_env("BREAKOUT_LOOKBACK", "20")))

    # --- ATR stops (strategy.build_trend_entry) ---
    atr_period: int = field(default_factory=lambda: int(_env("ATR_PERIOD", "14")))
    # Initial stop distance from entry, in ATRs of the 4h chart.
    stop_atr_mult: float = field(default_factory=lambda: float(_env("STOP_ATR_MULT", "2.5")))
    # Trailing ("chandelier") stop: this many ATRs (ATR fixed at entry)
    # behind the best price reached since entry. There is no take-profit;
    # the trailing stop is the exit.
    trail_atr_mult: float = field(default_factory=lambda: float(_env("TRAIL_ATR_MULT", "3")))

    # --- optional cross-sectional momentum filter (bot.py) ---
    # When on, longs are only taken in the top MOMENTUM_TOP_FRACTION of
    # watched symbols by MOMENTUM_LOOKBACK_DAYS return, shorts only in the
    # bottom fraction.
    momentum_filter: bool = field(default_factory=lambda: _bool("MOMENTUM_FILTER", False))
    momentum_lookback_days: int = field(default_factory=lambda: int(_env("MOMENTUM_LOOKBACK_DAYS", "30")))
    momentum_top_fraction: float = field(
        default_factory=lambda: float(_env("MOMENTUM_TOP_FRACTION", "0.5"))
    )

    # --- risk management: fixed margin per trade, leverage flexes to cap risk ---
    # Target risk, as % of equity, that position_size() solves each
    # trade's leverage for. Defaults to the hard cap itself (3%) -- i.e.
    # every trade targets the maximum allowed risk given its own
    # stop-loss distance. Lower this if you want trades to risk less
    # than the 3% ceiling.
    risk_pct_per_trade: float = field(
        default_factory=lambda: float(_env("RISK_PCT_PER_TRADE", "3")) / 100
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
    if not (0.0 < cfg.max_margin_utilization_pct <= 1.0):
        print(
            f"[config] MAX_MARGIN_UTILIZATION_PCT={cfg.max_margin_utilization_pct * 100:.0f}% "
            f"is out of range (0-100]; clamping to 50%."
        )
        cfg.max_margin_utilization_pct = 0.5
    if cfg.min_notional_usdt < 0:
        print(f"[config] MIN_NOTIONAL_USDT={cfg.min_notional_usdt} is negative; clamping to 0.")
        cfg.min_notional_usdt = 0.0
    for name in ("stop_atr_mult", "trail_atr_mult"):
        if getattr(cfg, name) <= 0:
            print(f"[config] {name.upper()}={getattr(cfg, name)} must be positive; using 3.")
            setattr(cfg, name, 3.0)
    if cfg.breakout_lookback < 2:
        print(f"[config] BREAKOUT_LOOKBACK={cfg.breakout_lookback} too small; using 20.")
        cfg.breakout_lookback = 20
    if not (0.0 < cfg.momentum_top_fraction <= 1.0):
        print(f"[config] MOMENTUM_TOP_FRACTION={cfg.momentum_top_fraction} out of (0, 1]; using 0.5.")
        cfg.momentum_top_fraction = 0.5
    return cfg
