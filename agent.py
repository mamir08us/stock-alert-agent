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
        search_url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={date.today()}&forms=8-K"
        headers = {"User-Agent": "mamir08@gmail.com StockAlertBot"}
        r = requests.get(search_url, headers=headers, timeout=10)
        return r.json().get("hits", {}).get("hits", [])
    except:
        return []

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
    prompt = f"""You are a stock analyst. Analyze this SEC 8-K filing for {ticker}.

Filing excerpt:
{filing_text[:3000]}

Respond in exactly this format:
CLASSIFICATION: [POSITIVE / NEGATIVE / NEUTRAL]
REASON: [one sentence explaining why]
SIGNAL: [BUY / SELL / HOLD]
SUMMARY: [one sentence plain English summary of what happened]"""
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except:
        return None

def check_price_alerts(tickers):
    alerts = []
    for ticker in tickers:
        price, pct = get_price(ticker)
        if price and pct:
            if abs(pct) >= 5:
                direction = "🟢 UP" if pct > 0 else "🔴 DOWN"
                alerts.append(
                    f"{direction} <b>{ticker}</b> {pct:+.1f}% today\n"
                    f"Price: ${price:.2f}"
                )
        time.sleep(0.5)
    return alerts

def check_sec_filings(tickers, seen_filings):
    alerts = []
    new_seen = set(seen_filings)
    
    for ticker in tickers:
        filings = get_sec_filings(ticker)
        for hit in filings:
            filing_id = hit.get("_id", "")
            if filing_id in seen_filings:
                continue
            new_seen.add(filing_id)
            
            source = hit.get("_source", {})
            form_type = source.get("form_type", "8-K")
            company = source.get("entity_name", ticker)
            filing_url = source.get("file_url", "")
            
            # Get filing text for AI analysis
            filing_text = source.get("file_text", "") or source.get("display_texts", "")
            if not filing_text:
                filing_text = f"New {form_type} filing from {company}"
            
            analysis = analyze_filing(ticker, str(filing_text))
            
            if analysis:
                msg = (
                    f"📋 <b>NEW SEC FILING — {ticker}</b>\n"
                    f"Form: {form_type} | {company}\n\n"
                    f"{analysis}\n\n"
                    f"🔗 <a href='https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=8-K&dateb=&owner=include&count=5'>View on SEC</a>"
                )
            else:
                msg = (
                    f"📋 <b>NEW SEC FILING — {ticker}</b>\n"
                    f"Form: {form_type} | {company}\n"
                    f"🔗 View: {filing_url}"
                )
            
            alerts.append(msg)
        time.sleep(1)
    
    return alerts, new_seen

def load_seen_filings():
    try:
        with open("seen_filings.json") as f:
            return set(json.load(f))
    except:
        return set()

def save_seen_filings(seen):
    with open("seen_filings.json", "w") as f:
        json.dump(list(seen), f)

def main():
    tickers = load_watchlist()
    seen_filings = load_seen_filings()
    
    now = datetime.now()
    hour = now.hour
    
    # Morning briefing at 7am
    if hour == 7:
        price_alerts = check_price_alerts(tickers)
        msg = f"🌅 <b>Morning Brief — {now.strftime('%b %d %Y')}</b>\n\n"
        if price_alerts:
            msg += "📊 <b>Overnight movers:</b>\n" + "\n\n".join(price_alerts)
        else:
            msg += "Markets quiet overnight. No significant moves on your watchlist."
        msg += "\n\n<i>Monitoring SEC filings every 15 min during market hours.</i>"
        send_telegram(msg)
    
    # Price alerts during market hours (9:30am - 4pm ET)
    elif 9 <= hour <= 16:
        price_alerts = check_price_alerts(tickers)
        for alert in price_alerts:
            send_telegram(alert)
    
    # SEC filing check — always run
    filing_alerts, new_seen = check_sec_filings(tickers, seen_filings)
    for alert in filing_alerts:
        send_telegram(alert)
    
    save_seen_filings(new_seen)
    print(f"Run complete at {now}. Filings seen: {len(new_seen)}")

if __name__ == "__main__":
    main()
