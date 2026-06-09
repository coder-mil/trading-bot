# Trading Bot — Hermes Cron v2

Bot de trading multi-coin em modo **WATCH** (simulação, sem ordens reais) rodando a cada 1h via Hermes cron.

## Estratégia v2

**COMPRA:** `RSI < 30` + `EMA(20) > EMA(50)` + `MACD hist > 0` + filtro BTC + confirmação volume
**VENDA:** `RSI > 70` + `EMA(20) < EMA(50)` + `MACD hist < 0`

### Melhorias v2

- **EMA(20/50)** — mais responsiva que SMA
- **ATR(14) dinâmico** — SL/TP baseado em volatilidade real do ativo
- **Trailing stop** — ativa quando preço sobe 2× ATR acima do entry; trail 1× ATR abaixo do highest
- **Filtro BTC** — bloqueia compra se BTC cair >3% nas últimas 4h (proteção contra risco sistêmico)
- **Volume** — sinal confirmado apenas se volume > 1.2× média 20 períodos
- **MACD real** — EMA exponencial do MACD line (não aproximação)

## Gestão de Risco

| Parâmetro | Valor |
|-----------|-------|
| Position size | 10% do saldo por coin |
| Stop Loss | Entry − 1× ATR(14) |
| Take Profit | Entry + 2× ATR(14) |
| Trailing stop | Ativa: price ≥ entry + 2× ATR → trail highest − 1× ATR |
| Trailing offset | 1× ATR abaixo do highest |
| BTC trend filter | Ignora BUY se BTC −3%+ em 4h |
| Volume threshold | 1.5× média 20 períodos |

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
Binance API (candles 1h) × 9 coins
     ↓
BTC 4h change (proteção sistêmica)
     ↓
Análise: RSI + EMA + MACD real + ATR + Volume
     ↓
Trailing stop check (por posição aberta)
     ↓
Telegram (relatório consolidado)
```

## Setup

```bash
# 1. Instalar dependências
pip install -r requirements.txt

# 2. Criar .env (copiar do exemplo)
cp .env.example .env
# Preencher:
#   BINANCE_API_KEY
#   BINANCE_SECRET_KEY
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#   TRADING_BUDGET

# 3. Testar watch
python3 multi_trader.py watch

# 4. Ver status
python3 multi_trader.py status
```

## Cron Hermes

```bash
hermes cron create \
  --name "Multi-Coin Watch v2" \
  --prompt "Execute: cd /root/trading-bot && python3 multi_trader.py watch" \
  --schedule "every 1h" \
  --deliver origin
```

## Banco de Dados

- `trades.db` — histórico de trades, sinais e posições
- Tabelas: `trades`, `signals`, `positions`, `coins`, `balance_history`
- Coins ativas configuradas na tabela `coins`
- Posições guardam: entry_price, atr, highest_price, trailing_activated

## Estrutura de Arquivos

```
multi_trader.py  ← orchestrator multi-coin v2 (usa este)
trading_bot.py   ← versão single-coin DOGE (legado)
simulate.py      ← testes de simulação
requirements.txt
.env.example
.gitignore
README.md
```

## ⚠️ Aviso

**Modo WATCH — sem ordens reais.** Este bot analisa e registra sinais apenas. Ordens reais desativadas por padrão (`SIMULATE_MODE = True`).

## Changelog

### v2 (2026-06-09)
- EMA(20/50) substitui SMA
- ATR(14)-based TP/SL com trailing stop
- Filtro de volume (1.2× avg 20)
- Filtro BTC 4h (bloqueia compra se BTC −3%+)
- MACD real com EMA signal line
- Escape HTML no Telegram

### v1 (2026-06-05)
- SMA(20/50) + RSI + MACD simplificado
- TP/SL fixo 4%/2%
- 5 cryptos (DOGE, XRP, ADA, SHIB, PEPE)