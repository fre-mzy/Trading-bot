"""Central settings. Override any of these with environment variables / repo variables."""
import os

DB_PATH = os.getenv("DB_PATH", "signals_test.db")

SYMBOL = os.getenv("SYMBOL", "XAU/USD")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Paper-trading account
START_BALANCE = float(os.getenv("START_BALANCE", "10000"))
RISK_PCT = float(os.getenv("RISK_PCT", "1.0"))          # % of balance risked per trade

# Twelve Data's quote endpoint does not reliably return bid/ask for XAU/USD, so the spread
# is an ASSUMPTION (in dollars per oz). Set this to what your broker really charges.
ASSUMED_SPREAD = float(os.getenv("ASSUMED_SPREAD", "0.30"))
MAX_SPREAD_FRACTION_OF_SL = 0.25    # skip trades where spread eats >25% of the stop distance

# Signal rules
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "75"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))   # gap between new signals
MAX_HOLD_CANDLES = int(os.getenv("MAX_HOLD_CANDLES", "144"))  # 144 x 5min = 12 trading hours
MAX_DATA_AGE_MINUTES = 20           # newest closed 5m candle must be at most this old
