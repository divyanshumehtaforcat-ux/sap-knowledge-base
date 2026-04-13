"""
SAP Knowledge Manual Injector
==============================
LAPTOP ONLY — never commit service_account.json to GitHub.

PURPOSE:
  The automated crawler can only get surface-level info (titles, descriptions).
  This script lets YOU inject the real gold — deep config steps, specific SAP
  Notes, community solutions, meeting notes — directly into the knowledge base
  with full Gemini summarisation.

WHAT IT DOES:
  • URL  → fetches the page (Playwright for SAP Help / JS sites, requests for others)
  • Text → paste raw notes, config steps, S-user SAP Note content, meeting notes
  • Gemini summarises with config-level detail (IMG paths, T-codes, iFlow names)
  • Routes to correct Google Sheet tab automatically
  • Extracts any SAP Note / KBA numbers mentioned
  • Score 9.0 for CONFIRMED, 7.0 for others — your manual entries outrank crawler data

ONE-TIME SETUP (run in Windows Command Prompt, once):
  pip install playwright google-generativeai gspread google-auth requests beautifulsoup4 lxml openpyxl
  playwright install chromium

SERVICE ACCOUNT FILE:
  Download your service account JSON from Google Cloud Console:
    IAM > Service Accounts > sap-crawler@... > Keys > Add Key > JSON
  Save the file as:
    C:\\Users\\divya\\OneDrive\\Desktop\\New folder\\service_account.json
  (same folder as this script — it is in .gitignore, will NOT go to GitHub)

GEMINI API KEY:
  Either set it once: setx GEMINI_API_KEY "your_key_here"
  Or just enter it when the script asks (hidden input, not saved).

RUN:
  python manual_inject.py
"""

import os
import sys
import re
import json
import time
import getpass
from datetime import datetime
from pathlib import Path

# ── Optional: Playwright for JavaScript SPAs (SAP Help Portal) ─────────────────
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
# NOTE: google-generativeai SDK is NOT imported — we call the Gemini REST API
# directly via requests. This avoids the protobuf/Python 3.14 incompatibility.

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

GOOGLE_SHEET_ID = "1eiZ3f1N-YAop3gA7pe3YEqrh2ZuW01RHoxx8ncGmkn4"
SCRIPT_DIR      = Path(__file__).parent
SA_JSON_PATH    = SCRIPT_DIR / "service_account.json"

SAP_NOTE_RE = re.compile(r'\b(?:SAP\s+)?[Nn]ote\s+#?(\d{6,10})\b|KBA\s+#?(\d{6,10})')

# These domains use JavaScript rendering — need Playwright
JS_DOMAINS = [
    "help.sap.com",
    "me.sap.com",
    "launchpad.support.sap.com",
    "sapui5.hana.ondemand.com",
    "learning.sap.com",
]

SHEET_HEADERS = [
    "Product", "Title", "URL", "Summary",
    "Score", "Evidence_Type", "Confidence",
    "Referenced_Notes", "Date_Crawled",
]

# (internal_key, display_label, sheet_tab, evidence_type)
PRODUCTS = [
    ("SAP_IPD",           "SAP IPD — Integrated Product Development",   "SAP_IPD_Direct",       "DIRECT_IPD"),
    ("SAP_EPD",           "SAP EPD — Engineering Product Development",   "SAP_EPD_Direct",       "DIRECT_IPD"),
    ("S4HANA",            "S/4HANA PLM or RISE Private Cloud",           "SAP_IPD_Direct",       "DIRECT"),
    ("SAP_PLM",           "SAP PLM Classic (ECC-based)",                  "SAP_IPD_Direct",       "DIRECT"),
    ("SAP_DMS",           "SAP DMS — Document Management",               "SAP_IPD_Direct",       "DIRECT"),
    ("SAP_ECTR",          "SAP ECTR / CAD Integration",                  "SAP_IPD_Direct",       "DIRECT"),
    ("Integration_Suite", "SAP Integration Suite / iFlow",               "BTP_Tools",            "BTP_TOOL"),
    ("BTP_Tools",         "BTP Service / Tool (CAP, Build, XSUAA, etc)", "BTP_Tools",            "BTP_TOOL"),
    ("SuccessFactors",    "SuccessFactors (BTP analogy pattern)",         "BTP_Analogy",          "BTP_ANALOGY"),
    ("Community",         "SAP Community post / blog / discussion",       "Community_Discussions","COMMUNITY"),
    ("SAP_Note",          "SAP Note or KBA",                             "SAP_Notes",            "SAP_NOTE"),
    ("GitHub",            "GitHub repository or code sample",            "GitHub_Community",     "GITHUB"),
    ("Migration",         "Migration / cutover / upgrade topic",          "SAP_IPD_Direct",       "DIRECT"),
    ("MDG",               "SAP MDG — Master Data Governance",            "SAP_IPD_Direct",       "DIRECT"),
    ("Teamcenter",        "Siemens Teamcenter integration",              "SAP_IPD_Direct",       "DIRECT"),
    ("Windchill",         "PTC Windchill integration",                   "SAP_IPD_Direct",       "DIRECT"),
    ("Other",             "Other / general SAP knowledge",               "SAP_IPD_Direct",       "DIRECT"),
]

# (value_written_to_sheet, display description)
CONFIDENCE_OPTIONS = [
    ("✓ CONFIRMED", "Official SAP doc, Help Portal page, or verified SAP Note"),
    ("~ ANALOGY",   "BTP/SF pattern — I have validated it applies to IPD"),
    ("? ASSUMED",   "Community post, blog, or reasonable inference"),
    ("⚡ SEARCH",   "Interesting but needs verification before using with clients"),
]

# ─── SETUP ────────────────────────────────────────────────────────────────────

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent?key={key}"
)

def setup_gemini() -> str:
    """Returns the API key — we call Gemini via REST, no SDK needed."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("\nGemini API key not found in environment variables.")
        api_key = getpass.getpass("Enter your Gemini API key (hidden, not saved): ").strip()
    if not api_key:
        sys.exit("Error: Gemini API key is required.")
    # Quick connection test
    try:
        test = _gemini_call(api_key, "Reply with exactly: OK")
        if "OK" in test:
            print("[Gemini] Connected (gemini-1.5-flash via REST API)")
        else:
            print(f"[Gemini] Connected (response: {test[:30]})")
    except Exception as e:
        sys.exit(f"[Gemini] Connection failed: {e}\nCheck your API key.")
    return api_key


def _gemini_call(api_key: str, prompt: str) -> str:
    """Direct REST call to Gemini Flash — no SDK, no protobuf issues."""
    resp = requests.post(
        GEMINI_URL.format(key=api_key),
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def setup_sheets():
    creds_dict = None

    # 1. Try local service_account.json in same folder
    if SA_JSON_PATH.exists():
        creds_dict = json.loads(SA_JSON_PATH.read_text(encoding="utf-8"))
        print(f"[Auth] Using service account: {SA_JSON_PATH.name}")

    # 2. Try environment variable (same as GitHub Actions)
    elif "GOOGLE_SERVICE_ACCOUNT_JSON" in os.environ:
        creds_dict = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        print("[Auth] Using GOOGLE_SERVICE_ACCOUNT_JSON environment variable")

    # 3. Ask user for path
    else:
        print(f"\nService account JSON not found at: {SA_JSON_PATH}")
        print("Download it from: Google Cloud Console → IAM → Service Accounts → Keys → Add Key → JSON")
        sa_path = input("Enter full path to your service_account.json: ").strip().strip('"')
        creds_dict = json.loads(Path(sa_path).read_text(encoding="utf-8"))

    creds = Credentials.from_service_account_info(creds_dict, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(GOOGLE_SHEET_ID)
    print(f"[Sheets] Connected: {ss.title}")
    return ss


# ─── URL FETCHING ─────────────────────────────────────────────────────────────

def needs_playwright(url: str) -> bool:
    return any(d in url for d in JS_DOMAINS)


def fetch_url(url: str):
    """Returns (title, content). Uses Playwright for JS sites like SAP Help Portal."""
    if needs_playwright(url):
        if PLAYWRIGHT_OK:
            return _fetch_playwright(url)
        else:
            print("\n[!] This is a SAP Help Portal page (JavaScript SPA).")
            print("    Playwright is not installed — content may be empty.")
            print("    To fix: pip install playwright && playwright install chromium")
            print("    Attempting basic fetch anyway...\n")
    return _fetch_requests(url)


def _fetch_playwright(url: str):
    """Render a JavaScript page with Playwright — gets full content from SAP Help Portal."""
    print(f"[Playwright] Rendering: {url}")
    print("            (This takes ~10-15 seconds while Chromium loads...)")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_extra_http_headers({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)

            # Dismiss cookie banners
            for btn_text in ["Accept All", "Accept all", "Accept Cookies", "I agree", "OK"]:
                try:
                    page.get_by_text(btn_text, exact=True).first.click(timeout=2000)
                    time.sleep(1)
                    break
                except Exception:
                    pass

            # Extra wait for SAP Help content to load (it's slow)
            time.sleep(4)

            # Get page title
            title = page.title().strip() or url.split("/")[-1]

            # Try content selectors in order of specificity for SAP Help Portal
            content = ""
            selectors_to_try = [
                ".help-content",
                ".documentation-content",
                "[data-testid='content']",
                "main",
                "article",
                "#content",
                "[role='main']",
                ".container-fluid .row",
            ]
            for sel in selectors_to_try:
                try:
                    el = page.query_selector(sel)
                    if el:
                        text = el.inner_text()
                        if len(text.strip()) > 300:
                            content = text
                            break
                except Exception:
                    continue

            # Fallback: full page body text
            if not content:
                content = page.inner_text("body")

            # Clean up excessive whitespace
            content = re.sub(r'\n{4,}', '\n\n\n', content)
            content = re.sub(r'[ \t]{2,}', ' ', content)
            content = content.strip()[:18000]  # Generous limit for Gemini

            char_count = len(content)
            print(f"[Playwright] Extracted {char_count:,} characters | Title: {title[:70]}")

            if char_count < 200:
                print("[!] Very little content extracted. The page may require login.")

            return title, content

        except Exception as e:
            print(f"[Playwright Error] {e}")
            return url.split("/")[-1], ""
        finally:
            browser.close()


def _fetch_requests(url: str):
    """Fetch regular (non-JS) pages with requests + BeautifulSoup."""
    print(f"[Fetch] Getting: {url}")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise
        for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                          "noscript", "iframe", "form"]):
            tag.decompose()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else url.split("/")[-1]

        # Try main content areas
        content = ""
        for sel in ["main", "article", "#content", ".content", ".post-content",
                    ".entry-content", "[role='main']"]:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    content = text
                    break

        if not content:
            content = soup.get_text(separator="\n", strip=True)

        content = re.sub(r'\n{4,}', '\n\n\n', content)
        content = content.strip()[:15000]

        print(f"[Fetch] Extracted {len(content):,} characters")
        return title.strip(), content

    except Exception as e:
        print(f"[Fetch Error] {e}")
        return url.split("/")[-1], ""


# ─── TEXT PASTE INPUT ─────────────────────────────────────────────────────────

def paste_text_input():
    """
    Multi-line text paste. User types/pastes content, ends with END.
    Returns (title, url, content).
    """
    print("\nPaste your content below (config steps, SAP Note text, meeting notes, etc.)")
    print("When finished, type  END  on a new line and press Enter:")
    print("─" * 55)

    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == "END":
            break
        lines.append(line)

    content = "\n".join(lines).strip()
    print("─" * 55)

    title = input("\nShort title for this entry\n(e.g. 'IPD BOM Transfer IMG Config Steps'): ").strip()
    if not title:
        title = f"Manual entry {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    url = input("\nSource URL or reference (press Enter to skip): ").strip()
    if not url:
        url = f"manual-entry-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    return title, url, content


# ─── GEMINI SUMMARISATION ─────────────────────────────────────────────────────

def gemini_summarise(api_key: str, text: str, product: str, url: str) -> str:
    """
    Rich summarisation designed to extract config-level detail.
    5 bullets with specific values — not generic descriptions.
    """
    print("[Gemini] Summarising... (this takes ~10 seconds)")
    try:
        prompt = f"""You are an SAP consultant knowledge base assistant.
Your job is to summarise SAP documentation so a PLM/IPD functional consultant
can answer client questions precisely and correctly.

Summarise the content below in exactly 5 bullet points, each starting with •.
Be SPECIFIC — do not be vague. Include actual values:
  - IMG menu paths (IMG > Enterprise Structure > ...)
  - Transaction codes (MM01, CS01, CV01N, etc.)
  - API endpoints or OData entity names
  - iFlow or integration scenario names
  - Field names, configuration parameters, role names
  - BTP service names and plan types

Bullet 1 — WHAT: What feature or capability is described and why it matters for IPD/PLM projects
Bullet 2 — HOW TO CONFIGURE: Specific configuration steps with actual paths/codes/values
Bullet 3 — PREREQUISITES: System requirements, activated services, licences, basis setup needed
Bullet 4 — INTEGRATION: How this connects SAP IPD / S4HANA / ECC / BTP — which APIs or iFlows
Bullet 5 — CAVEATS: Known issues, gotchas, version limitations, things that trip up clients

Product context: {product}
Source: {url}

Content to summarise:
{text[:9000]}
"""
        return _gemini_call(api_key, prompt)

    except Exception as e:
        return f"• Summarisation error: {e}\n• Raw content stored — please review manually."


def extract_note_refs(text: str) -> list:
    found = set()
    for m in SAP_NOTE_RE.finditer(text):
        num = m.group(1) or m.group(2)
        if num:
            found.add(num)
    return sorted(found)


# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

def get_or_create_tab(ss, tab_name: str):
    existing = {ws.title: ws for ws in ss.worksheets()}
    if tab_name in existing:
        return existing[tab_name]
    ws = ss.add_worksheet(title=tab_name, rows=2000, cols=len(SHEET_HEADERS))
    ws.append_row(SHEET_HEADERS)
    print(f"[Sheets] Created new tab: {tab_name}")
    return ws


def write_to_sheet(ss, tab: str, row: dict) -> None:
    ws = get_or_create_tab(ss, tab)
    ws.append_row([
        row["product"],
        row["title"],
        row["url"],
        row["summary"],
        row["score"],
        row["evidence_type"],
        row["confidence"],
        row["referenced_notes"],
        row["date"],
    ], value_input_option="RAW")


# ─── UI HELPERS ───────────────────────────────────────────────────────────────

def pick(options: list, prompt: str) -> int:
    """Numbered menu — returns 0-based index of chosen item."""
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        label = opt if isinstance(opt, str) else opt[1]
        print(f"  {i:2d}. {label}")
    while True:
        try:
            val = input(f"\nChoose 1–{len(options)}: ").strip()
            idx = int(val) - 1
            if 0 <= idx < len(options):
                return idx
        except (ValueError, KeyboardInterrupt):
            pass
        print(f"     Please enter a number between 1 and {len(options)}")


def show_preview(row: dict, tab: str) -> None:
    print("\n" + "═" * 62)
    print("  PREVIEW — what will be written to Google Sheet:")
    print("─" * 62)
    print(f"  Product    : {row['product']}")
    print(f"  Tab        : {tab}")
    print(f"  Confidence : {row['confidence']}")
    print(f"  Score      : {row['score']} / 10")
    print(f"  URL        : {row['url'][:75]}")
    print(f"  Title      : {row['title'][:75]}")
    if row["referenced_notes"]:
        print(f"  SAP Notes  : {row['referenced_notes']}")
    print("─" * 62)
    print("  Summary:")
    for line in row["summary"].split("\n"):
        if line.strip():
            print(f"    {line}")
    print("═" * 62)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 62)
    print("  SAP Knowledge Manual Injector")
    print("  Injects deep, config-level knowledge into your brain.")
    print("  LAPTOP ONLY — not for GitHub.")
    print("=" * 62)

    if not PLAYWRIGHT_OK:
        print("\n[!] Playwright not installed.")
        print("    SAP Help Portal pages (help.sap.com) are JavaScript SPAs")
        print("    and will return empty content without Playwright.")
        print("    Fix: pip install playwright && playwright install chromium\n")
    else:
        print("\n[✓] Playwright available — SAP Help Portal pages will render correctly.")

    print("\nConnecting to Gemini and Google Sheets...")
    api_key = setup_gemini()
    ss      = setup_sheets()
    print("\nReady. Let's inject some knowledge.\n")

    while True:
        print("─" * 62)
        print("What do you want to inject?")
        print("  1. A URL  (SAP Help page, Community post, API Hub, GitHub...)")
        print("  2. Text   (paste config steps, SAP Note content, notes...)")
        print("  3. Exit")

        choice = input("\nChoice (1 / 2 / 3): ").strip()

        if choice == "3" or choice.lower() in ("exit", "quit", "q"):
            print("\nDone. Your knowledge is saved.")
            print(f"View sheet: https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}")
            break

        # ── Get raw content ────────────────────────────────────────────────────
        url     = ""
        title   = ""
        content = ""

        if choice == "1":
            url = input("\nPaste URL: ").strip().strip('"')
            if not url:
                continue
            if not url.startswith("http"):
                url = "https://" + url
            title, content = fetch_url(url)

            if not content.strip():
                print("\n[!] Could not extract content from that URL.")
                if needs_playwright(url) and not PLAYWRIGHT_OK:
                    print("    This is a SAP Help page and needs Playwright.")
                    print("    Install: pip install playwright && playwright install chromium")
                manual = input("    Paste the key content manually instead? (y/n): ").strip().lower()
                if manual == "y":
                    print("Paste content below. Type END when done:")
                    lines = []
                    while True:
                        line = input()
                        if line.strip().upper() == "END":
                            break
                        lines.append(line)
                    content = "\n".join(lines)
                else:
                    continue

            # Let user refine the auto-detected title
            print(f"\nAuto-detected title: {title[:80]}")
            better = input("Press Enter to keep, or type a better title: ").strip()
            if better:
                title = better

        elif choice == "2":
            title, url, content = paste_text_input()

        else:
            continue

        if not content.strip():
            print("[!] No content. Skipping.")
            continue

        # ── Choose product ─────────────────────────────────────────────────────
        prod_idx = pick(PRODUCTS, "Which product / area is this about?")
        prod_key, prod_label, tab, ev_type = PRODUCTS[prod_idx]

        # ── Choose confidence ──────────────────────────────────────────────────
        conf_idx = pick(CONFIDENCE_OPTIONS, "How confident are you in this content?")
        confidence, conf_desc = CONFIDENCE_OPTIONS[conf_idx]

        # ── Summarise with Gemini ──────────────────────────────────────────────
        summary = gemini_summarise(api_key, content, prod_key, url)

        # ── Extract SAP Note references ────────────────────────────────────────
        notes = extract_note_refs(content + " " + title + " " + summary)

        # ── Build the row ──────────────────────────────────────────────────────
        score = 9.5 if "CONFIRMED" in confidence else (8.0 if "ANALOGY" in confidence else 7.0)
        row = {
            "product":          prod_key,
            "title":            title[:200],
            "url":              url,
            "summary":          summary,
            "score":            score,
            "evidence_type":    ev_type,
            "confidence":       confidence,
            "referenced_notes": ", ".join(notes),
            "date":             datetime.now().strftime("%Y-%m-%d"),
        }

        # ── Show preview ───────────────────────────────────────────────────────
        show_preview(row, tab)

        # ── Confirm and write ──────────────────────────────────────────────────
        confirm = input("\nWrite this to Google Sheet? (y/n): ").strip().lower()
        if confirm == "y":
            write_to_sheet(ss, tab, row)
            print(f"[✓] Saved to the '{tab}' tab!")
        else:
            print("Skipped — nothing written.")

        print()
        again = input("Inject another entry? (y/n): ").strip().lower()
        if again != "y":
            print("\nDone. Your knowledge is saved.")
            print(f"View sheet: https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted. Bye!")
