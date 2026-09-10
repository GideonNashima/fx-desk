#!/usr/bin/env python3
"""Fully autonomous FX briefing builder. No manual input after setup."""
import json, os, re, sys, urllib.request, datetime

OUT = "data.json"

BANK_NAMES = {
    "USD": "Federal Reserve", "EUR": "European Central Bank",
    "GBP": "Bank of England", "JPY": "Bank of Japan",
    "AUD": "Reserve Bank of Australia", "CAD": "Bank of Canada",
    "CHF": "Swiss National Bank", "NZD": "Reserve Bank of New Zealand",
}
# Best available free FRED proxies for policy rates. USD/EUR are the
# actual policy rate; others are OECD interbank-rate proxies (monthly,
# tracks policy rate closely but can lag). Verify at fred.stlouisfed.org
# if a currency keeps returning None.
RATE_SERIES = {
    "USD": "DFF", "EUR": "ECBDFR",
    "GBP": "IRSTCI01GBM156N", "JPY": "IRSTCI01JPM156N",
    "AUD": "IRSTCI01AUM156N", "CAD": "IRSTCI01CAM156N",
    "CHF": "IRSTCI01CHM156N", "NZD": "IRSTCI01NZM156N",
}
INTERMARKET_SERIES = {
    "US10Y": "DGS10", "US2Y": "DGS2",
    "brent": "DCOILBRENTEU", "wti": "DCOILWTICO", "vix": "VIXCLS",
}
RATE_CHANGE_THRESHOLD = 0.05  # percentage points

def log(m): print(m, file=sys.stderr)

def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "fx-desk/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def fetch_fx():
    try:
        d = fetch_json("https://api.frankfurter.app/latest?from=USD")
        return d.get("date"), d.get("rates", {})
    except Exception as e:
        log(f"FX fetch failed: {e}")
        return None, {}

def fetch_fred(sid, key):
    try:
        u = (f"https://api.stlouisfed.org/fred/series/observations"
             f"?series_id={sid}&api_key={key}&file_type=json&sort_order=desc&limit=1")
        obs = fetch_json(u)["observations"]
        v = obs[0]["value"] if obs else None
        return float(v) if v and v != "." else None
    except Exception as e:
        log(f"FRED {sid} failed: {e}")
        return None

def pairs_from(rates):
    if not rates: return {}
    inv = lambda c: round(1/rates[c], 5) if rates.get(c) else None
    return {
        "EUR/USD": inv("EUR"), "GBP/USD": inv("GBP"),
        "AUD/USD": inv("AUD"), "NZD/USD": inv("NZD"),
        "USD/JPY": round(rates["JPY"], 3) if rates.get("JPY") else None,
        "USD/CAD": round(rates["CAD"], 5) if rates.get("CAD") else None,
        "USD/CHF": round(rates["CHF"], 5) if rates.get("CHF") else None,
    }

def derive_stance(ccy, new_rate, prior_rate):
    if new_rate is None:
        return "hold", None
    if prior_rate is None:
        return "hold", None
    diff = round(new_rate - prior_rate, 3)
    if diff >= RATE_CHANGE_THRESHOLD:
        return "hike", diff
    if diff <= -RATE_CHANGE_THRESHOLD:
        return "cut", diff
    return "hold", diff

def rank(banks, yields):
    scores = {}
    for ccy, b in banks.items():
        s = {"hike": 2.0, "hold": 0.0, "cut": -2.0}.get(b["stance"], 0.0)
        y, u = yields.get(ccy), yields.get("USD")
        if y is not None and u is not None:
            s += (y - u) * 0.4
        scores[ccy] = s
    return sorted(scores, key=lambda c: -scores[c])

SYSTEM = """You are an FX desk analyst. You receive ONLY numeric market
data and must write a briefing strictly from it.

HARD RULES:
- NEVER state a price, rate, or event not present in the INPUT DATA below.
- NEVER invent news, dates, or facts. You have no information beyond
  these numbers.
- If you want to describe "why," describe it in terms of the data
  relationships themselves (e.g. rate differentials, yield spreads,
  a rate that just moved) — not external events you cannot verify.
- Output ONE JSON object, nothing else, no markdown fences.

Return exactly this shape:
{
  "macro": {"backdrop": "<p>...</p>", "risk": "<p>...</p>"},
  "ideas": {
    "<CCY>": {"direction": "bull|bear|neutral", "conviction": "high|medium|low",
              "convictionText": "...", "body": "<p>...</p>"}
  },
  "pairs": {
    "<NAME>": {"direction": "bull|bear|neutral", "bias": "...",
               "fundamental": "<p>...</p>", "technical": "<p>...</p>",
               "watch": "..."}
  },
  "weekly": {"dxy": "<p>...</p>"}
}
Include all 8 currencies in "ideas" and all 7 pairs in "pairs":
EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD, USD/CHF, NZD/USD."""

def call_gemini(api_key, market, change_log):
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash", system_instruction=SYSTEM)
    prompt = f"""INPUT DATA (use only this):
{json.dumps(market, indent=2)}

RECENT RATE CHANGES DETECTED:
{json.dumps(change_log[-10:], indent=2)}

Write the briefing now."""
    resp = model.generate_content(prompt)
    text = resp.text.strip()
    text = re.sub(r"^```json|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)

def build():
    prior = json.load(open(OUT)) if os.path.exists(OUT) else {}
    prior_market = prior.get("market", {})
    prior_rates = prior_market.get("rates", {})
    change_log = prior.get("change_log", [])

    fred_key = os.environ.get("FRED_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    today = datetime.date.today().isoformat()

    fx_date, fx_rates = fetch_fx()
    pairs = pairs_from(fx_rates) or prior_market.get("pairs", {})
    asof = fx_date or prior_market.get("asof", "—")

    rates, banks = {}, {}
    for ccy, sid in RATE_SERIES.items():
        new_rate = fetch_fred(sid, fred_key) if fred_key else None
        if new_rate is None:
            new_rate = prior_rates.get(ccy)
        stance, diff = derive_stance(ccy, new_rate, prior_rates.get(ccy))
        if diff is not None and abs(diff) >= RATE_CHANGE_THRESHOLD:
            change_log.append({"date": today, "ccy": ccy,
                                "from": prior_rates.get(ccy), "to": new_rate})
        rates[ccy] = new_rate
        banks[ccy] = {"bank": BANK_NAMES[ccy],
                       "rate": f"{new_rate:.2f}%" if new_rate is not None else "—",
                       "stance": stance, "lastChecked": today}

    fred = dict(prior_market.get("fred", {}))
    if fred_key:
        for label, sid in INTERMARKET_SERIES.items():
            v = fetch_fred(sid, fred_key)
            if v is not None:
                fred[label] = v

    market = {"asof": asof, "fetched": datetime.datetime.utcnow().isoformat(timespec="seconds")+"Z",
              "pairs": pairs, "rates": rates, "fred": fred}

    analysis = prior.get("analysis")  # fallback if Gemini call fails
    if gemini_key:
        try:
            analysis = call_gemini(gemini_key, market, change_log)
        except Exception as e:
            log(f"Gemini generation failed, keeping prior analysis: {e}")
    if analysis is None:
        analysis = {"macro": {"backdrop": "<p>Awaiting first successful analysis run.</p>", "risk": ""},
                    "ideas": {}, "pairs": {}, "weekly": {"dxy": ""}}

    order = rank(banks, rates)

    data = {
        "generated": market["fetched"], "market": market,
        "banks": banks, "change_log": change_log[-30:],
        "macro": analysis.get("macro", {}),
        "weekly": analysis.get("weekly", {}),
        "ideas": [dict(analysis.get("ideas", {}).get(c, {}), ccy=c, rank=i+1)
                  for i, c in enumerate(order)],
        "pairs": [dict(analysis.get("pairs", {}).get(n, {}), name=n)
                  for n in ["EUR/USD","GBP/USD","USD/JPY","AUD/USD","USD/CAD","USD/CHF","NZD/USD"]],
    }
    json.dump(data, open(OUT, "w"), indent=2)
    log(f"wrote {OUT} — FX as of {asof}, {len(change_log)} changes logged")

if __name__ == "__main__":
    build()
