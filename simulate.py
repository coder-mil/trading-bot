#!/usr/bin/env python3
"""
Simulacao de 24 horas - 1 ciclo por hora
Sem ordens reais, usa dados historicos da Binance
"""

import sys
import json
import time
from datetime import datetime, timedelta

sys.path.insert(0, '/root/trading-bot')

from trading_bot import BinanceClient, TechnicalIndicators, Config

def run_simulation(hours=24):
    print(f"\n{'='*60}")
    print(f"  SIMULACAO DE {hours}H - {Config.SYMBOL}")
    print(f"{'='*60}\n")

    client = BinanceClient()
    indicators = TechnicalIndicators()

    # Estado simulado
    quote_balance = 14.73664425  # Saldo atual real
    base_balance = 0.0
    position = None
    entry_price = 0
    quantity = 0

    # Historico de precos (ultimas 100 velas de 1h)
    klines = client.get_klines(interval='1h', limit=100)
    if not klines:
        print("Falha ao obter dados historicos")
        return

    prices = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    print(f"Saldo inicial: ${quote_balance:.2f} USDT")
    print(f"Preco atual: ${prices[-1]:.2f}")
    print(f"Simulando {hours} ciclos (1h cada)...\n")

    trades = []
    wins = 0
    losses = 0

    for hour in range(hours):
        # Simula das velas mais recentes pra mais antigas (24h =24 velas de 1h)
        # prices[-1] = mais recente, prices[-24] = 24h aträs
        idx = len(prices) - 1 - hour
        if idx < 0:
            break
        current_price = prices[idx]

        # Calcula indicadores com dados ate esse ponto (sem dados futuros)
        hist_prices = prices[:idx+1]
        hist_volumes = volumes[:idx+1]

        rsi = indicators.calculate_rsi(hist_prices, Config.RSI_PERIOD)
        ma_short = indicators.calculate_ma(hist_prices, Config.MA_SHORT)
        ma_long = indicators.calculate_ma(hist_prices, Config.MA_LONG)
        macd_line, signal_line, histogram = indicators.calculate_macd(
            hist_prices, Config.MACD_FAST, Config.MACD_SLOW, Config.MACD_SIGNAL
        )

        # Logica de sinais
        buy_conditions = (rsi < 30) and (ma_short > ma_long) and (histogram > 0)
        sell_conditions = (rsi > 70) and (ma_short < ma_long) and (histogram < 0)

        action = "HOLD"
        signal_reason = ""

        if buy_conditions and position is None:
            action = "BUY"
            signal_reason = f"RSI={rsi:.1f} < 30, MA20>{ma_long:.0f}, MACD>+0"
        elif sell_conditions and position == "LONG":
            action = "SELL"
            signal_reason = f"RSI={rsi:.1f} > 70, MA20<{ma_long:.0f}, MACD<0"
        elif position == "LONG":
            # Verifica stop loss / take profit
            pnl_pct = (current_price - entry_price) / entry_price * 100
            if pnl_pct <= -2:
                action = "SELL (SL)"
                signal_reason = f"Stop Loss triggered: {pnl_pct:.2f}%"
            elif pnl_pct >= 4:
                action = "SELL (TP)"
                signal_reason = f"Take Profit triggered: {pnl_pct:.2f}%"

        # Executa simulacao
        if action == "BUY" and position is None:
            invest_amount = quote_balance * Config.MAX_POSITION_SIZE
            quantity = invest_amount / current_price
            entry_price = current_price
            position = "LONG"
            quote_balance -= invest_amount
            trades.append({
                "hour": hour,
                "side": "BUY",
                "price": current_price,
                "quantity": quantity,
                "invested": invest_amount
            })
            print(f"[{hour:02d}h] BUY @ ${current_price:.2f} | Qty: {quantity:.6f} | ${invest_amount:.2f} | {signal_reason}")

        elif action in ["SELL", "SELL (SL)", "SELL (TP)"] and position == "LONG":
            proceeds = quantity * current_price
            pnl = proceeds - trades[-1]["invested"]
            pnl_pct = pnl / trades[-1]["invested"] * 100

            quote_balance += proceeds

            emoji = "WIN" if pnl > 0 else "LOSS"
            if pnl > 0:
                wins += 1
            else:
                losses += 1

            trades.append({
                "hour": hour,
                "side": "SELL",
                "price": current_price,
                "quantity": quantity,
                "proceeds": proceeds,
                "pnl": pnl,
                "pnl_pct": pnl_pct
            })

            print(f"[{hour:02d}h] {emoji} SELL @ ${current_price:.2f} | PnL: ${pnl:.4f} ({pnl_pct:+.2f}%) | {signal_reason}")

            position = None
            quantity = 0
            entry_price = 0

        time.sleep(0.01)

    # Resultado final
    print(f"\n{'='*60}")
    print(f"  RESULTADO DA SIMULACAO")
    print(f"{'='*60}")
    print(f"\nSaldo final: ${quote_balance:.2f} USDT")
    print(f"Total de trades: {len(trades)}")
    print(f"Wins: {wins} | Losses: {losses}")

    if trades:
        total_invested = sum(t.get("invested", t.get("proceeds", 0)) for t in trades if t["side"] == "BUY")
        total_pnl = quote_balance - 14.73664425
        print(f"\nPnL liquido: ${total_pnl:+.4f}")
        print(f"Retorno: {total_pnl/14.73664425*100:+.2f}%")

    print(f"\n{'='*60}\n")

    return {
        "final_balance": quote_balance,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "pnl": total_pnl if trades else 0,
        "return_pct": (total_pnl/14.73664425*100) if trades else 0
    }

if __name__ == "__main__":
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    run_simulation(hours)
