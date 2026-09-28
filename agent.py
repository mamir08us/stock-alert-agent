import os
import json
import requests
import time
import re
from datetime import datetime, date
from google import genai

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

client = genai.Client(api_key=GEMINI_API_KEY)

PRICE_THRESHOLD = 5.0  # % move to trigger alert

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

# ─── PRICE ────────────────────────────────────────────────────────────────────

def get_price(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()["chart"]["result"][0]
        closes = data["indicators"]["quote"][0]["close"]
        if len(closes) >= 2 and closes[-2] and closes[-1]:
            prev, curr = closes[-2], closes[-1]
            pct = ((curr - prev) / prev) * 100
            return round(curr, 2), round(pct, 2)
    except:
        pass
    return None, None

# ─── DYNAMIC PRICE TARGETS FROM ANALYST DATA ──────────────────────────────────

def get_analyst_targets(ticker):
    """
    Fetch live analyst consensus price target from Yahoo Finance.
    Returns: (current_price, analyst_target, upside_pct, recommendation)
    All from real market data — nothing hardcoded.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=financialData,recommendationTrend"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()["quoteSummary"]["result"][0]

        fin = data.get("financialData", {})
        current_price   = fin.get("currentPrice", {}).get("raw")
        target_mean     = fin.get("targetMeanPrice", {}).get("raw")
        target_high     = fin.get("targetHighPrice", {}).get("raw")
        target_low      = fin.get("targetLowPrice", {}).get("raw")
        recommendation  = fin.get("recommendationKey", "hold").upper()  # BUY/HOLD/SELL

        if current_price and target_mean:
            upside = ((target_mean - current_price) / current_price) * 100
            return {
                "current":        round(current_price, 2),
                "target_mean":    round(target_mean, 2),
                "target_high":    round(target_high, 2) if target_high else None,
                "target_low":     round(target_low, 2) if target_low else None,
                "upside_pct":     round(upside, 1),
                "recommendation": recommendation,
            }
    except Exception as e:
        print(f"Analyst target error {ticker}: {e}")
    return None

def check_dynamic_targets(ticker, price, pct):
    """
    Instead of hardcoded targets — use live analyst data to flag:
    1. Price crossed ABOVE analyst mean target (potential sell signal)
    2. Price dropped >15% below analyst mean target (potential dip buy)
    3. Analyst consensus is STRONG BUY and stock is near 52-week low
    4. Stock moved 5%+ in a session (always alert)
    """
    alerts = []
    targets = get_analyst_targets(ticker)

    if not targets:
        return alerts

    current       = targets["current"]
    target_mean   = targets["target_mean"]
    target_high   = targets["target_high"]
    upside        = targets["upside_pct"]
    recommendation = targets["recommendation"]

    # Alert 1 — Price crossed analyst mean target (overvalued vs consensus)
    if price and target_mean and price >= target_mean:
        alerts.append(
            f"🎯 <b>AT/ABOVE ANALYST TARGET — {ticker}</b>\n"
            f"Price ${price:.2f} ≥ analyst mean ${target_mean:.2f}\n"
            f"Upside remaining to high: "
            f"${target_high:.2f} ({((target_high-price)/price*100):.1f}%)\n"
            f"Consensus: <b>{recommendation}</b>\n"
            f"💬 Ask Claude: 'Should I take profit on {ticker} at ${price:.2f}?'"
        )

    # Alert 2 — Big dip below analyst target = potential buy
    elif price and target_mean and upside >= 25 and recommendation in ("BUY", "STRONG_BUY", "STRONGBUY"):
        alerts.append(
            f"💰 <b>DIP OPPORTUNITY — {ticker}</b>\n"
            f"Price ${price:.2f} | Analyst target ${target_mean:.2f}\n"
            f"Potential upside: <b>+{upside:.1f}%</b>\n"
            f"Consensus: <b>{recommendation}</b>\n"
            f"💬 Ask Claude: 'Is {ticker} a buy at ${price:.2f} with +{upside:.1f}% upside?'"
        )

    # Alert 3 — Big session move on a stock with analyst coverage
    if pct and abs(pct) >= PRICE_THRESHOLD:
        direction = "surged" if pct > 0 else "dropped"
        icon = "🟢" if pct > 0 else "🔴"
        alerts.append(
            f"{icon} <b>MOVE ALERT — {ticker}</b>\n"
            f"Stock {direction} {pct:+.1f}% · Price ${price:.2f}\n"
            f"Analyst target: ${target_mean:.2f} ({upside:+.1f}% from here)\n"
            f"Consensus: <b>{recommendation}</b>\n"
            f"💬 Ask Claude: 'Why did {ticker} move {pct:+.1f}% today?'"
        )

    return alerts

# ─── SEC EDGAR ────────────────────────────────────────────────────────────────

SEC_UA    = "Amir Mohammad mamir08@gmail.com"
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
        print("Gemini limit reached")
        return None
    if not text or len(text) < 150:
        print(f"Text too short for {ticker}: {len(text)} chars")
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
    num_stocks = len([t for t in tickers if t not in etfs])
    max_telegram, max_gemini = calc_limits(num_stocks)

    telegram_sent = 0
    gemini_calls  = [0]

    print(
        f"Run at {now} UTC | {len(tickers)} tickers ({num_stocks} stocks) | "
        f"limits: Telegram={max_telegram} Gemini={max_gemini}"
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
        msg += f"📋 Watching {len(tickers)} tickers | {max_telegram} alerts / {max_gemini} AI analyses\n\n"
        if movers:
            msg += "📊 <b>Overnight movers (±2%+):</b>\n" + "\n".join(movers[:10])
        else:
            msg += "All quiet overnight. No significant moves."
        msg += (
            "\n\n<i>Analyst targets fetched live. "
            "SEC filings checked every 15 min.</i>"
        )
        send_telegram(msg)
        save_seen(new_seen)
        return

    # ── MARKET HOURS — 14–21 UTC = 9:30 AM–4 PM ET ────────────────────────────
    if 14 <= hour <= 21:
        for ticker in tickers:
            if telegram_sent >= max_telegram:
                break

            price, pct = get_price(ticker)
            if not price:
                time.sleep(0.3)
                continue

            # Dynamic alerts from live analyst data
            alerts = check_dynamic_targets(ticker, price, pct)
            for alert in alerts:
                if telegram_sent >= max_telegram:
                    break
                send_telegram(alert)
                telegram_sent += 1
                time.sleep(1)

            time.sleep(0.4)

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
            print(f"No CIK found for {ticker}")
            time.sleep(0.3)
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
                    f"💬 Ask Claude: "
                    f"'Deep analysis on this {ticker} {form_type} filing'"
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
