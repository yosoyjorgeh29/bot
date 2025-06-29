import os
import logging
import asyncio

import pandas as pd
import numpy as np

from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)
from ta.trend import ADXIndicator
from tenacity import retry, stop_after_attempt, wait_exponential

from pocketoptionapi_async.client import PocketOptionClient

# ── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
PO_SS_ID       = os.environ.get("PO_SS_ID")  
if not TELEGRAM_TOKEN or not PO_SS_ID:
    raise RuntimeError

# ── Conversación ──────────────────────────────────────────────────────────────
CHOOSE_MARKET, CHOOSE_PAIR, WAIT_SIGNAL, WAIT_RESULT = range(4)

FOREX_PAIRS = {
    "AUD/CAD":"🇦🇺/🇨🇦","AUD/JPY":"🇦🇺/🇯🇵","AUD/USD":"🇦🇺/🇺🇸",
    "AUD/CHF":"🇦🇺/🇨🇭","CAD/CHF":"🇨🇦/🇨🇭","CAD/JPY":"🇨🇦/🇯🇵",
    "CHF/JPY":"🇨🇭/🇯🇵","EUR/AUD":"🇪🇺/🇦🇺","EUR/CAD":"🇪🇺/🇨🇦",
    "EUR/CHF":"🇪🇺/🇨🇭","EUR/GBP":"🇪🇺/🇬🇧","USD/CAD":"🇺🇸/🇨🇦",
    "USD/CHF":"🇺🇸/🇨🇭","USD/JPY":"🇺🇸/🇯🇵","GBP/CAD":"🇬🇧/🇨🇦",
    "GBP/CHF":"🇬🇧/🇨🇭",
}
OTC_PAIRS = {
     "AUDCAD-OTC":"🇦🇺/🇨🇦","AUDCHF-OTC":"🇦🇺/🇨🇭","AUDJPY-OTC":"🇦🇺/🇯🇵",
    "AUDNZD-OTC":"🇦🇺/🇳🇿","AUDUSD-OTC":"🇦🇺/🇺🇸","CADCHF-OTC":"🇨🇦/🇨🇭",
    "CADJPY-OTC":"🇨🇦/🇯🇵","CHFJPY-OTC":"🇨🇭/🇯🇵","EURAUD-OTC":"🇪🇺/🇦🇺",
    "EURCAD-OTC":"🇪🇺/🇨🇦","EURCHF-OTC":"🇪🇺/🇨🇭","EURGBP-OTC":"🇪🇺/🇬🇧",
    "EURNZD-OTC":"🇪🇺/🇳🇿","EURUSD-OTC":"🇪🇺/🇺🇸","GBPCAD-OTC":"🇬🇧/🇨🇦",
    "GBPCHF-OTC":"🇬🇧/🇨🇭","GBPJPY-OTC":"🇬🇧/🇯🇵","GBPNZD-OTC":"🇬🇧/🇳🇿",
    "GBPUSD-OTC":"🇬🇧/🇺🇸","NZDCAD-OTC":"🇳🇿/🇨🇦","NZDCHF-OTC":"🇳🇿/🇨🇭",
    "NZDJPY-OTC":"🇳🇿/🇯🇵","NZDUSD-OTC":"🇳🇿/🇺🇸","USDBRL-OTC":"🇺🇸/🇧🇷",
    "USDCAD-OTC":"🇺🇸/🇨🇦","USDCHF-OTC":"🇺🇸/🇨🇭","USDINR-OTC":"🇺🇸/🇮🇳",
}

# TwelveData API keys
TW_KEYS = [
    os.environ.get("TWELVE_KEY_1"),
    os.environ.get("TWELVE_KEY_2"),
    os.environ.get("TWELVE_KEY_3"),
    os.environ.get("TWELVE_KEY_4"),
    os.environ.get("TWELVE_KEY_5"),
    os.environ.get("TWELVE_KEY_6"),
]
PAIR_TO_KEY = {
    **dict.fromkeys(["AUD/CAD","AUD/JPY","AUD/USD"], TW_KEYS[0]),
    **dict.fromkeys(["AUD/CHF","CAD/CHF","CAD/JPY"], TW_KEYS[1]),
    **dict.fromkeys(["CHF/JPY","EUR/AUD","EUR/CAD"], TW_KEYS[2]),
    **dict.fromkeys(["EUR/CHF","EUR/GBP","USD/CAD"], TW_KEYS[3]),
    **dict.fromkeys(["USD/CHF","USD/JPY","GBP/CAD"], TW_KEYS[4]),
    **{"GBP/CHF": TW_KEYS[5]},
}

# ── RETRY para HTTP ────────────────────────────────────────────────────────────
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
def http_get(url, **kw):
    import requests
    r = requests.get(url, **kw)
    r.raise_for_status()
    return r

# ── Descarga velas OTC via WS de PocketOption (async) ──────────────────────────
async def fetch_candles_otc(symbol: str, interval: str="5m", count: int=30) -> pd.DataFrame:
    client = PocketOptionClient(ssid=PO_SS_ID, isDemo=True)
    await client.connect()
    raw = await client.get_candles(symbol, interval, count)
    await client.disconnect()

    # raw es lista de dicts con 't','o','h','l','c'
    df = pd.DataFrame(raw)
    df["datetime"] = pd.to_datetime(df["t"], unit="ms")
    df.set_index("datetime", inplace=True)
    df.rename(columns={"o":"open","h":"high","l":"low","c":"close"}, inplace=True)
    return df.astype(float)

# ── Descarga velas Forex via TwelveData ────────────────────────────────────────
async def fetch_candles_forex(pair: str, interval="5min", outputsize: int=30) -> pd.DataFrame:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": pair, "interval": interval,
        "outputsize": outputsize, "apikey": PAIR_TO_KEY[pair],
        "format": "JSON"
    }
    r = http_get(url, params=params, timeout=10)
    data = r.json().get("values", [])[::-1]
    df = pd.DataFrame(data)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    for c in ("open","high","low","close"):
        df[c] = pd.to_numeric(df[c])
    return df

# ── Selector genérico de velas ────────────────────────────────────────────────
async def fetch_candles(pair: str, interval="5min", size: int=30) -> pd.DataFrame:
    if pair.endswith("-OTC"):
        symbol = pair.replace("-OTC","")
        iv = interval.replace("min","m")
        return await fetch_candles_otc(symbol, interval=iv, count=size)
    return await fetch_candles_forex(pair, interval, size)

# ── Indicadores ───────────────────────────────────────────────────────────────
def compute_atr(df: pd.DataFrame, length=14) -> float:
    tr1 = df.high - df.low
    tr2 = (df.high - df.close.shift()).abs()
    tr3 = (df.low  - df.close.shift()).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.rolling(length).mean().iat[-1])

def check_retest(df: pd.DataFrame) -> tuple[str,float] | None:
    adx = ADXIndicator(df.high, df.low, df.close, window=14).adx().iat[-1]
    if adx < 25: return None
    atr = compute_atr(df, 14)
    if atr < (df.high - df.low).mean() * 0.3: return None
    y, x = df.close.values, np.arange(len(df))
    m, b  = np.polyfit(x, y, 1)
    last, trend = df.iloc[-1], m*(len(y)-1)+b
    gap = atr * 0.2
    if m>0 and last.low <= trend+gap:
        return "CALL", adx*(gap + abs(last.close-trend))
    if m<0 and last.high >= trend-gap:
        return "PUT",  adx*(gap + abs(last.close-trend))
    return None

# ── Handlers Telegram ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💹 Forex", callback_data="MARKET_FOREX")],
        [InlineKeyboardButton("📈 OTC",   callback_data="MARKET_OTC")],
    ]
    await update.message.reply_text("⭐️ Elige mercado:", reply_markup=InlineKeyboardMarkup(kb))
    return CHOOSE_MARKET

async def choose_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    m = q.data.split("_")[1]
    context.user_data["market"] = m
    mapping = FOREX_PAIRS if m=="FOREX" else OTC_PAIRS
    kb = [[InlineKeyboardButton("🤖 Auto", callback_data="AUTO")]]
    row=[]
    for p,flag in mapping.items():
        row.append(InlineKeyboardButton(f"{flag} {p}", callback_data=p))
        if len(row)==2: kb.append(row); row=[]
    if row: kb.append(row)
    await q.edit_message_text("⭐️ Elige par o modo:", reply_markup=InlineKeyboardMarkup(kb))
    return CHOOSE_PAIR

async def choose_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sel = q.data
    now = datetime.utcnow()
    m5  = (now.minute//5 + 1)*5
    entry = now.replace(minute=m5%60, second=0, microsecond=0)
    if m5 >= 60: entry += timedelta(hours=1)

    if sel=="AUTO":
        intro = await q.edit_message_text("🤖 Auto: escaneando…")
        pairs = list(FOREX_PAIRS if context.user_data["market"]=="FOREX" else OTC_PAIRS)
    else:
        intro = await q.edit_message_text(
            f"🎯 Has elegido *{sel}*.\nSeñal 1m antes cierre: {(entry-timedelta(minutes=1)).strftime('%H:%M')} UTC",
            parse_mode="Markdown"
        )
        pairs = [sel]

    context.job_queue.run_once(
        send_signal,
        when=entry - timedelta(minutes=1),
        data={"chat_id":q.message.chat_id,"pairs":pairs,"intro_id":intro.message_id,"entry_time":entry}
    )
    return WAIT_SIGNAL

async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    chat,pairs,entry = d["chat_id"],d["pairs"],d["entry_time"]
    try: await context.bot.delete_message(chat, d["intro_id"])
    except: pass

    best = None
    for p in pairs:
        df = await asyncio.to_thread(fetch_candles, p)
        sig_score = check_retest(df)
        if not sig_score: continue
        sig, score = sig_score
        if best is None or score > best["score"]:
            best = {"pair":p,"signal":sig,"score":score}

    if not best:
        await context.bot.send_message(chat,"⚠️ No señal clara")
        return WAIT_SIGNAL

    emoji = "🟢" if best["signal"]=="CALL" else "🔴"
    await context.bot.send_message(
        chat,
        f"🤖 Señal:\n🌐 {best['pair']}\n📈 {emoji} {best['signal']}\n⏰ {entry.strftime('%H:%M')} UTC\n🎯 Martingale OK"
    )
    context.job_queue.run_once(
        check_result,
        when=entry + timedelta(minutes=5),
        data={"chat_id":chat,"pair":best["pair"],"signal":best["signal"],"entry_time":entry}
    )
    return WAIT_RESULT

async def check_result(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    chat,pair,sig,entry = d["chat_id"],d["pair"],d["signal"],d["entry_time"]
    df = await asyncio.to_thread(fetch_candles, pair)
    try: candle = df.loc[entry]
    except KeyError:
        idx = df.index.get_indexer([entry], method="nearest")[0]
        candle = df.iloc[idx]

    won = (sig=="CALL" and candle.close>candle.open) or (sig=="PUT" and candle.close<candle.open)
    if won:
        await context.bot.send_message(chat,"✅ GANADA 🟢\n🔄 /start")
        return ConversationHandler.END

    context.job_queue.run_once(
        check_martingale,
        when=entry + timedelta(minutes=10),
        data=d
    )
    return WAIT_RESULT

async def check_martingale(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    chat,pair,sig,entry = d["chat_id"],d["pair"],d["signal"],d["entry_time"]
    df = await asyncio.to_thread(fetch_candles, pair)
    t2 = entry + timedelta(minutes=10)
    try: candle = df.loc[t2]
    except KeyError:
        idx = df.index.get_indexer([t2], method="nearest")[0]
        candle = df.iloc[idx]

    won2 = (sig=="CALL" and candle.close>candle.open) or (sig=="PUT" and candle.close<candle.open)
    text = "✅ GANADA 🟢 (Martingale)" if won2 else "❌ PERDIDA 🔴 (Martingale)"
    await context.bot.send_message(chat, text)
    await context.bot.send_message(chat,"🔄 /start")
    return ConversationHandler.END

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s - %(message)s")

    transport = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=10.0
    )
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(transport).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_MARKET:[CallbackQueryHandler(choose_market, pattern="^MARKET_")],
            CHOOSE_PAIR:  [CallbackQueryHandler(choose_pair)],
            WAIT_SIGNAL:  [], WAIT_RESULT: []
        },
        fallbacks=[CommandHandler("start", start)],
        per_chat=True,
    )
    app.add_handler(conv)

    app.run_polling()

if __name__ == "__main__":
    main()
