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

PRICE_THRESHOLD = 3.0   # % move to alert
DIP_UPSIDE_MIN  = 20.0  # % below analyst target to flag as dip

# ─── YOUR PERSONAL TARGETS ───────────────────────────────────────────────────
# These are YOUR specific positions with sell targets
# Agent alerts when price hits these — regardless of analyst data
PERSONAL_TARGETS = {
    "NBIS": {"sell": 286.0, "dip_buy": 199.0, "shares": 17.10},
    "IONQ": {"sell": 72.0,  "shares": 8.33},
    "EL":   {"sell": 120.0, "shares": 8},
    "SOUN": {"sell": 10.0,  "shares": 782},
    "QUBT": {"sell": 12.0,  "shares": 290},
    "QBTS": {"sell": 22.0,  "shares": 311},
    "RGTI": {"sell": 20.0,  "shares": 111.25},
}

# ─── DYNAMIC LIMITS ───────────────────────────────────────────────────────────

def calc_limits(num_stocks):
    telegram = max(3, min(8, num_stocks // 6))
    gemini   = max(5, min(20, num_stocks // 3))
    return telegram, gemini

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
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
    """Live price + daily % change — Finnhub free tier confirmed working"""
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

# ─── FMP — ANALYST TARGETS ────────────────────────────────────────────────────

# Cache targets — fetch once per run, reuse for all checks
_target_cache = {}

def get_analyst_target(ticker):
    """
    Fetch analyst consensus price target from FMP.
    Free tier: 250 calls/day — enough for 22 stocks × 11 runs.
    Returns: targetConsensus, targetHigh, targetLow
    """
    if ticker in _target_cache:
        return _target_cache[ticker]

    try:
        url = (
            f"https://financialmodelingprep.com/api/v4/price-target-consensus"
            f"?symbol={ticker}&apikey={FMP_API_KEY}"
        )
        r = requests.get(url, timeout=10)
        d = r.json()

        # FMP returns a list
        if isinstance(d, list) and len(d) > 0:
            item = d[0]
            result = {
                "consensus": item.get("targetConsensus"),
                "high":      item.get("targetHigh"),
                "low":       item.get("targetLow"),
                "median":    item.get("targetMedian"),
            }
        elif isinstance(d, dict) and d.get("targetConsensus"):
            result = {
                "consensus": d.get("targetConsensus"),
                "high":      d.get("targetHigh"),
                "low":       d.get("targetLow"),
                "median":    d.get("targetMedian"),
            }
        else:
            print(f"FMP no target for {ticker}: {str(d)[:80]}")
            result = None

        _target_cache[ticker] = result
        return result

    except Exception as e:
        print(f"FMP target error {ticker}: {e}")
        return None

def get_analyst_recommendation(ticker):
    """
    Fetch analyst buy/hold/sell counts from FMP.
    Returns recommendation string: STRONG BUY / BUY / HOLD / SELL
    """
    try:
        url = (
            f"https://financialmodelingprep.com/api/v3/analyst-stock-recommendations"
            f"/{ticker}?limit=1&apikey={FMP_API_KEY}"
        )
        r   = requests.get(url, timeout=10)
        d   = r.json()
        if isinstance(d, list) and len(d) > 0:
            latest     = d[0]
            strong_buy = latest.get("analystRatingsStrongBuy", 0)
            buy        = latest.get("analystRatingsbuy", 0)
            hold       = latest.get("analystRatingsHold", 0)
            sell       = latest.get("analystRatingsSell", 0)
            strong_sell= latest.get("analystRatingsStrongSell", 0)
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

# ─── ALERT LOGIC ──────────────────────────────────────────────────────────────

def check_all_alerts(ticker, price, pct):
    """
    Three layers of alerts:
    1. Personal targets — YOUR specific positions (hardcoded)
    2. Analyst targets — FMP consensus data (dynamic)
    3. Price moves — Finnhub ±3%+ (always)
    """
    alerts = []
    if not price:
        return alerts

    # ── LAYER 1: Personal targets ──────────────────────────────────────────────
    if ticker in PERSONAL_TARGETS:
        pt = PERSONAL_TARGETS[ticker]
        if price >= pt["sell"]:
            shares = pt.get("shares", 0)
            value  = round(shares * price, 0)
            alerts.append(
                f"🎯 <b>YOUR TARGET HIT — {ticker}</b>\n"
                f"Price ${price:.2f} ≥ your target ${pt['sell']:.2f}\n"
                f"You hold {shares} shares = <b>${value:,.0f}</b>\n\n"
                f"💬 Ask Claude: 'Should I sell {ticker} at ${price:.2f}?'"
            )
        elif "dip_buy" in pt and price <= pt["dip_buy"]:
            alerts.append(
                f"💰 <b>YOUR DIP TARGET — {ticker}</b>\n"
                f"Price ${price:.2f} ≤ your dip target ${pt['dip_buy']:.2f}\n\n"
                f"💬 Ask Claude: 'Should I buy more {ticker} at ${price:.2f}?'"
            )

    # ── LAYER 2: Analyst targets from FMP ─────────────────────────────────────
    target = get_analyst_target(ticker)
    time.sleep(0.2)  # FMP rate limit

    if target and target.get("consensus"):
        consensus = target["consensus"]
        high      = target.get("high")
        upside    = ((consensus - price) / price) * 100

        # Price at or above analyst consensus — potential sell signal
        if price >= consensus:
            rec, rec_detail = get_analyst_recommendation(ticker)
            remaining = ((high - price) / price * 100) if high else 0
            alerts.append(
                f"🎯 <b>AT ANALYST TARGET — {ticker}</b>\n"
                f"Price ${price:.2f} ≥ consensus ${consensus:.2f}\n"
                f"Upside to high: +{remaining:.1f}% (${high:.2f})\n"
                f"Consensus: <b>{rec or 'N/A'}</b> ({rec_detail or 'N/A'})\n\n"
                f"💬 Ask Claude: 'Should I take profit on {ticker} at ${price:.2f}?'"
            )

        # Price 20%+ below consensus with move — dip opportunity
        elif upside >= DIP_UPSIDE_MIN and pct and pct <= -3:
            rec, rec_detail = get_analyst_recommendation(ticker)
            alerts.append(
                f"💰 <b>DIP OPPORTUNITY — {ticker}</b>\n"
                f"Price ${price:.2f} | Analyst consensus ${consensus:.2f}\n"
                f"Potential upside: <b>+{upside:.1f}%</b>\n"
                f"Consensus: <b>{rec or 'N/A'}</b> ({rec_detail or 'N/A'})\n\n"
                f"💬 Ask Claude: 'Is {ticker} a buy at ${price:.2f} with +{upside:.1f}% upside?'"
            )

    # ── LAYER 3: Price move alert ──────────────────────────────────────────────
    if pct and abs(pct) >= PRICE_THRESHOLD:
        # Only send move alert if no target alert already sent
        if not alerts:
            icon      = "🟢" if pct > 0 else "🔴"
            direction = "surged" if pct > 0 else "dropped"
            # Add analyst context if available
            if target and target.get("consensus"):
                upside = ((target["consensus"] - price) / price) * 100
                alerts.append(
                    f"{icon} <b>MOVE ALERT — {ticker}</b>\n"
                    f"Stock {direction} {pct:+.1f}% · ${price:.2f}\n"
                    f"Analyst consensus: ${target['consensus']:.2f} ({upside:+.1f}% upside)\n\n"
                    f"💬 Ask Claude: 'Why did {ticker} move {pct:+.1f}% and should I act?'"
                )
            else:
                alerts.append(
                    f"{icon} <b>MOVE ALERT — {ticker}</b>\n"
                    f"Stock {direction} {pct:+.1f}% · ${price:.2f}\n\n"
                    f"💬 Ask Claude: 'Why did {ticker} move {pct:+.1f}% and should I act?'"
                )

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

        today   = date.today().isoformat()
        results = []

        for form, filing_date, accession, doc in zip(forms, dates, accessions, docs):
            if filing_date != today:
                continue
            if form not in ("8-K", "10-Q", "10-K", "S-1", "DEF 14A"):
                continue
            cik_int   = int(cik)
            acc_clean = accession.replace("-", "")
            file_url  = (
                f"https://www.sec.gov/Archives/edgar/data"
                f"/{cik_int}/{acc_clean}/{doc}"
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

# ─── GEMINI ───────────────────────────────────────────────────────────────────

def analyze_filing(ticker, filing, text, gemini_calls, max_gemini):
    if gemini_calls[0] >= max_gemini:
        return None
    if not text or len(text) < 150:
        return None

    form_type = filing.get("form", "8-K")
    prompt = f"""You are a stock analyst. Analyze this SEC {form_type} filing for {ticker}.

Filing excerpt:
{text[:3000]}

Respond in EXACTLY this format — no extra text:
CLASSIFICATION: [POSITIVE / NEGATIVE / NEUTRAL]
REASON: [one sentence — what specifically happened]
SIGNAL: [BUY / SELL / HOLD]
SUMMARY: [one plain English sentence for a retail investor]
IMPACT: [HIGH / MEDIUM / LOW]"""

    try:
        gemini_calls[0] += 1
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        print(f"Gemini error {ticker}: {e}")
        return None

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    tickers  = load_watchlist()
    seen     = load_seen()
    new_seen = set(seen)
    now      = datetime.utcnow()
    hour     = now.hour

    etfs = {
        "VOO","QQQ","QQQM","SCHG","SCHD",
        "VGT","VUG","SOXX","VWO","VXUS","VO"
    }
    num_stocks             = len([t for t in tickers if t not in etfs])
    max_telegram, max_gemini = calc_limits(num_stocks)
    telegram_sent          = 0
    gemini_calls           = [0]

    print(
        f"Run at {now} UTC | {len(tickers)} tickers ({num_stocks} stocks) | "
        f"Telegram={max_telegram} Gemini={max_gemini} | "
        f"threshold={PRICE_THRESHOLD}%"
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
            f"📋 {len(tickers)} tickers | "
            f"Alerts: {PRICE_THRESHOLD}%+ moves\n"
            f"📡 Finnhub prices + FMP analyst targets\n\n"
        )
        if movers:
            msg += "📊 <b>Overnight movers (±2%+):</b>\n" + "\n".join(movers[:10])
        else:
            msg += "All quiet overnight."
        msg += "\n\n<i>SEC filings + analyst targets checked every 15 min.</i>"
        send_telegram(msg)
        save_seen(new_seen)
        return

    # ── MARKET HOURS — 14–21 UTC = 9:30 AM–4 PM ET ────────────────────────────
    if 14 <= hour <= 21:
        print(f"Market hours — checking prices + analyst targets")
        for ticker in tickers:
            if telegram_sent >= max_telegram:
                break
            price, pct = get_price(ticker)
            if not price:
                time.sleep(0.3)
                continue

            alerts = check_all_alerts(ticker, price, pct)
            for alert in alerts:
                if telegram_sent >= max_telegram:
                    break
                send_telegram(alert)
                telegram_sent += 1
                time.sleep(1)
            time.sleep(0.3)
    else:
        print(f"Outside market hours (hour={hour} UTC)")

    # ── SEC FILING CHECK — all stock tickers ───────────────────────────────────
    load_cik_map()

    for ticker in tickers:
        if telegram_sent >= max_telegram:
            print("Telegram limit reached")
            break
        if gemini_calls[0] >= max_gemini:
            print("Gemini limit reached")
            break
        if ticker in etfs:
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
            analysis    = analyze_filing(
                ticker, filing, filing_text, gemini_calls, max_gemini
            )

            if analysis:
                msg = (
                    f"📋 <b>NEW FILING — {ticker}</b>\n"
                    f"<b>{company}</b> | {form_type} | {filing['date']}\n\n"
                    f"{analysis}\n\n"
                    f"💬 Ask Claude: 'Deep analysis on this {ticker} {form_type} filing'"
                )
            else:
                link = edgar_link(form_type, cik_int)
                msg = (
                    f"📋 <b>NEW FILING — {ticker}</b>\n"
                    f"{company} | {form_type} | {filing['date']}\n"
                    f"<a href='{link}'>View on EDGAR →</a>\n\n"
                    f"💬 Ask Claude: 'Analyze latest {ticker} {form_type}'"
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
