import os
import json
import requests
import time
from datetime import datetime, date
import google.generativeai as genai

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    })

def load_watchlist():
    with open("watchlist.txt") as f:
        return [line.strip() for line in f if line.strip()]

def get_sec_filings(ticker):
    try:
        url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={date.today()}&forms=8-K&hits.hits._source=period_of_report,entity_name,file_url,form_type"
        headers = {"User-Agent": "mamir08@gmail.com StockAlertBot"}
        r = requests.get(url, headers=headers, timeout=10)
        return r.json().get("hits", {}).get("hits", [])
    except Exception as e:
        print(f"SEC error for {ticker}: {e}")
        return []

def get_filing_text(url):
    try:
        headers = {"User-Agent": "mamir08@gmail.com StockAlertBot"}
        r = requests.get(url, headers=headers, timeout=15)
        # Strip HTML tags roughly
        text = r.text
        import re
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text[:4000]
    except:
        return ""

def get_price(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()["chart"]["result"][0]
        closes = data["indicators"]["quote"][0]["close"]
        if len(closes) >= 2 and closes[-2] and closes[-1]:
            prev = closes[-2]
            curr = closes[-1]
            pct = ((curr - prev) / prev) * 100
            return curr, pct
    except:
        pass
    return None, None

def analyze_filing(ticker, filing_text):
    if not filing_text or len(filing_text) < 100:
        return None
    prompt = f"""You are a stock analyst. Analyze this SEC 8-K filing for {ticker}.

Filing text:
{filing_text[:3000]}

Respond in exactly this format:
CLASSIFICATION: [POSITIVE / NEGATIVE / NEUTRAL]
REASON: [one sentence]
SIGNAL: [BUY / SELL / HOLD]
SUMMARY: [one sentence plain English summary]"""
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Gemini error: {e}")
        return None

def load_seen():
    try:
        with open("seen_filings.json") as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open("seen_filings.json", "w") as f:
        json.dump(list(seen), f)

def main():
    tickers = load_watchlist()
    seen = load_seen()
    new_seen = set(seen)
    now = datetime.now()
    hour = now.hour
    alerts_sent = 0
    MAX_ALERTS = 5  # prevent spam

    print(f"Running at {now} — {len(tickers)} tickers, {len(seen)} seen filings")

    # Morning brief at 7am ET (12 UTC)
    if hour == 12:
        movers = []
        for ticker in tickers:
            price, pct = get_price(ticker)
            if price and pct and abs(pct) >= 2:
                icon = "🟢" if pct > 0 else "🔴"
                movers.append(f"{icon} <b>{ticker}</b> {pct:+.1f}% · ${price:.2f}")
            time.sleep(0.3)
        msg = f"🌅 <b>Morning Brief — {now.strftime('%b %d %Y')}</b>\n\n"
        if movers:
            msg += "📊 <b>Movers on your watchlist:</b>\n" + "\n".join(movers)
        else:
            msg += "Markets quiet. No significant overnight moves."
        msg += "\n\n<i>SEC filing monitor active.</i>"
        send_telegram(msg)
        return

    # Price alerts during market hours
    if 14 <= hour <= 21:  # 9:30am-4pm ET = 14-21 UTC
        for ticker in tickers:
            price, pct = get_price(ticker)
            if price and pct and abs(pct) >= 5:
                icon = "🟢" if pct > 0 else "🔴"
                send_telegram(
                    f"{icon} <b>PRICE ALERT — {ticker}</b>\n"
                    f"Move: {pct:+.1f}% today\n"
                    f"Price: ${price:.2f}\n"
                    f"Open Claude and ask: 'Should I act on {ticker} at ${price:.2f}?'"
                )
                alerts_sent += 1
            time.sleep(0.5)

    # SEC filing check
    for ticker in tickers:
        if alerts_sent >= MAX_ALERTS:
            print("Max alerts reached, stopping")
            break
        filings = get_sec_filings(ticker)
        for hit in filings:
            filing_id = hit.get("_id", "")
            if not filing_id or filing_id in seen:
                continue
            new_seen.add(filing_id)

            source = hit.get("_source", {})
            form_type = source.get("form_type", "8-K")
            company = source.get("entity_name", ticker)
            file_url = source.get("file_url", "")

            # Get and analyze filing text
            filing_text = get_filing_text(file_url) if file_url else ""
            analysis = analyze_filing(ticker, filing_text)

            if analysis:
                msg = (
                    f"📋 <b>NEW FILING — {ticker}</b>\n"
                    f"<b>{company}</b> | {form_type}\n\n"
                    f"{analysis}\n\n"
                    f"💬 Ask Claude: 'Analyze this {ticker} filing for buy/sell signal'"
                )
            else:
                msg = (
                    f"📋 <b>NEW FILING — {ticker}</b>\n"
                    f"{company} | {form_type}\n"
                    f"Check SEC EDGAR for details."
                )

            send_telegram(msg)
            alerts_sent += 1
            time.sleep(2)

        time.sleep(1)

    save_seen(new_seen)
    print(f"Done. Sent {alerts_sent} alerts. Total seen: {len(new_seen)}")

if __name__ == "__main__":
    main()