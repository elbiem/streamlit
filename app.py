# -*- coding: utf-8 -*-
# Wisdom of the Crowd — Crypto Dashboard (v2, refresh button + HTTP fallback)
# Запуск: streamlit run app.py

import os
import math
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import ccxt  # используем, но при сетевой ошибке падаем в HTTP-фолбэк
import feedparser
from urllib.parse import urlparse, urlunparse
from datetime import datetime, timedelta
from dateutil import tz
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from pytrends.request import TrendReq
import streamlit as st

# ----------------------------- #
# SETTINGS / KEYS
# ----------------------------- #
st.set_page_config(page_title="Crypto Crowd Wisdom v2", page_icon="📊", layout="wide")

st.sidebar.title("⚙️ Controls")
SYMBOL = st.sidebar.selectbox("Тикер", ["BTC/USDT", "ETH/USDT", "SOL/USDT"], index=0)
TIMEFRAME = st.sidebar.selectbox("Таймфрейм OHLC", ["1m", "5m", "15m"], index=1)

# Кнопка мгновенного обновления
if st.sidebar.button("↻ Refresh now"):
    st.experimental_rerun()

# API keys (опц.)
def get_secret(path, env):
    try:
        return st.secrets.get("api", {}).get(path)  # type: ignore
    except Exception:
        return os.getenv(env)

COINGLASS_KEY   = get_secret("coinglass_key", "COINGLASS_KEY")
CRYPTOPANIC_KEY = get_secret("cryptopanic_key", "CRYPTOPANIC_KEY")
LUNARCRUSH_KEY  = get_secret("lunarcrush_key", "LUNARCRUSH_KEY")

LOCAL_TZ = tz.gettz("Europe/Budapest")
analyzer = SentimentIntensityAnalyzer()

# ----------------------------- #
# HELPERS
# ----------------------------- #
def cache(ttl=60):
    return st.cache_data(show_spinner=False, ttl=ttl)

def symbol_to_binance(symbol: str) -> str:
    # "BTC/USDT" -> "BTCUSDT"
    return symbol.replace("/", "")

def tf_to_binance(tf: str) -> str:
    # 1m/5m/15m совместимы с Binance
    return tf

BINANCE_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (CrowdDashboard; +https://streamlit.io)"
}

# ----------------------------- #
# MARKET DATA (ccxt + HTTP fallback)
# ----------------------------- #
@cache(ttl=15)
def fetch_price_and_ohlcv(symbol: str, timeframe="5m", limit=400):
    """
    Сначала пробуем ccxt.binance(); если сеть/эндпоинт недоступны -> HTTP фолбэк:
    - цена: https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT
    - свечи: https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=400
    """
    sym_http = symbol_to_binance(symbol)
    interval = tf_to_binance(timeframe)

    # 1) Пытаемся через ccxt
    try:
        ex = ccxt.binance({"enableRateLimit": True, "timeout": 10000})
        ticker = ex.fetch_ticker(symbol)
        ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["ts"], unit="ms").dt.tz_localize("UTC").dt.tz_convert(LOCAL_TZ)
        return float(ticker["last"]), df, "ccxt"
    except Exception:
        # 2) HTTP фолбэк (spot REST)
        price_url = "https://api.binance.com/api/v3/ticker/price"
        klines_url = "https://api.binance.com/api/v3/klines"
        price = None
        try:
            r = requests.get(price_url, params={"symbol": sym_http}, headers=BINANCE_HTTP_HEADERS, timeout=10)
            r.raise_for_status()
            price = float(r.json()["price"])
        except Exception:
            pass  # цену возьмём из последней свечи, если что

        r = requests.get(klines_url, params={"symbol": sym_http, "interval": interval, "limit": limit},
                         headers=BINANCE_HTTP_HEADERS, timeout=10)
        r.raise_for_status()
        rows = r.json()
        cols = ["open_time","open","high","low","close","volume","close_time",
                "qav","num_trades","taker_base","taker_quote","ignore"]
        df = pd.DataFrame(rows, columns=cols)
        df["open"] = pd.to_numeric(df["open"])
        df["high"] = pd.to_numeric(df["high"])
        df["low"] = pd.to_numeric(df["low"])
        df["close"] = pd.to_numeric(df["close"])
        df["volume"] = pd.to_numeric(df["volume"])
        df["time"] = pd.to_datetime(df["open_time"], unit="ms").dt.tz_localize("UTC").dt.tz_convert(LOCAL_TZ)

        last_price = price if price is not None else float(df["close"].iloc[-1])
        return last_price, df[["time","open","high","low","close","volume"]], "http"

@cache(ttl=60)
def fetch_fear_greed():
    url = "https://api.alternative.me/fng/?limit=1"
    r = requests.get(url, headers=BINANCE_HTTP_HEADERS, timeout=10)
    r.raise_for_status()
    d = r.json()["data"][0]
    ts = datetime.fromtimestamp(int(d["timestamp"]), tz=tz.tzutc()).astimezone(LOCAL_TZ)
    return {"value": int(d["value"]), "label": d["value_classification"], "time": ts}

@cache(ttl=60)
def fetch_binance_long_short_ratio(symbol_usdt="BTCUSDT", period="1h", limit=30):
    url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
    r = requests.get(url, params={"symbol": symbol_usdt, "period": period, "limit": limit},
                     headers=BINANCE_HTTP_HEADERS, timeout=10)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    if df.empty:
        return pd.DataFrame()
    df["time"] = pd.to_datetime(df["timestamp"], unit="ms").dt.tz_localize("UTC").dt.tz_convert(LOCAL_TZ)
    df["longShortRatio"] = pd.to_numeric(df["longShortRatio"])
    return df[["time", "longShortRatio"]]

@cache(ttl=60)
def fetch_binance_funding(symbol_usdt="BTCUSDT", limit=48):
    url = "https://fapi.binance.com/fapi/v1/fundingRate"
    r = requests.get(url, params={"symbol": symbol_usdt, "limit": limit},
                     headers=BINANCE_HTTP_HEADERS, timeout=10)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    if df.empty: return pd.DataFrame()
    df["time"] = pd.to_datetime(df["fundingTime"], unit="ms").dt.tz_localize("UTC").dt.tz_convert(LOCAL_TZ)
    df["rate"] = pd.to_numeric(df["fundingRate"])
    return df[["time", "rate"]]

@cache(ttl=120)
def fetch_google_trends(keyword="Bitcoin"):
    try:
        pytrends = TrendReq(hl='en-US', tz=0)
        pytrends.build_payload([keyword], timeframe='now 7-d', geo='')
        df = pytrends.interest_over_time()
        if df.empty: return pd.DataFrame()
        df = df.reset_index().rename(columns={"date":"time", keyword:"interest"})
        df["time"] = pd.to_datetime(df["time"]).dt.tz_localize("UTC").dt.tz_convert(LOCAL_TZ)
        return df[["time","interest"]]
    except Exception:
        return pd.DataFrame()

# ----------------------------- #
# NEWS (RSS) + DEDUP + SENTIMENT
# ----------------------------- #
def normalize_url(u: str) -> str:
    try:
        p = urlparse(u)
        clean = urlunparse((p.scheme, p.netloc, p.path, p.params, "", ""))
        return clean.lower().rstrip("/")
    except Exception:
        return u.lower().rstrip("/")

analyzer = SentimentIntensityAnalyzer()
def score_title_sentiment(title: str) -> float:
    if not title: return 0.0
    return analyzer.polarity_scores(title)["compound"]

@cache(ttl=30)
def fetch_news():
    items = []
    try:
        d = feedparser.parse("https://www.coindesk.com/arc/outboundfeeds/rss/")
        for e in d.entries[:30]:
            items.append({"title": e.title, "url": e.link, "source": "CoinDesk", "published": e.get("published")})
    except: pass
    try:
        d = feedparser.parse("https://cointelegraph.com/rss")
        for e in d.entries[:30]:
            items.append({"title": e.title, "url": e.link, "source": "Cointelegraph", "published": e.get("published")})
    except: pass
    try:
        d = feedparser.parse("https://www.theblock.co/rss")
        for e in d.entries[:30]:
            items.append({"title": e.title, "url": e.link, "source": "The Block", "published": e.get("published")})
    except: pass
    return items

def dedup_and_score_news(items):
    seen = set()
    result = []
    for it in items:
        url = normalize_url(it.get("url",""))
        if url in seen: 
            continue
        it["sentiment"] = score_title_sentiment(it.get("title",""))
        try:
            it["time"] = pd.to_datetime(it.get("published"), utc=True).tz_convert(LOCAL_TZ)
        except Exception:
            it["time"] = None
        seen.add(url)
        result.append(it)
    result.sort(key=lambda x: x.get("time") or datetime.now(LOCAL_TZ), reverse=True)
    return result

# ----------------------------- #
# CROWD INDEX
# ----------------------------- #
def normalize(val, vmin, vmax, invert=False):
    if val is None or (isinstance(val, float) and math.isnan(val)): return None
    if vmax == vmin: return 0.5
    x = (val - vmin) / (vmax - vmin)
    x = min(max(x, 0), 1)
    return 1 - x if invert else x

def compute_crowd_index(fear_greed_value, ls_ratio, funding_rate, ret_24h, news_bias, trends_interest):
    parts = []
    if fear_greed_value is not None:
        parts.append(normalize(fear_greed_value, 0, 100))
    if ls_ratio is not None and ls_ratio > 0:
        score = 1 / (1 + math.exp(-2 * (math.log(ls_ratio) / math.log(1.5))))
        parts.append(score)
    if funding_rate is not None:
        fr = max(min(funding_rate, 0.01), -0.01)
        parts.append(normalize(fr, -0.01, 0.01))
    if ret_24h is not None:
        r = max(min(ret_24h, 0.10), -0.10)
        parts.append(normalize(r, -0.10, 0.10))
    if news_bias is not None:
        parts.append((news_bias + 1) / 2)
    if trends_interest is not None:
        parts.append(normalize(trends_interest, 0, 100))
    if not parts: return 50.0
    return round(sum(parts)/len(parts)*100, 1)

# ----------------------------- #
# UI
# ----------------------------- #
st.title("📊 Wisdom of the Crowd — Crypto (v2)")
st.caption("Нажми ↻ Refresh now в сайдбаре, чтобы обновить данные.")

# Цена + свечи (с фолбэком)
last_price, df, data_src = fetch_price_and_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=400)
st.metric(f"{SYMBOL}", f"{last_price:,.2f} $")
st.caption(f"Источник цен/свечей: {data_src.upper()}")

# 24h доходность
ret_24h = None
try:
    tf_minutes = {"1m":1, "5m":5, "15m":15}[TIMEFRAME]
    lookback = (60//tf_minutes) * 24
    if len(df) > lookback:
        ret_24h = df["close"].iloc[-1] / df["close"].iloc[-lookback] - 1
except Exception:
    pass

fig_price = go.Figure()
fig_price.add_trace(go.Candlestick(
    x=df["time"], open=df["open"], high=df["high"], low=df["low"], close=df["close"], name="OHLC"
))
fig_price.update_layout(title=f"Цена {SYMBOL} ({TIMEFRAME})", height=420, margin=dict(l=10,r=10,t=40,b=10))
st.plotly_chart(fig_price, use_container_width=True)

# Метрики «толпы»
try:
    fg = fetch_fear_greed()
    fg_val = fg["value"]
    st.metric("Fear & Greed", f"{fg['value']} — {fg['label']}")
except Exception:
    fg_val = None
    st.metric("Fear & Greed", "нет данных")

symbol_usdt = symbol_to_binance(SYMBOL)
try:
    lsr_df = fetch_binance_long_short_ratio(symbol_usdt, "1h", 48)
    if not lsr_df.empty:
        last_lsr = float(lsr_df["longShortRatio"].iloc[-1])
        st.metric("Long/Short (Binance, 1h)", f"{last_lsr:.2f}x")
        fig_lsr = px.line(lsr_df, x="time", y="longShortRatio", title="Аккаунтное L/S")
        fig_lsr.update_layout(height=220, margin=dict(l=10,r=10,t=35,b=10))
        st.plotly_chart(fig_lsr, use_container_width=True)
    else:
        last_lsr = None
except Exception:
    last_lsr = None

try:
    fr_df = fetch_binance_funding(symbol_usdt, 72)
    if not fr_df.empty:
        last_fr = float(fr_df["rate"].iloc[-1])
        st.metric("Funding (последний)", f"{last_fr:.5f}")
        fig_fr = px.bar(fr_df, x="time", y="rate", title="Funding Rate (история)")
        fig_fr.update_layout(height=220, margin=dict(l=10,r=10,t=35,b=10))
        st.plotly_chart(fig_fr, use_container_width=True)
    else:
        last_fr = None
except Exception:
    last_fr = None

# Google Trends
kw_map = {"BTC":"Bitcoin","ETH":"Ethereum","SOL":"Solana"}
kw = kw_map.get(SYMBOL.split("/")[0], "Bitcoin")
trends_df = fetch_google_trends(kw)
last_trend = float(trends_df["interest"].iloc[-1]) if not trends_df.empty else None
if not trends_df.empty:
    fig_tr = px.line(trends_df.tail(200), x="time", y="interest", title=f"Google Trends: {kw}")
    fig_tr.update_layout(height=260, margin=dict(l=10,r=10,t=40,b=10))
    st.plotly_chart(fig_tr, use_container_width=True)

# Новости
raw = fetch_news()
news = dedup_and_score_news(raw)
nbias = (sum(n["sentiment"] for n in news[:30]) / max(1, min(30, len(news)))) if news else None

st.subheader("📰 Новости (без дублей)")
for n in news[:20]:
    sent = n["sentiment"]
    emj = "🟢" if sent > 0.2 else ("🟡" if sent > -0.2 else "🔴")
    when = n["time"].strftime("%Y-%m-%d %H:%M") if n["time"] else ""
    st.markdown(f"- {emj} [{n['title']}]({n['url']})  \n  <sub>{n.get('source','')} — {when}</sub>", unsafe_allow_html=True)

# Crowd Index
crowd_idx = compute_crowd_index(fg_val, last_lsr, last_fr, ret_24h, nbias, last_trend)
gauge = go.Figure(go.Indicator(
    mode="gauge+number",
    value=crowd_idx,
    title={"text": "Crowd Index (0..100)"},
    gauge={"axis": {"range": [0, 100]}}
))
st.plotly_chart(gauge, use_container_width=True)

st.caption("⚠️ Аналитический дашборд. Не является финансовой рекомендацией.")
