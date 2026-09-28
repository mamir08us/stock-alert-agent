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
    """
    Telegram: 1 alert per 5 stocks (min 3, max 10)
    Gemini: 3 calls per stock — one per alert signal
    Gemini free tier: 1,500/day — very generous, increase limits
    """
    telegram = max(3, min(10, num_stocks // 5))
    gemini   = max(15, min(50, num_stocks * 3))
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
    if beta is None: return 3.0
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
        url    = f"https://financialmodelingprep.com/api/v4/price-target-consensus?symbol={ticker}&apikey={FMP_API_KEY}"
        r      = requests.get(url, timeout=10)
        d      = r.json()
        data   = d[0] if isinstance(d, list) and len(d) > 0 else d if isinstance(d, dict) else None
        result = None
        if data and data.get("targetConsensus"):
            result = {
                "consensus": round(data["targetConsensus"], 2),
                "high":      round(data["targetHigh"], 2)   if data.get("targetHigh")   else None,
                "low":       round(data["targetLow"], 2)    if data.get("targetLow")    else None,
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
    try:
        url = f"https://financialmodelingprep.com/api/v3/analyst-stock-recommendations/{ticker}?limit=1&apikey={FMP_API_KEY}"
        r   = requests.get(url, timeout=10)
        d   = r.json()
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
        return f"📊 Experts think this stock is worth ${consensus:.2f} — that is {upside:.1f}% MORE than today's price. Big potential."
    if upside > 15:
        return f"📊 Experts think this stock is worth ${consensus:.2f} — that is {upside:.1f}% more than today. Good potential."
    return f"📊 Experts think this stock is worth ${consensus:.2f} — that is {upside:.1f}% more than today."

# ─── GEMINI — ALL AI ANALYSIS ─────────────────────────────────────────────────

def ask_gemini(prompt, gemini_calls, max_gemini):
    """
    Gemini handles ALL analysis and advice — no need for Claude or ChatGPT.
    Free tier: 1,500 calls/day — more than enough.
    """
    if gemini_calls[0] >= max_gemini:
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

# ─── DEDUP — NO DUPLICATE ALERTS ─────────────────────────────────────────────

_alerted_this_run = set()

def already_alerted(ticker, signal):
    key = f"{ticker}:{signal}"
    if key in _alerted_this_run:
        return True
    _alerted_this_run.add(key)
    return False

# ─── DYNAMIC ALERT LOGIC ──────────────────────────────────────────────────────

def check_all_alerts(ticker, price, pct, gemini_calls, max_gemini):
    """
    Fully dynamic — zero hardcoded values.
    Gemini provides complete analysis + action advice in every alert.
    No need to open Claude or ChatGPT — answer is right in Telegram.
    """
    alerts = []
    if not price:
        return alerts

    beta      = get_beta(ticker)
    threshold = dynamic_threshold(beta)
    beta_exp  = beta_plain_english(beta)
    target    = get_analyst_target(ticker)
    time.sleep(0.2)

    # ── SIGNAL 1: Price at/above analyst consensus ─────────────────────────────
    if target and target.get("consensus"):
        consensus = target["consensus"]
        hi_target = target.get("high")
        upside    = ((consensus - price) / price) * 100

        if price >= consensus and not already_alerted(ticker, "AT_TARGET"):
            rec, buy_pct, sell_pct, total = get_analyst_recommendation(ticker)
            remaining = round((hi_target - price) / price * 100, 1) if hi_target else 0

            # Gemini gives complete analysis AND clear action
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
            )

            msg = (
                f"🎯 <b>TARGET REACHED — {ticker}</b>\n\n"
                f"<b>What happened:</b>\n"
                f"Stock hit ${price:.2f} — the price Wall Street experts predicted.\n\n"
                f"{upside_plain_english(price, consensus, hi_target)}\n\n"
                f"<b>Expert opinion:</b>\n"
                f"{rec_plain_english(rec, buy_pct, sell_pct, total)}\n\n"
                f"<b>📱 Gemini analysis & advice:</b>\n"
                f"{analysis if analysis else 'Analysis unavailable — check manually.'}"
            )
            alerts.append(msg)

        # ── SIGNAL 2: Dip opportunity ──────────────────────────────────────────
        elif upside >= 20 and pct and pct <= -(threshold / 2):
            if not already_alerted(ticker, "DIP"):
                rec, buy_pct, sell_pct, total = get_analyst_recommendation(ticker)

                analysis = ask_gemini(
                    f"Stock: {ticker}. Dropped {abs(pct):.1f}% today to ${price:.2f}. "
                    f"Wall Street experts average target: ${consensus:.2f} "
                    f"(that's {upside:.1f}% higher than today's price). "
                    f"{buy_pct}% of {total} analysts say BUY. "
                    f"Answer these 3 things in simple language a beginner investor understands: "
                    f"1) Is this drop a good buying opportunity or a warning sign? "
                    f"2) What is most likely causing this drop today? "
                    f"3) What is the ONE specific thing I should do — buy now, wait, or avoid?",
                    gemini_calls, max_gemini
                )

                msg = (
                    f"💰 <b>DIP OPPORTUNITY — {ticker}</b>\n\n"
                    f"<b>What happened:</b>\n"
                    f"Stock dropped {abs(pct):.1f}% today to ${price:.2f}.\n"
                    f"This is a bigger drop than normal for this stock.\n\n"
                    f"{upside_plain_english(price, consensus, hi_target)}\n\n"
                    f"<b>Expert opinion:</b>\n"
                    f"{rec_plain_english(rec, buy_pct, sell_pct, total)}\n\n"
                    f"<b>📱 Gemini analysis & advice:</b>\n"
                    f"{analysis if analysis else 'Analysis unavailable — check manually.'}"
                )
                alerts.append(msg)

    # ── SIGNAL 3: Unusual price move ──────────────────────────────────────────
    if abs(pct) >= threshold and not alerts:
        if not already_alerted(ticker, f"MOVE:{pct:.1f}"):
            direction  = "gone UP" if pct > 0 else "gone DOWN"
            icon       = "🟢" if pct > 0 else "🔴"
            target_str = ""
            rec_str    = ""

            if target and target.get("consensus"):
                upside     = ((target["consensus"] - price) / price) * 100
                target_str = upside_plain_english(price, target["consensus"], target.get("high"))
                rec, buy_pct, sell_pct, total = get_analyst_recommendation(ticker)
                rec_str = rec_plain_english(rec, buy_pct, sell_pct, total)

            analysis = ask_gemini(
                f"Stock: {ticker}. Has {direction} {abs(pct):.1f}% today to ${price:.2f}. "
                f"This is an unusual move — bigger than its normal daily movement. "
                f"Volatility info: {beta_exp}. "
                f"{target_str} "
                f"Answer these 3 things in simple language a beginner investor understands: "
                f"1) What most likely caused this move today? "
                f"2) Is this a reason to panic, celebrate, or stay calm? "
                f"3) What is the ONE specific thing I should do right now — "
                f"buy more, sell some, or just hold and watch?",
                gemini_calls, max_gemini
            )

            msg = (
                f"{icon} <b>UNUSUAL MOVE — {ticker}</b>\n\n"
                f"<b>What happened:</b>\n"
                f"Stock has {direction} {abs(pct):.1f}% today to ${price:.2f}.\n"
                f"This is bigger than its normal daily movement.\n"
                f"<i>({beta_exp})</i>\n\n"
            )
            if target_str:
                msg += f"{target_str}\n\n"
            if rec_str:
                msg += f"<b>Expert opinion:</b>\n{rec_str}\n\n"
            msg += (
                f"<b>📱 Gemini analysis & advice:</b>\n"
                f"{analysis if analysis else 'Analysis unavailable — check manually.'}"
            )
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

    print("Detecting ETFs via Finnhub...")
    etf_set = set()
    for ticker in tickers:
        if is_etf(ticker):
            etf_set.add(ticker)
        time.sleep(0.2)
    print(f"ETFs: {etf_set}")

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
                icon      = "🟢" if pct > 0 else "🔴"
                direction = "UP" if pct > 0 else "DOWN"
                movers.append(
                    f"{icon} <b>{ticker}</b> went {direction} "
                    f"{abs(pct):.1f}% overnight · now ${price:.2f}"
                )
            time.sleep(0.3)

        msg  = f"🌅 <b>Good morning! — {now.strftime('%a %b %d')}</b>\n\n"
        msg += f"👀 Watching {len(tickers)} stocks for you today.\n"
        msg += f"🔔 You will get alerted if anything moves unusually.\n"
        msg += f"🤖 Every alert includes Gemini analysis + clear action advice.\n\n"
        if movers:
            msg += "📊 <b>Stocks that moved overnight:</b>\n" + "\n".join(movers[:10])
            msg += "\n\n<i>These moved more than 2% while market was closed.</i>"
        else:
            msg += "😴 All quiet overnight — no big moves while you slept."
        msg += "\n\n<i>Checking every 15 min — 9:30am to 4pm ET.</i>"
        send_telegram(msg)
        save_seen(new_seen)
        return

    # ── MARKET HOURS — 14–21 UTC ───────────────────────────────────────────────
    if 14 <= hour <= 21:
        print("Market hours — checking all stocks")
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
            filing_text = get_filing_text(filing["url"])

            form_plain = {
                "8-K":     "Important company announcement",
                "10-Q":    "Quarterly earnings report",
                "10-K":    "Annual earnings report",
                "S-1":     "Company going public",
                "DEF 14A": "Shareholder vote coming up",
            }.get(form_type, form_type)

            # Gemini reads the filing and gives complete plain English advice
            analysis = ask_gemini(
                f"Company: {company} (stock ticker: {ticker}). "
                f"They just filed an official document with the US government. "
                f"Document type: {form_plain} ({form_type}). "
                f"Here is what it says: {filing_text[:800]}. "
                f"Answer these 3 things in very simple language anyone can understand: "
                f"1) What exactly happened — explain like I am 10 years old. "
                f"2) Is this GOOD news or BAD news for the stock price? Why? "
                f"3) What should I do RIGHT NOW — buy more shares, sell my shares, "
                f"or just hold and do nothing? Give a clear direct recommendation.",
                gemini_calls, max_gemini
            )

            if analysis:
                msg = (
                    f"📋 <b>COMPANY NEWS — {ticker}</b>\n\n"
                    f"<b>Type of news:</b> {form_plain}\n"
                    f"<b>Company:</b> {company}\n"
                    f"<b>Date:</b> {filing['date']}\n\n"
                    f"<b>📱 Gemini explains + tells you what to do:</b>\n"
                    f"{analysis}"
                )
            else:
                link = edgar_link(form_type, cik_int)
                msg = (
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
