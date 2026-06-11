#!/usr/bin/env python3
"""
Multi-Coin Trading Bot v2
Improved: EMA(20/50), ATR-based TP/SL, real MACD, volume filter, BTC trend filter
Simulation mode: no real Binance orders.
"""

import os
import json
import time
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from dataclasses import dataclass

import requests
import numpy as np

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/root/trading-bot/trading.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

from dotenv import load_dotenv
load_dotenv("/root/trading-bot/.env")

class Config:
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")

    INITIAL_BUDGET = float(os.getenv("TRADING_BUDGET", "14.74"))
    SIMULATE_MODE = True  # Simulation — no real trades
    MAX_POSITION_SIZE = 0.10  # 10% per coin

    # --- Risk management (ATR-based) ---
    ATR_PERIOD = 14
    ATR_TP_MULT = 2.0    # Take Profit: entry + 2 × ATR
    ATR_SL_MULT = 1.0    # Stop Loss: entry - 1 × ATR
    ATR_TRAIL_MULT = 1.0 # Trailing stop activation: 2 × ATR above entry
    ATR_TRAIL_OFFSET = 1.0  # Trail 1 × ATR below highest price

    # --- Indicators ---
    RSI_PERIOD = 14
    MA_SHORT = 20   # EMA period
    MA_LONG = 50    # EMA period
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9

    # --- Filters ---
    VOLUME_THRESHOLD = 1.2  # Volume must be > 1.2× average 20 to confirm signal
    BTC_TREND_THRESHOLD = 0.03  # Ignore BUY if BTC dropped >3% in last 4h

    # --- Timing ---
    CANDLE_INTERVAL = "1h"
    COOLDOWN_MINUTES = 30

    # --- Database ---
    DB_PATH = "/root/trading-bot/trades.db"

    # --- Telegram ---
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_message(text: str) -> bool:
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured")
        return False
    try:
        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
        # Escape HTML entities in text
        text = (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
        data = urllib.parse.urlencode({
            "chat_id": Config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


# ============================================================
# DATABASE
# ============================================================

class TradeDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS coins (
                symbol TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                position_size REAL DEFAULT 0.10,
                last_signal TEXT,
                last_signal_time TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                side TEXT,
                entry_price REAL,
                quantity REAL,
                atr REAL,
                highest_price REAL,
                trailing_activated INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                value REAL NOT NULL,
                fee REAL,
                strategy TEXT,
                pnl REAL,
                pnl_pct REAL,
                notes TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                price REAL NOT NULL,
                rsi REAL,
                ma_short REAL,
                ma_long REAL,
                macd_histogram REAL,
                atr REAL,
                volume_ratio REAL,
                btc_change REAL,
                strength REAL,
                action TEXT,
                executed BOOLEAN DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

    def get_enabled_coins(self) -> List[str]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT symbol FROM coins WHERE enabled = 1 ORDER BY symbol")
        result = [r[0] for r in c.fetchall()]
        conn.close()
        return result

    def get_position(self, symbol: str) -> Optional[Dict]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            SELECT symbol, side, entry_price, quantity, atr, highest_price,
                   trailing_activated, created_at
            FROM positions WHERE symbol = ?
        """, (symbol,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                "symbol": row[0], "side": row[1], "entry_price": row[2],
                "quantity": row[3], "atr": row[4], "highest_price": row[5],
                "trailing_activated": bool(row[6]), "created_at": row[7]
            }
        return None

    def upsert_position(self, position: Dict) -> None:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO positions (symbol, side, entry_price, quantity, atr,
                                  highest_price, trailing_activated, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                side = excluded.side,
                entry_price = excluded.entry_price,
                quantity = excluded.quantity,
                atr = excluded.atr,
                highest_price = excluded.highest_price,
                trailing_activated = excluded.trailing_activated,
                created_at = excluded.created_at
        """, (position['symbol'], position['side'], position['entry_price'],
              position['quantity'], position.get('atr', 0),
              position.get('highest_price', position['entry_price']),
              1 if position.get('trailing_activated') else 0,
              position['created_at']))
        conn.commit()
        conn.close()

    def remove_position(self, symbol: str) -> None:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        conn.commit()
        conn.close()

    def record_trade(self, trade: dict) -> None:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO trades (timestamp, symbol, side, price, quantity, value,
                               fee, strategy, pnl, pnl_pct, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (trade['timestamp'], trade['symbol'], trade['side'],
              trade['price'], trade['quantity'], trade['value'],
              trade.get('fee', 0), trade.get('strategy', ''),
              trade.get('pnl'), trade.get('pnl_pct'), trade.get('notes', '')))
        conn.commit()
        conn.close()

    def record_signal(self, signal: dict) -> None:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO signals (timestamp, symbol, signal_type, price, rsi,
                               ma_short, ma_long, macd_histogram, atr,
                               volume_ratio, btc_change, strength, action, executed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (signal['timestamp'], signal['symbol'], signal['signal_type'],
              signal['price'], signal.get('rsi'), signal.get('ma_short'),
              signal.get('ma_long'), signal.get('macd_histogram'),
              signal.get('atr'), signal.get('volume_ratio'),
              signal.get('btc_change'), signal.get('strength', 0),
              signal['action'], signal.get('executed', 0)))
        conn.commit()
        conn.close()

    def get_last_signal(self, symbol: str) -> Optional[Dict]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            SELECT signal_type, action, timestamp FROM signals
            WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1
        """, (symbol,))
        row = c.fetchone()
        conn.close()
        if row:
            return {"signal_type": row[0], "action": row[1], "timestamp": row[2]}
        return None

    def get_total_balance(self) -> float:
        env_balance = os.getenv("TRADING_BUDGET", "")
        if env_balance:
            return float(env_balance)
        client = BinanceClient()
        balance = client.get_balance("USDT")
        if balance > 0:
            return balance
        return 0.0


# ============================================================
# BINANCE CLIENT
# ============================================================

class BinanceClient:
    BASE_URL = "https://api.binance.com"

    def __init__(self, api_key: str = "", secret_key: str = ""):
        self.api_key = api_key or Config.BINANCE_API_KEY
        self.secret_key = secret_key or Config.BINANCE_SECRET_KEY
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": self.api_key})

    def get_price(self, symbol: str) -> float:
        try:
            r = self.session.get(
                f"{self.BASE_URL}/api/v3/ticker/price",
                params={"symbol": symbol}, timeout=5
            )
            return float(r.json()['price'])
        except:
            return 0.0

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> List:
        try:
            r = self.session.get(
                f"{self.BASE_URL}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=10
            )
            return r.json()
        except Exception as e:
            logger.error(f"Error fetching klines for {symbol}: {e}")
            return []

    def get_btc_4h_change(self) -> float:
        """Returns BTCUSDT % change over last 4 hours (4 × 1h candles)."""
        try:
            klines = self.get_klines("BTCUSDT", interval="1h", limit=5)
            if len(klines) < 5:
                return 0.0
            # Use close prices: current vs 4 candles ago
            current = float(klines[-1][4])
            past = float(klines[-5][4])
            return (current - past) / past
        except Exception as e:
            logger.warning(f"BTC trend check failed: {e}")
            return 0.0

    def get_balance(self, asset: str = "USDT") -> float:
        import hmac, hashlib
        params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        sorted_params = sorted(params.items())
        query = "&".join([f"{k}={v}" for k, v in sorted_params])
        signature = hmac.new(self.secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        try:
            r = self.session.get(
                f"{self.BASE_URL}/api/v3/account",
                params=params, timeout=10
            )
            if r.status_code != 200:
                return 0.0
            for b in r.json().get("balances", []):
                if b["asset"] == asset:
                    return float(b["free"]) + float(b["locked"])
        except:
            pass
        return 0.0

    def place_order(self, side: str, quantity: float, symbol: str) -> dict:
        import hmac, hashlib
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": round(quantity, 6),
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000
        }
        sorted_params = sorted(params.items())
        query = "&".join([f"{k}={v}" for k, v in sorted_params])
        params["signature"] = hmac.new(self.secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()
        try:
            r = self.session.post(
                f"{self.BASE_URL}/api/v3/order",
                params=params, timeout=10
            )
            return r.json()
        except:
            return {}


# ============================================================
# TECHNICAL INDICATORS (v2)
# ============================================================

class TechnicalIndicators:
    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> float:
        """Exponential Moving Average — more responsive than SMA."""
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        multiplier = 2 / (period + 1)
        ema = float(np.mean(prices[:period]))
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    @staticmethod
    def calculate_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
        """
        Real MACD with EMA signal line.
        Returns: (macd_line, signal_line, histogram)
        """
        if len(prices) < slow + signal:
            return 0.0, 0.0, 0.0
        ema_fast = TechnicalIndicators.calculate_ema(prices, fast)
        ema_slow = TechnicalIndicators.calculate_ema(prices, slow)
        macd_line = ema_fast - ema_slow
        # Real MACD signal line: EMA of MACD line over `signal` periods
        # Build MACD series to compute EMA on it
        macd_series = []
        for i in range(len(prices)):
            ef = TechnicalIndicators.calculate_ema(prices[:i+1], fast) if i >= fast else float(np.mean(prices[:i+1]))
            es = TechnicalIndicators.calculate_ema(prices[:i+1], slow) if i >= slow else float(np.mean(prices[:i+1]))
            macd_series.append(ef - es)
        # EMA of MACD series (need at least `signal` values)
        if len(macd_series) < signal:
            signal_line = macd_line * 0.9
        else:
            # Compute EMA of MACD line
            macd_ema_start = np.mean(macd_series[-signal:])
            mult = 2 / (signal + 1)
            signal_line = macd_ema_start
            for v in macd_series[-signal:]:
                signal_line = (v - signal_line) * mult + signal_line
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    @staticmethod
    def calculate_atr(klines: List, period: int = 14) -> float:
        """
        Average True Range — measures volatility.
        Uses high, low, close from klines (Binance format).
        """
        if len(klines) < period + 1:
            return 0.0
        trs = []
        for i in range(1, len(klines)):
            high = float(klines[i][2])   # high
            low = float(klines[i][3])   # low
            prev_close = float(klines[i-1][4])  # previous close
            tr = max(high - low,
                     abs(high - prev_close),
                     abs(low - prev_close))
            trs.append(tr)
        if len(trs) < period:
            return 0.0
        return float(np.mean(trs[-period:]))

    @staticmethod
    def calculate_volume_ratio(volumes: List[float], period: int = 20) -> float:
        """Volume relative to 20-period average."""
        if len(volumes) < period:
            return 1.0
        avg_volume = float(np.mean(volumes[-period:]))
        current_volume = volumes[-1]
        return current_volume / avg_volume if avg_volume > 0 else 1.0


# ============================================================
# STRATEGY (v2)
# ============================================================

class Strategy:
    def __init__(self):
        self.indicators = TechnicalIndicators()

    def analyze(self, prices: List[float], volumes: List[float],
                klines: List, btc_change: float = 0.0) -> dict:
        """
        btc_change: BTCUSDT % change over last 4h (from BinanceClient).
        """
        min_data = max(Config.MA_LONG, Config.RSI_PERIOD + 1, Config.ATR_PERIOD + 1)
        if len(prices) < min_data:
            return {"action": "HOLD", "reason": "Insufficient data",
                    "rsi": 50, "atr": 0, "volume_ratio": 1, "price": prices[-1] if prices else 0}

        current_price = prices[-1]
        rsi = self.indicators.calculate_rsi(prices, Config.RSI_PERIOD)
        # EMA (not SMA)
        ma_short = self.indicators.calculate_ema(prices, Config.MA_SHORT)
        ma_long = self.indicators.calculate_ema(prices, Config.MA_LONG)
        macd_line, signal_line, histogram = self.indicators.calculate_macd(
            prices, Config.MACD_FAST, Config.MACD_SLOW, Config.MACD_SIGNAL
        )
        atr = self.indicators.calculate_atr(klines, Config.ATR_PERIOD)
        volume_ratio = self.indicators.calculate_volume_ratio(volumes)

        # --- Filters ---
        btc_filter_blocked = (btc_change < -Config.BTC_TREND_THRESHOLD)
        volume_confirmed = (volume_ratio >= Config.VOLUME_THRESHOLD)

        # ALL base conditions must agree
        buy_base = (rsi < 30) and (ma_short > ma_long) and (histogram > 0)
        sell_base = (rsi > 70) and (ma_short < ma_long) and (histogram < 0)

        # Apply BTC trend filter on BUY
        if buy_base and btc_filter_blocked:
            buy_base = False
            reason_btc = f"⛔ BTC -{(abs(btc_change)*100):.1f}% 4h (compra suspensa)"
        else:
            reason_btc = ""

        # Volume confirmation (weaken signal if volume is low — still shows but notes it)
        vol_note = f" | 📊 vol {volume_ratio:.1f}×" if volume_ratio >= Config.VOLUME_THRESHOLD else f" | ⚠️ vol {volume_ratio:.1f}×"

        if buy_base:
            action = "BUY"
        elif sell_base:
            action = "SELL"
        else:
            action = "HOLD"

        reason = self._build_reason(rsi, ma_short, ma_long, histogram,
                                     atr, btc_change, reason_btc, vol_note)

        return {
            "action": action,
            "price": current_price,
            "rsi": rsi,
            "ma_short": ma_short,
            "ma_long": ma_long,
            "macd_histogram": histogram,
            "atr": atr,
            "volume_ratio": volume_ratio,
            "btc_change": btc_change,
            "btc_filter_blocked": btc_filter_blocked,
            "volume_confirmed": volume_confirmed,
            "reason": reason
        }

    def _build_reason(self, rsi: float, ma_short: float, ma_long: float,
                      histogram: float, atr: float, btc_change: float,
                      btc_note: str, vol_note: str) -> str:
        parts = []
        if rsi < 30:
            parts.append(f"RSI {rsi:.1f} < 30 (sobrevenda)")
        elif rsi > 70:
            parts.append(f"RSI {rsi:.1f} > 70 (sobrecompra)")
        else:
            parts.append(f"RSI {rsi:.1f}")
        if ma_short > ma_long:
            parts.append("EMA(20) > EMA(50) ☝️")
        elif ma_short < ma_long:
            parts.append("EMA(20) < EMA(50) 👇")
        if histogram > 0:
            parts.append("MACD hist +▲")
        elif histogram < 0:
            parts.append("MACD hist -▼")
        if atr > 0:
            parts.append(f"ATR ${atr:.4f}")
        parts.append(f"BTC {'+' if btc_change >= 0 else ''}{(btc_change*100):.1f}% 4h")
        if btc_note:
            parts.append(btc_note)
        parts.append(vol_note)
        return " | ".join(parts)


# ============================================================
# PER-COIN TRADER (v2 — ATR trailing stop)
# ============================================================

class CoinTrader:
    def __init__(self, symbol: str, db: TradeDatabase):
        self.symbol = symbol
        self.db = db
        self.binance = BinanceClient()
        self.strategy = Strategy()

    def analyze(self, btc_change: float = 0.0) -> dict:
        klines = self.binance.get_klines(self.symbol, interval=Config.CANDLE_INTERVAL, limit=100)
        if not klines:
            return {"action": "HOLD", "reason": "API error", "rsi": 50, "atr": 0, "price": 0}
        prices = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        return self.strategy.analyze(prices, volumes, klines, btc_change)

    def get_position(self) -> Optional[Dict]:
        return self.db.get_position(self.symbol)

    def execute_buy(self, price: float, quote_balance: float, atr: float) -> dict:
        position_size = quote_balance * Config.MAX_POSITION_SIZE
        quantity = position_size / price

        if Config.SIMULATE_MODE:
            self.db.upsert_position({
                "symbol": self.symbol,
                "side": "LONG",
                "entry_price": price,
                "quantity": quantity,
                "atr": atr,
                "highest_price": price,
                "trailing_activated": False,
                "created_at": datetime.now().isoformat()
            })
            logger.info(f"[SIM] BUY {self.symbol}: {quantity} @ {price} | ATR ${atr:.4f}")
            return {"status": "simulated", "side": "BUY", "price": price,
                    "quantity": quantity, "atr": atr}

        order = self.binance.place_order("BUY", quantity, self.symbol)
        if order and "orderId" in order:
            self.db.upsert_position({
                "symbol": self.symbol,
                "side": "LONG",
                "entry_price": price,
                "quantity": quantity,
                "atr": atr,
                "highest_price": price,
                "trailing_activated": False,
                "created_at": datetime.now().isoformat()
            })
            return {"status": "success", "side": "BUY", "price": price,
                    "quantity": quantity, "atr": atr}
        return {"status": "error"}

    def execute_sell(self, price: float, reason: str = "") -> dict:
        position = self.get_position()
        if not position:
            return {"status": "skipped", "reason": "No position"}

        quantity = position["quantity"]
        entry_price = position["entry_price"]
        pnl = (price - entry_price) * quantity
        pnl_pct = (price - entry_price) / entry_price * 100

        if Config.SIMULATE_MODE:
            self.db.remove_position(self.symbol)
            logger.info(f"[SIM] SELL {self.symbol}: {quantity} @ {price}, "
                        f"PnL: {pnl:.4f} ({pnl_pct:.2f}%) | {reason}")
            self.db.record_trade({
                "timestamp": datetime.now().isoformat(),
                "symbol": self.symbol,
                "side": "SELL",
                "price": price,
                "quantity": quantity,
                "value": price * quantity,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "notes": reason
            })
            return {"status": "simulated", "side": "SELL", "price": price,
                    "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason}

        order = self.binance.place_order("SELL", quantity, self.symbol)
        if order and "orderId" in order:
            self.db.remove_position(self.symbol)
            self.db.record_trade({
                "timestamp": datetime.now().isoformat(),
                "symbol": self.symbol,
                "side": "SELL",
                "price": price,
                "quantity": quantity,
                "value": price * quantity,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "notes": reason
            })
            return {"status": "success", "side": "SELL", "price": price,
                    "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason}
        return {"status": "error"}

    def check_trailing_stop(self, current_price: float) -> Optional[dict]:
        """
        ATR-based trailing stop:
        - Initial SL: entry - ATR_SL_MULT × ATR
        - Activate trailing when price > entry + ATR_TRAIL_MULT × ATR
        - Trail: highest - ATR_TRAIL_OFFSET × ATR
        """
        position = self.get_position()
        if not position or position["side"] != "LONG":
            return None

        entry_price = position["entry_price"]
        atr = position.get("atr", 0)
        highest = position.get("highest_price", entry_price)

        if atr <= 0:
            # Fallback to fixed percentage if no ATR
            pnl_pct = (current_price - entry_price) / entry_price
            if pnl_pct <= -0.02:
                return self.execute_sell(current_price, "SL 2% (no ATR)")
            if pnl_pct >= 0.04:
                return self.execute_sell(current_price, "TP 4% (no ATR)")
            return None

        # Update highest price
        if current_price > highest:
            highest = current_price

        initial_sl = entry_price - Config.ATR_SL_MULT * atr
        activation_price = entry_price + Config.ATR_TRAIL_MULT * atr
        trailing_stop = highest - Config.ATR_TRAIL_OFFSET * atr

        # Check if trailing is activated
        trailing_activated = (current_price >= activation_price)
        effective_stop = trailing_stop if trailing_activated else initial_sl

        # Update highest in DB
        self.db.upsert_position({
            **position,
            "highest_price": highest,
            "trailing_activated": trailing_activated
        })

        # Check stop triggers
        if current_price <= initial_sl:
            return self.execute_sell(current_price, f"SL ${initial_sl:.4f} (ATR)")
        if trailing_activated and current_price <= trailing_stop:
            return self.execute_sell(current_price, f"TRAIL STOP ${trailing_stop:.4f} (ATR)")

        return None


# ============================================================
# MULTI-TRADER ORCHESTRATOR (v2)
# ============================================================

class MultiTrader:
    def __init__(self):
        self.db = TradeDatabase(Config.DB_PATH)
        self.coins = self.db.get_enabled_coins()
        self.traders = {symbol: CoinTrader(symbol, self.db) for symbol in self.coins}
        self.coin_results = {}

        # Fetch BTC trend once for all coins
        btc_client = BinanceClient()
        self.btc_change = btc_client.get_btc_4h_change()

    def analyze_all(self) -> Dict[str, dict]:
        results = {}
        for symbol in self.coins:
            trader = self.traders[symbol]
            analysis = trader.analyze(btc_change=self.btc_change)
            position = trader.get_position()
            current_price = analysis.get("price", 0)

            # Check cooldown
            last_signal = self.db.get_last_signal(symbol)
            cooldown_active = False
            if last_signal and last_signal["action"] == analysis["action"]:
                last_time = datetime.fromisoformat(last_signal["timestamp"])
                if datetime.now() - last_time < timedelta(minutes=Config.COOLDOWN_MINUTES):
                    cooldown_active = True

            if cooldown_active and analysis["action"] in ("BUY", "SELL"):
                analysis["action"] = "HOLD"
                analysis["reason"] += " | ⏳ Cooldown 30min"

            # Check trailing stop if position open
            sl_tp_result = None
            if position and position["side"] == "LONG":
                sl_tp_result = trader.check_trailing_stop(current_price)

            # Record signal
            self.db.record_signal({
                "timestamp": datetime.now().isoformat(),
                "symbol": symbol,
                "signal_type": analysis["action"],
                "price": current_price,
                "rsi": analysis.get("rsi"),
                "ma_short": analysis.get("ma_short"),
                "ma_long": analysis.get("ma_long"),
                "macd_histogram": analysis.get("macd_histogram"),
                "atr": analysis.get("atr"),
                "volume_ratio": analysis.get("volume_ratio"),
                "btc_change": analysis.get("btc_change"),
                "strength": 0,
                "action": analysis["action"]
            })

            results[symbol] = {
                "analysis": analysis,
                "position": position,
                "sl_tp_result": sl_tp_result
            }
            self.coin_results[symbol] = results[symbol]

        return results

    def execute_all(self, quote_balance: float) -> Dict[str, dict]:
        results = {}
        for symbol in self.coins:
            trader = self.traders[symbol]
            result = self.coin_results.get(symbol, {})
            analysis = result.get("analysis", {})
            position = result.get("position")

            action = analysis.get("action")
            price = analysis.get("price", 0)
            atr = analysis.get("atr", 0)

            trade_result = None
            if action == "BUY" and position is None:
                trade_result = trader.execute_buy(price, quote_balance, atr)
            elif action == "SELL" and position and position["side"] == "LONG":
                trade_result = trader.execute_sell(price, "Signal SELL")

            if trade_result:
                results[symbol] = trade_result
                self.coin_results[symbol]["position"] = trader.get_position()

        return results

    def get_portfolio_status(self) -> dict:
        total_balance = self.db.get_total_balance()
        open_positions = []
        total_value = total_balance

        for symbol in self.coins:
            position = self.db.get_position(symbol)
            if position and position["side"] == "LONG":
                current_price = self.traders[symbol].binance.get_price(symbol)
                pnl = (current_price - position["entry_price"]) * position["quantity"]
                pnl_pct = (current_price - position["entry_price"]) / position["entry_price"] * 100
                coin_value = position["quantity"] * current_price
                total_value += coin_value
                atr = position.get("atr", 0)
                tp_price = position["entry_price"] + Config.ATR_TP_MULT * atr
                sl_price = position["entry_price"] - Config.ATR_SL_MULT * atr
                trailing_act = position.get("trailing_activated", False)
                highest = position.get("highest_price", position["entry_price"])
                trail_stop = highest - Config.ATR_TRAIL_OFFSET * atr if trailing_act else 0
                open_positions.append({
                    "symbol": symbol,
                    "entry_price": position["entry_price"],
                    "current_price": current_price,
                    "quantity": position["quantity"],
                    "value": coin_value,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "atr": atr,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "trailing_activated": trailing_act,
                    "highest_price": highest,
                    "trailing_stop": trail_stop
                })

        return {
            "total_balance": total_balance,
            "total_value": total_value,
            "open_positions": open_positions,
            "open_count": len(open_positions),
            "total_coins": len(self.coins),
            "btc_4h_change": self.btc_change
        }


# ============================================================
# TELEGRAM REPORT BUILDER (v3 — só envia se BUY ou SELL)
# ============================================================

def build_report(multi_results: Dict[str, dict], portfolio: dict,
                 trades: Dict[str, dict], btc_change: float) -> Optional[str]:
    """
    Retorna None se não houver nenhuma ação BUY ou SELL.
    Nesse caso, o Telegram não envia nada (silêncio).
    """
    # Coletar ações
    buy_signals = []
    sell_signals = []
    executed_trades = {}

    for symbol in sorted(multi_results.keys()):
        result = multi_results[symbol]
        analysis = result["analysis"]
        position = result["position"]
        action = analysis.get("action", "HOLD")
        price = analysis.get("price", 0)
        rsi = analysis.get("rsi", 50)
        reason = analysis.get("reason", "")

        if action == "BUY":
            buy_signals.append({
                "symbol": symbol, "price": price, "rsi": rsi, "reason": reason
            })
        elif action == "SELL":
            sell_signals.append({
                "symbol": symbol, "price": price, "rsi": rsi, "reason": reason
            })

        # Trade executado
        if symbol in trades:
            executed_trades[symbol] = trades[symbol]

    # Se não tem nada, silêncio
    if not buy_signals and not sell_signals and not executed_trades:
        return None

    # --- Montar relatório ---
    lines = []
    now = datetime.now()
    btc_emoji = "🟢" if btc_change >= 0 else "🔴"

    # Header
    lines.append(f"📊 <b>Multi-Coin Watch</b>  {now.strftime('%d/%m %H:%M')}")
    lines.append(f"{btc_emoji} BTC {'+' if btc_change >= 0 else ''}{(btc_change*100):.2f}% (4h)")

    # BUY signals
    if buy_signals:
        lines.append("")
        lines.append("🟢 <b>COMPRAS</b>")
        for s in buy_signals:
            atr_val = s.get("atr", 0)
            lines.append(f"  • {s['symbol']} @ ${s['price']:.5f}")
            lines.append(f"    RSI {s['rsi']:.0f} | {s['reason']}")

    # SELL signals
    if sell_signals:
        lines.append("")
        lines.append("🔴 <b>VENDAS</b>")
        for s in sell_signals:
            lines.append(f"  • {s['symbol']} @ ${s['price']:.5f}")
            lines.append(f"    RSI {s['rsi']:.0f} | {s['reason']}")

    # Executed trades
    if executed_trades:
        lines.append("")
        lines.append("⚡ <b>TRADES EXECUTADOS</b>")
        for symbol, t in executed_trades.items():
            side = t.get("side", "")
            pnl = t.get("pnl", 0)
            pnl_pct = t.get("pnl_pct", 0)
            reason_t = t.get("reason", "")
            emoji = "🟢" if side == "BUY" else "🔴"
            pnl_str = f"{pnl_pct:+.2f}%" if pnl != 0 else ""
            lines.append(f"  {emoji} {side} {symbol} {pnl_str}")
            if reason_t:
                lines.append(f"    {reason_t}")

    # Portfolio summary (só se tiver posições abertas)
    if portfolio.get("open_positions"):
        lines.append("")
        lines.append("💼 <b>Posições Abertas</b>")
        for p in portfolio["open_positions"]:
            pnl_emoji = "🟢" if p["pnl_pct"] >= 0 else "🔴"
            trail = " ⬆️" if p.get("trailing_activated") else ""
            lines.append(
                f"  {pnl_emoji} {p['symbol']}: {p['pnl_pct']:+.1f}% "
                f"(@ ${p['entry_price']:.5f}){trail}"
            )

    lines.append("")
    lines.append(f"💰 ${portfolio['total_balance']:.2f} | 📦 {portfolio['open_count']}/{portfolio['total_coins']}")

    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def main():
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        logger.info("=== MULTI-COIN WATCH v3 ===")
        mt = MultiTrader()
        results = mt.analyze_all()
        portfolio = mt.get_portfolio_status()
        report = build_report(results, portfolio, {}, mt.btc_change)
        if report:
            send_telegram_message(report)
            print("✅ Watch report sent to Telegram")
        else:
            print("🤐 All HOLD — no message sent (silence mode)")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        mt = MultiTrader()
        portfolio = mt.get_portfolio_status()
        print(json.dumps(portfolio, indent=2, default=str))
        return

    # Full trading cycle
    logger.info("=== MULTI-COIN TRADING CYCLE v3 ===")
    mt = MultiTrader()
    results = mt.analyze_all()
    portfolio = mt.get_portfolio_status()
    trades = mt.execute_all(portfolio["total_balance"])
    report = build_report(results, portfolio, trades, mt.btc_change)
    if report:
        send_telegram_message(report)
        print("✅ Report sent to Telegram")
    else:
        print("🤐 All HOLD — no message sent (silence mode)")
    print(json.dumps({"portfolio": portfolio, "trades": trades}, indent=2, default=str))


if __name__ == "__main__":
    main()