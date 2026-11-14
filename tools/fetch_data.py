"""
Скачивает историю свечей с Binance в CSV (для исследований стратегий).
    python tools/fetch_data.py 1095      # 3 года часовых свечей
Файлы сохраняются в папку data/.
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # noqa: E402
import core    # noqa: E402

days = int(sys.argv[1]) if len(sys.argv) > 1 else 1095
os.makedirs("data", exist_ok=True)
for symbol in config.SYMBOLS:
    candles = core.get_history(symbol, interval="1h", days=days)
    path = os.path.join("data", f"{symbol}_1h.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume", "taker_buy"])
        for c in candles:
            w.writerow([c["open_time"], c["open"], c["high"], c["low"],
                        c["close"], c["volume"], c["taker_buy"]])
    print(f"{symbol}: {len(candles)} свечей → {path}")
