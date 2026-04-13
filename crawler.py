"""
SAP Knowledge Crawler
=====================
Runs automatically every Sunday on GitHub Actions.
Uses SAP's own public search API to get documentation content — no browser needed.
Crawls: SAP Help API, SAP Community, GitHub repos.
Saves to Google Sheets (organised by tab) and Excel.
Uses Gemini Flash (free) for scoring and summarisation.
"""

import os
import json
import time
import re
import base64
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai
from openpyxl import Workbook

# ─── CONFIG ────────────────────────────────────────────────────────────────────

GEMINI_API_KEY          = os.environ["GEMINI_API_KEY"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GITHUB_TOKEN            = os.environ.get("GITHUB_TOKEN", "")

# Your Google Sheet — pre-created and shared with the service account
GOOGLE_SHEET_ID = "1eiZ3f1N-YAop3gA7pe3YEqrh2ZuW01RHoxx8ncGmkn4"

RATE_LIMIT      = 2.5   # seconds between requests
RETRY_WAIT      = 60    # seconds to wait on 429 error
BATCH_SIZE      = 20    # rows per Sheets write
SCORE_THRESHOLD = 4     # skip pages scoring below this
SAVE_INTERVAL   = 20    # save progress every N pages
PROGRESS_FILE   = "progress.json"
EXCEL_FILE      = "sap_knowledge_base.xlsx"

# SAP products to search — these drive ALL content discovery
SAP_SEARCH_QUERIES = [
    # Direct IPD/EPD
    ("SAP_IPD",           "SAP Integrated Product Development IPD"),
    ("SAP_IPD",           "SAP IPD BTP configuration admin"),
    ("SAP_IPD",           "SAP IPD API integration extension"),
    ("SAP_EPD",           "SAP Engineering Product Development EPD"),
    ("SAP_EPD",           "SAP EPD BOM variant configuration"),
    # BTP tools (both sides connector)
    ("BTP_Tools",         "SAP Integration Suite Cloud Integration iFlow"),
    ("BTP_Tools",         "SAP BTP Extension Suite CAP model"),
    ("BTP_Tools",         "SAP Build Apps low code extension"),
    ("BTP_Tools",         "SAP Event Mesh messaging BTP"),
    ("BTP_Tools",         "SAP API Management BTP"),
    # BTP SaaS analogy peers
    ("SuccessFactors",    "SAP SuccessFactors BTP extension side-by-side"),
    ("SuccessFactors",    "SAP SuccessFactors integration BTP API"),
    ("Ariba",             "SAP Ariba BTP extension integration"),
    # Integration landscape — other side
    ("S4HANA",            "SAP S4HANA RISE private cloud integration BTP"),
    ("S4HANA",            "SAP S4HANA PLM product lifecycle management"),
    ("SAP_PLM",           "SAP PLM classic ECC product lifecycle"),
    ("SAP_PLM",           "SAP PLM migration S4HANA IPD"),
    ("SAP_DMS",           "SAP DMS document management system integration"),
    ("SAP_ECTR",          "SAP ECTR Engineering Control Center CAD"),
    ("SAP_MDG",           "SAP MDG master data governance material"),
    # Migration
    ("Migration",         "ECC to S4HANA PLM migration roadmap"),
    ("Migration",         "SAP PLM to IPD migration transition"),
]

SAP_COMMUNITY_QUERIES = [
    "SAP IPD integrated product development",
    "SAP EPD engineering product development BTP",
    "SAP PLM S4HANA migration integration",
    "SAP IPD BOM integration S4HANA",
    "SAP BTP extension SuccessFactors side-by-side",
    "SAP Integration Suite PLM iFlow",
    "SAP IPD API extension customization",
    "SAP DMS document management IPD",
]

SHEET_TABS = [
    "SAP_IPD_Direct",
    "SAP_EPD_Direct",
    "BTP_Analogy",
    "BTP_Tools",
    "Community_Discussions",
    "SAP_Notes",
    "GitHub_Community",
    "On_Demand_Search",
]

SHEET_HEADERS = [
    "Product", "Title", "URL", "Summary",
    "Score", "Evidence_Type", "Confidence",
    "Referenced_Notes", "Date_Crawled",
]

SAP_NOTE_RE = re.compile(
    r'\b(?:SAP\s+)?[Nn]ote\s+#?(\d{6,10})\b|KBA\s+#?(\d{6,10})'
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ─── GEMINI ────────────────────────────────────────────────────────────────────

genai.configure(api_key=GEMINI_API_KEY)
_model = genai.GenerativeModel("gemini-1.5-flash")


def gemini_score(text: str, product: str = "SAP") -> float:
    try:
        prompt = (
            "Score this SAP content 0-10 for relevance to SAP PLM, IPD, EPD, "
            "BTP extensions, SuccessFactors, or S/4HANA integration. "
            "Return ONLY a single number 0-10, nothing else.\n\n"
            f"Product: {product}\nContent:\n{text[:1500]}"
        )
        raw = _model.generate_content(prompt).text.strip().split()[0]
        return min(max(float(raw), 0.0), 10.0)
    except Exception:
        return 0.0


def gemini_summarise(text: str, product: str, url: str) -> str:
    try:
        prompt = (
            "Summarise this SAP content in exactly 3 concise bullet points. "
            "Focus on: what capability is described, how to configure/use it, "
            "and any integration or extension relevance. Start each bullet with •\n\n"
            f"Product: {product}\nURL: {url}\nContent:\n{text[:3000]}"
        )
        return _model.generate_content(prompt).text.strip()
    except Exception:
        return "• Summary unavailable"


def gemini_github_queries() -> list:
    try:
        prompt = (
            "Generate 10 GitHub search queries to find open-source SAP solutions. "
            "Focus on: SAP IPD, SAP EPD, SAP PLM, BTP extensions, S/4HANA migration, "
            "SuccessFactors CAP, SAP Integration Suite iFlows, ECC PLM migration tools. "
            "Return ONLY the queries, one per line."
        )
        lines = _model.generate_content(prompt).text.strip().split("\n")
        return [l.strip().lstrip("0123456789.-) ") for l in lines if l.strip()][:10]
    except Exception:
        return [
            "SAP IPD BTP extension",
            "SAP PLM S4HANA integration",
            "SAP Integration Suite iFlow PLM",
            "SuccessFactors BTP extension CAP",
            "SAP ECC PLM migration S4HANA",
            "SAP CAP model PLM",
            "SAP BTP adapter ABAP",
            "SAP DMS document management integration",
            "SAP MDG master data governance",
            "SAP EPD engineering product development",
        ]


# ─── PROGRESS ─────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"visited_queries": [], "sheet_id": GOOGLE_SHEET_ID, "last_save": None}


def save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


# ─── HTTP ──────────────────────────────────────────────────────────────────────

def safe_get(url: str, headers: dict = None, params: dict = None, retries: int = 3):
    time.sleep(RATE_LIMIT)
    h = headers or _HEADERS
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=h, params=params, timeout=20)
            if resp.status_code == 429:
                print(f"    [429] Waiting {RETRY_WAIT}s...")
                time.sleep(RETRY_WAIT)
                continue
            if resp.status_code == 200:
                return resp
            print(f"    [HTTP {resp.status_code}] {url}")
            return None
        except Exception as e:
            print(f"    [Error {attempt+1}] {e}")
            time.sleep(5)
    return None


# ─── SAP HELP PORTAL API ──────────────────────────────────────────────────────

def search_sap_help_api(query: str, max_results: int = 15) -> list:
    """
    Use SAP's public help portal search API to get documentation content.
    This bypasses the JavaScript rendering problem completely.
    """
    results = []

    # SAP Help Portal search API (public, no auth required)
    api_url = "https://help.sap.com/api/search"
    params = {
        "q": query,
        "language": "en-US",
        "state": "PRODUCTION",
        "type": "TOPIC",
    }
    resp = safe_get(api_url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }, params=params)

    if resp:
        try:
            data = resp.json()
            items = data.get("data", []) or data.get("hits", []) or []
            for item in items[:max_results]:
                title = item.get("title", "") or item.get("shortTitle", "")
                url   = item.get("link", "") or item.get("url", "") or item.get("path", "")
                desc  = (item.get("description", "") or
                         item.get("excerpt", "") or
                         item.get("body", ""))[:2000]
                if url and not url.startswith("http"):
                    url = f"https://help.sap.com{url}"
                if title and url:
                    results.append({"title": title, "url": url, "text": desc})
            print(f"    [SAP API] '{query}' → {len(results)} results")
            return results
        except Exception as e:
            print(f"    [SAP API parse error] {e}")

    # Fallback: DuckDuckGo search scoped to help.sap.com
    return duckduckgo_search(f"site:help.sap.com {query}", max_results)


def duckduckgo_search(query: str, max_results: int = 10) -> list:
    """Search DuckDuckGo — free, no API key needed."""
    print(f"    [DuckDuckGo] {query}")
    results = []
    url = f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}"
    resp = safe_get(url)
    if not resp:
        return results
    soup = BeautifulSoup(resp.text, "lxml")
    for item in soup.select(".result")[:max_results]:
        link    = item.select_one(".result__a")
        snippet = item.select_one(".result__snippet")
        if link and link.get("href"):
            href = link["href"]
            # DuckDuckGo wraps URLs — extract the actual URL
            if "uddg=" in href:
                href = requests.utils.unquote(href.split("uddg=")[-1].split("&")[0])
            results.append({
                "title":   link.get_text(strip=True),
                "url":     href,
                "text":    snippet.get_text(strip=True) if snippet else "",
            })
    return results


# ─── SAP COMMUNITY ────────────────────────────────────────────────────────────

def crawl_community() -> list:
    """Search SAP Community for IPD/PLM related discussions and blog posts."""
    print("\n[Community] Searching SAP Community...")
    results = []
    seen = set()

    for query in SAP_COMMUNITY_QUERIES:
        encoded = requests.utils.quote(query)
        search_url = f"https://community.sap.com/t5/forums/searchpage/tab/message?q={encoded}&advanced=false&collapse_discussion=true&search_type=thread&solved=false"
        resp = safe_get(search_url)
        if not resp:
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        # Extract search result links from community
        for a in soup.select("a.page-link, a.lia-link-navigation, h2 a, .search-results a")[:8]:
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not href or not title or len(title) < 10:
                continue
            if not href.startswith("http"):
                href = f"https://community.sap.com{href}"
            if href in seen or "community.sap.com" not in href:
                continue
            seen.add(href)

            # Fetch the actual discussion page
            page_resp = safe_get(href)
            if not page_resp:
                continue
            page_soup = BeautifulSoup(page_resp.text, "lxml")
            text = page_soup.get_text(separator=" ", strip=True)[:3000]
            notes = extract_note_refs(text)
            score = gemini_score(text, "SAP Community")
            if score < SCORE_THRESHOLD:
                continue
            summary = gemini_summarise(text, "Community", href)
            results.append(_row("Community", title[:200], href, summary,
                                score, "COMMUNITY", "? ASSUMED", notes))
            print(f"    [Community OK] {title[:60]} | Score: {score:.1f}")

    # Also search DuckDuckGo for SAP community posts
    for query in ["SAP IPD integrated product development community.sap.com",
                  "SAP IPD BTP extension site:community.sap.com",
                  "SAP PLM IPD migration site:community.sap.com"]:
        for r in duckduckgo_search(query, max_results=5):
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            combined = f"{r['title']}\n{r['text']}"
            score = gemini_score(combined, "SAP Community")
            if score < SCORE_THRESHOLD:
                continue
            summary = gemini_summarise(combined, "Community", r["url"])
            notes = extract_note_refs(combined)
            results.append(_row("Community", r["title"][:200], r["url"], summary,
                                score, "COMMUNITY", "? ASSUMED", notes))
            print(f"    [DDG Community] {r['title'][:60]} | Score: {score:.1f}")

    print(f"[Community] Total: {len(results)} relevant posts")
    return results


# ─── GITHUB SEARCH ────────────────────────────────────────────────────────────

def search_github() -> list:
    """Search GitHub for community SAP solutions."""
    print("\n[GitHub] Searching for SAP solutions...")
    results = []
    queries = gemini_github_queries()
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    seen = set()

    for query in queries:
        time.sleep(2)
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "per_page": 5},
                headers=headers, timeout=15
            )
        except Exception:
            continue
        if resp.status_code != 200:
            continue

        for repo in resp.json().get("items", []):
            url = repo["html_url"]
            if url in seen:
                continue
            seen.add(url)

            readme = ""
            readme_resp = safe_get(
                f"https://api.github.com/repos/{repo['full_name']}/readme",
                headers=headers
            )
            if readme_resp:
                try:
                    readme = base64.b64decode(
                        readme_resp.json().get("content", "")
                    ).decode("utf-8", errors="ignore")[:3000]
                except Exception:
                    pass

            combined = f"{repo['name']}\n{repo.get('description','')}\n{readme}"
            score = gemini_score(combined, "GitHub SAP")
            if score < SCORE_THRESHOLD:
                continue
            summary = gemini_summarise(combined, "GitHub", url)
            results.append(_row("GitHub", repo["full_name"], url, summary,
                                score, "GITHUB", "? ASSUMED", []))
            print(f"    [GitHub OK] {repo['full_name']} | Score: {score:.1f}")

    return results


# ─── MAIN CRAWL LOOP ──────────────────────────────────────────────────────────

def crawl_sap_documentation(visited_queries: set, progress: dict, sheets: dict) -> set:
    """
    Use SAP Help API + DuckDuckGo to gather documentation for every search query.
    Each query maps to a product/topic. Results are scored and written to Sheets.
    """
    buffer = []
    queries_done = 0

    for product, query in SAP_SEARCH_QUERIES:
        if query in visited_queries:
            print(f"  [Skip] Already done: {query}")
            continue

        print(f"\n[SAP Docs] {product}: {query}")
        results = search_sap_help_api(query, max_results=15)

        for r in results:
            text = r["text"]
            if len(text) < 50:
                continue
            score = gemini_score(text, product)
            if score < SCORE_THRESHOLD:
                print(f"    [Skip] Score {score:.1f}: {r['title'][:60]}")
                continue

            notes = extract_note_refs(text)
            ev_type, confidence = classify(product, r["url"])
            summary = gemini_summarise(text, product, r["url"])
            buffer.append(_row(product, r["title"], r["url"], summary,
                               score, ev_type, confidence, notes))
            print(f"    [OK] {r['title'][:65]} | Score: {score:.1f} | {ev_type}")

        visited_queries.add(query)
        queries_done += 1

        if len(buffer) >= BATCH_SIZE:
            flush(buffer, sheets)
            buffer = []

        if queries_done % SAVE_INTERVAL == 0:
            progress["visited_queries"] = list(visited_queries)
            progress["last_save"] = datetime.now().isoformat()
            save_progress(progress)
            print(f"  [Progress saved] {queries_done} queries done")

    if buffer:
        flush(buffer, sheets)

    return visited_queries


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def extract_note_refs(text: str) -> list:
    found = []
    for m in SAP_NOTE_RE.finditer(text):
        num = m.group(1) or m.group(2)
        if num:
            found.append(num)
    return list(set(found))


def classify(product: str, url: str) -> tuple:
    p, u = product.lower(), url.lower()
    if any(x in u or x in p for x in ["sap_ipd", "ipd", "integrated-product"]):
        return "DIRECT_IPD", "✓ CONFIRMED"
    if any(x in u or x in p for x in ["sap_epd", "epd", "engineering-product"]):
        return "DIRECT_IPD", "✓ CONFIRMED"
    if any(x in p for x in ["successfactors", "ariba", "concur", "fsm"]):
        return "BTP_ANALOGY", "~ ANALOGY"
    if any(x in p for x in ["btp", "btp_tools", "integration_suite", "build",
                             "cap", "event", "api management"]):
        return "BTP_TOOL", "✓ CONFIRMED"
    if "community.sap.com" in u:
        return "COMMUNITY", "? ASSUMED"
    if "github.com" in u:
        return "GITHUB", "? ASSUMED"
    return "DIRECT", "✓ CONFIRMED"


def _row(product, title, url, summary, score, ev_type, confidence, notes) -> dict:
    return {
        "product":    product,
        "title":      str(title)[:200],
        "url":        str(url),
        "summary":    str(summary),
        "score":      round(float(score), 1),
        "evidence_type": ev_type,
        "confidence": confidence,
        "referenced_notes": ", ".join(notes) if notes else "",
        "date":       datetime.now().strftime("%Y-%m-%d"),
    }


# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

def init_sheets(progress):
    import json as _json
    creds_dict = _json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)

    ss = gc.open_by_key(GOOGLE_SHEET_ID)
    progress["sheet_id"] = GOOGLE_SHEET_ID
    print(f"[Sheets] Opened: {ss.title}")

    existing = {ws.title: ws for ws in ss.worksheets()}
    sheets = {}
    for tab in SHEET_TABS:
        if tab not in existing:
            ws = ss.add_worksheet(title=tab, rows=2000, cols=len(SHEET_HEADERS))
            ws.append_row(SHEET_HEADERS)
            print(f"[Sheets] Created tab: {tab}")
        else:
            ws = existing[tab]
        sheets[tab] = ws

    return gc, ss, sheets


def _route_tab(ev_type: str, product: str) -> str:
    p = product.lower()
    if "epd" in p:
        return "SAP_EPD_Direct"
    if ev_type == "DIRECT_IPD" or "ipd" in p:
        return "SAP_IPD_Direct"
    if ev_type == "BTP_ANALOGY":
        return "BTP_Analogy"
    if ev_type == "BTP_TOOL" or "btp" in p:
        return "BTP_Tools"
    if ev_type == "COMMUNITY" or "community" in p:
        return "Community_Discussions"
    if ev_type == "GITHUB" or "github" in p:
        return "GitHub_Community"
    # Route by product name
    if any(x in p for x in ["s4hana", "plm", "dms", "ectr", "mdg", "migration"]):
        return "SAP_IPD_Direct"
    return "SAP_IPD_Direct"


def flush(results: list, sheets: dict) -> None:
    groups = {}
    for r in results:
        tab = _route_tab(r["evidence_type"], r["product"])
        groups.setdefault(tab, []).append(r)

    for tab, rows in groups.items():
        ws = sheets.get(tab)
        if not ws:
            continue
        batch = [[
            r["product"], r["title"], r["url"], r["summary"],
            r["score"], r["evidence_type"], r["confidence"],
            r["referenced_notes"], r["date"],
        ] for r in rows]
        ws.append_rows(batch, value_input_option="RAW")
        print(f"    [Sheets] {len(batch)} rows → {tab}")


# ─── EXCEL ────────────────────────────────────────────────────────────────────

def export_excel(ss) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    for ws in ss.worksheets():
        if ws.title == "SAP_Notes":
            continue
        data = ws.get_all_values()
        if not data:
            continue
        xws = wb.create_sheet(title=ws.title[:31])
        for row in data:
            xws.append(row)
    wb.save(EXCEL_FILE)
    print(f"[Excel] Saved: {EXCEL_FILE}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("SAP Knowledge Crawler")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 65)

    progress = load_progress()
    visited_queries = set(progress.get("visited_queries", []))
    print(f"Resuming — {len(visited_queries)} queries already done")

    gc, ss, sheets = init_sheets(progress)

    # 1 — SAP documentation via Help API + DuckDuckGo
    visited_queries = crawl_sap_documentation(visited_queries, progress, sheets)

    # 2 — SAP Community discussions
    community = crawl_community()
    if community:
        flush(community, sheets)

    # 3 — GitHub community solutions
    github = search_github()
    if github:
        flush(github, sheets)

    # 4 — Save final progress
    progress["visited_queries"] = list(visited_queries)
    progress["last_save"] = datetime.now().isoformat()
    save_progress(progress)

    # 5 — Export Excel
    export_excel(ss)

    print("\n" + "=" * 65)
    print("Crawl complete!")
    print(f"Google Sheet : https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}")
    print(f"Excel file   : {EXCEL_FILE}")
    print("=" * 65)


if __name__ == "__main__":
    main()
