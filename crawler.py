"""
SAP Knowledge Crawler
=====================
Runs automatically every Sunday on GitHub Actions.
Crawls all public SAP documentation, community posts, and GitHub repos.
Saves results to Google Sheets (organised by tab) and Excel.
Uses Gemini Flash (free) for scoring and summarisation.
No paid APIs. No S-User. Nothing on your laptop.
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

GEMINI_API_KEY           = os.environ["GEMINI_API_KEY"]
GOOGLE_CREDENTIALS_JSON  = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GITHUB_TOKEN             = os.environ.get("GITHUB_TOKEN", "")
DRIVE_FOLDER_ID          = "1YxH9VRbbKR3t1Ci4C7RhZ28T4vKDCppo"  # Your Google Drive folder

RATE_LIMIT       = 2.5   # seconds between every page request (SAP never blocks at this speed)
RETRY_WAIT       = 60    # seconds to wait when SAP returns a 429 "too many requests" error
BATCH_SIZE       = 20    # rows written to Google Sheets at once
SCORE_THRESHOLD  = 4     # pages scoring below this are skipped
SAVE_INTERVAL    = 20    # save progress.json every N pages
PROGRESS_FILE    = "progress.json"
EXCEL_FILE       = "sap_knowledge_base.xlsx"

# These sources are always crawled — critical for BTP SaaS analogy reasoning
PRIORITY_SOURCES = [
    ("SAP_IPD",           "https://help.sap.com/docs/SAP_IPD"),
    ("SAP_EPD",           "https://help.sap.com/docs/SAP_EPD"),
    ("SuccessFactors",    "https://help.sap.com/docs/SAP_SUCCESSFACTORS_HXM_SUITE"),
    ("Ariba",             "https://help.sap.com/docs/ARIBA"),
    ("SAP_BTP",           "https://help.sap.com/docs/BTP"),
    ("Integration_Suite", "https://help.sap.com/docs/CLOUD_INTEGRATION"),
    ("SAP_Build_Apps",    "https://help.sap.com/docs/SAP_BUILD_APPS"),
    ("SAP_CAP",           "https://help.sap.com/docs/btp/sap-business-application-studio"),
    ("S4HANA_Cloud",      "https://help.sap.com/docs/SAP_S4HANA_CLOUD"),
    ("SAP_PLM",           "https://help.sap.com/docs/SAP_PLM"),
    ("SAP_DMS",           "https://help.sap.com/docs/SAP_DOCUMENT_MANAGEMENT"),
    ("SAP_MDG",           "https://help.sap.com/docs/SAP_MASTER_DATA_GOVERNANCE"),
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

# ─── GEMINI SETUP ──────────────────────────────────────────────────────────────

genai.configure(api_key=GEMINI_API_KEY)
_model = genai.GenerativeModel("gemini-1.5-flash")


def gemini_score(text: str, product: str = "SAP") -> float:
    """Ask Gemini to score page relevance 0–10. Returns 0 on failure."""
    try:
        prompt = (
            "Score this SAP documentation page 0-10 for relevance to "
            "SAP PLM, IPD, EPD, BTP extensions, SuccessFactors, Ariba, "
            "or S/4HANA integration. Return ONLY a single number 0-10.\n\n"
            f"Product context: {product}\n"
            f"Content preview:\n{text[:1500]}"
        )
        raw = _model.generate_content(prompt).text.strip().split()[0]
        return min(max(float(raw), 0.0), 10.0)
    except Exception:
        return 0.0


def gemini_summarise(text: str, product: str, url: str) -> str:
    """Summarise a page in 3 bullet points. Returns fallback on failure."""
    try:
        prompt = (
            "Summarise this SAP documentation page in exactly 3 concise bullet points. "
            "Focus on: what capability is described, how to configure or use it, "
            "and any integration or extension relevance. Start each bullet with •\n\n"
            f"Product: {product}\nURL: {url}\n\nContent:\n{text[:3000]}"
        )
        return _model.generate_content(prompt).text.strip()
    except Exception:
        return "• Summary unavailable"


def gemini_github_queries() -> list:
    """Generate 10 smart GitHub search queries using Gemini."""
    try:
        prompt = (
            "Generate 10 GitHub search queries to find community-built SAP solutions. "
            "Cover: SAP IPD, SAP EPD, SAP PLM, BTP extensions, S/4HANA integration, "
            "SuccessFactors extensions, SAP CAP models, SAP Integration Suite iFlows, "
            "ECC PLM migration, SAP ABAP BTP. "
            "Return ONLY the queries, one per line, no numbering, no explanation."
        )
        lines = _model.generate_content(prompt).text.strip().split("\n")
        return [l.strip() for l in lines if l.strip()][:10]
    except Exception:
        return [
            "SAP IPD BTP extension",
            "SAP PLM S4HANA integration",
            "SAP EPD customization BTP",
            "SAP Integration Suite iFlow PLM",
            "SuccessFactors BTP extension CAP",
            "SAP ABAP BTP adapter",
            "SAP ECC PLM migration S4HANA",
            "SAP CAP model PLM",
            "SAP API hub PLM connector",
            "SAP DMS document management integration",
        ]


# ─── PROGRESS MANAGER ─────────────────────────────────────────────────────────

def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"visited": [], "sheet_id": None, "last_save": None}


def save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


# ─── HTTP HELPER ──────────────────────────────────────────────────────────────

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SAP-Crawler/1.0)"}


def safe_get(url: str, headers: dict = None, retries: int = 3):
    """Fetch a URL with rate limiting and automatic 429 retry."""
    time.sleep(RATE_LIMIT)
    h = headers or _HEADERS
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=h, timeout=20)
            if resp.status_code == 429:
                print(f"    [429] Rate limited — waiting {RETRY_WAIT}s then retrying...")
                time.sleep(RETRY_WAIT)
                continue
            if resp.status_code == 200:
                return resp
            print(f"    [HTTP {resp.status_code}] {url}")
            return None
        except Exception as e:
            print(f"    [Error attempt {attempt+1}] {url}: {e}")
            time.sleep(5)
    return None


# ─── PRODUCT DISCOVERY ────────────────────────────────────────────────────────

def discover_sap_products() -> list:
    """Auto-discover all SAP products from help.sap.com/docs."""
    print("\n[Discovery] Reading help.sap.com/docs for all SAP products...")
    products = list(PRIORITY_SOURCES)  # always start with priority sources
    priority_urls = {p[1] for p in PRIORITY_SOURCES}

    resp = safe_get("https://help.sap.com/docs")
    if resp:
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set(priority_urls)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/docs/" not in href:
                continue
            full_url = href if href.startswith("http") else f"https://help.sap.com{href}"
            if full_url in seen:
                continue
            seen.add(full_url)
            name = a.get_text(strip=True)
            if name and 2 < len(name) < 100:
                products.append((name[:80], full_url))

    print(f"    Found {len(products)} SAP products/sources total")
    return products


def discover_subpages(base_url: str, max_pages: int = 40) -> list:
    """Find sub-pages for a product's documentation hub."""
    resp = safe_get(base_url)
    if not resp:
        return [base_url]

    soup = BeautifulSoup(resp.text, "lxml")
    pages = [base_url]
    seen = {base_url}

    for a in soup.find_all("a", href=True):
        if len(pages) >= max_pages:
            break
        href = a["href"]
        if not ("/docs/" in href or "/viewer/" in href):
            continue
        full = href if href.startswith("http") else f"https://help.sap.com{href}"
        if full not in seen and "help.sap.com" in full:
            seen.add(full)
            pages.append(full)

    return pages


# ─── NOTE REFERENCE EXTRACTOR ─────────────────────────────────────────────────

def extract_note_refs(text: str) -> list:
    """Extract SAP Note and KBA numbers from page text."""
    found = []
    for m in SAP_NOTE_RE.finditer(text):
        num = m.group(1) or m.group(2)
        if num:
            found.append(num)
    return list(set(found))


# ─── EVIDENCE CLASSIFIER ──────────────────────────────────────────────────────

def classify(product: str, url: str) -> tuple:
    """Return (evidence_type, confidence) for a page."""
    p, u = product.lower(), url.lower()
    if any(x in u or x in p for x in ["sap_ipd", "integrated-product-development", "/ipd"]):
        return "DIRECT_IPD", "✓ CONFIRMED"
    if any(x in u or x in p for x in ["sap_epd", "engineering-product-development", "/epd"]):
        return "DIRECT_IPD", "✓ CONFIRMED"
    if any(x in p for x in ["successfactors", "ariba", "concur", "fieldservice", "fsm"]):
        return "BTP_ANALOGY", "~ ANALOGY"
    if any(x in p for x in ["btp", "integration_suite", "integration suite", "build", "cap",
                             "event mesh", "api management", "extension suite"]):
        return "BTP_TOOL", "✓ CONFIRMED"
    if "discovery" in u or "api.sap.com" in u:
        return "BTP_TOOL", "✓ CONFIRMED"
    if "community.sap.com" in u:
        return "COMMUNITY", "? ASSUMED"
    return "DIRECT", "✓ CONFIRMED"


# ─── COMMUNITY CRAWLER ────────────────────────────────────────────────────────

COMMUNITY_URLS = [
    "https://community.sap.com/t5/product-lifecycle-management/ct-p/product-lifecycle-management",
    "https://community.sap.com/t5/sap-integrated-product-development/ct-p/integrated-product-development",
    "https://community.sap.com/t5/sap-btp-blog-posts/ct-p/btp-blog-posts",
    "https://community.sap.com/t5/enterprise-resource-planning/ct-p/enterprise-resource-planning",
    "https://community.sap.com/t5/sap-for-high-tech/ct-p/high-tech",
    "https://community.sap.com/t5/technology-blog-posts/ct-p/technology-blog-posts",
]


def crawl_community() -> list:
    """Crawl SAP Community pages for discussions, answers, and Note references."""
    print("\n[Community] Crawling SAP Community...")
    results = []
    for url in COMMUNITY_URLS:
        resp = safe_get(url)
        if not resp:
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(separator=" ", strip=True)
        notes = extract_note_refs(text)
        score = gemini_score(text, "SAP Community")
        if score < SCORE_THRESHOLD:
            continue
        title = soup.title.get_text(strip=True) if soup.title else url
        summary = gemini_summarise(text, "Community", url)
        results.append(_row("Community", title, url, summary, score,
                            "COMMUNITY", "? ASSUMED", notes))
        print(f"    [OK] {title[:70]} | Score: {score:.1f} | Notes: {notes or 'none'}")
    return results


# ─── GITHUB SEARCH ────────────────────────────────────────────────────────────

def search_github() -> list:
    """Search GitHub for community SAP solutions using Gemini-generated queries."""
    print("\n[GitHub] Searching for community SAP solutions...")
    results = []
    queries = gemini_github_queries()
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    seen = set()

    for query in queries:
        time.sleep(2)
        api_url = (
            f"https://api.github.com/search/repositories"
            f"?q={requests.utils.quote(query)}&sort=stars&per_page=5"
        )
        try:
            resp = requests.get(api_url, headers=headers, timeout=15)
        except Exception:
            continue
        if resp.status_code != 200:
            continue

        for repo in resp.json().get("items", []):
            repo_url = repo["html_url"]
            if repo_url in seen:
                continue
            seen.add(repo_url)

            # Try to get README
            readme_text = ""
            readme_resp = safe_get(
                f"https://api.github.com/repos/{repo['full_name']}/readme",
                headers=headers,
            )
            if readme_resp:
                try:
                    data = readme_resp.json()
                    readme_text = base64.b64decode(
                        data.get("content", "")
                    ).decode("utf-8", errors="ignore")[:3000]
                except Exception:
                    pass

            combined = f"{repo['name']}\n{repo.get('description','')}\n{readme_text}"
            score = gemini_score(combined, "GitHub SAP")
            if score < SCORE_THRESHOLD:
                continue
            summary = gemini_summarise(combined, "GitHub", repo_url)
            results.append(_row("GitHub", repo["full_name"], repo_url, summary,
                                score, "GITHUB", "? ASSUMED", []))
            print(f"    [OK] {repo['full_name']} | Score: {score:.1f}")

    return results


# ─── MAIN PRODUCT CRAWLER ─────────────────────────────────────────────────────

def crawl_all_products(products, visited, progress, sheets) -> set:
    """Crawl all discovered SAP product documentation pages."""
    buffer = []
    pages_since_save = 0

    for product_name, base_url in products:
        print(f"\n[Product] {product_name}")
        subpages = discover_subpages(base_url)

        for page_url in subpages:
            if page_url in visited:
                continue

            resp = safe_get(page_url)
            visited.add(page_url)
            pages_since_save += 1

            if not resp:
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            text = soup.get_text(separator=" ", strip=True)
            if len(text) < 150:
                continue

            score = gemini_score(text, product_name)
            if score < SCORE_THRESHOLD:
                print(f"    [Skip] Score {score:.1f} | {page_url}")
                continue

            title = (soup.title.get_text(strip=True) if soup.title else page_url)[:200]
            summary = gemini_summarise(text, product_name, page_url)
            notes = extract_note_refs(text)
            ev_type, confidence = classify(product_name, page_url)

            buffer.append(_row(product_name, title, page_url, summary,
                               score, ev_type, confidence, notes))
            print(f"    [OK] {title[:65]} | {score:.1f} | {ev_type}")

            if len(buffer) >= BATCH_SIZE:
                flush(buffer, sheets)
                buffer = []

            if pages_since_save >= SAVE_INTERVAL:
                progress["visited"] = list(visited)
                progress["last_save"] = datetime.now().isoformat()
                save_progress(progress)
                pages_since_save = 0
                print(f"    [Saved] {len(visited)} URLs visited so far")

    if buffer:
        flush(buffer, sheets)

    return visited


# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

def init_sheets(progress):
    """Authenticate with Google and open (or create) the knowledge base Sheet."""
    import json as _json
    creds_dict = _json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)

    if progress.get("sheet_id"):
        try:
            ss = gc.open_by_key(progress["sheet_id"])
            print(f"[Sheets] Opened existing sheet: {ss.title}")
        except Exception:
            ss = _create_sheet(gc, progress)
    else:
        ss = _create_sheet(gc, progress)

    # Ensure all tabs exist with headers
    existing = {ws.title: ws for ws in ss.worksheets()}
    sheets = {}
    for tab in SHEET_TABS:
        if tab not in existing:
            ws = ss.add_worksheet(title=tab, rows=2000, cols=len(SHEET_HEADERS))
            ws.append_row(SHEET_HEADERS)
            sheets[tab] = ws
        else:
            sheets[tab] = existing[tab]

    return gc, ss, sheets


def _create_sheet(gc, progress):
    ss = gc.create(
        f"SAP Knowledge Base {datetime.now().strftime('%Y-%m-%d')}",
        folder_id=DRIVE_FOLDER_ID,
    )
    progress["sheet_id"] = ss.id
    save_progress(progress)
    print(f"[Sheets] Created new sheet: {ss.title} (ID: {ss.id})")
    return ss


def _route_tab(ev_type: str, product: str) -> str:
    """Map evidence type + product to the correct Sheet tab."""
    p = product.lower()
    if "epd" in p or ev_type == "DIRECT_IPD" and "epd" in p:
        return "SAP_EPD_Direct"
    if ev_type == "DIRECT_IPD":
        return "SAP_IPD_Direct"
    if ev_type == "BTP_ANALOGY":
        return "BTP_Analogy"
    if ev_type == "BTP_TOOL":
        return "BTP_Tools"
    if ev_type == "COMMUNITY":
        return "Community_Discussions"
    if ev_type == "GITHUB":
        return "GitHub_Community"
    return "SAP_IPD_Direct"


def _row(product, title, url, summary, score, ev_type, confidence, notes) -> dict:
    return {
        "product": product,
        "title": title[:200],
        "url": url,
        "summary": summary,
        "score": round(float(score), 1),
        "evidence_type": ev_type,
        "confidence": confidence,
        "referenced_notes": ", ".join(notes) if notes else "",
        "date": datetime.now().strftime("%Y-%m-%d"),
    }


def flush(results: list, sheets: dict) -> None:
    """Write a batch of results to the appropriate Sheet tabs."""
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
        print(f"    [Sheets] Wrote {len(batch)} rows → {tab}")


# ─── EXCEL EXPORT ─────────────────────────────────────────────────────────────

def export_excel(ss) -> None:
    """Mirror all Sheet tabs into an Excel file."""
    wb = Workbook()
    wb.remove(wb.active)
    for ws in ss.worksheets():
        if ws.title == "SAP_Notes":
            continue  # Notes are added locally via notes_lookup.py only
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

    # Load previous progress (resume support)
    progress = load_progress()
    visited = set(progress.get("visited", []))
    print(f"Resuming from {len(visited)} previously visited URLs")

    # Connect to Google Sheets
    gc, ss, sheets = init_sheets(progress)

    # 1 — Discover all SAP products
    products = discover_sap_products()

    # 2 — Crawl all product documentation
    visited = crawl_all_products(products, visited, progress, sheets)

    # 3 — Crawl SAP Community
    community = crawl_community()
    if community:
        flush(community, sheets)

    # 4 — Search GitHub
    github = search_github()
    if github:
        flush(github, sheets)

    # 5 — Final progress save
    progress["visited"] = list(visited)
    progress["last_save"] = datetime.now().isoformat()
    save_progress(progress)

    # 6 — Export Excel
    export_excel(ss)

    print("\n" + "=" * 65)
    print("Crawl complete!")
    print(f"Total URLs visited : {len(visited)}")
    print(f"Google Sheet       : https://docs.google.com/spreadsheets/d/{progress['sheet_id']}")
    print(f"Excel file         : {EXCEL_FILE}")
    print("=" * 65)


if __name__ == "__main__":
    main()
