#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NIFTY LONG STRADDLE
------------------
✔ No ATM rolling
✔ Exit ONLY on Target / SL
✔ Re-entry only after full exit
✔ FUT-based ATR (minute candles)
✔ ATR stored in DB
✔ Railway compatible
✔ Key parameters via ENV variables
"""

import os, time, math, pytz
from datetime import datetime
import psycopg2
from kiteconnect import KiteConnect

# =========================================================
# CONFIG (ENV SAFE)
# =========================================================

TRADE_MODE = os.getenv("TRADE_MODE", "PAPER")

API_KEY = os.getenv("KITE_API_KEY", "")
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

# ---------- STRATEGY PARAMS (ENV) ----------
ENTRY_TOL = int(os.getenv("ENTRY_TOL", 10))
QTY_PER_LEG = int(os.getenv("QTY_PER_LEG", 65))
PROFIT_TARGET = float(os.getenv("PROFIT_TARGET", 1500))
STOP_LOSS = float(os.getenv("STOP_LOSS", 1500))
SNAPSHOT_INTERVAL = int(os.getenv("SNAPSHOT_INTERVAL", 30))

# ---------- INSTRUMENT ----------
INDEX_SYMBOL = "NSE:NIFTY 50"
FUT_SYMBOL = "NSE:NIFTY FUT"
UNDERLYING = "NIFTY"
STRIKE_STEP = 50

ATR_PERIOD = 14
TICK_INTERVAL = 1

MARKET_TZ = pytz.timezone("Asia/Kolkata")

# =========================================================
# ATR BUILDER (UNCHANGED – YOUR LOGIC)
# =========================================================

class FutAtrBuilder:
    def __init__(self, atr_period: int):
        self.atr_period = atr_period
        self.tr_history = []

        self.last_minute_key = None
        self.minute_high = None
        self.minute_low = None
        self.minute_close = None

        self.prev_close = None
        self.atr_val = None

    def update(self, now: datetime, fut_ltp: float):
        if fut_ltp is None or math.isnan(fut_ltp):
            return self.atr_val

        minute_key = now.replace(second=0, microsecond=0)

        if self.last_minute_key is None:
            self.last_minute_key = minute_key
            self.minute_high = fut_ltp
            self.minute_low = fut_ltp
            self.minute_close = fut_ltp
            self.prev_close = fut_ltp
            return self.atr_val

        if minute_key == self.last_minute_key:
            self.minute_high = max(self.minute_high, fut_ltp)
            self.minute_low = min(self.minute_low, fut_ltp)
            self.minute_close = fut_ltp
            return self.atr_val

        tr = max(
            self.minute_high - self.minute_low,
            abs(self.minute_high - self.prev_close),
            abs(self.minute_low - self.prev_close),
        )
        self.tr_history.append(tr)

        if len(self.tr_history) >= self.atr_period:
            self.atr_val = round(
                sum(self.tr_history[-self.atr_period:]) / self.atr_period, 2
            )
        else:
            self.atr_val = None

        self.prev_close = self.minute_close
        self.last_minute_key = minute_key
        self.minute_high = fut_ltp
        self.minute_low = fut_ltp
        self.minute_close = fut_ltp

        return self.atr_val

# =========================================================
# DB HELPERS
# =========================================================

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def ensure_table():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS niftylong_strangle (
            ts TIMESTAMPTZ,
            status TEXT,
            event TEXT,
            reason TEXT,
            spot DOUBLE PRECISION,
            ce_strike INT,
            pe_strike INT,
            ce_entry DOUBLE PRECISION,
            pe_entry DOUBLE PRECISION,
            ce_ltp DOUBLE PRECISION,
            pe_ltp DOUBLE PRECISION,
            unreal_pnl DOUBLE PRECISION,
            realized_pnl DOUBLE PRECISION,
            atr DOUBLE PRECISION
        );
        """)
        cur.execute("""
        ALTER TABLE niftylong_strangle
        ADD COLUMN IF NOT EXISTS atr DOUBLE PRECISION;
        """)
        conn.commit()

def log_db(**row):
    cols = ",".join(row.keys())
    vals = tuple(row.values())
    ph = ",".join(["%s"] * len(vals))

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO niftylong_strangle ({cols}) VALUES ({ph})",
            vals
        )
        conn.commit()

# =========================================================
# KITE
# =========================================================

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

# =========================================================
# STATE
# =========================================================

position_open = False
ce_symbol = pe_symbol = None
ce_entry = pe_entry = None
realized_pnl = 0.0

atr_builder = FutAtrBuilder(ATR_PERIOD)
last_snapshot_ts = time.time()

# =========================================================
# HELPERS
# =========================================================

def round_to_strike(price):
    return int(round(price / STRIKE_STEP) * STRIKE_STEP)

def is_near_strike(spot):
    return abs(spot - round_to_strike(spot)) <= ENTRY_TOL

# =========================================================
# MAIN LOOP
# =========================================================

ensure_table()
print("🚀 NIFTY Long Straddle started")
print(
    f"CONFIG → ENTRY_TOL={ENTRY_TOL}, QTY={QTY_PER_LEG}, "
    f"TARGET={PROFIT_TARGET}, SL={STOP_LOSS}, "
    f"SNAPSHOT={SNAPSHOT_INTERVAL}s"
)

while True:
    try:
        now = datetime.now(MARKET_TZ)

        spot = kite.ltp(INDEX_SYMBOL)[INDEX_SYMBOL]["last_price"]
        fut = kite.ltp(FUT_SYMBOL)[FUT_SYMBOL]["last_price"]

        atr = atr_builder.update(now, fut)

        # ================= ENTRY =================
        if not position_open and is_near_strike(spot):
            atm = round_to_strike(spot)

            ce_symbol = f"NFO:{UNDERLYING}{atm}CE"
            pe_symbol = f"NFO:{UNDERLYING}{atm}PE"

            ce_entry = kite.ltp(ce_symbol)[ce_symbol]["last_price"]
            pe_entry = kite.ltp(pe_symbol)[pe_symbol]["last_price"]

            position_open = True

            log_db(
                ts=now, status="OPEN", event="ENTRY", reason="ATM",
                spot=spot,
                ce_strike=atm, pe_strike=atm,
                ce_entry=ce_entry, pe_entry=pe_entry,
                ce_ltp=ce_entry, pe_ltp=pe_entry,
                unreal_pnl=0.0,
                realized_pnl=realized_pnl,
                atr=atr
            )

        # ================= MONITOR =================
        if position_open:
            ce_ltp = kite.ltp(ce_symbol)[ce_symbol]["last_price"]
            pe_ltp = kite.ltp(pe_symbol)[pe_symbol]["last_price"]

            unreal = (ce_ltp - ce_entry + pe_ltp - pe_entry) * QTY_PER_LEG

            # ---- EXIT ----
            if unreal >= PROFIT_TARGET or unreal <= -STOP_LOSS:
                realized_pnl += unreal
                position_open = False

                log_db(
                    ts=now, status="EXIT", event="EXIT",
                    reason="TARGET" if unreal >= PROFIT_TARGET else "SL",
                    spot=spot,
                    ce_strike=None, pe_strike=None,
                    ce_entry=ce_entry, pe_entry=pe_entry,
                    ce_ltp=ce_ltp, pe_ltp=pe_ltp,
                    unreal_pnl=unreal,
                    realized_pnl=realized_pnl,
                    atr=atr
                )

            # ---- SNAPSHOT ----
            elif time.time() - last_snapshot_ts >= SNAPSHOT_INTERVAL:
                last_snapshot_ts = time.time()

                log_db(
                    ts=now, status="RUNNING", event="", reason="",
                    spot=spot,
                    ce_strike=None, pe_strike=None,
                    ce_entry=ce_entry, pe_entry=pe_entry,
                    ce_ltp=ce_ltp, pe_ltp=pe_ltp,
                    unreal_pnl=unreal,
                    realized_pnl=realized_pnl,
                    atr=atr
                )

        time.sleep(TICK_INTERVAL)

    except Exception as e:
        print("❌ ERROR:", e)
        time.sleep(5)
