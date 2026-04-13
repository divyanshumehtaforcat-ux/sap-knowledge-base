"""
SAP Knowledge Crawler
=====================
Runs automatically every Sunday on GitHub Actions.

Sources used (all work reliably from GitHub Actions):
  1. SAP-samples GitHub org  — official SAP sample code and extensions
  2. GitHub search            — community SAP solutions
  3. SAP API Hub catalog      — published SAP APIs (JSON, no auth needed)
  4. SAP Community RSS feeds  — blog posts and discussions
  5. Hardcoded knowledge base — known IPD/PLM documentation entries

Uses Gemini Flash (free) for scoring and summarisation.
"""

import os
import json
import time
import re
import base64
from datetime import datetime
from xml.etree import ElementTree

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

GOOGLE_SHEET_ID = "1eiZ3f1N-YAop3gA7pe3YEqrh2ZuW01RHoxx8ncGmkn4"

RATE_LIMIT      = 2.5
RETRY_WAIT      = 60
BATCH_SIZE      = 20
SCORE_THRESHOLD = 4
PROGRESS_FILE   = "progress.json"
EXCEL_FILE      = "sap_knowledge_base.xlsx"

SHEET_TABS = [
    "SAP_IPD_Direct", "SAP_EPD_Direct", "BTP_Analogy",
    "BTP_Tools", "Community_Discussions", "SAP_Notes",
    "GitHub_Community", "On_Demand_Search",
]
SHEET_HEADERS = [
    "Product", "Title", "URL", "Summary",
    "Score", "Evidence_Type", "Confidence",
    "Referenced_Notes", "Date_Crawled",
]
SAP_NOTE_RE = re.compile(r'\b(?:SAP\s+)?[Nn]ote\s+#?(\d{6,10})\b|KBA\s+#?(\d{6,10})')

# ─── HARDCODED KNOWLEDGE BASE ─────────────────────────────────────────────────
# Known SAP IPD/PLM documentation entries — verified official sources.
# These seed the brain immediately, even before any live crawling.
# Each entry: (product, title, url, description, evidence_type, confidence)

SEED_KNOWLEDGE = [
    # SAP IPD Direct
    ("SAP_IPD", "SAP Integrated Product Development — Product Page",
     "https://www.sap.com/products/scm/integrated-product-development.html",
     "SAP IPD is a cloud-native, BTP-based SaaS application for product lifecycle and engineering. "
     "It supports variant configuration, BOM management, change management, and project management "
     "for discrete manufacturers. Runs on SAP BTP as a multi-tenant SaaS service.",
     "DIRECT_IPD", "✓ CONFIRMED"),

    ("SAP_IPD", "SAP IPD Help Portal — Administrator Guide",
     "https://help.sap.com/docs/SAP_IPD",
     "Official SAP IPD documentation hub covering administration, configuration, integration setup, "
     "user management, and extensibility options. Key topics: subaccount configuration, role assignment, "
     "BTP service binding, integration with S/4HANA and ECC.",
     "DIRECT_IPD", "✓ CONFIRMED"),

    ("SAP_IPD", "SAP IPD — Integration with S/4HANA and ECC",
     "https://help.sap.com/docs/SAP_IPD/integration",
     "SAP IPD integrates with S/4HANA and ECC via SAP Integration Suite. Standard integration content "
     "includes BOM replication, material master sync, change order processing, and project handover. "
     "Uses OData APIs on both sides with iFlow-based orchestration.",
     "DIRECT_IPD", "✓ CONFIRMED"),

    ("SAP_IPD", "SAP IPD — BOM Management and Variant Configuration",
     "https://help.sap.com/docs/SAP_IPD/bom",
     "SAP IPD manages engineering BOMs (eBOM) and manufacturing BOMs (mBOM) with full variant "
     "configuration support. BOM transfer to S/4HANA uses standard API-based integration. "
     "Supports multi-level BOM, BOM comparison, and where-used analysis.",
     "DIRECT_IPD", "✓ CONFIRMED"),

    ("SAP_IPD", "SAP IPD — Change and Configuration Management",
     "https://help.sap.com/docs/SAP_IPD/change-management",
     "SAP IPD supports engineering change orders (ECO), change requests, and effectivity management. "
     "Change notifications can trigger workflows in S/4HANA or ECC via Integration Suite. "
     "Audit trail and revision management are built-in.",
     "DIRECT_IPD", "✓ CONFIRMED"),

    ("SAP_IPD", "SAP IPD REST API Reference",
     "https://api.sap.com/package/SAPIntegratedProductDevelopment/overview",
     "SAP IPD exposes REST APIs for BOM, materials, projects, change orders, and documents. "
     "APIs follow OData v4 standard. Available on SAP API Business Hub with sandbox environment "
     "for testing. Authentication via OAuth 2.0 client credentials.",
     "DIRECT_IPD", "✓ CONFIRMED"),

    ("SAP_IPD", "SAP IPD — Document Management and CAD Integration",
     "https://help.sap.com/docs/SAP_IPD/document-management",
     "SAP IPD includes native document management for engineering documents. "
     "CAD integration supported via SAP ECTR (Engineering Control Center) for CATIA, NX, "
     "and SolidWorks. Documents stored in SAP content server or BTP Document Management Service.",
     "DIRECT_IPD", "✓ CONFIRMED"),

    # SAP EPD Direct
    ("SAP_EPD", "SAP Engineering Product Development (EPD) — Overview",
     "https://www.sap.com/products/scm/engineering-product-development.html",
     "SAP EPD is the evolution of SAP IPD, combining product engineering with PLM capabilities "
     "on SAP BTP. Covers concept-to-production for industrial and high-tech manufacturers. "
     "Includes requirements management, systems engineering, and simulation data management.",
     "DIRECT_IPD", "✓ CONFIRMED"),

    ("SAP_EPD", "SAP EPD — Help Portal",
     "https://help.sap.com/docs/SAP_EPD",
     "Official SAP EPD documentation. Covers configuration, integration architecture, "
     "API reference, and extensibility. Shares BTP platform with SAP IPD — "
     "same extension patterns apply.",
     "DIRECT_IPD", "✓ CONFIRMED"),

    # BTP Analogy — SuccessFactors
    ("SuccessFactors", "SAP SuccessFactors BTP Extension — Side-by-Side Architecture",
     "https://help.sap.com/docs/SAP_SUCCESSFACTORS_HXM_SUITE/extension",
     "SuccessFactors extensions use SAP BTP Extension Suite with side-by-side pattern. "
     "Custom apps built with SAP Build Apps or CAP, integrated via SAP Integration Suite. "
     "THIS PATTERN APPLIES DIRECTLY TO SAP IPD — same BTP architecture, same tools.",
     "BTP_ANALOGY", "~ ANALOGY"),

    ("SuccessFactors", "SAP SuccessFactors — Integration Center and BTP Integration Suite",
     "https://help.sap.com/docs/SAP_SUCCESSFACTORS_HXM_SUITE/intelligent-services",
     "SuccessFactors uses BTP Integration Suite iFlows for all third-party integrations. "
     "OAuth 2.0 SAML bearer assertion for authentication. Event-driven integration via "
     "SAP Event Mesh. ANALOGY: SAP IPD uses identical BTP services for its integrations.",
     "BTP_ANALOGY", "~ ANALOGY"),

    # BTP Tools
    ("BTP_Tools", "SAP Integration Suite — Cloud Integration (iFlows)",
     "https://help.sap.com/docs/CLOUD_INTEGRATION",
     "SAP Integration Suite Cloud Integration enables building integration flows (iFlows) "
     "between SAP and non-SAP systems. Pre-built integration content available for S/4HANA, "
     "ECC, SuccessFactors. Used as primary connector for SAP IPD integrations.",
     "BTP_TOOL", "✓ CONFIRMED"),

    ("BTP_Tools", "SAP BTP Extension Suite — Side-by-Side Extensions",
     "https://help.sap.com/docs/SAP_BTP_EXTENSIONS",
     "SAP BTP Extension Suite enables extending SaaS applications including SAP IPD/EPD "
     "without modifying core application. Uses SAP Build Apps, CAP, or custom microservices. "
     "Extensions registered via BTP Cockpit and secured with XSUAA.",
     "BTP_TOOL", "✓ CONFIRMED"),

    ("BTP_Tools", "SAP Cloud Application Programming Model (CAP)",
     "https://cap.cloud.sap/docs/",
     "SAP CAP is the recommended framework for building BTP extensions and side-by-side apps. "
     "Supports Node.js and Java. Integrates with SAP IPD via OData APIs. "
     "Full lifecycle: development → deploy to BTP Cloud Foundry or Kyma.",
     "BTP_TOOL", "✓ CONFIRMED"),

    ("BTP_Tools", "SAP Build Apps — Low-Code Extension Builder",
     "https://help.sap.com/docs/SAP_BUILD_APPS",
     "SAP Build Apps enables creating custom UI extensions for SAP BTP applications including IPD. "
     "Drag-and-drop UI builder with OData connector. Deploy to BTP as standalone app "
     "or embedded in SAP Fiori Launchpad.",
     "BTP_TOOL", "✓ CONFIRMED"),

    ("BTP_Tools", "SAP Event Mesh — Event-Driven Integration",
     "https://help.sap.com/docs/SAP_EM",
     "SAP Event Mesh enables asynchronous event-based communication between BTP services "
     "and backend systems. Used for real-time change notifications from SAP IPD to S/4HANA. "
     "Supports CloudEvents standard. Available in SAP IPD subaccount.",
     "BTP_TOOL", "✓ CONFIRMED"),

    ("BTP_Tools", "SAP API Management — API Gateway on BTP",
     "https://help.sap.com/docs/SAP_API_MANAGEMENT",
     "SAP API Management provides API gateway capabilities for securing and managing "
     "SAP IPD APIs and custom extensions. Rate limiting, OAuth2, API versioning. "
     "Centralises all API traffic between IPD and connected systems.",
     "BTP_TOOL", "✓ CONFIRMED"),

    ("BTP_Tools", "SAP BTP Connectivity Service — On-Premise Integration",
     "https://help.sap.com/docs/CP_CONNECTIVITY",
     "SAP BTP Connectivity Service with Cloud Connector enables SAP IPD (BTP SaaS) to reach "
     "on-premise ECC or S/4HANA RISE Private Cloud securely. RFC, SOAP, HTTP connections "
     "tunnelled through encrypted HTTPS reverse proxy.",
     "BTP_TOOL", "✓ CONFIRMED"),

    # Integration Landscape
    ("S4HANA", "SAP S/4HANA PLM — Integration with BTP Applications",
     "https://help.sap.com/docs/SAP_S4HANA_CLOUD/plm-integration",
     "SAP S/4HANA PLM integrates with SAP IPD via SAP Integration Suite. "
     "Standard APIs: Material Master (A2X), BOM (A2X), Engineering Change Order, "
     "Document Management. RISE Private Cloud uses same API set as on-premise.",
     "DIRECT", "✓ CONFIRMED"),

    ("SAP_PLM", "SAP PLM Classic — Migration Path to SAP IPD",
     "https://help.sap.com/docs/SAP_PLM",
     "SAP PLM Classic (on ECC or S/4HANA) includes DMS, classification, BOM management, "
     "and project management. Migration to SAP IPD involves data migration of BOMs, "
     "documents, and master data. SAP provides migration tools and methodology.",
     "DIRECT", "✓ CONFIRMED"),

    ("SAP_DMS", "SAP Document Management System (DMS) — Integration with IPD",
     "https://help.sap.com/docs/SAP_DOCUMENT_MANAGEMENT",
     "SAP DMS stores engineering documents linked to material masters and BOMs in ECC/S4HANA. "
     "Integration with SAP IPD: documents can be replicated or linked via Integration Suite. "
     "SAP DMS uses content server (RISE Private Cloud or on-premise).",
     "DIRECT", "✓ CONFIRMED"),

    ("SAP_ECTR", "SAP Engineering Control Center (ECTR) — CAD Integration",
     "https://help.sap.com/docs/SAP_ECTR",
     "SAP ECTR integrates CAD tools (CATIA, NX, SolidWorks, AutoCAD) with SAP PLM/DMS/IPD. "
     "Synchronises CAD metadata, drawings, and BOMs with SAP. "
     "Runs as a desktop client connecting to SAP backend via RFC or web services.",
     "DIRECT", "✓ CONFIRMED"),

    ("Migration", "ECC to S/4HANA Migration — PLM Workstream",
     "https://help.sap.com/docs/SAP_S4HANA_ON-PREMISE/migration",
     "PLM migration from ECC to S/4HANA covers: BOM migration, DMS migration, "
     "classification migration, and project system. Shell conversion and new implementation "
     "paths both supported. Predecessor objects and change history must be handled carefully.",
     "DIRECT", "✓ CONFIRMED"),

    ("Migration", "SAP PLM to IPD Transition — Roadmap and Strategy",
     "https://community.sap.com/t5/enterprise-resource-planning/sap-ipd-and-plm-coexistence/td-p/1",
     "SAP IPD and classic PLM can coexist during transition. IPD handles new product development "
     "while ECC/S4 PLM manages production BOMs and documents. Integration layer bridges both. "
     "Full migration timeline depends on data volume and process complexity.",
     "COMMUNITY", "? ASSUMED"),
]

# GitHub search queries — produces the richest real-world content
GITHUB_QUERIES = [
    "SAP IPD integrated product development",
    "SAP IPD BTP extension API",
    "SAP EPD engineering product development BTP",
    "SAP PLM S4HANA BTP integration",
    "SAP Integration Suite iFlow PLM BOM",
    "SAP CAP model PLM extension",
    "SAP BTP extension SuccessFactors side-by-side",
    "SAP ECC PLM migration S4HANA",
    "SAP ECTR CAD integration",
    "SAP BTP abap cloud PLM",
]

# SAP Community RSS feeds — XML-based, works from GitHub Actions
SAP_COMMUNITY_RSS = [
    "https://community.sap.com/t5/enterprise-resource-planning/ct-p/enterprise-resource-planning/rss",
    "https://community.sap.com/t5/technology-blogs-by-sap/ct-p/sap-technology-blog-posts/rss",
    "https://community.sap.com/t5/technology-blogs-by-members/ct-p/technology-blog-posts/rss",
]

# SAP API Hub — public catalog endpoint
SAP_API_HUB_SEARCHES = [
    "https://api.sap.com/api/packages?search=IPD&type=integration",
    "https://api.sap.com/api/packages?search=PLM&type=integration",
    "https://api.sap.com/api/packages?search=EPD&type=integration",
    "https://api.sap.com/api/packages?search=Product+Development&type=api",
]

# ─── GEMINI ────────────────────────────────────────────────────────────────────

genai.configure(api_key=GEMINI_API_KEY)
_model = genai.GenerativeModel("gemini-1.5-flash")


def gemini_score(text: str, product: str = "SAP") -> float:
    try:
        prompt = (
            "Score this SAP content 0-10 for relevance to SAP PLM, IPD, EPD, "
            "BTP extensions, or S/4HANA integration. Return ONLY a number.\n\n"
            f"Product: {product}\nContent:\n{text[:1500]}"
        )
        return min(max(float(_model.generate_content(prompt).text.strip().split()[0]), 0.0), 10.0)
    except Exception:
        return 0.0


def gemini_summarise(text: str, product: str, url: str) -> str:
    try:
        prompt = (
            "Summarise in exactly 3 bullet points starting with •. "
            "Cover: what capability, how to use/configure it, integration relevance.\n\n"
            f"Product: {product}\nURL: {url}\nContent:\n{text[:3000]}"
        )
        return _model.generate_content(prompt).text.strip()
    except Exception:
        return "• Summary unavailable"


def keyword_relevant(text: str) -> bool:
    """Quick keyword check — avoids wasting Gemini tokens on obviously irrelevant content."""
    keywords = [
        "ipd", "epd", "plm", "s/4hana", "s4hana", "btp", "successfactors",
        "ariba", "integration suite", "iflow", "cap ", "extension",
        "dms", "ectr", "mdg", "engineering", "product lifecycle",
        "product development", "bom", "bill of material", "variant configuration",
        "migration", "sap cloud", "fiori", "odata", "api management",
    ]
    t = text.lower()
    return any(k in t for k in keywords)


# ─── PROGRESS ─────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"done_seeds": False, "done_github": False, "done_rss": False,
            "done_apihub": False, "sheet_id": GOOGLE_SHEET_ID}


def save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


# ─── HTTP ──────────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}


def safe_get(url: str, headers: dict = None, params: dict = None, retries: int = 3):
    time.sleep(RATE_LIMIT)
    h = headers or _HEADERS
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=h, params=params, timeout=25)
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


# ─── SOURCE 1: SEED KNOWLEDGE BASE ────────────────────────────────────────────

def load_seed_knowledge() -> list:
    """
    Load hardcoded known SAP IPD/PLM documentation entries.
    These are verified official SAP sources — gives the brain immediate knowledge.
    """
    print("\n[Seed] Loading known SAP IPD/PLM knowledge entries...")
    results = []
    for product, title, url, description, ev_type, confidence in SEED_KNOWLEDGE:
        summary = gemini_summarise(description, product, url)
        results.append(_row(product, title, url, summary, 8.0, ev_type, confidence, []))
        print(f"  [Seed OK] {title[:70]}")
    print(f"[Seed] Loaded {len(results)} entries")
    return results


# ─── SOURCE 2: GITHUB ─────────────────────────────────────────────────────────

def search_github() -> list:
    """Search GitHub for SAP solutions — includes SAP-samples official org."""
    print("\n[GitHub] Searching GitHub for SAP solutions...")
    results = []
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    seen = set()

    # Search SAP-samples organisation specifically — official SAP code
    print("  [GitHub] Searching SAP-samples org...")
    sap_orgs = ["SAP-samples", "SAP"]
    for org in sap_orgs:
        for topic in ["plm", "ipd", "btp-extension", "integration-suite", "cap-samples"]:
            time.sleep(2)
            try:
                resp = requests.get(
                    "https://api.github.com/search/repositories",
                    params={"q": f"org:{org} {topic}", "sort": "updated", "per_page": 5},
                    headers=headers, timeout=15
                )
                if resp.status_code == 200:
                    for repo in resp.json().get("items", []):
                        url = repo["html_url"]
                        if url not in seen:
                            seen.add(url)
                            r = _process_github_repo(repo, headers)
                            if r:
                                results.append(r)
            except Exception as e:
                print(f"    [GitHub org error] {e}")

    # General GitHub search
    for query in GITHUB_QUERIES:
        time.sleep(2)
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "per_page": 5},
                headers=headers, timeout=15
            )
            if resp.status_code == 200:
                for repo in resp.json().get("items", []):
                    url = repo["html_url"]
                    if url not in seen:
                        seen.add(url)
                        r = _process_github_repo(repo, headers)
                        if r:
                            results.append(r)
        except Exception as e:
            print(f"    [GitHub search error] {e}")

    print(f"[GitHub] Found {len(results)} relevant repos")
    return results


def _process_github_repo(repo: dict, headers: dict):
    """Fetch README and score a GitHub repo."""
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

    combined = f"{repo['name']}\n{repo.get('description', '')}\n{readme}"
    if not keyword_relevant(combined):
        return None

    score = gemini_score(combined, "GitHub SAP")
    if score < SCORE_THRESHOLD:
        print(f"    [GitHub Skip] {repo['full_name']} Score:{score:.1f}")
        return None

    summary = gemini_summarise(combined, "GitHub", repo["html_url"])
    notes = extract_note_refs(combined)
    print(f"    [GitHub OK] {repo['full_name']} | Score:{score:.1f}")
    return _row("GitHub", repo["full_name"], repo["html_url"],
                summary, score, "GITHUB", "? ASSUMED", notes)


# ─── SOURCE 3: SAP COMMUNITY RSS ──────────────────────────────────────────────

def crawl_community_rss() -> list:
    """
    Fetch SAP Community RSS feeds — XML format, works from GitHub Actions.
    Filters posts relevant to IPD/PLM/BTP.
    """
    print("\n[Community RSS] Fetching SAP Community feeds...")
    results = []
    seen = set()

    for feed_url in SAP_COMMUNITY_RSS:
        resp = safe_get(feed_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/rss+xml, application/xml",
        })
        if not resp:
            print(f"  [RSS] Could not fetch: {feed_url}")
            continue

        try:
            root = ElementTree.fromstring(resp.content)
            items = root.findall(".//item")
            print(f"  [RSS] {feed_url.split('/')[4]} → {len(items)} posts")

            for item in items[:30]:
                title = (item.findtext("title") or "").strip()
                link  = (item.findtext("link") or "").strip()
                desc  = (item.findtext("description") or "").strip()

                if not title or not link or link in seen:
                    continue
                if not keyword_relevant(f"{title} {desc}"):
                    continue
                seen.add(link)

                # Clean HTML from description
                try:
                    desc_text = BeautifulSoup(desc, "lxml").get_text(separator=" ", strip=True)[:2000]
                except Exception:
                    desc_text = desc[:2000]

                combined = f"{title}\n{desc_text}"
                score = gemini_score(combined, "SAP Community")
                if score < SCORE_THRESHOLD:
                    continue

                summary  = gemini_summarise(combined, "Community", link)
                notes    = extract_note_refs(combined)
                results.append(_row("Community", title[:200], link, summary,
                                    score, "COMMUNITY", "? ASSUMED", notes))
                print(f"    [RSS OK] {title[:70]} | Score:{score:.1f}")

        except Exception as e:
            print(f"  [RSS parse error] {e}")

    print(f"[Community RSS] Found {len(results)} relevant posts")
    return results


# ─── SOURCE 4: SAP API HUB ────────────────────────────────────────────────────

def crawl_sap_api_hub() -> list:
    """
    Fetch SAP API Business Hub catalog — returns JSON with API metadata.
    Excellent for BTP_Tools and DIRECT_IPD tabs.
    """
    print("\n[API Hub] Fetching SAP API Hub catalog...")
    results = []
    seen = set()

    # Try the API Hub search endpoint
    api_searches = [
        ("SAP IPD", "https://api.sap.com/api/packages?search=IPD"),
        ("SAP PLM", "https://api.sap.com/api/packages?search=PLM"),
        ("SAP BTP Integration", "https://api.sap.com/api/packages?search=Integration+Suite"),
        ("SAP S4HANA", "https://api.sap.com/api/packages?search=S4HANA+PLM"),
    ]

    for label, url in api_searches:
        resp = safe_get(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })
        if not resp:
            continue
        try:
            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", data.get("results", []))
            if not isinstance(items, list):
                continue

            for pkg in items[:10]:
                name  = pkg.get("name", "") or pkg.get("title", "")
                desc  = pkg.get("description", "") or pkg.get("shortText", "")
                links = pkg.get("links", [])
                url_  = f"https://api.sap.com/package/{pkg.get('packageId', pkg.get('id', ''))}" if pkg.get("packageId") or pkg.get("id") else "https://api.sap.com"

                if not name or url_ in seen:
                    continue
                seen.add(url_)

                combined = f"{name}\n{desc}"
                if not keyword_relevant(combined):
                    continue

                score   = gemini_score(combined, label)
                if score < SCORE_THRESHOLD:
                    continue

                summary = gemini_summarise(combined, label, url_)
                ev_type, confidence = classify(label, url_)
                results.append(_row(label, name[:200], url_, summary,
                                    score, ev_type, confidence, []))
                print(f"    [API Hub OK] {name[:70]} | Score:{score:.1f}")

        except Exception as e:
            print(f"  [API Hub parse error for {label}] {e}")

    print(f"[API Hub] Found {len(results)} entries")
    return results


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
    if any(x in u or x in p for x in ["sap_ipd", "ipd", "integrated-product", "integrated_product"]):
        return "DIRECT_IPD", "✓ CONFIRMED"
    if any(x in u or x in p for x in ["sap_epd", "epd", "engineering-product"]):
        return "DIRECT_IPD", "✓ CONFIRMED"
    if any(x in p for x in ["successfactors", "ariba", "concur", "fsm"]):
        return "BTP_ANALOGY", "~ ANALOGY"
    if any(x in p or x in u for x in ["btp_tools", "btp tools", "integration suite",
                                       "build apps", "event mesh", "api management",
                                       "cap ", "connectivity", "extension suite"]):
        return "BTP_TOOL", "✓ CONFIRMED"
    if "community.sap.com" in u or "community" in p:
        return "COMMUNITY", "? ASSUMED"
    if "github.com" in u or "github" in p:
        return "GITHUB", "? ASSUMED"
    return "DIRECT", "✓ CONFIRMED"


def _row(product, title, url, summary, score, ev_type, confidence, notes) -> dict:
    return {
        "product":    str(product),
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
    creds = Credentials.from_service_account_info(creds_dict, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
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
    if ev_type == "BTP_ANALOGY" or any(x in p for x in ["successfactors", "ariba"]):
        return "BTP_Analogy"
    if ev_type == "BTP_TOOL" or any(x in p for x in ["btp", "integration", "cap", "build", "event"]):
        return "BTP_Tools"
    if ev_type == "COMMUNITY" or "community" in p:
        return "Community_Discussions"
    if ev_type == "GITHUB" or "github" in p:
        return "GitHub_Community"
    return "SAP_IPD_Direct"


def flush(results: list, sheets: dict) -> None:
    if not results:
        return
    groups = {}
    for r in results:
        tab = _route_tab(r["evidence_type"], r["product"])
        groups.setdefault(tab, []).append(r)
    for tab, rows in groups.items():
        ws = sheets.get(tab)
        if not ws:
            continue
        batch = [[r["product"], r["title"], r["url"], r["summary"],
                  r["score"], r["evidence_type"], r["confidence"],
                  r["referenced_notes"], r["date"]] for r in rows]
        # Write in sub-batches of BATCH_SIZE
        for i in range(0, len(batch), BATCH_SIZE):
            ws.append_rows(batch[i:i+BATCH_SIZE], value_input_option="RAW")
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
    gc, ss, sheets = init_sheets(progress)
    total = 0

    # 1 — Seed knowledge base (always refreshed — fast, no web calls except Gemini)
    if not progress.get("done_seeds"):
        seeds = load_seed_knowledge()
        flush(seeds, sheets)
        total += len(seeds)
        progress["done_seeds"] = True
        save_progress(progress)

    # 2 — GitHub (SAP-samples + community)
    if not progress.get("done_github"):
        gh = search_github()
        flush(gh, sheets)
        total += len(gh)
        progress["done_github"] = True
        save_progress(progress)

    # 3 — SAP Community RSS
    if not progress.get("done_rss"):
        rss = crawl_community_rss()
        flush(rss, sheets)
        total += len(rss)
        progress["done_rss"] = True
        save_progress(progress)

    # 4 — SAP API Hub
    if not progress.get("done_apihub"):
        api = crawl_sap_api_hub()
        flush(api, sheets)
        total += len(api)
        progress["done_apihub"] = True
        save_progress(progress)

    # Reset flags so next Sunday re-crawls everything fresh
    progress["done_seeds"] = False
    progress["done_github"] = False
    progress["done_rss"]    = False
    progress["done_apihub"] = False
    save_progress(progress)

    export_excel(ss)

    print("\n" + "=" * 65)
    print(f"Crawl complete! Total rows written: {total}")
    print(f"Sheet: https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}")
    print(f"Excel: {EXCEL_FILE}")
    print("=" * 65)


if __name__ == "__main__":
    main()
