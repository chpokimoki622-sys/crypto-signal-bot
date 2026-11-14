"""
Бот сигналов: раз в час проверяет пары из config.SYMBOLS и присылает
сообщение в Telegram, когда срабатывает правило.

Локальный запуск без Telegram (сообщения выводятся в консоль):
    python bot.py --report
"""
import os
import sys

import requests

import config
import core

DISCLAIMER = "Это не гарантия прибыли, а подсказка по правилам. Решение за тобой."


def fmt_price(p):
    return f"{p:,.2f}".replace(",", " ") if p >= 1 else f"{p:.6f}"


def build_message(symbol, ind, events, book):
    i = len(ind["close"]) - 1
    price = ind["close"][i]
    fast, slow = ind["ema_fast"][i], ind["ema_slow"][i]
    r, flow = ind["rsi"][i], ind["buy_share"][i]

    lines = [f"<b>{symbol}</b> — {fmt_price(price)} USDT ({config.INTERVAL})"]
    if events:
        lines.append("")
        lines += [text for _, text in events]
    lines.append("")
    if fast is not None and slow is not None:
        trend = "вверх 📈" if fast > slow else "вниз 📉"
        lines.append(f"Тренд (EMA {config.EMA_FAST}/{config.EMA_SLOW}): {trend}")
    if r is not None:
        lines.append(f"RSI: {r:.0f}")
    if flow is not None:
        lines.append(f"Покупки/продажи за {config.FLOW_WINDOW} свечей: "
                     f"{flow * 100:.0f}% / {(1 - flow) * 100:.0f}%")
    if book is not None:
        lines.append(f"Стакан около цены: заявки на покупку {book * 100:.0f}%, "
                     f"на продажу {(1 - book) * 100:.0f}%")
    lines.append("")
    lines.append(f"<i>{DISCLAIMER}</i>")
    return "\n".join(lines)


def send_telegram(text):
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("--- (Telegram не настроен, вывожу в консоль) ---")
        print(text)
        print()
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": "true"},
                      timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram ответил {r.status_code}: {r.text[:200]}")


def main():
    force = "--report" in sys.argv or os.getenv("FORCE_REPORT") == "1"
    failed = 0

    for symbol in config.SYMBOLS:
        try:
            candles = core.get_klines(symbol)
            if len(candles) < config.EMA_SLOW + 2:
                raise core.BinanceError("слишком мало свечей для анализа")
            ind = core.analyze(candles)
            events = core.signals_at(ind, len(candles) - 1)
            print(f"{symbol}: цена {candles[-1]['close']}, "
                  f"события: {[code for code, _ in events] or 'нет'}")

            if events or force:
                try:
                    book = core.order_book_balance(symbol)
                except core.BinanceError:
                    book = None
                send_telegram(build_message(symbol, ind, events, book))
        except core.BinanceError as e:
            failed += 1
            print(f"{symbol}: ошибка Binance — {e}")
            if "451" in str(e):
                print("Подсказка: Binance не отдаёт данные этому серверу из-за "
                      "региона. Запусти бота на своём компьютере (см. README).")
        except Exception as e:  # чтобы одна пара не роняла остальные
            failed += 1
            print(f"{symbol}: ошибка — {e}")

    if failed == len(config.SYMBOLS):
        sys.exit(1)  # всё упало — GitHub пометит запуск красным и пришлёт письмо


if __name__ == "__main__":
    main()
