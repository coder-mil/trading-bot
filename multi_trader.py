#!/usr/bin/env python3
"""
Multi-Coin Trading Bot
Analyzes and trades multiple low-price, high-volatility assets simultaneously.
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
    
    # Risk management
    MAX_POSITION_SIZE = 0.10  # 10% per coin
    STOP_LOSS_PCT = 0.02      # -2%
    TAKE_PROFIT_PCT = 0.04    # +4%
    
    # Indicators
    RSI_PERIOD = 14
    MA_SHORT = 20
    MA_LONG = 50
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9
    
    # Timing
    CANDLE_INTERVAL = "1h"
    COOLDOWN_MINUTES = 30
    
    # Database
    DB_PATH = "/root/trading-bot/trades.db"
    
    # Telegram
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
        c.execute("SELECT symbol, side, entry_price, quantity, created_at FROM positions WHERE symbol = ?", (symbol,))
        row = c.fetchone()
        conn.close()
        if row:
            return {"symbol": row[0], "side": row[1], "entry_price": row[2], "quantity": row[3], "created_at": row[4]}
        return None

    def upsert_position(self, position: Dict) -> None:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO positions (symbol, side, entry_price, quantity, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                side = excluded.side,
                entry_price = excluded.entry_price,
                quantity = excluded.quantity,
                created_at = excluded.created_at
        """, (position['symbol'], position['side'], position['entry_price'], position['quantity'], position['created_at']))
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
            INSERT INTO trades (timestamp, symbol, side, price, quantity, value, fee, strategy, pnl, pnl_pct, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade['timestamp'], trade['symbol'], trade['side'],
            trade['price'], trade['quantity'], trade['value'],
            trade.get('fee', 0), trade.get('strategy', ''),
            trade.get('pnl'), trade.get('pnl_pct'), trade.get('notes', '')
        ))
        conn.commit()
        conn.close()

    def record_signal(self, signal: dict) -> None:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO signals (timestamp, symbol, signal_type, price, rsi, ma_short, ma_long, volume_ratio, strength, action, executed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal['timestamp'], signal['symbol'], signal['signal_type'],
            signal['price'], signal.get('rsi'), signal.get('ma_short'),
            signal.get('ma_long'), signal.get('volume_ratio'),
            signal.get('strength', 0), signal['action'],
            signal.get('executed', 0)
        ))
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
        """Get total USDT balance. Falls back to env var if API fails."""
        # Try env var first (set manually or updated by trades)
        env_balance = os.getenv("TRADING_BUDGET", "")
        if env_balance:
            return float(env_balance)
        # Try API
        client = BinanceClient()
        balance = client.get_balance("USDT")
        if balance > 0:
            return balance
        # Fallback: return 0 (user checks manually)
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
# INDICATORS
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
    def calculate_ma(prices: List[float], period: int) -> float:
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        return float(np.mean(prices[-period:]))

    @staticmethod
    def calculate_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
        if len(prices) < slow + signal:
            return 0.0, 0.0, 0.0
        ema_fast = TechnicalIndicators._ema(prices, fast)
        ema_slow = TechnicalIndicators._ema(prices, slow)
        macd_line = ema_fast - ema_slow
        signal_line = macd_line * 0.9
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    @staticmethod
    def _ema(prices: List[float], period: int) -> float:
        if len(prices) < period:
            return prices[-1] if prices else 0.0
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

        # ALL 3 conditions must agree
        buy_conditions = (rsi < 30) and (ma_short > ma_long) and (histogram > 0)
        sell_conditions = (rsi > 70) and (ma_short < ma_long) and (histogram < 0)

        if buy_conditions:
            action = "BUY"
        elif sell_conditions:
            action = "SELL"
        else:
            action = "HOLD"

        return {
            "action": action,
            "price": current_price,
            "rsi": rsi,
            "ma_short": ma_short,
            "ma_long": ma_long,
            "macd_histogram": histogram,
            "reason": self._build_reason(rsi, ma_short, ma_long, histogram)
        }

    def _build_reason(self, rsi: float, ma_short: float, ma_long: float, histogram: float) -> str:
        parts = []
        if rsi < 30:
            parts.append(f"RSI {rsi:.1f} < 30 (sobrevenda)")
        elif rsi > 70:
            parts.append(f"RSI {rsi:.1f} > 70 (sobrecompra)")
        else:
            parts.append(f"RSI {rsi:.1f}")
        if ma_short > ma_long:
            parts.append("MA(20) above MA(50) ☝️")
        elif ma_short < ma_long:
            parts.append("MA(20) below MA(50) 👇")
        if histogram > 0:
            parts.append("MACD hist +▲")
        elif histogram < 0:
            parts.append("MACD hist -▼")
        return " | ".join(parts)


# ============================================================
# PER-COIN TRADER
# ============================================================

class CoinTrader:
    """Manages analysis and trading for a single coin."""

    def __init__(self, symbol: str, db: TradeDatabase):
        self.symbol = symbol
        self.db = db
        self.binance = BinanceClient()
        self.strategy = Strategy()

    def analyze(self) -> dict:
        klines = self.binance.get_klines(self.symbol, interval=Config.CANDLE_INTERVAL, limit=100)
        if not klines:
            return {"action": "HOLD", "reason": "API error", "rsi": 50, "price": 0}
        prices = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        return self.strategy.analyze(prices, volumes)

    def get_position(self) -> Optional[Dict]:
        return self.db.get_position(self.symbol)

    def execute_buy(self, price: float, quote_balance: float) -> dict:
        position_size = quote_balance * Config.MAX_POSITION_SIZE
        quantity = position_size / price

        if Config.SIMULATE_MODE:
            self.db.upsert_position({
                "symbol": self.symbol,
                "side": "LONG",
                "entry_price": price,
                "quantity": quantity,
                "created_at": datetime.now().isoformat()
            })
            logger.info(f"[SIM] BUY {self.symbol}: {quantity} @ {price}")
            return {"status": "simulated", "side": "BUY", "price": price, "quantity": quantity}

        order = self.binance.place_order("BUY", quantity, self.symbol)
        if order and "orderId" in order:
            self.db.upsert_position({
                "symbol": self.symbol,
                "side": "LONG",
                "entry_price": price,
                "quantity": quantity,
                "created_at": datetime.now().isoformat()
            })
            return {"status": "success", "side": "BUY", "price": price, "quantity": quantity}
        return {"status": "error"}

    def execute_sell(self, price: float) -> dict:
        position = self.get_position()
        if not position:
            return {"status": "skipped", "reason": "No position"}

        quantity = position["quantity"]
        entry_price = position["entry_price"]
        pnl = (price - entry_price) * quantity
        pnl_pct = (price - entry_price) / entry_price * 100

        if Config.SIMULATE_MODE:
            self.db.remove_position(self.symbol)
            logger.info(f"[SIM] SELL {self.symbol}: {quantity} @ {price}, PnL: {pnl:.4f} ({pnl_pct:.2f}%)")
            return {"status": "simulated", "side": "SELL", "price": price, "pnl": pnl, "pnl_pct": pnl_pct}

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
                "pnl_pct": pnl_pct
            })
            return {"status": "success", "side": "SELL", "price": price, "pnl": pnl, "pnl_pct": pnl_pct}
        return {"status": "error"}

    def check_sl_tp(self, price: float) -> Optional[dict]:
        position = self.get_position()
        if not position or position["side"] != "LONG":
            return None
        entry_price = position["entry_price"]
        pnl_pct = (price - entry_price) / entry_price
        if pnl_pct <= -Config.STOP_LOSS_PCT:
            logger.info(f"{self.symbol}: Stop loss triggered ({pnl_pct:.2f}%)")
            return self.execute_sell(price)
        if pnl_pct >= Config.TAKE_PROFIT_PCT:
            logger.info(f"{self.symbol}: Take profit triggered ({pnl_pct:.2f}%)")
            return self.execute_sell(price)
        return None


# ============================================================
# MULTI-TRADER ORCHESTRATOR
# ============================================================

class MultiTrader:
    def __init__(self):
        self.db = TradeDatabase(Config.DB_PATH)
        self.coins = self.db.get_enabled_coins()
        self.traders = {symbol: CoinTrader(symbol, self.db) for symbol in self.coins}
        self.coin_results = {}  # symbol -> result dict

    def analyze_all(self) -> Dict[str, dict]:
        results = {}
        for symbol in self.coins:
            trader = self.traders[symbol]
            analysis = trader.analyze()
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

            # Check SL/TP if position open
            sl_tp_result = None
            if position and position["side"] == "LONG":
                sl_tp_result = trader.check_sl_tp(current_price)

            # Record signal
            self.db.record_signal({
                "timestamp": datetime.now().isoformat(),
                "symbol": symbol,
                "signal_type": analysis["action"],
                "price": current_price,
                "rsi": analysis.get("rsi"),
                "ma_short": analysis.get("ma_short"),
                "ma_long": analysis.get("ma_long"),
                "volume_ratio": None,
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

            trade_result = None
            if action == "BUY" and position is None:
                trade_result = trader.execute_buy(price, quote_balance)
            elif action == "SELL" and position and position["side"] == "LONG":
                trade_result = trader.execute_sell(price)

            if trade_result:
                results[symbol] = trade_result
                # Refresh position after trade
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
                open_positions.append({
                    "symbol": symbol,
                    "entry_price": position["entry_price"],
                    "current_price": current_price,
                    "quantity": position["quantity"],
                    "value": coin_value,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct
                })

        return {
            "total_balance": total_balance,
            "total_value": total_value,
            "open_positions": open_positions,
            "open_count": len(open_positions),
            "total_coins": len(self.coins)
        }


# ============================================================
# TELEGRAM REPORT BUILDER
# ============================================================

def build_report(multi_results: Dict[str, dict], portfolio: dict, trades: Dict[str, dict]) -> str:
    lines = []
    lines.append("📊 <b>Multi-Coin Watch</b>")
    lines.append(f"🕐 {datetime.now().strftime('%H:%M')} — {datetime.now().strftime('%d/%m')}")
    lines.append("")

    for symbol in sorted(multi_results.keys()):
        result = multi_results[symbol]
        analysis = result["analysis"]
        position = result["position"]
        action = analysis.get("action", "HOLD")
        rsi = analysis.get("rsi", 50)
        price = analysis.get("price", 0)
        reason = analysis.get("reason", "")

        # Emoji
        if action == "BUY":
            emoji = "🟢"
        elif action == "SELL":
            emoji = "🔴"
        else:
            emoji = "⚪️"

        # Position info
        pos_info = ""
        if position and position["side"] == "LONG":
            pnl_pct = (price - position["entry_price"]) / position["entry_price"] * 100
            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
            pos_info = f" | {pnl_emoji}{pnl_pct:+.1f}%"

        lines.append(f"{emoji} <b>{symbol}</b> | RSI: {rsi:.0f} | {action}{pos_info}")
        if reason:
            lines.append(f"   {reason}")

        # Trade result
        if symbol in trades:
            t = trades[symbol]
            side = t.get("side", "")
            pnl = t.get("pnl", 0)
            if side == "BUY":
                lines.append(f"   ✅ {side} executado (sim)")
            elif side == "SELL":
                lines.append(f"   ✅ {side} executado (sim) | PnL: {pnl:+.4f}")

        lines.append("")

    # Portfolio summary
    lines.append("─" * 22)
    lines.append(f"💰 Saldo: ${portfolio['total_balance']:.2f}")
    lines.append(f"📦 Posições: {portfolio['open_count']}/{portfolio['total_coins']}")

    if portfolio['open_positions']:
        for p in portfolio['open_positions']:
            lines.append(f"  • {p['symbol']}: LONG @ ${p['entry_price']:.5f} | {p['pnl_pct']:+.1f}%")

    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def main():
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        # Watch mode: analyze all coins, send Telegram, no trades
        logger.info("=== MULTI-COIN WATCH ===")
        mt = MultiTrader()
        results = mt.analyze_all()
        portfolio = mt.get_portfolio_status()
        report = build_report(results, portfolio, {})
        send_telegram_message(report)
        print("✅ Watch report sent to Telegram")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        mt = MultiTrader()
        portfolio = mt.get_portfolio_status()
        print(json.dumps(portfolio, indent=2, default=str))
        return

    # Full trading cycle
    logger.info("=== MULTI-COIN TRADING CYCLE ===")
    mt = MultiTrader()
    results = mt.analyze_all()
    portfolio = mt.get_portfolio_status()
    trades = mt.execute_all(portfolio["total_balance"])
    report = build_report(results, portfolio, trades)
    send_telegram_message(report)
    print("✅ Trading cycle complete")
    print(json.dumps({"portfolio": portfolio, "trades": trades}, indent=2, default=str))


if __name__ == "__main__":
    main()
