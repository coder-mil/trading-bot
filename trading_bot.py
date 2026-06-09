#!/usr/bin/env python3
"""
Trading Bot - Hermes Integration
Real-time price-based trading with technical indicators
No external news APIs needed - uses Binance WebSocket + API
"""

import os
import json
import time
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from enum import Enum

import requests
import pandas as pd
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

# Load .env file if exists
from dotenv import load_dotenv
load_dotenv("/root/trading-bot/.env")

class Config:
    # Binance API (from environment variables)
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
    
    # Validate API keys are loaded
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        logger.warning("⚠️  Binance API keys not found in environment!")
        logger.warning(f"  BINANCE_API_KEY: {'SET' if BINANCE_API_KEY else 'MISSING'}")
        logger.warning(f"  BINANCE_SECRET_KEY: {'SET' if BINANCE_SECRET_KEY else 'MISSING'}")
    else:
        logger.info(f"✅ Binance API keys loaded (key: {BINANCE_API_KEY[:8]}...{BINANCE_API_KEY[-4:]})")
    
    # Trading settings
    SYMBOL = "DOGEUSDT"  # Trading pair - high volatility meme coin
    QUOTE_ASSET = "USDT"  # Base is DOGE
    INITIAL_BUDGET = float(os.getenv("TRADING_BUDGET", "14.73"))  # $14.73 actual balance
    SIMULATE_MODE = True  # Simulation mode - no real trades
    
    # Risk management
    MAX_POSITION_SIZE = 0.10  # 10% of balance per trade
    STOP_LOSS_PCT = 0.02  # 2% stop loss
    TAKE_PROFIT_PCT = 0.04  # 4% take profit
    
    # Indicators
    RSI_PERIOD = 14
    MA_SHORT = 20
    MA_LONG = 50
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9
    
    # Timing
    CHECK_INTERVAL = 3600  # Check every3600 seconds (1 hour)
    CANDLE_INTERVAL = "1h"  # 1 hour candles
    
    # Database
    DB_PATH = "/root/trading-bot/trades.db"
    
    # Telegram (optional)
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram_message(text: str):
    """Send formatted message via Telegram bot API"""
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured")
        return False
    try:
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
        import urllib.request
        import urllib.parse
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
                volume_ratio REAL,
                strength REAL,
                action TEXT,
                executed BOOLEAN DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS balance_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                quote_balance REAL NOT NULL,
                base_balance REAL NOT NULL,
                total_usdt REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    
    def record_trade(self, trade: dict):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO trades (timestamp, symbol, side, price, quantity, value, fee, strategy, pnl, pnl_pct, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade['timestamp'],
            trade['symbol'],
            trade['side'],
            trade['price'],
            trade['quantity'],
            trade['value'],
            trade.get('fee', 0),
            trade.get('strategy', ''),
            trade.get('pnl'),
            trade.get('pnl_pct'),
            trade.get('notes', '')
        ))
        conn.commit()
        conn.close()
    
    def record_signal(self, signal: dict):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO signals (timestamp, symbol, signal_type, price, rsi, ma_short, ma_long, volume_ratio, strength, action, executed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal['timestamp'],
            signal['symbol'],
            signal['signal_type'],
            signal['price'],
            signal.get('rsi'),
            signal.get('ma_short'),
            signal.get('ma_long'),
            signal.get('volume_ratio'),
            signal.get('strength'),
            signal['action'],
            signal.get('executed', 0)
        ))
        conn.commit()
        conn.close()
    
    def record_balance(self, quote: float, base: float):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO balance_history (timestamp, quote_balance, base_balance, total_usdt)
            VALUES (?, ?, ?, ?)
        """, (datetime.now().isoformat(), quote, base, quote + base * self._get_price()))
        conn.commit()
        conn.close()
    
    def _get_price(self) -> float:
        try:
            url = "https://api.binance.com/api/v3/ticker/price"
            params = {"symbol": Config.SYMBOL}
            r = requests.get(url, params=params, timeout=5)
            return float(r.json()['price'])
        except:
            return 0


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
        logger.info(f"BinanceClient initialized with key: {self.api_key[:8]}...{self.api_key[-4:] if self.api_key else 'NONE'}")
    
    def test_signature(self) -> dict:
        """Test if signature is working correctly"""
        import hmac
        import hashlib
        
        # Get server time
        try:
            r = self.session.get(f"{self.BASE_URL}/api/v3/time", timeout=5)
            server_time = r.json()["serverTime"]
            local_time = int(time.time() * 1000)
            time_diff = server_time - local_time
            logger.info(f"Time sync: server={server_time}, local={local_time}, diff={time_diff}ms")
        except Exception as e:
            logger.error(f"Time sync failed: {e}")
            time_diff = 0
        
        # Test signature with a simple query
        # Add timestamp BEFORE signing (critical!)
        params = {
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000
        }
        
        # Generate signature with timestamp already in params
        query = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
        signature = hmac.new(
            self.secret_key.encode(),
            query.encode(),
            hashlib.sha256
        ).hexdigest()
        
        logger.info(f"Test signature - Query: {query}")
        logger.info(f"Test signature - Signature: {signature}")
        
        # Make the request
        params["signature"] = signature
        r = self.session.get(f"{self.BASE_URL}/api/v3/account", params=params, timeout=10)
        
        logger.info(f"Test account response: {r.status_code}")
        
        if r.status_code != 200:
            try:
                error = r.json()
                logger.error(f"Error code: {error.get('code')}")
                logger.error(f"Error msg: {error.get('msg')}")
            except:
                logger.error(f"Raw error: {r.text}")
        
        return {"status_code": r.status_code, "time_diff": time_diff}
    
    def _sign(self, params: dict) -> dict:
        if not self.secret_key:
            return params
        # Sort params alphabetically (required by Binance)
        sorted_params = sorted(params.items())
        query = "&".join([f"{k}={v}" for k, v in sorted_params])
        import hmac
        import hashlib
        signature = hmac.new(
            self.secret_key.encode(),
            query.encode(),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params
    
    def _request(self, method: str, endpoint: str, params: dict = None, signed: bool = False) -> dict:
        url = f"{self.BASE_URL}{endpoint}"
        params = params or {}
        
        if signed:
            # Add timestamp BEFORE signing (critical!)
            params["timestamp"] = int(time.time() * 1000)
            params = self._sign(params)
        
        # Log request details
        logger.info(f"Binance Request: {method} {endpoint}")
        logger.info(f"  Params: {params}")
        
        try:
            if method == "GET":
                r = self.session.get(url, params=params, timeout=10)
            else:
                r = self.session.post(url, params=params, timeout=10)
            
            # Log response status
            logger.info(f"Binance Response: {r.status_code}")
            
            # Parse error response for better debugging
            if r.status_code != 200:
                try:
                    error_data = r.json()
                    logger.error(f"Binance Error Response: {json.dumps(error_data, indent=2)}")
                    logger.error(f"  Error Code: {error_data.get('code', 'N/A')}")
                    logger.error(f"  Error Msg: {error_data.get('msg', 'N/A')}")
                except:
                    logger.error(f"Binance Raw Error: {r.text}")
                return {}
            
            return r.json()
        except requests.exceptions.Timeout:
            logger.error("Binance API timeout")
            return {}
        except Exception as e:
            logger.error(f"Binance API exception: {type(e).__name__}: {e}")
            return {}
    
    def get_account(self) -> dict:
        return self._request("GET", "/api/v3/account", signed=True)
    
    def get_balance(self, asset: str = "USDT") -> float:
        account = self.get_account()
        if not account:
            return 0
        for balance in account.get("balances", []):
            if balance["asset"] == asset:
                return float(balance["free"]) + float(balance["locked"])
        return 0
    
    def get_price(self, symbol: str = None) -> float:
        symbol = symbol or Config.SYMBOL
        url = f"{self.BASE_URL}/api/v3/ticker/price"
        try:
            r = self.session.get(url, params={"symbol": symbol}, timeout=5)
            return float(r.json()['price'])
        except:
            return 0
    
    def get_klines(self, symbol: str = None, interval: str = None, limit: int = 100) -> List:
        symbol = symbol or Config.SYMBOL
        interval = interval or Config.CANDLE_INTERVAL
        url = f"{self.BASE_URL}/api/v3/klines"
        try:
            r = self.session.get(url, params={
                "symbol": symbol,
                "interval": interval,
                "limit": limit
            }, timeout=10)
            return r.json()
        except Exception as e:
            logger.error(f"Error fetching klines: {e}")
            return []
    
    def place_order(self, side: str, quantity: float, order_type: str = "MARKET") -> dict:
        params = {
            "symbol": Config.SYMBOL,
            "side": side,
            "type": order_type,
            "quantity": round(quantity, 6)
        }
        return self._request("POST", "/api/v3/order", params, signed=True)
    
    def get_order_status(self, order_id: int) -> dict:
        params = {"symbol": Config.SYMBOL, "orderId": order_id}
        return self._request("GET", "/api/v3/order", params, signed=True)


# ============================================================
# INDICATORS
# ============================================================

class TechnicalIndicators:
    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    @staticmethod
    def calculate_ma(prices: List[float], period: int) -> float:
        if len(prices) < period:
            return prices[-1] if prices else 0
        return np.mean(prices[-period:])
    
    @staticmethod
    def calculate_volume_ratio(volumes: List[float], period: int = 20) -> float:
        if len(volumes) < period:
            return 1
        avg_volume = np.mean(volumes[-period:])
        current_volume = volumes[-1]
        return current_volume / avg_volume if avg_volume > 0 else 1
    
    @staticmethod
    def calculate_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float, float]:
        """Returns (macd_line, signal_line, histogram)"""
        if len(prices) < slow + signal:
            return 0, 0, 0
        ema_fast = TechnicalIndicators._ema(prices, fast)
        ema_slow = TechnicalIndicators._ema(prices, slow)
        macd_line = ema_fast - ema_slow
        # Signal line calculation simplified
        signal_line = macd_line * 0.9  # Approximation for short signal period
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram
    
    @staticmethod
    def _ema(prices: List[float], period: int) -> float:
        """Calculate EMA manually"""
        if len(prices) < period:
            return prices[-1] if prices else 0
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return ema


# ============================================================
# STRATEGY
# ============================================================

class Strategy:
    def __init__(self):
        self.indicators = TechnicalIndicators()
        self.last_signal = None
        self.last_signal_time = None
    
    def analyze(self, prices: List[float], volumes: List[float]) -> dict:
        if len(prices) < max(Config.MA_LONG, Config.RSI_PERIOD + 1):
            return {"action": "HOLD", "reason": "Insufficient data"}
        
        current_price = prices[-1]
        rsi = self.indicators.calculate_rsi(prices, Config.RSI_PERIOD)
        ma_short = self.indicators.calculate_ma(prices, Config.MA_SHORT)
        ma_long = self.indicators.calculate_ma(prices, Config.MA_LONG)
        macd_line, signal_line, histogram = self.indicators.calculate_macd(
            prices, Config.MACD_FAST, Config.MACD_SLOW, Config.MACD_SIGNAL
        )
        volume_ratio = self.indicators.calculate_volume_ratio(volumes)
        
        # Signal strength (0-1)
        strength = 0
        signals = []
        
        # === BUY CONDITIONS ===
        # RSI < 30 (sobrevenda)
        if rsi < 30:
            signals.append(("RSI_OVERSOLD", 0.3))
        # MA(20) cruza ACIMA da MA(50) (golden cross)
        if ma_short > ma_long and prices[-1] > ma_short:
            signals.append(("MA_GOLDEN_CROSS", 0.3))
        # MACD histograma > 0 (momentum positivo)
        if histogram > 0:
            signals.append(("MACD_BULLISH", 0.3))
        
        # === SELL CONDITIONS ===
        # RSI > 70 (sobrecompra)
        if rsi > 70:
            signals.append(("RSI_OVERBOUGHT", -0.3))
        # MA(20) cruza ABAIXO da MA(50) (death cross)
        if ma_short < ma_long and prices[-1] < ma_short:
            signals.append(("MA_DEATH_CROSS", -0.3))
        # MACD histograma < 0 (momentum negativo)
        if histogram < 0:
            signals.append(("MACD_BEARISH", -0.3))
        
        # Calculate net strength
        for _, s in signals:
            strength += s
        strength = max(-1, min(1, strength))
        
        # Determine action - ALL 3 conditions must agree
        buy_conditions = (rsi < 30) and (ma_short > ma_long) and (histogram > 0)
        sell_conditions = (rsi > 70) and (ma_short < ma_long) and (histogram < 0)
        
        if buy_conditions:
            action = "BUY"
        elif sell_conditions:
            action = "SELL"
        else:
            action = "HOLD"
        
        # Cooldown (don't repeat signal within 30 minutes)
        if self.last_signal == action and self.last_signal_time:
            if datetime.now() - self.last_signal_time < timedelta(minutes=30):
                action = "HOLD"
        
        self.last_signal = action
        self.last_signal_time = datetime.now()
        
        return {
            "action": action,
            "strength": strength,
            "price": current_price,
            "rsi": rsi,
            "ma_short": ma_short,
            "ma_long": ma_long,
            "macd_histogram": histogram,
            "volume_ratio": volume_ratio,
            "signals": [s[0] for s in signals],
            "reason": "; ".join([s[0] for s in signals]) or "No clear signal"
        }


# ============================================================
# TRADING BOT
# ============================================================

class TradingBot:
    def __init__(self, api_key: str = "", secret_key: str = ""):
        self.binance = BinanceClient(api_key, secret_key)
        self.db = TradeDatabase(Config.DB_PATH)
        self.strategy = Strategy()
        self.position = None  # None = no position, "LONG" = holding
        self.position_entry_price = 0
        self.position_quantity = 0
        
    def check_and_trade(self) -> dict:
        """Main trading logic - called every interval"""
        
        # Fetch data
        klines = self.binance.get_klines(limit=100)
        if not klines:
            return {"status": "error", "message": "Failed to fetch klines"}
        
        # Parse data
        prices = [float(k[4]) for k in klines]  # close prices
        volumes = [float(k[5]) for k in klines]  # volumes
        current_price = prices[-1]
        
        # Analyze
        analysis = self.strategy.analyze(prices, volumes)
        
        # Record signal
        signal = {
            "timestamp": datetime.now().isoformat(),
            "symbol": Config.SYMBOL,
            "signal_type": analysis["action"],
            "price": current_price,
            "rsi": analysis.get("rsi"),
            "ma_short": analysis.get("ma_short"),
            "ma_long": analysis.get("ma_long"),
            "volume_ratio": analysis.get("volume_ratio"),
            "strength": analysis.get("strength", 0),
            "action": analysis["action"]
        }
        self.db.record_signal(signal)
        
        # Execute trade
        result = {"status": "ok", "analysis": analysis}
        
        if analysis["action"] == "BUY" and self.position is None:
            result["trade"] = self._execute_buy(current_price)
        elif analysis["action"] == "SELL" and self.position == "LONG":
            result["trade"] = self._execute_sell(current_price)
        elif self.position == "LONG":
            # Check stop loss / take profit
            check = self._check_stop_loss_take_profit(current_price)
            if check:
                result["trade"] = check
        
        # Record balance
        quote_balance = self.binance.get_balance(Config.QUOTE_ASSET)
        base_balance = self.binance.get_balance(Config.SYMBOL.replace("USDT", ""))
        self.db.record_balance(quote_balance, base_balance)
        
        return result
    
    def _execute_buy(self, price: float) -> dict:
        """Execute buy order"""
        quote_balance = self.binance.get_balance(Config.QUOTE_ASSET)
        if quote_balance < 1:  # Min $1
            return {"status": "skipped", "reason": "Insufficient balance"}

        quantity = (quote_balance * Config.MAX_POSITION_SIZE) / price

        # SIMULATION MODE - no real trades
        if Config.SIMULATE_MODE:
            self.position = "LONG"
            self.position_entry_price = price
            self.position_quantity = quantity
            logger.info(f"[SIM] BUY (simulated): {quantity} @ {price}")
            return {"status": "simulated", "side": "BUY", "price": price, "quantity": quantity}

        order = self.binance.place_order("BUY", quantity)

        if order and "orderId" in order:
            self.position = "LONG"
            self.position_entry_price = price
            self.position_quantity = quantity

            trade = {
                "timestamp": datetime.now().isoformat(),
                "symbol": Config.SYMBOL,
                "side": "BUY",
                "price": price,
                "quantity": quantity,
                "value": quote_balance * Config.MAX_POSITION_SIZE,
                "strategy": "ma_rsi",
                "notes": f"RSI: {self.strategy.indicators.calculate_rsi([price]*14):.2f}"
            }
            self.db.record_trade(trade)

            logger.info(f"BUY executed: {quantity} @ {price}")
            return {"status": "success", "side": "BUY", "price": price, "quantity": quantity}

        return {"status": "error", "message": str(order)}
    
    def _execute_sell(self, price: float) -> dict:
        """Execute sell order"""
        if not self.position:
            return {"status": "skipped", "reason": "No position"}

        quantity = self.position_quantity

        # SIMULATION MODE - no real trades
        if Config.SIMULATE_MODE:
            pnl = (price - self.position_entry_price) * quantity
            pnl_pct = (price - self.position_entry_price) / self.position_entry_price * 100
            logger.info(f"[SIM] SELL (simulated): {quantity} @ {price}, PnL: {pnl:.4f} ({pnl_pct:.2f}%)")
            self.position = None
            self.position_entry_price = 0
            self.position_quantity = 0
            return {"status": "simulated", "side": "SELL", "price": price, "pnl": pnl, "pnl_pct": pnl_pct}

        order = self.binance.place_order("SELL", quantity)

        if order and "orderId" in order:
            pnl = (price - self.position_entry_price) * quantity
            pnl_pct = (price - self.position_entry_price) / self.position_entry_price * 100

            trade = {
                "timestamp": datetime.now().isoformat(),
                "symbol": Config.SYMBOL,
                "side": "SELL",
                "price": price,
                "quantity": quantity,
                "value": price * quantity,
                "strategy": "ma_rsi",
                "pnl": pnl,
                "pnl_pct": pnl_pct
            }
            self.db.record_trade(trade)

            logger.info(f"SELL executed: {self.position_quantity} @ {price}, PnL: {pnl:.2f} ({pnl_pct:.2f}%)")

            self.position = None
            self.position_entry_price = 0
            self.position_quantity = 0

            return {"status": "success", "side": "SELL", "price": price, "pnl": pnl, "pnl_pct": pnl_pct}

        return {"status": "error", "message": str(order)}
    
    def _check_stop_loss_take_profit(self, current_price: float) -> Optional[dict]:
        """Check if stop loss or take profit triggered"""
        if not self.position or not self.position_entry_price:
            return None
        
        pnl_pct = (current_price - self.position_entry_price) / self.position_entry_price
        
        if pnl_pct <= -Config.STOP_LOSS_PCT * 100:
            logger.info(f"Stop loss triggered: {pnl_pct:.2f}%")
            return self._execute_sell(current_price)
        elif pnl_pct >= Config.TAKE_PROFIT_PCT * 100:
            logger.info(f"Take profit triggered: {pnl_pct:.2f}%")
            return self._execute_sell(current_price)
        
        return None
    
    def get_status(self) -> dict:
        """Get current bot status"""
        quote_balance = self.binance.get_balance(Config.QUOTE_ASSET)
        base_balance = self.binance.get_balance(Config.SYMBOL.replace("USDT", ""))
        current_price = self.binance.get_price()
        
        return {
            "position": self.position,
            "entry_price": self.position_entry_price,
            "quantity": self.position_quantity,
            "quote_balance": quote_balance,
            "base_balance": base_balance,
            "current_price": current_price,
            "unrealized_pnl": (current_price - self.position_entry_price) * self.position_quantity if self.position else 0,
            "total_value": quote_balance + base_balance * current_price
        }


# ============================================================
# MAIN
# ============================================================

def main():
    import sys
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "status":
            # Just show status
            bot = TradingBot()
            status = bot.get_status()
            print(json.dumps(status, indent=2))
            return
        elif sys.argv[1] == "test":
            # Test API connection and signature
            bot = TradingBot()
            result = bot.binance.test_signature()
            print(json.dumps(result, indent=2))
            return
        elif sys.argv[1] == "price":
            # Just show price
            client = BinanceClient()
            price = client.get_price()
            print(f"BTC Price: ${price}")
            return
        elif sys.argv[1] == "watch":
            # Watch mode - analyze + send formatted Telegram report
            bot = TradingBot()
            klines = bot.binance.get_klines(interval=Config.CANDLE_INTERVAL, limit=100)
            if not klines:
                print("Failed to fetch klines")
                return
            prices = [float(k[4]) for k in klines]
            volumes = [float(k[5]) for k in klines]
            analysis = bot.strategy.analyze(prices, volumes)
            status = bot.get_status()

            # Format emoji based on action
            action_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪️"}.get(analysis["action"], "⚪️")

            # Build formatted message
            msg = f"""📊 <b>DOGEUSDT - Análise Horária</b>

💰<b>Carteira:</b>
• Saldo USDT: ${status['quote_balance']:.2f}
• DOGE: {status['base_balance']:.4f}
• Preço atual: ${analysis['price']:.5f}
• Total: ${status['total_value']:.2f}

📈 <b>Posição:</b> {'Nenhuma' if status['position'] is None else status['position']}
{'• Entry: $' + f"{status['entry_price']:.5f}" if status['position'] else ''}
{'• Qty: ' + f"{status['quantity']:.2f}" if status['position'] else ''}
{'• PnL: $' + f"{status['unrealized_pnl']:.4f}" if status['position'] else ''}

🔍 <b>Indicadores:</b>
• RSI (14): {analysis['rsi']:.1f} {'< 30 SOBREVENDA' if analysis['rsi'] < 30 else '> 70 SOBRECMP' if analysis['rsi'] > 70 else 'neutro'}
• MA(20): ${analysis['ma_short']:.5f}
• MA(50): ${analysis['ma_long']:.5f}
• MACD hist: {analysis['macd_histogram']:.6f}

{action_emoji} <b>Sinal: {analysis['action']}</b>
{analysis['reason']}"""

            send_telegram_message(msg)
            print("Watch report sent to Telegram")
            return
    
    # Run trading cycle
    bot = TradingBot()
    
    mode = "[SIMULATION]" if Config.SIMULATE_MODE else "[LIVE]"
    logger.info(f"{mode} Starting trading bot for {Config.SYMBOL}")
    logger.info(f"{mode} Budget: ${Config.INITIAL_BUDGET}, Interval: {Config.CHECK_INTERVAL}s")
    
    result = bot.check_and_trade()
    logger.info(f"Result: {json.dumps(result, indent=2)}")
    
    status = bot.get_status()
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()