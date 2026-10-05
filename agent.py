"""
Amir Mohammad — Stock Alert Agent
GitHub: github.com/mamir08us/stock-alert-agent
Telegram: @AmirStockAnalysis

Config-driven — edit config.json to change models, limits, thresholds.
Never edit this file for routine changes.
"""

import os
import json
import requests
import time
import re
from datetime import datetime, date
from google import genai

# ─── ENV VARS ─────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
FINNHUB_API_KEY  = os.environ["FINNHUB_API_KEY"]
FMP_API_KEY      = os.environ["FMP_API_KEY"]

client = genai.Client(api_key=GEMINI_API_KEY)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

def load_config():
    """
    Load config.json. All agent behaviour is controlled here —
    Gemini model names, thresholds, limits, schedule hours.
    Falls back to safe defaults if file is missing.
    """
    defaults = {
        "gemini_models":           ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-flash-latest"],
        "telegram_max":            None,
        "gemini_max":              None,
        "morning_brief_hour_utc":  [11, 12, 13],
        "market_hours_utc":        [14, 21],
        "etf_alert_threshold_pct": 2.0,
        "move_alert_min_pct":      3.0,
        "dip_min_upside_pct":      20.0,
        "sec_forms":               ["8-K", "10-Q", "10-K", "S-1", "DEF 14A"],
        "price_sleep_sec":         0.3,
        "gemini_sleep_sec":        5,
    }
    try:
        with open("config.json") as f:
            loaded = json.load(f)
            defaults.update(loaded)
            print(f"Config loaded — Gemini models: {defaults['gemini_models']}")
    except FileNotFoundError:
        print("config.json not found — using defaults")
    except Exception as e:
        print(f"config.json error: {e} — using defaults")
    return defaults

CONFIG = load_config()

# ─── DYNAMIC LIMITS ───────────────────────────────────────────────────────────

def calc_limits(num_stocks):
    """
    If not set in config.json, auto-calculate:
      Telegram: 1 per 5 stocks (min 3, max 10)
      Gemini:   3 per stock    (min 15, max 50)
    """
    tg  = CONFIG.get("telegram_max")
    gem = CONFIG.get("gemini_max")
    if tg  is None: tg  = max(3,  min(10, num_stocks // 5))
    if gem is None: gem = max(15, min(50, num_stocks * 3))
    return tg, gem

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        if len(message) > 4000:
            message = message[:3997] + "..."
        requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ─── FILE HELPERS ─────────────────────────────────────────────────────────────

def load_watchlist():
    with open("watchlist.txt") as f:
        return [line.strip() for line in f if line.strip()]

def load_seen():
    try:
        with open("seen_filings.json") as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open("seen_filings.json", "w") as f:
        json.dump(list(seen), f)

# ─── FINNHUB — LIVE PRICE ─────────────────────────────────────────────────────

def get_price(ticker):
    try:
        url  = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_API_KEY}"
        r    = requests.get(url, timeout=10)
        d    = r.json()
        curr = d.get("c")
        prev = d.get("pc")
        if curr and prev and prev > 0:
            pct = ((curr - prev) / prev) * 100
            return round(curr, 2), round(pct, 2)
    except Exception as e:
        print(f"Price error {ticker}: {e}")
    return None, None

# ─── FINNHUB — BETA ───────────────────────────────────────────────────────────

_beta_cache = {}

def get_beta(ticker):
    if ticker in _beta_cache:
        return _beta_cache[ticker]
    try:
        url  = f"https://finnhub.io/api/v1/stock/metric?symbol={ticker}&metric=all&token={FINNHUB_API_KEY}"
        r    = requests.get(url, timeout=10)
        beta = r.json().get("metric", {}).get("beta")
        _beta_cache[ticker] = beta
        return beta
    except:
        return None

def dynamic_threshold(beta):
    if beta is None: return CONFIG["move_alert_min_pct"]
    if beta > 2.0:   return 5.0
    if beta > 1.5:   return 4.0
    if beta > 1.0:   return 3.0
    return 2.0

def beta_plain_english(beta):
    if beta is None:
        return "normal stock — standard alert level used"
    if beta > 2.0:
        return f"very wild stock (volatility score {beta:.1f}) — only big moves worth alerting"
    if beta > 1.5:
        return f"active stock (volatility score {beta:.1f}) — today's move is bigger than usual"
    if beta > 1.0:
        return f"slightly active stock (volatility score {beta:.1f}) — worth watching"
    return f"calm stable stock (volatility score {beta:.1f}) — even small moves matter here"

# ─── FINNHUB — ETF DETECTION ──────────────────────────────────────────────────

_etf_cache = {}

def is_etf(ticker):
    if ticker in _etf_cache:
        return _etf_cache[ticker]
    try:
        url      = f"https://finnhub.io/api/v1/stock/profile2?symbol={ticker}&token={FINNHUB_API_KEY}"
        r        = requests.get(url, timeout=8)
        industry = r.json().get("finnhubIndustry", "")
        result   = not industry or industry == ""
        _etf_cache[ticker] = result
        return result
    except:
        _etf_cache[ticker] = False
        return False

# ─── FMP — ANALYST TARGETS ────────────────────────────────────────────────────

_target_cache = {}

def get_analyst_target(ticker):
    if ticker in _target_cache:
        return _target_cache[ticker]
    try:
        url = f"https://financialmodelingprep.com/api/v4/price-target?symbol={ticker}&apikey={FMP_API_KEY}"
        r   = requests.get(url, timeout=10)
        d   = r.json()
        result = None
        if isinstance(d, list) and len(d) > 0:
            targets = [item.get("priceTarget") for item in d[:10] if item.get("priceTarget")]
            if targets:
                result = {
                    "consensus": round(sum(targets) / len(targets), 2),
                    "high":      round(max(targets), 2),
                    "low":       round(min(targets), 2),
                    "median":    round(sorted(targets)[len(targets) // 2], 2),
                }
        if not result:
            print(f"FMP no target for {ticker}")
        else:
            print(f"FMP target for {ticker}: ${result['consensus']}")
        _target_cache[ticker] = result
        return result
    except Exception as e:
        print(f"FMP target error {ticker}: {e}")
        return None

def get_analyst_recommendation(ticker):
    try:
        url = (
            f"https://financialmodelingprep.com/api/v3/analyst-stock-recommendations/"
            f"{ticker}?limit=1&apikey={FMP_API_KEY}"
        )
        r = requests.get(url, timeout=10)
        d = r.json()
        if isinstance(d, list) and len(d) > 0:
            l           = d[0]
            strong_buy  = l.get("analystRatingsStrongBuy", 0)
            buy         = l.get("analystRatingsbuy", 0)
            hold        = l.get("analystRatingsHold", 0)
            sell        = l.get("analystRatingsSell", 0)
            strong_sell = l.get("analystRatingsStrongSell", 0)
            total = strong_buy + buy + hold + sell + strong_sell
            if total > 0:
                buy_pct  = (strong_buy + buy) / total * 100
                sell_pct = (sell + strong_sell) / total * 100
                if strong_buy / total * 100 >= 50: rec = "STRONG BUY"
                elif buy_pct >= 60:                rec = "BUY"
                elif sell_pct >= 40:               rec = "SELL"
                else:                              rec = "HOLD"
                return rec, int(buy_pct), int(sell_pct), total
    except Exception as e:
        print(f"FMP rec error {ticker}: {e}")
    return None, 0, 0, 0

def rec_plain_english(rec, buy_pct, sell_pct, total):
    if not rec:
        return "No expert opinion available."
    if rec == "STRONG BUY":
        return f"✅ {buy_pct}% of {total} Wall Street experts say BUY — very strong confidence."
    if rec == "BUY":
        return f"✅ {buy_pct}% of {total} experts say BUY — generally positive outlook."
    if rec == "HOLD":
        return f"⚠️ Experts are mixed — {buy_pct}% say buy, {sell_pct}% say sell. No clear signal."
    return f"🔴 {sell_pct}% of {total} experts say SELL — proceed with caution."

def upside_plain_english(price, consensus, hi_target):
    if not consensus:
        return ""
    upside = ((consensus - price) / price) * 100
    if price >= consensus:
        remaining = ((hi_target - price) / price * 100) if hi_target else 0
        return (
            f"📊 Stock reached the average expert price target of ${consensus:.2f}. "
            f"Still {remaining:.1f}% room to the most optimistic expert target of ${hi_target:.2f}."
        )
    if upside > 30:
        return (
            f"📊 Experts think this stock is worth ${consensus:.2f} — "
            f"that is {upside:.1f}% MORE than today's price. Big potential."
        )
    if upside > 15:
        return (
            f"📊 Experts think this stock is worth ${consensus:.2f} — "
            f"that is {upside:.1f}% more than today. Good potential."
        )
    return f"📊 Experts think this stock is worth ${consensus:.2f} — that is {upside:.1f}% more than today."

# ─── GEMINI — CONFIG-DRIVEN MODEL LIST ────────────────────────────────────────

def ask_gemini(prompt, gemini_calls, max_gemini):
    """
    Try Gemini models in the order defined in config.json['gemini_models'].
    To update models: edit config.json — do NOT touch this function.
    Falls back to smart_analysis if all models fail.
    """
    if gemini_calls[0] >= max_gemini:
        return None

    models_to_try = CONFIG.get("gemini_models", ["gemini-flash-latest"])
    sleep_sec     = CONFIG.get("gemini_sleep_sec", 5)

    gemini_calls[0] += 1
    print(f"Gemini call #{gemini_calls[0]}/{max_gemini} — trying: {models_to_try}")

    time.sleep(sleep_sec)

    for model_name in models_to_try:
        try:
            # Use Chat.send_message to avoid AFC warning
            chat     = client.chats.create(model=model_name)
            response = chat.send_message(prompt)
            result   = response.text.strip()
            # Strip markdown artifacts
            result   = re.sub(r"\*{1,3}|#{1,3}", "", result)
            result   = re.sub(r"\s{2,}", " ", result).strip()
            print(f"Gemini OK — model={model_name} ({len(result)} chars)")
            return result
        except Exception as e:
            err = str(e)[:80]
            print(f"Model {model_name} failed: {err} — trying next")
            time.sleep(2)
            continue

    print("Gemini FAILED: all models failed — falling back to smart_analysis")
    return None


# ─── SMART FALLBACK — NO API NEEDED ───────────────────────────────────────────

def smart_analysis(ticker, price, pct, signal_type,
                   consensus=None, upside=None, rec=None, beta=None):
    """
    Context-aware fallback analysis. Called when Gemini fails or limit reached.
    Never returns empty — always gives a clear action.
    """
    direction = "up" if pct and pct > 0 else "down"
    abs_pct   = abs(pct) if pct else 0

    if signal_type == "AT_TARGET":
        if rec in ("STRONG BUY", "BUY"):
            return (
                f"{ticker} just hit the average price target set by Wall Street analysts. "
                f"With {rec} consensus still intact, experts still see upside from here. "
                f"Consider holding rather than selling immediately."
            )
        return (
            f"{ticker} reached its analyst price target — good time to review your position. "
            f"Consider taking some profit here as the stock has delivered what analysts expected."
        )

    if signal_type == "DIP":
        if upside and upside > 30 and rec in ("STRONG BUY", "BUY"):
            return (
                f"{ticker} dropped {abs_pct:.1f}% today but analysts still see "
                f"+{upside:.1f}% upside to their target. With {rec} consensus "
                f"this looks like a genuine buying opportunity. "
                f"Consider adding to your position if you have spare cash."
            )
        return (
            f"{ticker} is down {abs_pct:.1f}% with {upside:.1f}% upside to analyst target. "
            f"Let the dust settle 24-48 hours before acting. "
            f"Wait to see if the stock stabilizes before adding more."
        )

    if signal_type == "MOVE":
        if direction == "down":
            if abs_pct >= 5:
                advice = (
                    f"A drop of {abs_pct:.1f}% feels scary but do NOT panic sell. "
                    f"Selling on a red day turns a temporary paper loss into a permanent real loss. "
                    f"Hold your position and wait 24-48 hours for the market to stabilize."
                )
            else:
                advice = (
                    f"A {abs_pct:.1f}% drop is within normal market turbulence. "
                    f"Stay calm, hold your position, and do not make any rushed decisions today."
                )
            if consensus and upside and upside > 0:
                advice += f" Analysts still target ${consensus:.2f} — {upside:.1f}% above today's price."
        else:
            if abs_pct >= 5:
                advice = (
                    f"Strong {abs_pct:.1f}% gain today — do NOT chase by buying at today's high. "
                    f"Wait for a small pullback before adding more."
                )
            else:
                advice = (
                    f"Nice {abs_pct:.1f}% gain — hold and let it run. "
                    f"No action needed unless this crosses your personal sell target."
                )
            if consensus and price >= consensus * 0.95:
                advice += f" Stock is near analyst target of ${consensus:.2f} — consider taking some profit."
        return advice

    return "Hold your current position and monitor the situation."

# ─── DEDUP ────────────────────────────────────────────────────────────────────

_alerted_this_run = set()

def already_alerted(ticker, signal):
    key = f"{ticker}:{signal}"
    if key in _alerted_this_run:
        return True
    _alerted_this_run.add(key)
    return False

# ─── ALERT LOGIC ──────────────────────────────────────────────────────────────

def check_all_alerts(ticker, price, pct, gemini_calls, max_gemini):
    alerts    = []
    if not price:
        return alerts

    beta      = get_beta(ticker)
    threshold = dynamic_threshold(beta)
    beta_exp  = beta_plain_english(beta)
    target    = get_analyst_target(ticker)
    time.sleep(0.2)

    # ── Signal 1: AT ANALYST TARGET ───────────────────────────────────────────
    if target and target.get("consensus"):
        consensus = target["consensus"]
        hi_target = target.get("high")
        upside    = ((consensus - price) / price) * 100

        if price >= consensus and not already_alerted(ticker, "AT_TARGET"):
            rec, buy_pct, sell_pct, total = get_analyst_recommendation(ticker)
            remaining = round((hi_target - price) / price * 100, 1) if hi_target else 0

            analysis = ask_gemini(
                f"Stock: {ticker}. Current price: ${price:.2f}. "
                f"Just reached analyst consensus target of ${consensus:.2f}. "
                f"{buy_pct}% of {total} Wall Street analysts say BUY. "
                f"Remaining upside to highest analyst target: +{remaining:.1f}%. "
                f"Today's move: {pct:+.1f}%. "
                f"Answer these 3 things in simple language a beginner investor understands: "
                f"1) Why is this significant? "
                f"2) Should I sell now, hold for more gains, or wait for a pullback? "
                f"3) What is the ONE specific thing I should do today?",
                gemini_calls, max_gemini
            ) or smart_analysis(ticker, price, pct, "AT_TARGET",
                                 consensus=consensus, rec=rec)

            alerts.append(
                f"🎯 <b>TARGET REACHED — {ticker}</b>\n\n"
                f"<b>What happened:</b>\n"
                f"Stock hit ${price:.2f} — the price Wall Street experts predicted.\n\n"
                f"{upside_plain_english(price, consensus, hi_target)}\n\n"
                f"<b>Expert opinion:</b>\n"
                f"{rec_plain_english(rec, buy_pct, sell_pct, total)}\n\n"
                f"<b>📱 Analysis & advice:</b>\n{analysis}"
            )

        # ── Signal 2: DIP OPPORTUNITY ──────────────────────────────────────────
        elif upside >= CONFIG["dip_min_upside_pct"] and pct and pct <= -(threshold / 2):
            if not already_alerted(ticker, "DIP"):
                rec, buy_pct, sell_pct, total = get_analyst_recommendation(ticker)

                analysis = ask_gemini(
                    f"{ticker} dropped {abs(pct):.1f}% to ${price:.2f}. "
                    f"Analyst target ${consensus:.2f} = {upside:.1f}% upside. "
                    f"{buy_pct}% of {total} say BUY. "
                    f"3 sentences, plain text, no markdown: "
                    f"1) Buy dip or falling knife? "
                    f"2) Why is it dropping? "
                    f"3) One specific action now.",
                    gemini_calls, max_gemini
                ) or smart_analysis(ticker, price, pct, "DIP",
                                     consensus=consensus, upside=upside, rec=rec)

                alerts.append(
                    f"💰 <b>DIP OPPORTUNITY — {ticker}</b>\n\n"
                    f"<b>What happened:</b>\n"
                    f"Stock dropped {abs(pct):.1f}% today to ${price:.2f}.\n\n"
                    f"{upside_plain_english(price, consensus, hi_target)}\n\n"
                    f"<b>Expert opinion:</b>\n"
                    f"{rec_plain_english(rec, buy_pct, sell_pct, total)}\n\n"
                    f"<b>📱 Analysis & advice:</b>\n{analysis}"
                )

    # ── Signal 3: UNUSUAL MOVE ────────────────────────────────────────────────
    if abs(pct) >= threshold and not alerts:
        if not already_alerted(ticker, f"MOVE:{pct:.1f}"):
            direction  = "gone UP" if pct > 0 else "gone DOWN"
            icon       = "🟢" if pct > 0 else "🔴"
            target_str = ""
            rec_str    = ""
            upside_val = None
            consensus  = None

            if target and target.get("consensus"):
                consensus  = target["consensus"]
                upside_val = ((consensus - price) / price) * 100
                target_str = upside_plain_english(price, consensus, target.get("high"))
                rec, buy_pct, sell_pct, total = get_analyst_recommendation(ticker)
                rec_str = rec_plain_english(rec, buy_pct, sell_pct, total)
            else:
                rec, buy_pct, sell_pct, total = None, 0, 0, 0

            analysis = ask_gemini(
                f"Stock {ticker} moved {pct:+.1f}% today to ${price:.2f}. {beta_exp}. "
                f"{target_str} "
                f"Write 3 SHORT plain text sentences, no markdown, no asterisks, no headers: "
                f"Sentence 1: Most likely reason for this move. "
                f"Sentence 2: Should investor panic or stay calm and why. "
                f"Sentence 3: One specific action — hold, buy more, or sell.",
                gemini_calls, max_gemini
            ) or smart_analysis(ticker, price, pct, "MOVE",
                                 consensus=consensus, upside=upside_val,
                                 rec=rec, beta=beta)

            msg = (
                f"{icon} <b>UNUSUAL MOVE — {ticker}</b>\n\n"
                f"<b>What happened:</b>\n"
                f"Stock has {direction} {abs(pct):.1f}% today to ${price:.2f}.\n"
                f"This is bigger than its normal daily movement.\n"
                f"<i>({beta_exp})</i>\n\n"
            )
            if target_str: msg += f"{target_str}\n\n"
            if rec_str:    msg += f"<b>Expert opinion:</b>\n{rec_str}\n\n"
            msg += f"<b>📱 Analysis & advice:</b>\n{analysis}"
            alerts.append(msg)

    return alerts

# ─── SEC EDGAR ────────────────────────────────────────────────────────────────

SEC_UA     = "Amir Mohammad mamir08@gmail.com"
_cik_cache = {}

def load_cik_map():
    global _cik_cache
    if _cik_cache:
        return _cik_cache
    try:
        url = "https://www.sec.gov/files/company_tickers.json"
        r   = requests.get(url, headers={"User-Agent": SEC_UA}, timeout=15)
        for entry in r.json().values():
            t = entry.get("ticker", "").upper()
            if t:
                _cik_cache[t] = str(entry["cik_str"]).zfill(10)
        print(f"CIK map loaded: {len(_cik_cache)} tickers")
    except Exception as e:
        print(f"CIK map load error: {e}")
    return _cik_cache

def get_company_cik(ticker):
    return load_cik_map().get(ticker.upper())

def get_sec_filings_by_cik(cik, ticker):
    try:
        url  = f"https://data.sec.gov/submissions/CIK{cik}.json"
        r    = requests.get(url, headers={"User-Agent": SEC_UA}, timeout=15)
        data = r.json()
        recent     = data.get("filings", {}).get("recent", {})
        forms      = recent.get("form", [])
        dates      = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        docs       = recent.get("primaryDocument", [])
        today      = date.today().isoformat()
        watched    = set(CONFIG.get("sec_forms", ["8-K", "10-Q", "10-K", "S-1", "DEF 14A"]))
        results    = []
        for form, filing_date, accession, doc in zip(forms, dates, accessions, docs):
            if filing_date != today:
                continue
            if form not in watched:
                continue
            cik_int   = int(cik)
            acc_clean = accession.replace("-", "")
            file_url  = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{acc_clean}/{doc}"
            )
            results.append({
                "form":      form,
                "date":      filing_date,
                "accession": accession,
                "url":       file_url,
                "company":   data.get("name", ticker),
                "cik_int":   cik_int,
            })
            if len(results) >= 2:
                break
        print(f"{ticker} (CIK {cik}): {len(results)} filings today")
        return results
    except Exception as e:
        print(f"SEC filing error {ticker}: {e}")
        return []

def get_filing_text(file_url):
    try:
        r    = requests.get(file_url, headers={"User-Agent": SEC_UA}, timeout=15)
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[500:4000] if len(text) > 500 else text
    except Exception as e:
        print(f"Filing text error: {e}")
        return ""

def edgar_link(form_type, cik_int):
    return (
        f"https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcompany&CIK={cik_int}&type={form_type}"
        f"&dateb=&owner=include&count=5"
    )

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    tickers  = load_watchlist()
    seen     = load_seen()
    new_seen = set(seen)
    now      = datetime.utcnow()
    hour     = now.hour
    sleep    = CONFIG.get("price_sleep_sec", 0.3)

    print("Detecting ETFs via Finnhub...")
    etf_set = set()
    for ticker in tickers:
        if is_etf(ticker):
            etf_set.add(ticker)
        time.sleep(sleep)
    print(f"ETFs: {etf_set}")

    num_stocks               = len([t for t in tickers if t not in etf_set])
    max_telegram, max_gemini = calc_limits(num_stocks)
    telegram_sent            = 0
    gemini_calls             = [0]

    print(
        f"Run at {now} UTC | {len(tickers)} tickers "
        f"({num_stocks} stocks / {len(etf_set)} ETFs) | "
        f"Telegram={max_telegram} Gemini={max_gemini}"
    )

    morning_hours = CONFIG.get("morning_brief_hour_utc", [11, 12, 13])
    mh_start, mh_end = CONFIG.get("market_hours_utc", [14, 21])

    # ── MORNING BRIEF ─────────────────────────────────────────────────────────
    if hour in morning_hours:
        brief_key = f"brief_{now.strftime('%Y-%m-%d')}"
        if brief_key in seen:
            print(f"Morning brief already sent today — skipping")
            save_seen(new_seen)
            return

        movers = []
        for ticker in tickers:
            price, pct = get_price(ticker)
            if price and pct and abs(pct) >= CONFIG["etf_alert_threshold_pct"]:
                icon      = "🟢" if pct > 0 else "🔴"
                direction = "UP" if pct > 0 else "DOWN"
                movers.append(
                    f"{icon} <b>{ticker}</b> {direction} "
                    f"{abs(pct):.1f}% overnight · now ${price:.2f}"
                )
            time.sleep(sleep)

        msg  = f"🌅 <b>Good morning! — {now.strftime('%a %b %d')}</b>\n\n"
        msg += f"👀 Watching {len(tickers)} stocks for you today.\n"
        msg += f"🔔 Alerts fire when anything moves unusually.\n"
        msg += f"🤖 Every alert includes AI analysis + clear action advice.\n\n"
        if movers:
            msg += "📊 <b>Moved overnight (≥2%):</b>\n" + "\n".join(movers[:10])
            msg += "\n\n<i>These moved while market was closed.</i>"
        else:
            msg += "😴 All quiet overnight — no big moves while you slept."
        msg += "\n\n<i>Checking every 15 min — 9:30am to 4pm ET.</i>"
        send_telegram(msg)
        new_seen.add(brief_key)
        save_seen(new_seen)
        return

    # ── MARKET HOURS ──────────────────────────────────────────────────────────
    if mh_start <= hour <= mh_end:
        print("Market hours — checking all stocks")
        for ticker in tickers:
            if telegram_sent >= max_telegram:
                break
            price, pct = get_price(ticker)
            if not price:
                time.sleep(sleep)
                continue
            alerts = check_all_alerts(ticker, price, pct, gemini_calls, max_gemini)
            for alert in alerts:
                if telegram_sent >= max_telegram:
                    break
                send_telegram(alert)
                telegram_sent += 1
                time.sleep(1)
            time.sleep(sleep)
    else:
        print(f"Outside market hours (hour={hour} UTC)")

    # ── SEC FILING CHECK ───────────────────────────────────────────────────────
    load_cik_map()

    form_plain_map = {
        "8-K":     "Important company announcement",
        "10-Q":    "Quarterly earnings report",
        "10-K":    "Annual earnings report",
        "S-1":     "Company going public",
        "DEF 14A": "Shareholder vote coming up",
    }

    for ticker in tickers:
        if telegram_sent >= max_telegram:
            break
        if gemini_calls[0] >= max_gemini:
            break
        if ticker in etf_set:
            continue

        cik = get_company_cik(ticker)
        if not cik:
            continue

        filings = get_sec_filings_by_cik(cik, ticker)

        for filing in filings:
            if telegram_sent >= max_telegram:
                break
            filing_id = filing["accession"]
            if filing_id in seen:
                continue

            new_seen.add(filing_id)
            company     = filing["company"]
            form_type   = filing["form"]
            cik_int     = filing["cik_int"]
            form_plain  = form_plain_map.get(form_type, form_type)
            filing_text = get_filing_text(filing["url"])

            analysis = ask_gemini(
                f"{company} ({ticker}) filed a {form_type}. "
                f"Filing content: {filing_text[:400]}. "
                f"Write 3 SHORT plain text sentences, no markdown, no asterisks: "
                f"Sentence 1: What happened in simple words. "
                f"Sentence 2: Is this good or bad news for the stock price? "
                f"Sentence 3: Should investor buy more, sell, or hold right now?",
                gemini_calls, max_gemini
            )

            if analysis:
                msg = (
                    f"📋 <b>COMPANY NEWS — {ticker}</b>\n\n"
                    f"<b>Type of news:</b> {form_plain}\n"
                    f"<b>Company:</b> {company}\n"
                    f"<b>Date:</b> {filing['date']}\n\n"
                    f"<b>📱 Analysis & what to do:</b>\n{analysis}"
                )
            else:
                link = edgar_link(form_type, cik_int)
                msg  = (
                    f"📋 <b>COMPANY NEWS — {ticker}</b>\n\n"
                    f"{company} filed a {form_plain}.\n"
                    f"Date: {filing['date']}\n\n"
                    f"<a href='{link}'>Read the full filing →</a>"
                )

            send_telegram(msg)
            telegram_sent += 1
            time.sleep(2)

        time.sleep(0.5)

    save_seen(new_seen)
    print(
        f"Done. Telegram: {telegram_sent}/{max_telegram} | "
        f"Gemini: {gemini_calls[0]}/{max_gemini}"
    )

if __name__ == "__main__":
    main()
