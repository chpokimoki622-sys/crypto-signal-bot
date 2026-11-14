"""
Ядро бота: загрузка данных с Binance, индикаторы и правила сигналов.
Одни и те же правила используют и bot.py (живые сигналы), и backtest.py
(проверка на истории) — так бэктест честно показывает, как работал бы бот.
"""
import time

import requests

import config

# Сначала адрес Binance только для рыночных данных (ключи не нужны),
# потом обычные адреса API как запасные.
BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api-gcp.binance.com",
]

INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000,
}


class BinanceError(Exception):
    pass


# ------------------------------------------------------------
#  Загрузка данных
# ------------------------------------------------------------
def _get(path, params):
    """GET-запрос к Binance с перебором запасных адресов."""
    errors = []
    for base in BASE_URLS:
        try:
            r = requests.get(base + path, params=params, timeout=15)
        except requests.RequestException as e:
            errors.append(f"{base}: {e.__class__.__name__}")
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 451:
            errors.append(f"{base}: 451 (Binance закрыт для этого региона/сервера)")
        elif r.status_code == 429:
            errors.append(f"{base}: 429 (слишком много запросов)")
            time.sleep(2)
        else:
            errors.append(f"{base}: HTTP {r.status_code} {r.text[:120]}")
    raise BinanceError("; ".join(errors))


def _parse_kline(k):
    return {
        "open_time": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "close_time": int(k[6]),
        "taker_buy": float(k[9]),  # объём рыночных ПОКУПОК
    }


def get_klines(symbol, interval=config.INTERVAL, limit=300):
    """Последние свечи. Незакрытую (текущую) свечу отбрасываем."""
    raw = _get("/api/v3/klines",
               {"symbol": symbol, "interval": interval, "limit": limit})
    candles = [_parse_kline(k) for k in raw]
    now_ms = int(time.time() * 1000)
    return [c for c in candles if c["close_time"] < now_ms]


def get_history(symbol, interval=config.INTERVAL, days=config.BACKTEST_DAYS):
    """История за N дней (Binance отдаёт максимум 1000 свечей за запрос)."""
    step = INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000)
    start = now_ms - days * 86_400_000
    candles = []
    while start < now_ms:
        raw = _get("/api/v3/klines", {"symbol": symbol, "interval": interval,
                                      "startTime": start, "limit": 1000})
        if not raw:
            break
        candles.extend(_parse_kline(k) for k in raw)
        start = int(raw[-1][0]) + step
        time.sleep(0.2)  # не спамим биржу
    return [c for c in candles if c["close_time"] < now_ms]


def order_book_balance(symbol, depth_pct=config.BOOK_DEPTH_PCT):
    """Доля заявок на покупку в стакане в пределах ±depth_pct% от цены."""
    book = _get("/api/v3/depth", {"symbol": symbol, "limit": 5000})
    bids = [(float(p), float(q)) for p, q in book["bids"]]
    asks = [(float(p), float(q)) for p, q in book["asks"]]
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    lo, hi = mid * (1 - depth_pct / 100), mid * (1 + depth_pct / 100)
    bid_vol = sum(p * q for p, q in bids if p >= lo)
    ask_vol = sum(p * q for p, q in asks if p <= hi)
    total = bid_vol + ask_vol
    return bid_vol / total if total else None


# ------------------------------------------------------------
#  Индикаторы
# ------------------------------------------------------------
def ema(values, period):
    """Экспоненциальная скользящая средняя. Первые period-1 значений = None."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes, period=config.RSI_PERIOD):
    """RSI по Уайлдеру (как на TradingView)."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period

    def calc(g, l):
        if l == 0:
            return 100.0
        return 100 - 100 / (1 + g / l)

    out[period] = calc(avg_g, avg_l)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
        out[i] = calc(avg_g, avg_l)
    return out


def buy_share(candles, window=config.FLOW_WINDOW):
    """Доля рыночных покупок в объёме за последние window свечей."""
    out = [None] * len(candles)
    for i in range(window - 1, len(candles)):
        part = candles[i - window + 1:i + 1]
        vol = sum(c["volume"] for c in part)
        buy = sum(c["taker_buy"] for c in part)
        out[i] = buy / vol if vol else None
    return out


def analyze(candles):
    closes = [c["close"] for c in candles]
    return {
        "close": closes,
        "ema_fast": ema(closes, config.EMA_FAST),
        "ema_slow": ema(closes, config.EMA_SLOW),
        "rsi": rsi(closes),
        "buy_share": buy_share(candles),
    }


# ------------------------------------------------------------
#  Правила сигналов
# ------------------------------------------------------------
def _crossed_up(a_prev, a_now, b_prev, b_now):
    return a_prev <= b_prev and a_now > b_now


def _crossed_down(a_prev, a_now, b_prev, b_now):
    return a_prev >= b_prev and a_now < b_now


def signals_at(ind, i):
    """
    Какие события произошли на свече i. Возвращает список (код, текст).
    BUY/SELL — торговые сигналы (их проверяет бэктест),
    RSI_LOW/RSI_HIGH — предупреждения для информации.
    """
    if i < 1:
        return []
    f0, f1 = ind["ema_fast"][i - 1], ind["ema_fast"][i]
    s0, s1 = ind["ema_slow"][i - 1], ind["ema_slow"][i]
    r0, r1 = ind["rsi"][i - 1], ind["rsi"][i]
    flow = ind["buy_share"][i]
    events = []

    if None not in (f0, f1, s0, s1):
        if _crossed_up(f0, f1, s0, s1) and flow is not None \
                and flow >= config.FLOW_MIN_BUY:
            events.append(("BUY", "🟢 Сигнал на покупку: тренд развернулся вверх "
                                  "(EMA пересеклись), покупатели активнее продавцов"))
        elif _crossed_down(f0, f1, s0, s1):
            events.append(("SELL", "🔴 Сигнал на выход: тренд развернулся вниз "
                                   "(EMA пересеклись вниз)"))

    if r0 is not None and r1 is not None:
        if r0 >= config.RSI_LOW > r1:
            events.append(("RSI_LOW", f"⚠️ RSI упал ниже {config.RSI_LOW}: цена сильно "
                                      "просела. Бывает отскок, но падение может продолжиться"))
        elif r0 <= config.RSI_HIGH < r1:
            events.append(("RSI_HIGH", f"⚠️ RSI выше {config.RSI_HIGH}: цена сильно выросла, "
                                       "растёт риск отката"))
    return events
