"""
SAP On-Demand Search
====================
Triggered manually from GitHub Actions when the weekly knowledge base
does not have enough information on a specific topic.

Usage on GitHub:
  Go to Actions tab → SAP Knowledge Crawler → Run workflow
  Type your search topic in the "search_query" box (e.g. "IPD BOM sync S4HANA")
  Click Run workflow

Results appear in the "On_Demand_Search" tab of your Google Sheet within minutes.
"""

import os
import sys
import json
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# ─── CONFIG ────────────────────────────────────────────────────────────────────

GEMINI_API_KEY          = os.environ["GEMINI_API_KEY"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GITHUB_TOKEN            = os.environ.get("GITHUB_TOKEN", "")
PROGRESS_FILE           = "progress.json"

genai.configure(api_key=GEMINI_API_KEY)
_model = genai.GenerativeModel("gemini-1.5-flash")

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SAP-OnDemand/1.0)"}

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def safe_get(url: str):
    time.sleep(2)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        if resp.status_code == 200:
            return resp
    except Exception as e:
        print(f"  [Error] {url}: {e}")
    return None


def gemini_score(text: str) -> float:
    try:
        raw = _model.generate_content(
            f"Score 0-10 for relevance to SAP PLM/IPD/BTP. Return ONLY a number.\n{text[:1000]}"
        ).text.strip().split()[0]
        return min(max(float(raw), 0.0), 10.0)
    except Exception:
        return 5.0


def gemini_summarise(text: str) -> str:
    try:
        return _model.generate_content(
            f"Summarise in 2 bullet points for an SAP PLM/IPD consultant:\n{text[:2000]}"
        ).text.strip()
    except Exception:
        return text[:300]


# ─── SEARCH ENGINES ───────────────────────────────────────────────────────────

def duckduckgo_search(query: str, max_results: int = 8) -> list:
    """Search DuckDuckGo — no API key needed, completely free."""
    print(f"  [DuckDuckGo] {query}")
    results = []
    encoded = requests.utils.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    resp = safe_get(url)
    if not resp:
        return results

    soup = BeautifulSoup(resp.text, "lxml")
    for item in soup.select(".result")[:max_results]:
        link = item.select_one(".result__a")
        snippet = item.select_one(".result__snippet")
        if link and link.get("href"):
            results.append({
                "title": link.get_text(strip=True),
                "url": link["href"],
                "snippet": snippet.get_text(strip=True) if snippet else "",
            })
    return results


def sap_community_search(query: str) -> list:
    """Search SAP Community directly."""
    print(f"  [SAP Community] {query}")
    encoded = requests.utils.quote(query)
    url = f"https://community.sap.com/t5/forums/searchpage/tab/message?q={encoded}"
    resp = safe_get(url)
    if not resp:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []
    for a in soup.select("a.page-link, a.lia-link-navigation")[:10]:
        href = a.get("href", "")
        title = a.get_text(strip=True)
        if title and href and "community.sap.com" in href:
            results.append({"title": title, "url": href, "snippet": ""})
    return results


def github_search(query: str) -> list:
    """Search GitHub repos for community SAP solutions."""
    print(f"  [GitHub] {query}")
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    encoded = requests.utils.quote(query)
    url = f"https://api.github.com/search/repositories?q={encoded}&sort=stars&per_page=5"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            return [
                {
                    "title": r["full_name"],
                    "url": r["html_url"],
                    "snippet": r.get("description", ""),
                }
                for r in resp.json().get("items", [])
            ]
    except Exception:
        pass
    return []


# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

def load_on_demand_tab():
    """Open the On_Demand_Search tab in the existing Google Sheet."""
    import json as _json

    creds_dict = _json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)

    if not os.path.exists(PROGRESS_FILE):
        print("progress.json not found — run crawler.py first to create the Google Sheet")
        sys.exit(1)

    with open(PROGRESS_FILE, encoding="utf-8") as f:
        progress = json.load(f)

    sheet_id = progress.get("sheet_id")
    if not sheet_id:
        print("No sheet ID in progress.json — run crawler.py first")
        sys.exit(1)

    ss = gc.open_by_key(sheet_id)
    try:
        ws = ss.worksheet("On_Demand_Search")
    except Exception:
        ws = ss.add_worksheet("On_Demand_Search", rows=2000, cols=10)
        ws.append_row([
            "Product", "Title", "URL", "Summary",
            "Score", "Evidence_Type", "Confidence",
            "Referenced_Notes", "Date_Crawled",
        ])
    return ws


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    # Get search query from environment (set by GitHub Actions) or command line
    query = (
        os.environ.get("SEARCH_QUERY", "").strip()
        or (sys.argv[1].strip() if len(sys.argv) > 1 else "")
    )
    if not query:
        print("No search query provided.")
        print("Set the SEARCH_QUERY environment variable or pass it as a command-line argument.")
        sys.exit(1)

    print("=" * 65)
    print(f"On-Demand SAP Search: '{query}'")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 65)

    # Run all three search sources
    raw_results = []
    raw_results += duckduckgo_search(f"SAP IPD {query} site:help.sap.com OR site:community.sap.com")
    raw_results += duckduckgo_search(f"SAP BTP {query}")
    raw_results += sap_community_search(f"SAP IPD {query}")
    raw_results += github_search(f"SAP {query}")

    print(f"\nRaw results collected: {len(raw_results)}")

    # Score and summarise
    rows = []
    seen_urls = set()
    for r in raw_results:
        if r["url"] in seen_urls:
            continue
        seen_urls.add(r["url"])

        combined = f"{r['title']}\n{r['snippet']}"
        score = gemini_score(combined)
        if score < 4:
            continue

        summary = gemini_summarise(combined)
        rows.append([
            "On-Demand",
            r["title"][:200],
            r["url"],
            summary,
            round(score, 1),
            "COMMUNITY",
            "? ASSUMED",
            "",
            datetime.now().strftime("%Y-%m-%d"),
        ])
        print(f"  [Kept] {r['title'][:70]} | Score: {score:.1f}")

    # Write to Google Sheet
    if rows:
        ws = load_on_demand_tab()
        ws.append_rows(rows, value_input_option="RAW")
        print(f"\nSaved {len(rows)} results to On_Demand_Search tab")
    else:
        print("\nNo relevant results found for this query")

    print("Done.")


if __name__ == "__main__":
    main()
