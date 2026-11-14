"""
Бэктест: проверяем, сколько заработала (или потеряла) бы стратегия бота
на истории, с учётом комиссий. И сравниваем с «купил и держал».

    python backtest.py            # период из config.BACKTEST_DAYS
    python backtest.py 365        # за последние 365 дней
"""
import os
import sys

import config
import core


def run(candles):
    ind = core.analyze(candles)
    closes = ind["close"]
    start = config.EMA_SLOW + 1  # ждём, пока индикаторы «прогреются»

    cash, coins = config.START_BALANCE, 0.0
    entry_value = 0.0
    trades, wins = 0, 0
    peak, max_dd = config.START_BALANCE, 0.0

    for i in range(start, len(closes)):
        price = closes[i]
        codes = [code for code, _ in core.signals_at(ind, i)]

        if coins == 0 and "BUY" in codes:
            entry_value = cash
            coins = cash * (1 - config.FEE) / price
            cash = 0.0
        elif coins > 0 and "SELL" in codes:
            cash = coins * price * (1 - config.FEE)
            coins = 0.0
            trades += 1
            wins += cash > entry_value

        equity = cash + coins * price
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

    if coins > 0:  # закрываем открытую позицию по последней цене
        cash = coins * closes[-1] * (1 - config.FEE)
        trades += 1
        wins += cash > entry_value

    hold = (closes[-1] / closes[start] - 1) * 100
    return {
        "result": (cash / config.START_BALANCE - 1) * 100,
        "hold": hold,
        "trades": trades,
        "winrate": (wins / trades * 100) if trades else 0.0,
        "max_dd": max_dd * 100,
        "final": cash,
    }


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else config.BACKTEST_DAYS
    rows = []
    for symbol in config.SYMBOLS:
        print(f"Загружаю {symbol} за {days} дней...")
        candles = core.get_history(symbol, days=days)
        if len(candles) < config.EMA_SLOW + 10:
            print(f"{symbol}: мало данных ({len(candles)} свечей)")
            continue
        rows.append((symbol, run(candles)))

    header = (f"Бэктест за {days} дн., свечи {config.INTERVAL}, "
              f"старт {config.START_BALANCE:.0f} USDT, комиссия {config.FEE * 100:.1f}%")
    md = [f"### {header}", "",
          "| Пара | Стратегия | Купил и держал | Сделок | Прибыльных | Макс. просадка |",
          "|---|---|---|---|---|---|"]
    verdicts = []
    print("\n" + header)
    for symbol, r in rows:
        print(f"{symbol}: стратегия {r['result']:+.1f}% | купил и держал {r['hold']:+.1f}% | "
              f"сделок {r['trades']} | прибыльных {r['winrate']:.0f}% | "
              f"макс. просадка {r['max_dd']:.1f}%")
        md.append(f"| {symbol} | {r['result']:+.1f}% | {r['hold']:+.1f}% | {r['trades']} | "
                  f"{r['winrate']:.0f}% | {r['max_dd']:.1f}% |")
        if r["result"] < r["hold"]:
            verdict = "стратегия проиграла простому «купил и держал»"
        elif r["result"] <= 0:
            verdict = "стратегия в минусе, хоть и лучше удержания"
        else:
            verdict = "стратегия обогнала удержание на этом отрезке (это не гарантия на будущее)"
        print(f"  → {verdict}")
        verdicts.append(f"- **{symbol}**: {verdict}")
    md += [""] + verdicts

    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:  # красивая таблица на странице запуска в GitHub
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")

    if not rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
