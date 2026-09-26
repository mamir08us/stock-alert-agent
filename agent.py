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

MAX_TELEGRAM = 3
MAX_GEMINI = 10
PRICE_THRESHOLD = 5.0

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

def get_sec_filings(ticker):
    try:
        today = date.today().isoformat()
        url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
            f"&dateRange=custom&startdt={today}&enddt={today}&forms=8-K"
        )
        headers = {"User-Agent": "mamir08@gmail.com StockAlertBot"}
        r = requests.get(url, headers=headers, timeout=10)
        hits = r.json().get("hits", {}).get("hits", [])
        print(f"{ticker}: {len(hits)} filings today")
        return hits[:2]
    except Exception as e:
        print(f"SEC error {ticker}: {e}")
        return []

def get_filing_text(file_url):
    try:
        headers = {"User-Agent": "mamir08@gmail.com StockAlertBot"}
        r = requests.get(file_url, headers=headers, timeout=15)
        text = re.sub(r'<[^>]+>', ' ', r.text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:3000]
    except:
        return ""

def analyze_filing(ticker, text, gemini_calls):
    if gemini_calls[0] >= MAX_GEMINI:
        print("Gemini limit reached")
        return None
    if not text or len(text) < 100:
        return None
    prompt = f"""You are a stock analyst. Analyze this SEC 8-K filing for {ticker}.

Filing:
{text}

Reply in exactly this format:
CLASSIFICATION: [POSITIVE / NEGATIVE / NEUTRAL]
REASON: [one sentence max]
SIGNAL: [BUY / SELL / HOLD]
SUMMARY: [one plain English sentence of what happened]"""
    try:
        gemini_calls[0] += 1
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Gemini error: {e}")
        return None

def main():
    tickers = load_watchlist()
    seen = load_seen()
    new_seen = set(seen)
    now = datetime.utcnow()
    hour = now.hour
    telegram_sent = 0
    gemini_calls = [0]

    print(f"Run at {now} UTC | {len(tickers)} tickers | {len(seen)} seen filings")

    # Morning brief 12 UTC = 7am ET
    if hour == 12:
        movers = []
        for ticker in tickers:
            price, pct = get_price(ticker)
            if price and pct and abs(pct) >= 2:
                icon = "🟢" if pct > 0 else "🔴"
                movers.append(f"{icon} <b>{ticker}</b> {pct:+.1f}% · ${price:.2f}")
            time.sleep(0.3)
        msg = f"🌅 <b>Morning Brief — {now.strftime('%a %b %d')}</b>\n\n"
        if movers:
            msg += "📊 <b>Overnight movers:</b>\n" + "\n".join(movers)
        else:
            msg += "All quiet overnight. No significant moves on your watchlist."
        msg += "\n\n<i>Agent monitoring SEC filings every 15 min.</i>"
        send_telegram(msg)
        save_seen(new_seen)
        return

    # Price alerts 14-21 UTC = 9:30am-4pm ET
    if 14 <= hour <= 21:
        for ticker in tickers:
            if telegram_sent >= MAX_TELEGRAM:
                break
            price, pct = get_price(ticker)
            if price and pct and abs(pct) >= PRICE_THRESHOLD:
                icon = "🟢" if pct > 0 else "🔴"
                send_telegram(
                    f"{icon} <b>PRICE ALERT — {ticker}</b>\n"
                    f"Move: {pct:+.1f}% today · Price: ${price:.2f}\n\n"
                    f"💬 Open Claude: 'Should I act on {ticker} at ${price:.2f}?'"
                )
                telegram_sent += 1
            time.sleep(0.4)

    # SEC filing check
    for ticker in tickers:
        if telegram_sent >= MAX_TELEGRAM:
            print("Telegram limit reached")
            break
        if gemini_calls[0] >= MAX_GEMINI:
            print("Gemini limit reached")
            break
        filings = get_sec_filings(ticker)
        for hit in filings:
            if telegram_sent >= MAX_TELEGRAM:
                break
            filing_id = hit.get("_id", "")
            if not filing_id or filing_id in seen:
                continue
            new_seen.add(filing_id)
            source = hit.get("_source", {})
            form_type = source.get("form_type", "8-K")
            company = source.get("entity_name", ticker)
            file_url = source.get("file_url", "")
            filing_text = get_filing_text(file_url) if file_url else ""
            analysis = analyze_filing(ticker, filing_text, gemini_calls)
            if analysis:
                msg = (
                    f"📋 <b>NEW FILING — {ticker}</b>\n"
                    f"<b>{company}</b> | {form_type}\n\n"
                    f"{analysis}\n\n"
                    f"💬 Open Claude: 'Deep analysis on this {ticker} 8-K filing'"
                )
            else:
                msg = (
                    f"📋 <b>NEW FILING — {ticker}</b>\n"
                    f"{company} | {form_type}\n"
                    f"New SEC filing detected. Check EDGAR for details."
                )
            send_telegram(msg)
            telegram_sent += 1
            time.sleep(2)
        time.sleep(0.5)

    save_seen(new_seen)
    print(f"Done. Telegram: {telegram_sent}/{MAX_TELEGRAM} | Gemini: {gemini_calls[0]}/{MAX_GEMINI}")

if __name__ == "__main__":
    main()
