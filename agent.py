import os
import json
import requests
import time
import re
from datetime import datetime, date
import google.generativeai as genai

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

MAX_TELEGRAM = 5       # ✅ increased from 3
MAX_GEMINI = 15        # ✅ increased from 10
PRICE_THRESHOLD = 5.0

# ─── TELEGRAM ───────────────────────────────────────────────────────────────

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

# ─── FILE HELPERS ────────────────────────────────────────────────────────────

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

# ─── PRICE ───────────────────────────────────────────────────────────────────

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

# ─── SEC EDGAR ───────────────────────────────────────────────────────────────

# ✅ FIX 1: Correct EDGAR API — use company CIK lookup then filings
# ✅ FIX 2: Proper SEC User-Agent format required by SEC.gov
SEC_HEADERS = {
    "User-Agent": "Amir Mohammad mamir08@gmail.com",
    "Accept-Encoding": "gzip, deflate",
    "Host": "efts.sec.gov"
}

def get_company_cik(ticker):
    """Get CIK number for a ticker from SEC company_tickers.json"""
    try:
        url = "https://www.sec.gov/files/company_tickers.json"
        r = requests.get(url, headers={"User-Agent": "Amir Mohammad mamir08@gmail.com"}, timeout=15)
        data = r.json()
        ticker_upper = ticker.upper()
        for entry in data.values():
            if entry.get("ticker", "").upper() == ticker_upper:
                cik = str(entry["cik_str"]).zfill(10)
                return cik
    except Exception as e:
        print(f"CIK lookup error {ticker}: {e}")
    return None

def get_sec_filings_by_cik(cik, ticker):
    """Get latest filings for a company using their CIK — most reliable method"""
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        r = requests.get(url, headers={"User-Agent": "Amir Mohammad mamir08@gmail.com"}, timeout=15)
        data = r.json()

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        descriptions = recent.get("primaryDocument", [])

        today = date.today().isoformat()
        results = []

        for i, (form, filing_date, accession, doc) in enumerate(
            zip(forms, dates, accessions, descriptions)
        ):
            if filing_date == today and form in ("8-K", "10-Q", "10-K", "S-1", "DEF 14A"):
                acc_clean = accession.replace("-", "")
                file_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{doc}"
                results.append({
                    "form": form,
                    "date": filing_date,
                    "accession": accession,
                    "url": file_url,
                    "company": data.get("name", ticker)
                })
                if len(results) >= 2:
                    break

        print(f"{ticker} (CIK {cik}): {len(results)} filings today")
        return results

    except Exception as e:
        print(f"SEC CIK filing error {ticker}: {e}")
        return []

def get_filing_text(file_url):
    """Fetch and clean filing text for Gemini analysis"""
    try:
        r = requests.get(
            file_url,
            headers={"User-Agent": "Amir Mohammad mamir08@gmail.com"},
            timeout=15
        )
        # Strip HTML tags
        text = re.sub(r'<[^>]+>', ' ', r.text)
        # Clean whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        # ✅ FIX 3: Take middle section where substance usually is (skip boilerplate header)
        if len(text) > 6000:
            text = text[500:4000]  # skip first 500 chars (boilerplate) take next 3500
        return text
    except Exception as e:
        print(f"Filing text error: {e}")
        return ""

# ─── GEMINI ANALYSIS ─────────────────────────────────────────────────────────

def analyze_filing(ticker, filing_info, text, gemini_calls):
    """Analyze SEC filing with Gemini — improved prompt"""
    if gemini_calls[0] >= MAX_GEMINI:
        print("Gemini limit reached")
        return None
    if not text or len(text) < 150:
        print(f"Text too short for {ticker}: {len(text)} chars")
        return None

    form_type = filing_info.get("form", "8-K")

    prompt = f"""You are a stock analyst. Analyze this SEC {form_type} filing for {ticker}.

Filing text (excerpt):
{text[:3000]}

Respond in EXACTLY this format (no extra text):
CLASSIFICATION: [POSITIVE / NEGATIVE / NEUTRAL]
REASON: [one sentence — what specifically happened]
SIGNAL: [BUY / SELL / HOLD]
SUMMARY: [one plain English sentence for a retail investor]
IMPACT: [HIGH / MEDIUM / LOW]"""

    try:
        gemini_calls[0] += 1
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Gemini error {ticker}: {e}")
        return None

# ─── PRICE TARGET ALERTS ─────────────────────────────────────────────────────

# ✅ NEW: Your personal price targets — get alert when hit
PRICE_TARGETS = {
    "NBIS":  {"sell": 286.0,  "dip_buy": 199.0, "shares": 17.10},
    "IONQ":  {"sell": 72.0,   "shares": 8.33},
    "EL":    {"sell": 120.0,  "shares": 8},
    "SOUN":  {"sell": 10.0,   "shares": 782},
    "QUBT":  {"sell": 12.0,   "shares": 290},
    "QBTS":  {"sell": 22.0,   "shares": 311},
    "RGTI":  {"sell": 20.0,   "shares": 111.25},
}

def check_price_targets(ticker, price):
    """Check if price hit any personal targets"""
    if ticker not in PRICE_TARGETS or price is None:
        return None
    targets = PRICE_TARGETS[ticker]
    alerts = []

    if "sell" in targets and price >= targets["sell"]:
        shares = targets.get("shares", 0)
        value = round(shares * price, 0)
        alerts.append(
            f"🎯 <b>TARGET HIT — {ticker}</b>\n"
            f"Price ${price:.2f} ≥ target ${targets['sell']:.2f}\n"
            f"You hold {shares} shares = <b>${value:,.0f}</b>\n"
            f"💬 Ask Claude: 'Should I sell {ticker} now?'"
        )

    if "dip_buy" in targets and price <= targets["dip_buy"]:
        alerts.append(
            f"💰 <b>DIP BUY ALERT — {ticker}</b>\n"
            f"Price ${price:.2f} ≤ dip target ${targets['dip_buy']:.2f}\n"
            f"💬 Ask Claude: 'Buy {ticker} dip at ${price:.2f}?'"
        )

    return "\n\n".join(alerts) if alerts else None

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    tickers = load_watchlist()
    seen = load_seen()
    new_seen = set(seen)
    now = datetime.utcnow()
    hour = now.hour
    telegram_sent = 0
    gemini_calls = [0]

    print(f"Run at {now} UTC | {len(tickers)} tickers | {len(seen)} seen filings")

    # ─── MORNING BRIEF — 12 UTC = 7am ET ────────────────────────────────────
    if hour == 12:
        movers = []
        targets_hit = []

        for ticker in tickers:
            price, pct = get_price(ticker)

            # Check price targets
            if price:
                target_alert = check_price_targets(ticker, price)
                if target_alert:
                    targets_hit.append(target_alert)

            # Check overnight movers
            if price and pct and abs(pct) >= 2:
                icon = "🟢" if pct > 0 else "🔴"
                movers.append(f"{icon} <b>{ticker}</b> {pct:+.1f}% · ${price:.2f}")
            time.sleep(0.3)

        # Send target alerts first
        for alert in targets_hit[:2]:
            send_telegram(alert)
            telegram_sent += 1
            time.sleep(1)

        # Morning brief
        msg = f"🌅 <b>Morning Brief — {now.strftime('%a %b %d')}</b>\n\n"
        if movers:
            msg += "📊 <b>Overnight movers (±2%+):</b>\n" + "\n".join(movers[:8])
        else:
            msg += "All quiet overnight. No significant moves on your watchlist."
        msg += "\n\n<i>Agent monitoring SEC filings every 15 min. 12 price alerts active.</i>"
        send_telegram(msg)
        save_seen(new_seen)
        return

    # ─── MARKET HOURS — 14-21 UTC = 9:30am-4pm ET ───────────────────────────
    if 14 <= hour <= 21:

        # Price target checks
        for ticker in tickers:
            if telegram_sent >= MAX_TELEGRAM:
                break
            price, pct = get_price(ticker)

            # Check personal targets
            if price:
                target_alert = check_price_targets(ticker, price)
                if target_alert:
                    send_telegram(target_alert)
                    telegram_sent += 1
                    time.sleep(1)
                    continue

            # Check big % moves
            if price and pct and abs(pct) >= PRICE_THRESHOLD:
                icon = "🟢" if pct > 0 else "🔴"
                send_telegram(
                    f"{icon} <b>PRICE ALERT — {ticker}</b>\n"
                    f"Move: {pct:+.1f}% today · Price: ${price:.2f}\n\n"
                    f"💬 Ask Claude: 'Should I act on {ticker} at ${price:.2f}?'"
                )
                telegram_sent += 1
            time.sleep(0.4)

    # ─── SEC FILING CHECK — all hours ───────────────────────────────────────
    # ✅ FIX: Use CIK-based lookup instead of broken search endpoint
    # Only check your key holdings — not all 35 tickers (too slow + limits)
    key_tickers = ["NBIS", "NVDA", "PLTR", "IONQ", "META", "AMD", "AMZN",
                   "GOOGL", "MSFT", "AVGO", "MRVL", "MU", "ORCL", "LLY",
                   "SOUN", "QUBT", "QBTS", "RGTI"]

    for ticker in key_tickers:
        if telegram_sent >= MAX_TELEGRAM:
            print("Telegram limit reached")
            break
        if gemini_calls[0] >= MAX_GEMINI:
            print("Gemini limit reached")
            break

        # Get CIK
        cik = get_company_cik(ticker)
        if not cik:
            print(f"No CIK found for {ticker}")
            time.sleep(0.5)
            continue

        # Get today's filings
        filings = get_sec_filings_by_cik(cik, ticker)

        for filing in filings:
            if telegram_sent >= MAX_TELEGRAM:
                break

            filing_id = filing["accession"]
            if filing_id in seen:
                continue

            new_seen.add(filing_id)
            company = filing["company"]
            form_type = filing["form"]
            file_url = filing["url"]

            # Get and analyze filing text
            filing_text = get_filing_text(file_url)
            analysis = analyze_filing(ticker, filing, filing_text, gemini_calls)

            if analysis:
                msg = (
                    f"📋 <b>NEW FILING — {ticker}</b>\n"
                    f"<b>{company}</b> | {form_type} | {filing['date']}\n\n"
                    f"{analysis}\n\n"
                    f"💬 Ask Claude: 'Deep analysis on this {ticker} {form_type} filing'"
                )
            else:
                # ✅ FIX: Better fallback with actual EDGAR link
                acc = filing_id.replace("-", "")
                cik_int = int(cik)
                edgar_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_int}&type={form_type}&dateb=&owner=include&count=5"
                msg = (
                    f"📋 <b>NEW FILING — {ticker}</b>\n"
                    f"{company} | {form_type} | {filing['date']}\n"
                    f"New SEC filing detected.\n"
                    f"<a href='{edgar_url}'>View on EDGAR →</a>\n\n"
                    f"💬 Ask Claude: 'Analyze latest {ticker} {form_type} filing'"
                )

            send_telegram(msg)
            telegram_sent += 1
            time.sleep(2)

        time.sleep(0.8)

    save_seen(new_seen)
    print(f"Done. Telegram: {telegram_sent}/{MAX_TELEGRAM} | Gemini: {gemini_calls[0]}/{MAX_GEMINI}")

if __name__ == "__main__":
    main()
