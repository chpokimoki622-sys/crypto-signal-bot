"""
Исследование стратегий с честной проверкой на отложенных данных.

Протокол (задан заранее, до просмотра результатов):
  * Свечи 1h с Binance за YEARS лет (4h и 1d собираются из часовых).
  * Обучение = всё, кроме последнего года. Проверка = последний год.
  * Сигнал на закрытии свечи, сделка по открытию СЛЕДУЮЩЕЙ свечи.
  * Издержки: 0.10% комиссия + 0.05% проскальзывание на каждую сделку.
  * Лучший вариант выбирается по МЕДИАНЕ доходности на обучении
    по всем монетам и оценивается только на проверочном годе.

Примеры:
  python tools/research.py --universe small          # малые монеты
  python tools/research.py --symbols BTCUSDT,ETHUSDT
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import core  # noqa: E402

COST = 0.0015
DAY_MS = 86_400_000
STABLES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "AEUR",
    "USDE", "USD1", "BFUSD", "RLUSD", "PYUSD", "XUSD", "USDS", "PAXG", "XAUT",
    "WBTC", "WBETH", "BETH", "BNSOL", "GBP", "TRY", "BRL",
}
OUT = []


def say(line=""):
    print(line)
    OUT.append(line)


# ------------------------------------------------------------------
#  Выбор монет
# ------------------------------------------------------------------
def pick_small(skip_top, max_rank, years, limit):
    info = core._get("/api/v3/exchangeInfo", {})
    base = {s["symbol"]: s["baseAsset"] for s in info["symbols"]
            if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"}
    tick = core._get("/api/v3/ticker/24hr", {})
    ranked = sorted(
        ((float(t["quoteVolume"]), t["symbol"]) for t in tick
         if t["symbol"] in base and base[t["symbol"]] not in STABLES
         and not base[t["symbol"]].endswith(("UP", "DOWN", "BULL", "BEAR"))),
        reverse=True)
    ranked = [s for _, s in ranked]
    start = int(time.time() * 1000) - years * 365 * DAY_MS
    chosen = []
    for rank, sym in enumerate(ranked[skip_top:max_rank], start=skip_top + 1):
        k = core._get("/api/v3/klines", {"symbol": sym, "interval": "1d",
                                         "startTime": start, "limit": 1})
        if k and int(k[0][0]) <= start + 7 * DAY_MS:
            chosen.append((rank, sym))
        if len(chosen) >= limit:
            break
        time.sleep(0.1)
    return chosen


def load_binance(sym, years):
    c = core.get_history(sym, interval="1h", days=years * 365)
    df = pd.DataFrame(c)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("time")[["open", "high", "low", "close", "volume", "taker_buy"]]


def load_csv(path):
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("time").drop(columns="open_time")


# ------------------------------------------------------------------
#  Индикаторы и варианты стратегий
# ------------------------------------------------------------------
def resample(df, rule):
    if rule == "1h":
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last",
           "volume": "sum", "taker_buy": "sum"}
    return df.resample(rule, label="left", closed="left").agg(agg).dropna()


def ema(s, n):
    out = s.ewm(span=n, adjust=False).mean()
    out.iloc[: n - 1] = np.nan
    return out


def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    r = 100 - 100 / (1 + g / l)
    r.iloc[:n] = np.nan
    return r


def flow(df, n=6):
    return df["taker_buy"].rolling(n).sum() / df["volume"].rolling(n).sum()


def ema_cross(df, fast=20, slow=50, flow_min=0.52, trend=None):
    f, s = ema(df.close, fast), ema(df.close, slow)
    up = (f.shift() <= s.shift()) & (f > s)
    dn = (f.shift() >= s.shift()) & (f < s)
    entry = up
    if flow_min is not None:
        entry &= flow(df) >= flow_min
    if trend:
        entry &= df.close > ema(df.close, trend)
    return entry, dn


def ma_trend(df, n=200):
    m = ema(df.close, n)
    return ((df.close > m) & (df.close.shift() <= m.shift()),
            (df.close < m) & (df.close.shift() >= m.shift()))


def rsi_dip(df, low=30, high=55, trend=200):
    r = rsi(df.close)
    return ((r < low) & (r.shift() >= low) & (df.close > ema(df.close, trend)),
            r > high)


def donchian(df, up=20, down=10):
    return (df.close > df.high.rolling(up).max().shift(),
            df.close < df.low.rolling(down).min().shift())


VARIANTS = {
    "1h EMA20/50+поток (текущая)": ("1h", lambda d: ema_cross(d)),
    "4h EMA20/50+поток": ("4h", lambda d: ema_cross(d)),
    "4h EMA20/50+поток+EMA200": ("4h", lambda d: ema_cross(d, trend=200)),
    "4h EMA20/50+EMA200": ("4h", lambda d: ema_cross(d, flow_min=None, trend=200)),
    "1d цена vs EMA200": ("1D", lambda d: ma_trend(d, 200)),
    "4h откат RSI в тренде": ("4h", lambda d: rsi_dip(d)),
    "1d пробой 20/10": ("1D", lambda d: donchian(d)),
}


def positions(entry, exit_):
    pos = np.zeros(len(entry))
    holding = 0
    e, x = entry.fillna(False).values, exit_.fillna(False).values
    for i in range(len(e)):
        if holding == 0 and e[i]:
            holding = 1
        elif holding == 1 and x[i]:
            holding = 0
        pos[i] = holding
    return pd.Series(pos, index=entry.index).shift().fillna(0)


def evaluate(df, pos, start, end):
    oo = df.open.shift(-1) / df.open - 1
    m = (df.index >= start) & (df.index < end)
    p, r = pos[m], oo[m].fillna(0)
    changes = p.diff().abs().fillna(p.iloc[0])
    strat = p * r - changes * COST
    eq = (1 + strat).cumprod()
    return {"ret": eq.iloc[-1] - 1,
            "hold": df.open[m].iloc[-1] / df.open[m].iloc[0] - 1,
            "dd": (1 - eq / eq.cummax()).max(),
            "trades": int((p.diff() == 1).sum() + (p.iloc[0] == 1))}


def hold_stats(df, t0, t_split, t_end):
    c = df.close
    full = c[(c.index >= t0) & (c.index < t_end)]
    test = c[(c.index >= t_split) & (c.index < t_end)]
    return {"full_ret": full.iloc[-1] / full.iloc[0] - 1,
            "test_ret": test.iloc[-1] / test.iloc[0] - 1,
            "max_dd": (1 - full / full.cummax()).max(),
            "from_peak": 1 - full.iloc[-1] / full.max()}


def pct(x):
    return f"{x * 100:+.0f}%"


# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--skip-top", type=int, default=30)
    ap.add_argument("--max-rank", type=int, default=150)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--local-data", default="")
    a = ap.parse_args()

    if a.universe == "small":
        chosen = pick_small(a.skip_top, a.max_rank, a.years, a.limit)
    else:
        chosen = [(None, s) for s in a.symbols.split(",") if s]
    benchmark = "BTCUSDT"

    data = {}
    for rank, sym in chosen + [(None, benchmark)]:
        if sym in data:
            continue
        try:
            if a.local_data:
                data[sym] = load_csv(os.path.join(a.local_data, f"{sym}_1h.csv"))
            else:
                data[sym] = load_binance(sym, a.years)
            print(f"loaded {sym}: {len(data[sym])} candles")
        except Exception as e:  # noqa: BLE001
            print(f"skip {sym}: {e}")

    t_end = min(d.index[-1] for d in data.values()) + pd.Timedelta(hours=1)
    t0 = t_end - pd.DateOffset(years=a.years)
    t_split = t_end - pd.DateOffset(years=1)
    coins = [(r, s) for r, s in chosen if s in data]
    say(f"## Проверка: {len(coins)} монет, {t0.date()} — {t_end.date()}")
    say(f"Обучение: {t0.date()} — {t_split.date()} · Проверка: {t_split.date()} — {t_end.date()}")
    say("Издержки 0.15% на сделку, вход по следующей свече.")
    say()

    # --- удержание ---
    hs = {s: hold_stats(data[s], t0, t_split, t_end) for _, s in coins}
    b = hold_stats(data[benchmark], t0, t_split, t_end)
    H = pd.DataFrame(hs).T
    say("### Просто купить и держать")
    say("| Монета | Ранг по объёму | За весь период | За последний год | Макс. просадка | Сейчас от пика |")
    say("|---|---|---|---|---|---|")
    for rank, s in coins:
        h = hs[s]
        say(f"| {s} | {rank or ''} | {pct(h['full_ret'])} | {pct(h['test_ret'])} | "
            f"{h['max_dd'] * 100:.0f}% | −{h['from_peak'] * 100:.0f}% |")
    say(f"| **BTC (для сравнения)** | | {pct(b['full_ret'])} | {pct(b['test_ret'])} | "
        f"{b['max_dd'] * 100:.0f}% | −{b['from_peak'] * 100:.0f}% |")
    say()
    say(f"- Медиана за весь период: {pct(H.full_ret.median())} (BTC {pct(b['full_ret'])})")
    say(f"- Монет в плюсе за весь период: {(H.full_ret > 0).sum()} из {len(H)}")
    say(f"- Обогнали BTC: {(H.full_ret > b['full_ret']).sum()} из {len(H)}")
    say(f"- Падали от пика больше чем на 80%: {(H.max_dd > 0.8).sum()} из {len(H)}")
    say(f"- Медиана за последний год: {pct(H.test_ret.median())} (BTC {pct(b['test_ret'])})")
    say()

    # --- стратегии ---
    rows = []
    for name, (tf, fn) in VARIANTS.items():
        for _, s in coins:
            df = resample(data[s], tf)
            entry, exit_ = fn(df)
            pos = positions(entry, exit_)
            tr = evaluate(df, pos, t0, t_split)
            te = evaluate(df, pos, t_split, t_end)
            rows.append({"variant": name, "sym": s,
                         **{f"tr_{k}": v for k, v in tr.items()},
                         **{f"te_{k}": v for k, v in te.items()}})
    R = pd.DataFrame(rows)
    R["te_beat"] = R.te_ret > R.te_hold
    g = R.groupby("variant", sort=False).agg(
        tr_med=("tr_ret", "median"), tr_hold=("tr_hold", "median"),
        te_med=("te_ret", "median"), te_mean=("te_ret", "mean"),
        te_hold=("te_hold", "median"), te_pos=("te_ret", lambda x: (x > 0).mean()),
        te_beat=("te_beat", "mean"), te_trades=("te_trades", "mean"))
    say("### Стратегии (медиана по всем монетам)")
    say("| Стратегия | Обучение | Удержание (обуч.) | Проверка | Удержание (пров.) | В плюсе на проверке | Лучше удержания | Сделок за год |")
    say("|---|---|---|---|---|---|---|---|")
    for name, r in g.iterrows():
        say(f"| {name} | {pct(r.tr_med)} | {pct(r.tr_hold)} | {pct(r.te_med)} | {pct(r.te_hold)} | "
            f"{r.te_pos * 100:.0f}% монет | {r.te_beat * 100:.0f}% монет | {r.te_trades:.0f} |")
    best = g.tr_med.idxmax()
    sb = g.loc[best]
    say()
    say(f"**Выбрано по обучению:** {best} → на проверке медиана {pct(sb.te_med)}, "
        f"среднее {pct(sb.te_mean)}, в плюсе {sb.te_pos * 100:.0f}% монет "
        f"(удержание: медиана {pct(sb.te_hold)}).")

    R.to_csv("research_results.csv", index=False)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(OUT) + "\n")
    print("=====RESULTS-CSV-BEGIN=====")
    print(R.round(4).to_csv(index=False), end="")
    print("=====RESULTS-CSV-END=====")


if __name__ == "__main__":
    main()
