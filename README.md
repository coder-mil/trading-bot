# Trading Bot — Hermes Cron

Bot de trading multi-coin em modo **WATCH** (simulação, sem ordens reais) rodando a cada 1h via Hermes cron.

## Estratégia

**COMPRA:** `RSI < 30` + `MA20 > MA50` + `MACD hist > 0` (sobrevenda → rebound)  
**VENDA:** `RSI > 70` + `MA20 < MA50` + `MACD hist < 0` (sobrecompra)

## Gestão de Risco

| Parâmetro | Valor |
|-----------|-------|
| Position size | 10% do saldo por coin |
| Stop Loss | -2% |
| Take Profit | +4% |
| Taxas (estimadas) | 0.10% cada lado |

## Cryptos Monitoradas (9 coins)

| Coin | Symbol |
|------|--------|
| Cardano | ADAUSDT |
| Binance Coin | BNBUSDT |
| Dogecoin | DOGEUSDT |
| Ethereum | ETHUSDT |
| Hyperliquid | HYPERUSDT |
| Pepe | PEPEUSDT |
| Shiba Inu | SHIBUSDT |
| Solana | SOLUSDT |
| XRP | XRPUSDT |

## Arquitetura

```
Hermes cron (1h)
     ↓
multi_trader.py watch
     ↓
Binance API (candles 1h)
     ↓
Análise técnica (RSI, MA, MACD) × 9 coins
     ↓
Telegram (relatório consolidado)
```

- **multi_trader.py** — orchestrator multi-coin (usa este)
- **trading_bot.py** — versão single-coin DOGE (legado)
- **simulate.py** — testes de simulação

## Setup

```bash
# 1. Instalar dependências
pip install -r requirements.txt

# 2. Criar .env
cp .env.example .env
# Preencher BINANCE_API_KEY, BINANCE_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 3. Testar watch
python3 multi_trader.py watch

# 4. Ver status
python3 multi_trader.py status
```

## Cron Hermes

```bash
hermes cron create \
  --name "DOGE Multi Watch" \
  --prompt "Execute: cd /root/trading-bot && python3 multi_trader.py watch" \
  --schedule "every 1h" \
  --deliver origin
```

## Banco de Dados

- `trades.db` — histórico de trades, sinais e posições
- Tabelas: `trades`, `signals`, `positions`, `coins`, `balance_history`
- Coins ativas configuradas na tabela `coins`

## ⚠️ Aviso

**Modo WATCH — sem ordens reais.** Este bot analisa e registra sinais apenas. Ordens reais desativadas por padrão (`SIMULATE_MODE = True`).