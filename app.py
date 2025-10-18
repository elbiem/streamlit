# -*- coding: utf-8 -*-
# Wisdom of the Crowd — Crypto Dashboard (v2, realtime 10s + dedup news)
# Запуск: streamlit run app.py
#
# Опционально добавьте ключи в .streamlit/secrets.toml или ENV:
# [api]
# coinglass_key = "YOUR_COINGLASS_KEY"
# cryptopanic_key = "YOUR_CRYPTOPANIC_TOKEN"
# lunarcrush_key = "YOUR_LUNARCRUSH_KEY"   # (опционально)
#
# Что нового в v2:
# • Живая лента новостей (обновл. каждые 10с) из нескольких источников + дедупликация
# • Больше источников «мудрости толпы»: Google Trends (pytrends), Reddit hot (RSS/JSON)
# • Улучшенный sentiment (VADER) по заголовкам новостей
# • Crowd Index агрегирует больше сигналов

import os
import math
import time
import json
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import ccxt
import feedparser
from urllib.parse import urlparse, urlunparse
from datetime import datetime, timedelta
from dateutil import tz
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from pytrends.request import TrendReq
import streamlit as st

# --------------------------------
# SETTINGS / KEYS
# --------------------------------
st.set_page_config(page_title="Crypto Crowd Wisdom v2", page_icon="📊", layout="wide")

# Авто-обновление дашборда
REFRESH_MS = 10_000  # 10s
st.autorefresh(interval=REFRESH_MS, key="auto_refresh_v2")

# Sidebar controls
SYMBOL = st.sidebar.selectbox("Тикер", ["BTC/USDT", "ETH/USDT", "SOL/USDT"], index=0)
TIMEFRAME = st.sidebar.selectbox("Таймфрейм OHLC", ["1m", "5m", "15m"], index=1)

# API keys
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

# --------------------------------
# CACHING HELPERS
# --------------------------------
def cache(ttl=60):
    return st.cache_data(show_spinner=False, ttl=ttl)

# --------------------------------
# MARKET DATA
# --------------------------------
@cache(ttl=15)
def fetch_price_and_ohlcv(symbol: str, timeframe="5m", limit=400):
    ex = ccxt.binance()
    ticker = ex.fetch_ticker(symbol)
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms").dt.tz_localize("UTC").dt.tz_convert(LOCAL_TZ)
    return float(ticker["last"]), df

@cache(ttl=60)
def fetch_fear_greed():
    url = "https://api.alternative.me/fng/?limit=1"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    d = r.json()["data"][0]
    ts = datetime.fromtimestamp(int(d["timestamp"]), tz=tz.tzutc()).astimezone(LOCAL_TZ)
    return {"value": int(d["value"]), "label": d["value_classification"], "time": ts}

@cache(ttl=60)
def fetch_binance_long_short_ratio(symbol_usdt="BTCUSDT", period="1h", limit=30):
    url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
    r = requests.get(url, params={"symbol": symbol_usdt, "period": period, "limit": limit}, timeout=10)
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
    r = requests.get(url, params={"symbol": symbol_usdt, "limit": limit}, timeout=10)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    if df.empty: return pd.DataFrame()
    df["time"] = pd.to_datetime(df["fundingTime"], unit="ms").dt.tz_localize("UTC").dt.tz_convert(LOCAL_TZ)
    df["rate"] = pd.to_numeric(df["fundingRate"])
    return df[["time", "rate"]]

@cache(ttl=120)
def fetch_google_trends(keyword="Bitcoin"):
    # Относительный интерес за 7 дней по всему миру, часовая гранулярность
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

# --------------------------------
# SOCIAL / ON-CHAIN (optional)
# --------------------------------
@cache(ttl=120)
def fetch_coinglass_long_short(symbol="BTC"):
    if not COINGLASS_KEY: return None
    url = "https://open-api.coinglass.com/public/v2/futures/longShort"
    r = requests.get(url, headers={"coinglassSecret": COINGLASS_KEY}, params={"symbol": symbol, "timeType": 0}, timeout=10)
    if r.status_code != 200: return None
    return r.json()

# --------------------------------
# NEWS SOURCES (10s)
# --------------------------------
def normalize_url(u: str) -> str:
    """Убираем трекинг-параметры, приводим к канонической форме для дедупликации."""
    try:
        p = urlparse(u)
        # убираем utm_*, ref, fbclid и т.п.
        query = ""
        clean = urlunparse((p.scheme, p.netloc, p.path, p.params, query, ""))
        return clean.lower().rstrip("/")
    except Exception:
        return u.lower().rstrip("/")

def score_title_sentiment(title: str) -> float:
    if not title: return 0.0
    vs = analyzer.polarity_scores(title)
    # compound [-1..1]
    return vs["compound"]

def pick_symbol_keyword(symbol: str) -> str:
    base = symbol.split("/")[0]
    return {"BTC":"Bitcoin","ETH":"Ethereum","SOL":"Solana"}.get(base, base)

@cache(ttl=8)  # < 10s для плавности
def fetch_news_sources(symbol: str):
    """Сбор новостей из нескольких источников (RSS/JSON), возврат сырого списка items."""
    base = symbol.replace("/USDT","")
    keyword = pick_symbol_keyword(symbol)

    items = []

    # 1) CryptoPanic (если есть ключ)
    if CRYPTOPANIC_KEY:
        try:
            url = "https://cryptopanic.com/api/v1/posts/"
            params = {"auth_token": CRYPTOPANIC_KEY, "currencies": base, "kind": "news", "public": "true"}
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                for it in r.json().get("results", []):
                    items.append({
                        "title": it.get("title"),
                        "url": it.get("url"),
                        "source": (it.get("source") or {}).get("title"),
                        "published": it.get("published_at")
                    })
        except Exception:
            pass

    # 2) CoinDesk RSS
    try:
        d = feedparser.parse("https://www.coindesk.com/arc/outboundfeeds/rss/")
        for e in d.entries[:50]:
            items.append({
                "title": e.title,
                "url": e.link,
                "source": "CoinDesk",
                "published": e.get("published") or e.get("updated")
            })
    except Exception:
        pass

    # 3) Cointelegraph RSS
    try:
        d = feedparser.parse("https://cointelegraph.com/rss")
        for e in d.entries[:50]:
            items.append({
                "title": e.title,
                "url": e.link,
                "source": "Cointelegraph",
                "published": e.get("published") or e.get("updated")
            })
    except Exception:
        pass

    # 4) The Block RSS
    try:
        d = feedparser.parse("https://www.theblock.co/rss")
        for e in d.entries[:50]:
            items.append({
                "title": e.title,
                "url": e.link,
                "source": "The Block",
                "published": e.get("published") or e.get("updated")
            })
    except Exception:
        pass

    # 5) Binance Announcements RSS (листинги/обновления)
    try:
        d = feedparser.parse("https://www.binance.com/en/support/announcement/c-48?navId=48&hl=en&rss=1")
        for e in d.entries[:50]:
            items.append({
                "title": e.title,
                "url": e.link,
                "source": "Binance Announcements",
                "published": e.get("published") or e.get("updated")
            })
    except Exception:
        pass

    # 6) Reddit r/CryptoCurrency hot (RSS)
    try:
        d = feedparser.parse("https://www.reddit.com/r/CryptoCurrency/.rss")
        for e in d.entries[:30]:
            items.append({
                "title": e.title,
                "url": e.link,
                "source": "Reddit r/CryptoCurrency",
                "published": e.get("published") or e.get("updated")
            })
    except Exception:
        pass

    # 7) Reddit r/Bitcoin hot (RSS)
    try:
        d = feedparser.parse("https://www.reddit.com/r/Bitcoin/.rss")
        for e in d.entries[:30]:
            items.append({
                "title": e.title,
                "url": e.link,
                "source": "Reddit r/Bitcoin",
                "published": e.get("published") or e.get("updated")
            })
    except Exception:
        pass

    # 8) (опционально) LunarCrush headlines for BTC/ETH/SOL (требуется ключ)
    if LUNARCRUSH_KEY:
        try:
            sym = symbol.split("/")[0]
            url = "https://lunarcrush.com/api3/news"
            r = requests.get(url, params={"symbols": sym, "limit": 30, "key": LUNARCRUSH_KEY}, timeout=10)
            if r.status_code == 200:
                for n in r.json().get("data", []):
                    items.append({
                        "title": n.get("title"),
                        "url": n.get("url"),
                        "source": "LunarCrush",
                        "published": n.get("published_at")
                    })
        except Exception:
            pass

    return items

def dedup_and_score_news(items):
    """Дедуп по нормализованному URL + мягкая проверка по очень похожим заголовкам, расчёт sentiment."""
    seen = set()
    result = []

    def similar(a,b):
        # простая эвристика: сравним нижние строки без пунктуации
        import re
        aa = re.sub(r"[^a-z0-9 ]+", "", a.lower())
        bb = re.sub(r"[^a-z0-9 ]+", "", b.lower())
        # доля совпадения по множеству слов
        sa, sb = set(aa.split()), set(bb.split())
        if not sa or not sb: return 0.0
        return len(sa & sb) / max(len(sa), len(sb))

    for it in items:
        url = it.get("url") or ""
        title = it.get("title") or ""
        if not url and not title:
            continue
        key = normalize_url(url) if url else title.strip().lower()
        # если уже видели URL — пропускаем
        if key in seen:
            continue
        # грубая защита от дублей по похожим тайтлам
        is_dup = False
        for ex in result:
            if similar(title, ex["title"]) >= 0.9:
                is_dup = True
                break
        if is_dup: 
            continue

        it["sentiment"] = score_title_sentiment(title)
        # нормализованная дата
        published = it.get("published")
        try:
            dt = pd.to_datetime(published, utc=True).tz_convert(LOCAL_TZ)
        except Exception:
            dt = None
        it["time"] = dt
        result.append(it)
        seen.add(key)

    # сортируем: самые свежие сверху
    result.sort(key=lambda x: x.get("time") or datetime.now(tz=LOCAL_TZ)-timedelta(days=3650), reverse=True)
    return result

# --------------------------------
# CROWD INDEX
# --------------------------------
def normalize(val, vmin, vmax, invert=False):
    if val is None or (isinstance(val, float) and math.isnan(val)): return None
    if vmax == vmin: return 0.5
    x = (val - vmin) / (vmax - vmin)
    x = min(max(x, 0), 1)
    return 1 - x if invert else x

def compute_crowd_index(
    fear_greed_value: int | None,
    ls_ratio: float | None,        # >1 = бычье
    funding_rate: float | None,    # >0 = бычье
    ret_24h: float | None,         # доходность
    news_bias: float | None,       # -1..+1
    trends_interest: float | None  # 0..100
):
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

# --------------------------------
# UI
# --------------------------------
st.title("📊 Wisdom of the Crowd — Crypto (v2)")
st.caption("Реальное время, автообновление каждые 10 секунд. Агрегация рыночных и «толповых» сигналов.")

# Top row: price + F&G + L/S + Funding
c1, c2, c3, c4 = st.columns([1.4, 1, 1, 1])

with c1:
    with st.spinner("Цена и свечи..."):
        last_price, df = fetch_price_and_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=400)
    st.metric(f"{SYMBOL}", f"{last_price:,.2f} $")
    ret_24h = None
    try:
        # 24 часа в минутах для выбранного таймфрейма
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

with c2:
    try:
        fg = fetch_fear_greed()
        st.metric("Fear & Greed", f"{fg['value']} — {fg['label']}")
        fg_val = fg["value"]
    except Exception:
        fg_val = None
        st.metric("Fear & Greed", "нет данных")

with c3:
    symbol_usdt = SYMBOL.replace("/", "")
    try:
        lsr_df = fetch_binance_long_short_ratio(symbol_usdt=symbol_usdt, period="1h", limit=48)
        if not lsr_df.empty:
            last_lsr = float(lsr_df["longShortRatio"].iloc[-1])
            st.metric("Long/Short (Binance, 1h)", f"{last_lsr:.2f}x")
            fig_lsr = px.line(lsr_df, x="time", y="longShortRatio", title="Аккаунтное L/S")
            fig_lsr.update_layout(height=200, margin=dict(l=10,r=10,t=35,b=10))
            st.plotly_chart(fig_lsr, use_container_width=True)
        else:
            last_lsr = None
            st.write("Нет данных L/S.")
    except Exception:
        last_lsr = None
        st.write("Ошибка L/S.")

with c4:
    try:
        fr_df = fetch_binance_funding(symbol_usdt=symbol_usdt, limit=72)
        if not fr_df.empty:
            last_fr = float(fr_df["rate"].iloc[-1])
            st.metric("Funding (последний)", f"{last_fr:.5f}")
            fig_fr = px.bar(fr_df, x="time", y="rate", title="Funding Rate (история)")
            fig_fr.update_layout(height=200, margin=dict(l=10,r=10,t=35,b=10))
            st.plotly_chart(fig_fr, use_container_width=True)
        else:
            last_fr = None
            st.write("Нет данных funding.")
    except Exception:
        last_fr = None
        st.write("Ошибка funding.")

st.markdown("---")

# Middle: Trends + Crowd Index + News
m1, m2 = st.columns([1.2, 1])

# Google Trends
with m1:
    st.subheader("📈 Google Trends (интерес толпы)")
    kw = pick_symbol_keyword(SYMBOL)
    trends_df = fetch_google_trends(kw)
    if not trends_df.empty:
        st.caption(f"Ключ: {kw}")
        fig_tr = px.line(trends_df.tail(200), x="time", y="interest", title="Интерес в Google (7д, почас.)")
        fig_tr.update_layout(height=290, margin=dict(l=10,r=10,t=40,b=10))
        st.plotly_chart(fig_tr, use_container_width=True)
        last_trend = float(trends_df["interest"].iloc[-1])
    else:
        last_trend = None
        st.info("Нет данных Google Trends (лимит/ошибка).")

    # News feed (live + dedup)
    st.subheader("📰 Живая лента крипто-новостей (10s, без дублей)")
    raw_items = fetch_news_sources(SYMBOL)
    news = dedup_and_score_news(raw_items)
    if news:
        for n in news[:20]:
            t = n.get("time")
            when = t.strftime("%Y-%m-%d %H:%M") if t else ""
            sent = n.get("sentiment", 0.0)
            emj = "🟢" if sent > 0.2 else ("🟡" if sent > -0.2 else "🔴")
            st.markdown(f"- {emj} [{n.get('title')}]({n.get('url')})  \n  <sub>{n.get('source','')} — {when}</sub>", unsafe_allow_html=True)
    else:
        st.write("Пока нет новостей.")

with m2:
    # News sentiment → bias [-1..1]
    if news:
        nbias = sum(x["sentiment"] for x in news[:50]) / max(1, min(50, len(news)))
        nbias = max(min(nbias, 1.0), -1.0)
    else:
        nbias = None

    crowd_idx = compute_crowd_index(
        fear_greed_value=fg_val if 'fg_val' in locals() else None,
        ls_ratio=last_lsr if 'last_lsr' in locals() else None,
        funding_rate=last_fr if 'last_fr' in locals() else None,
        ret_24h=ret_24h,
        news_bias=nbias,
        trends_interest=last_trend
    )

    gauge = go.Figure(go.Indicator(
        mode="gauge+number",
        value=crowd_idx,
        title={"text": "Crowd Index (0..100)"},
        gauge={
            "axis": {"range": [0, 100]},
            "bar": {"thickness": 0.3},
            "steps": [
                {"range": [0,25],  "color":"#ffdddd"},
                {"range": [25,45], "color":"#ffeccc"},
                {"range": [45,55], "color":"#eeeeee"},
                {"range": [55,75], "color":"#e0f5e0"},
                {"range": [75,100],"color":"#d1f0ff"},
            ],
            "threshold": {"line": {"width": 2}, "thickness": 0.75, "value": crowd_idx}
        }
    ))
    gauge.update_layout(height=330, margin=dict(l=10,r=10,t=40,b=10))
    st.plotly_chart(gauge, use_container_width=True)

    st.write("**Компоненты индекса:**")
    st.write("- Fear & Greed")
    st.write("- Binance L/S Ratio")
    st.write("- Funding Rate")
    st.write("- Доходность 24ч")
    st.write("- Тональность новостей (VADER)")
    st.write("- Google Trends (интерес)")

st.markdown("---")
b1, b2 = st.columns(2)

with b1:
    # Скользящая 24h доходность
    if not df.empty:
        try:
            tf_minutes = {"1m":1, "5m":5, "15m":15}[TIMEFRAME]
            lookback = (60//tf_minutes) * 24
            if len(df) >= lookback + 1:
                df2 = df.copy()
                df2["ret24h"] = df2["close"].pct_change(lookback)
                fig_ret = px.line(df2.tail(lookback*2), x="time", y="ret24h", title="24h доходность (скользящее окно)")
                fig_ret.update_layout(height=300, margin=dict(l=10,r=10,t=40,b=10))
                st.plotly_chart(fig_ret, use_container_width=True)
            else:
                st.write("Недостаточно свечей для скользящей 24h доходности.")
        except Exception:
            st.write("Ошибка расчёта доходности.")

with b2:
    st.write("**Сводка:**")
    st.write(f"- Свечей загружено: {len(df)} ({TIMEFRAME})")
    if 'lsr_df' in locals() and not lsr_df.empty:
        st.write(f"- L/S точек: {len(lsr_df)}")
    if 'fr_df' in locals() and not fr_df.empty:
        st.write(f"- Funding записей: {len(fr_df)}")
    if 'news' in locals():
        st.write(f"- Новостей в ленте: {len(news)}")

st.caption("⚠️ Аналитический дашборд. Не является финансовой рекомендацией.")
