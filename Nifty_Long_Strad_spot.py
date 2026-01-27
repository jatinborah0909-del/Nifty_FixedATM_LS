#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
NIFTY LONG STRADDLE – SCHEMA SAFE (FIXED + VIX)
---------------------------------------------
✔ No ATM rolling
✔ Exit ONLY on Target / SL
✔ Re-entry only after full exit
✔ FUT-based ATR (minute candles)
✔ ATR stored in DB
✔ CE / PE SYMBOL + STRIKE STORED
✔ VIX (PREV CLOSE + LIVE) STORED
✔ AUTO schema migration
✔ Railway compatible
"""

import os, time, math, pytz
from datetime import datetime, time as dt_time
import psycopg2
from kiteconnect import KiteConnect

# =========================================================
# CONFIG
# =========================================================

API_KEY = os.getenv("KITE_API_KEY", "")
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL")

ENTRY_TOL = int(os.getenv("ENTRY_TOL", 10))
QTY_PER_LEG = int(os.getenv("QTY_PER_LEG", 65))
PROFIT_TARGET = float(os.getenv("PROFIT_TARGET", 1500))
STOP_LOSS = float(os.getenv("STOP_LOSS", 1500))
SNAPSHOT_INTERVAL = int(os.getenv("SNAPSHOT_INTERVAL", 30))

MARKET_START_TIME = os.getenv("MARKET_START_TIME", "09:15")
MARKET_END_TIME   = os.getenv("MARKET_END_TIME", "15:30")

INDEX_SYMBOL = "NSE:NIFTY 50"
STRIKE_STEP = 50
ATR_PERIOD = 14
TICK_INTERVAL = 1

MARKET_TZ = pytz.timezone("Asia/Kolkata")

def parse_time(t):
    h, m = map(int, t.split(":"))
    return dt_time(h, m)

MARKET_START = parse_time(MARKET_START_TIME)
MARKET_END   = parse_time(MARKET_END_TIME)

# =========================================================
# KITE
# =========================================================

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

# =========================================================
# INSTRUMENT RESOLUTION
# =========================================================

NFO_INSTRUMENTS = kite.instruments("NFO")

def get_nearest_nifty_fut():
    futs = [i for i in NFO_INSTRUMENTS if i["name"] == "NIFTY" and i["instrument_type"] == "FUT"]
    futs.sort(key=lambda x: x["expiry"])
    return "NFO:" + futs[0]["tradingsymbol"]

def get_nearest_option(strike, opt_type):
    opts = [
        i for i in NFO_INSTRUMENTS
        if i["name"] == "NIFTY"
        and i["instrument_type"] == opt_type
        and i["strike"] == strike
    ]
    if not opts:
        return None
    opts.sort(key=lambda x: x["expiry"])
    return "NFO:" + opts[0]["tradingsymbol"]

FUT_SYMBOL = get_nearest_nifty_fut()
print("✅ FUT:", FUT_SYMBOL)

# =========================================================
# VIX
# =========================================================

def get_vix():
    q = kite.quote(["NSE:INDIA VIX"])
    d = q["NSE:INDIA VIX"]
    vix_prev = float(d["ohlc"]["close"]) if d.get("ohlc") else None
    vix = float(d["last_price"]) if d.get("last_price") else None
    return vix_prev, vix

# =========================================================
# ATR BUILDER
# =========================================================

class FutAtrBuilder:
    def __init__(self, period):
        self.period = period
        self.trs = []
        self.last_min = None
        self.h = self.l = self.c = self.prev_c = None
        self.atr = None

    def update(self, now, ltp):
        if ltp is None or math.isnan(ltp):
            return self.atr

        mk = now.replace(second=0, microsecond=0)

        if self.last_min is None:
            self.last_min = mk
            self.h = self.l = self.c = self.prev_c = ltp
            return self.atr

        if mk == self.last_min:
            self.h = max(self.h, ltp)
            self.l = min(self.l, ltp)
            self.c = ltp
            return self.atr

        tr = max(
            self.h - self.l,
            abs(self.h - self.prev_c),
            abs(self.l - self.prev_c),
        )
        self.trs.append(tr)

        if len(self.trs) >= self.period:
            self.atr = round(sum(self.trs[-self.period:]) / self.period, 2)

        self.prev_c = self.c
        self.last_min = mk
        self.h = self.l = self.c = ltp
        return self.atr

# =========================================================
# DB (AUTO MIGRATING)
# =========================================================

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def ensure_table():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS niftylong_strangle (
            timestamp TIMESTAMPTZ
        );
        """)

        cur.execute("""
        ALTER TABLE niftylong_strangle
        ADD COLUMN IF NOT EXISTS status TEXT,
        ADD COLUMN IF NOT EXISTS event TEXT,
        ADD COLUMN IF NOT EXISTS reason TEXT,
        ADD COLUMN IF NOT EXISTS spot DOUBLE PRECISION,

        ADD COLUMN IF NOT EXISTS ce_symbol TEXT,
        ADD COLUMN IF NOT EXISTS pe_symbol TEXT,
        ADD COLUMN IF NOT EXISTS ce_strike INTEGER,
        ADD COLUMN IF NOT EXISTS pe_strike INTEGER,

        ADD COLUMN IF NOT EXISTS ce_entry DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS pe_entry DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS ce_ltp DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS pe_ltp DOUBLE PRECISION,

        ADD COLUMN IF NOT EXISTS unreal_pnl DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS realized_pnl DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS atr DOUBLE PRECISION,

        ADD COLUMN IF NOT EXISTS vix_prev DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS vix DOUBLE PRECISION;
        """)
        conn.commit()

def log_db(**row):
    cols = ",".join(row.keys())
    vals = tuple(row.values())
    ph = ",".join(["%s"] * len(vals))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"INSERT INTO niftylong_strangle ({cols}) VALUES ({ph})", vals)
        conn.commit()

# =========================================================
# STATE
# =========================================================

position_open = False
ce_symbol = pe_symbol = None
ce_strike = pe_strike = None
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

def in_market_hours(now):
    t = now.time()
    return MARKET_START <= t <= MARKET_END

# =========================================================
# MAIN LOOP
# =========================================================

ensure_table()
print("🚀 Bot started")

while True:
    try:
        now = datetime.now(MARKET_TZ)

        if not in_market_hours(now):
            time.sleep(5)
            continue

        spot = kite.ltp([INDEX_SYMBOL])[INDEX_SYMBOL]["last_price"]
        fut  = kite.ltp([FUT_SYMBOL])[FUT_SYMBOL]["last_price"]
        atr  = atr_builder.update(now, fut)

        vix_prev, vix = get_vix()

        # ================= ENTRY =================
        if not position_open and is_near_strike(spot):
            atm = round_to_strike(spot)

            ce_strike = pe_strike = atm
            ce_symbol = get_nearest_option(ce_strike, "CE")
            pe_symbol = get_nearest_option(pe_strike, "PE")

            if not ce_symbol or not pe_symbol:
                time.sleep(1)
                continue

            ce_entry = kite.ltp([ce_symbol])[ce_symbol]["last_price"]
            pe_entry = kite.ltp([pe_symbol])[pe_symbol]["last_price"]
            position_open = True

            log_db(
                timestamp=now,
                status="OPEN",
                event="ENTRY",
                reason="ATM",
                spot=spot,
                ce_symbol=ce_symbol,
                pe_symbol=pe_symbol,
                ce_strike=ce_strike,
                pe_strike=pe_strike,
                ce_entry=ce_entry,
                pe_entry=pe_entry,
                ce_ltp=ce_entry,
                pe_ltp=pe_entry,
                unreal_pnl=0.0,
                realized_pnl=realized_pnl,
                atr=atr,
                vix_prev=vix_prev,
                vix=vix
            )

        # ================= MONITOR =================
        if position_open:
            ce_ltp = kite.ltp([ce_symbol])[ce_symbol]["last_price"]
            pe_ltp = kite.ltp([pe_symbol])[pe_symbol]["last_price"]
            unreal = (ce_ltp - ce_entry + pe_ltp - pe_entry) * QTY_PER_LEG

            if unreal >= PROFIT_TARGET or unreal <= -STOP_LOSS:
                realized_pnl += unreal
                position_open = False

                log_db(
                    timestamp=now,
                    status="EXIT",
                    event="EXIT",
                    reason="TARGET" if unreal >= PROFIT_TARGET else "SL",
                    spot=spot,
                    ce_symbol=ce_symbol,
                    pe_symbol=pe_symbol,
                    ce_strike=ce_strike,
                    pe_strike=pe_strike,
                    ce_entry=ce_entry,
                    pe_entry=pe_entry,
                    ce_ltp=ce_ltp,
                    pe_ltp=pe_ltp,
                    unreal_pnl=unreal,
                    realized_pnl=realized_pnl,
                    atr=atr,
                    vix_prev=vix_prev,
                    vix=vix
                )

            elif time.time() - last_snapshot_ts >= SNAPSHOT_INTERVAL:
                last_snapshot_ts = time.time()

                log_db(
                    timestamp=now,
                    status="RUNNING",
                    event="SNAPSHOT",
                    reason="",
                    spot=spot,
                    ce_symbol=ce_symbol,
                    pe_symbol=pe_symbol,
                    ce_strike=ce_strike,
                    pe_strike=pe_strike,
                    ce_entry=ce_entry,
                    pe_entry=pe_entry,
                    ce_ltp=ce_ltp,
                    pe_ltp=pe_ltp,
                    unreal_pnl=unreal,
                    realized_pnl=realized_pnl,
                    atr=atr,
                    vix_prev=vix_prev,
                    vix=vix
                )

        time.sleep(TICK_INTERVAL)

    except Exception as e:
        print("❌ ERROR:", e)
        time.sleep(5)
