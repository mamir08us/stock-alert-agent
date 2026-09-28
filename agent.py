import os
import json
import requests
import time
import re
from datetime import datetime, date
from google import genai

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
FINNHUB_API_KEY  = os.environ["FINNHUB_API_KEY"]
FMP_API_KEY      = os.environ["FMP_API_KEY"]

client = genai.Client(api_key=GEMINI_API_KEY)

# ─── DYNAMIC LIMITS ───────────────────────────────────────────────────────────

def calc_limits(num_stocks):
    telegram = max(3, min(8, num_stocks // 6))
    gemini   = max(5, min(20, num_stocks // 3))
    return telegram, gemini

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        if len(message) > 4000:
            message = message[:3997] + "..."
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
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
    """Live price + daily % change from Finnhub"""
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_API_KEY}"
        r   = requests.get(url, timeout=10)
        d   = r.json()
        curr = d.get("c")
        prev = d.get("pc")
        if curr and prev and prev > 0:
            pct = ((curr - prev) / prev) * 100
            return round(curr, 2), round(pct, 2)
    except Exception as e:
        print(f"Price error {ticker}: {e}")
    return None, None

# ─── FINNHUB — BETA FOR DYNAMIC THRESHOLD ─────────────────────────────────────

def get_beta(ticker):
    """Get stock beta from Finnhub — used to set dynamic alert threshold"""
    try:
        url = f"https://finnhub.io/api/v1/stock/metric?symbol={ticker}&metric=all&token={FINNHUB_API_KEY}"
        r   = requests.get(url, timeout=10)
        d   = r.json()
        return d.get("metric", {}).get("beta")
    except:
        return None

def dynamic_threshold(beta):
    """
    Alert threshold based on live beta — no hardcoded values.
    High beta = volatile stock = needs bigger move to alert.
    Low beta  = stable stock  = smaller move worth alerting.
    """
    if beta is None:
        return 3.0
    if beta > 2.0:  return 5.0   # e.g. quantum stocks, SOUN
    if beta > 1.5:  return 4.0   # e.g. NVDA, META
    if beta > 1.0:  return 3.0   # e.g. AMZN, GOOGL
    return 2.0                   # e.g. stable blue chips

# ─── FINNHUB — ETF DETECTION ──────────────────────────────────────────────────

_etf_cache = {}

def is_etf(ticker):
    """
    Detect ETFs dynamically via Finnhub profile.
    No hardcoded ETF list — auto detects any ETF added to watchlist.
    """
    if ticker in _etf_cache:
        return _etf_cache[ticker]
    try:
        url = f"https://finnhub.io/api/v1/stock/profile2?symbol={ticker}&token={FINNHUB_API_KEY}"
        r   = requests.get(url, timeout=8)
        d   = r.json()
        # ETFs have no finnhubIndustry or empty string
        industry = d.get("finnhubIndustry", "")
        result   = not industry or industry == ""
        _etf_cache[ticker] = result
        return result
    except:
        _etf_cache[ticker] = False
        return False

# ─── FMP — ANALYST TARGETS ────────────────────────────────────────────────────

_target_cache = {}

def get_analyst_target(ticker):
    """
    Live analyst consensus price target from FMP free tier.
    Cached per run — one API call per ticker.
    250 free calls/day — sufficient for watchlist.
    """
    if ticker in _target_cache:
        return _target_cache[ticker]
    try:
        url = (
            f"https://financialmodelingprep.com/api/v4/price-target-consensus"
            f"?symbol={ticker}&apikey={FMP_API_KEY}"
        )
        r      = requests.get(url, timeout=10)
        d      = r.json()
        result = None
        data   = d[0] if isinstance(d, list) and len(d) > 0 else d if isinstance(d, dict) else None
        if data and data.get("targetConsensus"):
            result = {
                "consensus": round(data["targetConsensus"], 2),
                "high":      round(data["targetHigh"], 2) if data.get("targetHigh") else None,
                "low":       round(data["targetLow"], 2) if data.get("targetLow") else None,
                "median":    round(data["targetMedian"], 2) if data.get("targetMedian") else None,
            }
        else:
            print(f"FMP no target for {ticker}: {str(d)[:60]}")
        _target_cache[ticker] = result
        return result
    except Exception as e:
        print(f"FMP target error {ticker}: {e}")
        return None

def get_analyst_recommendation(ticker):
    """Live analyst buy/hold/sell counts from FMP"""
    try:
        url = (
            f"https://financialmodelingprep.com/api/v3/analyst-stock-recommendations"
            f"/{ticker}?limit=1&apikey={FMP_API_KEY}"
        )
        r = requests.get(url, timeout=10)
        d = r.json()
        if isinstance(d, list) and len(d) > 0:
            latest      = d[0]
            strong_buy  = latest.get("analystRatingsStrongBuy", 0)
            buy         = latest.get("analystRatingsbuy", 0)
            hold        = latest.get("analystRatingsHold", 0)
            sell        = latest.get("analystRatingsSell", 0)
            strong_sell = latest.get("analystRatingsStrongSell", 0)
            total = strong_buy + buy + hold + sell + strong_sell
            if total > 0:
                buy_pct  = (strong_buy + buy) / total * 100
                sell_pct = (sell + strong_sell) / total * 100
                if strong_buy / total * 100 >= 50:
                    rec = "STRONG BUY"
                elif buy_pct >= 60:
                    rec = "BUY"
                elif sell_pct >= 40:
                    rec = "SELL"
                else:
                    rec = "HOLD"
                return rec, f"{int(buy_pct)}% BUY | {int(sell_pct)}% SELL | {total} analysts"
    except Exception as e:
        print(f"FMP rec error {ticker}: {e}")
    return None, None

# ─── GEMINI — INSTANT ANALYSIS ────────────────────────────────────────────────

def gemini_analysis(prompt, gemini_calls, max_gemini):
    """
    Use Gemini for ALL AI analysis — price moves + SEC filings.
    Free tier: 1,500 calls/day — more than enough.
    Single AI — clean and simple.
    """
    if gemini_calls[0] >= max_gemini:
        print("Gemini limit reached")
        return None
    try:
        gemini_calls[0] += 1
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        print(f"Gemini error: {e}")
        return None

# ─── DYNAMIC ALERT LOGIC ──────────────────────────────────────────────────────

def check_all_alerts(ticker, price, pct, gemini_calls, max_gemini):
    """
    Fully dynamic alerts — zero hardcoded values.

    Sources:
    - Finnhub: live price, % change, beta
    - FMP: live analyst consensus target + recommendation
    - Gemini: AI analysis of every alert

    Three signal types:
    1. Price at/above analyst consensus → potential sell
    2. Price well below consensus + dropping → dip opportunity
    3. Price moved beyond dynamic beta threshold → move alert
    """
    alerts = []
    if not price:
        return alerts

    # Get beta for dynamic threshold
    beta      = get_beta(ticker)
    threshold = dynamic_threshold(beta)
    beta_str  = f"{beta:.1f}" if beta else "N/A"

    # Get live analyst data from FMP
    target = get_analyst_target(ticker)
    time.sleep(0.2)

    # ── SIGNAL 1: At/above analyst consensus ──────────────────────────────────
    if target and target.get("consensus"):
        consensus = target["consensus"]
        hi_target = target.get("high")
        upside    = ((consensus - price) / price) * 100

        if price >= consensus:
            rec, rec_detail = get_analyst_recommendation(ticker)
            remaining = round((hi_target - price) / price * 100, 1) if hi_target else 0

            analysis = gemini_analysis(
                f"{ticker} at ${price:.2f} has reached its analyst consensus target of "
                f"${consensus:.2f}. Analyst consensus: {rec}. {rec_detail}. "
                f"Upside remaining to analyst high: +{remaining:.1f}%. "
                f"Beta: {beta_str}. "
                f"In 2-3 sentences: should the investor take profit, hold for the high target, "
                f"or wait for pullback? Be specific and direct.",
                gemini_calls, max_gemini
            )
            msg = (
                f"🎯 <b>AT ANALYST TARGET — {ticker}</b>\n"
                f"Price ${price:.2f} ≥ consensus ${consensus:.2f}\n"
                f"Upside to analyst high: +{remaining:.1f}%\n"
                f"Consensus: <b>{rec or 'N/A'}</b> | {rec_detail or 'N/A'}\n"
            )
            if analysis:
                msg += f"\n🤖 <b>Gemini:</b> {analysis}"
            alerts.append(msg)

        # ── SIGNAL 2: Dip opportunity ──────────────────────────────────────────
        elif upside >= 20 and pct and pct <= -(threshold / 2):
            rec, rec_detail = get_analyst_recommendation(ticker)

            analysis = gemini_analysis(
                f"{ticker} dropped {pct:.1f}% today to ${price:.2f}. "
                f"Analyst consensus target is ${consensus:.2f} implying +{upside:.1f}% upside. "
                f"Consensus: {rec}. {rec_detail}. Beta: {beta_str}. "
                f"In 2-3 sentences: is this a real dip to buy or a falling knife? "
                f"What should the investor do? Be specific.",
                gemini_calls, max_gemini
            )
            msg = (
                f"💰 <b>DIP OPPORTUNITY — {ticker}</b>\n"
                f"Price ${price:.2f} | Consensus ${consensus:.2f}\n"
                f"Implied upside: <b>+{upside:.1f}%</b>\n"
                f"Consensus: <b>{rec or 'N/A'}</b> | {rec_detail or 'N/A'}\n"
            )
            if analysis:
                msg += f"\n🤖 <b>Gemini:</b> {analysis}"
            alerts.append(msg)

    # ── SIGNAL 3: Dynamic threshold move alert ────────────────────────────────
    if abs(pct) >= threshold and not alerts:
        icon      = "🟢" if pct > 0 else "🔴"
        direction = "surged" if pct > 0 else "dropped"
        consensus_line = ""
        upside_str     = ""
        if target and target.get("consensus"):
            upside        = ((target["consensus"] - price) / price) * 100
            upside_str    = f"+{upside:.1f}% to target"
            consensus_line = f"Analyst consensus: ${target['consensus']:.2f} ({upside_str})\n"

        analysis = gemini_analysis(
            f"{ticker} {direction} {abs(pct):.1f}% today to ${price:.2f}. "
            f"Beta: {beta_str} — alert threshold was {threshold:.1f}%. "
            f"{consensus_line}"
            f"In 2-3 sentences: what likely caused this move and what should "
            f"the investor do? Be specific and direct.",
            gemini_calls, max_gemini
        )
        msg = (
            f"{icon} <b>MOVE ALERT — {ticker}</b>\n"
            f"Stock {direction} {pct:+.1f}% · ${price:.2f}\n"
            f"Dynamic threshold: {threshold:.1f}% (beta={beta_str})\n"
            f"{consensus_line}"
        )
        if analysis:
            msg += f"\n🤖 <b>Gemini:</b> {analysis}"
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
        results    = []
        for form, filing_date, accession, doc in zip(forms, dates, accessions, docs):
            if filing_date != today:
                continue
            if form not in ("8-K", "10-Q", "10-K", "S-1", "DEF 14A"):
                continue
            cik_int   = int(cik)
            acc_clean = accession.replace("-", "")
            file_url  = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{doc}"
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
        text = re.sub(r'<[^>]+>', ' ', r.text)
        text = re.sub(r'\s+', ' ', text).strip()
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

    # Detect ETFs dynamically — no hardcoded list
    print("Detecting ETFs via Finnhub...")
    etf_set = set()
    for ticker in tickers:
        if is_etf(ticker):
            etf_set.add(ticker)
        time.sleep(0.3)
    print(f"ETFs detected: {etf_set}")

    num_stocks             = len([t for t in tickers if t not in etf_set])
    max_telegram, max_gemini = calc_limits(num_stocks)
    telegram_sent          = 0
    gemini_calls           = [0]

    print(
        f"Run at {now} UTC | {len(tickers)} tickers "
        f"({num_stocks} stocks / {len(etf_set)} ETFs) | "
        f"Telegram={max_telegram} Gemini={max_gemini}"
    )

    # ── MORNING BRIEF — 12 UTC = 7 AM ET ──────────────────────────────────────
    if hour == 12:
        movers = []
        for ticker in tickers:
            price, pct = get_price(ticker)
            if price and pct and abs(pct) >= 2:
                icon = "🟢" if pct > 0 else "🔴"
                movers.append(f"{icon} <b>{ticker}</b> {pct:+.1f}% · ${price:.2f}")
            time.sleep(0.3)

        msg  = f"🌅 <b>Morning Brief — {now.strftime('%a %b %d')}</b>\n\n"
        msg += (
            f"📋 {len(tickers)} tickers | {num_stocks} stocks | {len(etf_set)} ETFs\n"
            f"📡 Finnhub prices · FMP analyst targets · Gemini AI\n\n"
        )
        if movers:
            msg += "📊 <b>Overnight movers (±2%+):</b>\n" + "\n".join(movers[:10])
        else:
            msg += "All quiet overnight."
        msg += "\n\n<i>SEC filings + analyst targets checked every 15 min.</i>"
        send_telegram(msg)
        save_seen(new_seen)
        return

    # ── MARKET HOURS — 14–21 UTC ───────────────────────────────────────────────
    if 14 <= hour <= 21:
        print("Market hours — live prices + analyst targets + Gemini analysis")
        for ticker in tickers:
            if telegram_sent >= max_telegram:
                break
            price, pct = get_price(ticker)
            if not price:
                time.sleep(0.3)
                continue
            alerts = check_all_alerts(ticker, price, pct, gemini_calls, max_gemini)
            for alert in alerts:
                if telegram_sent >= max_telegram:
                    break
                send_telegram(alert)
                telegram_sent += 1
                time.sleep(1)
            time.sleep(0.3)
    else:
        print(f"Outside market hours (hour={hour} UTC)")

    # ── SEC FILING CHECK ───────────────────────────────────────────────────────
    load_cik_map()

    for ticker in tickers:
        if telegram_sent >= max_telegram:
            print("Telegram limit reached")
            break
        if gemini_calls[0] >= max_gemini:
            print("Gemini limit reached")
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
            company   = filing["company"]
            form_type = filing["form"]
            cik_int   = filing["cik_int"]

            filing_text = get_filing_text(filing["url"])

            # Gemini analyzes both structure + plain English
            structured = gemini_analysis(
                f"Analyze this SEC {form_type} filing for {ticker}.\n\n"
                f"Filing:\n{filing_text[:2000]}\n\n"
                f"Respond in EXACTLY this format:\n"
                f"CLASSIFICATION: [POSITIVE / NEGATIVE / NEUTRAL]\n"
                f"REASON: [one sentence]\n"
                f"SIGNAL: [BUY / SELL / HOLD]\n"
                f"SUMMARY: [one plain English sentence]\n"
                f"IMPACT: [HIGH / MEDIUM / LOW]",
                gemini_calls, max_gemini
            )

            plain = gemini_analysis(
                f"SEC {form_type} just filed for {ticker} ({company}). "
                f"Filing summary: {filing_text[:500]}. "
                f"In 2-3 sentences: what happened and is this good or bad for the stock?",
                gemini_calls, max_gemini
            )

            if structured or plain:
                msg = (
                    f"📋 <b>NEW FILING — {ticker}</b>\n"
                    f"<b>{company}</b> | {form_type} | {filing['date']}\n\n"
                )
                if structured:
                    msg += f"{structured}\n\n"
                if plain:
                    msg += f"🤖 <b>Gemini plain English:</b>\n{plain}"
            else:
                link = edgar_link(form_type, cik_int)
                msg = (
                    f"📋 <b>NEW FILING — {ticker}</b>\n"
                    f"{company} | {form_type} | {filing['date']}\n"
                    f"<a href='{link}'>View on EDGAR →</a>"
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
