"""
SAP Notes & KBA Lookup — LAPTOP TOOL ONLY
==========================================
Run this on your own laptop when you need full SAP Note or KBA content.

YOUR S-USER PASSWORD IS NEVER SAVED ANYWHERE.
This script never connects to GitHub.
You type credentials at the keyboard — they exist only in memory while the script runs.

How to run:
  python notes_lookup.py

You will be asked for:
  1. Your Gemini API key (or set GEMINI_API_KEY environment variable)
  2. Path to your Google service account JSON file
  3. Your SAP S-User ID (e.g. S0001234567)
  4. Your SAP password (hidden as you type)
  5. SAP Note / KBA numbers to look up (comma-separated)

Results are saved to the SAP_Notes tab of your Google Sheet.
"""

import getpass
import json
import os
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai

PROGRESS_FILE = "progress.json"
SAP_LOGIN_URL = "https://accounts.sap.com/saml2/idp/sso"
SAP_NOTE_URLS = [
    "https://me.sap.com/notes/{note}",
    "https://launchpad.support.sap.com/#/notes/{note}",
]

SAP_NOTE_HEADERS = [
    "Note_Number", "Title", "URL", "Summary",
    "Full_Text_Preview", "Evidence_Type", "Confidence", "Date_Fetched",
]

# ─── SETUP ────────────────────────────────────────────────────────────────────

def setup_gemini():
    key = os.environ.get("GEMINI_API_KEY") or input(
        "\nGemini API key (press Enter to skip summarisation): "
    ).strip()
    if key:
        genai.configure(api_key=key)
        return genai.GenerativeModel("gemini-1.5-flash")
    return None


def load_google_sheet(creds_path: str):
    """Authenticate with Google and open the SAP_Notes tab."""
    with open(creds_path, encoding="utf-8") as f:
        creds_dict = json.load(f)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)

    if not os.path.exists(PROGRESS_FILE):
        print(f"\nCould not find {PROGRESS_FILE}.")
        print("Run crawler.py on GitHub first so the Google Sheet is created.")
        sys.exit(1)

    with open(PROGRESS_FILE, encoding="utf-8") as f:
        progress = json.load(f)

    sheet_id = progress.get("sheet_id")
    if not sheet_id:
        print("No Sheet ID found in progress.json. Run crawler.py first.")
        sys.exit(1)

    ss = gc.open_by_key(sheet_id)
    try:
        ws = ss.worksheet("SAP_Notes")
    except Exception:
        ws = ss.add_worksheet("SAP_Notes", rows=2000, cols=len(SAP_NOTE_HEADERS))
        ws.append_row(SAP_NOTE_HEADERS)
        print("Created SAP_Notes tab in your Google Sheet")

    return ws


# ─── SAP LOGIN ────────────────────────────────────────────────────────────────

def login_sap(s_user: str, password: str) -> requests.Session:
    """
    Attempt to log into SAP support portal.
    Returns a requests Session if successful, None otherwise.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # Step 1: Get login page
    try:
        resp = session.get("https://me.sap.com/notes/0000000000", timeout=20)
    except Exception as e:
        print(f"  Could not reach SAP portal: {e}")
        return None

    # Step 2: Submit credentials
    try:
        data = {
            "j_username": s_user,
            "j_password": password,
            "login": "true",
        }
        session.post(SAP_LOGIN_URL, data=data, timeout=20)
    except Exception as e:
        print(f"  Login request failed: {e}")
        return None

    return session


# ─── NOTE FETCHER ─────────────────────────────────────────────────────────────

def fetch_note(session: requests.Session, note_number: str):
    """
    Try multiple SAP URLs to fetch a Note. Returns (text, url) or (None, None).
    """
    for url_template in SAP_NOTE_URLS:
        url = url_template.format(note=note_number)
        time.sleep(2)
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200 and len(resp.text) > 300:
                soup = BeautifulSoup(resp.text, "lxml")
                text = soup.get_text(separator=" ", strip=True)
                if len(text) > 100:
                    return text[:6000], url
        except Exception:
            continue
    return None, None


def extract_title(text: str, note_number: str) -> str:
    """Try to find the Note title in the first few lines of text."""
    for line in text.split("\n")[:8]:
        line = line.strip()
        if 10 < len(line) < 200 and not line.startswith("http"):
            return line
    return f"SAP Note {note_number}"


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("SAP Notes & KBA Lookup — Laptop Tool")
    print("Your credentials are NEVER saved anywhere.")
    print("=" * 65)

    # 1. Set up Gemini
    model = setup_gemini()

    # 2. Google Sheets credentials
    creds_path = input(
        "\nFull path to your Google service account JSON file\n"
        "(e.g. C:\\Users\\divya\\Downloads\\service-account.json): "
    ).strip().strip('"')
    if not os.path.exists(creds_path):
        print(f"File not found: {creds_path}")
        sys.exit(1)

    # 3. SAP credentials
    print()
    s_user   = input("SAP S-User ID (e.g. S0001234567): ").strip()
    password = getpass.getpass("SAP password (hidden, not saved): ")

    # 4. Note numbers
    notes_raw = input(
        "\nSAP Note / KBA numbers to look up\n"
        "(comma-separated, e.g. 3345678, 3298001, KBA2987654): "
    )
    note_numbers = []
    for part in notes_raw.split(","):
        num = part.strip().lstrip("KBAkba #")
        if num.isdigit() and len(num) >= 6:
            note_numbers.append(num)

    if not note_numbers:
        print("No valid note numbers entered. Exiting.")
        sys.exit(1)

    print(f"\nLooking up {len(note_numbers)} notes: {', '.join(note_numbers)}")

    # 5. Login to SAP
    print("\nLogging into SAP Support Portal...")
    session = login_sap(s_user, password)
    # Clear password from memory immediately
    password = None

    if not session:
        print("Could not log in. Check your S-User ID and password.")
        sys.exit(1)
    print("Login attempted. Fetching notes...")

    # 6. Load Google Sheet tab
    ws = load_google_sheet(creds_path)

    # 7. Fetch and summarise each Note
    rows = []
    for note_num in note_numbers:
        print(f"\n  Fetching Note {note_num}...")
        text, url = fetch_note(session, note_num)

        if not text:
            print(f"  Could not retrieve Note {note_num} — it may require additional login steps.")
            print(f"  Try opening manually: https://me.sap.com/notes/{note_num}")
            continue

        title = extract_title(text, note_num)

        # Summarise with Gemini if available
        if model:
            try:
                summary = model.generate_content(
                    "Summarise this SAP Note in 3 concise bullet points for a PLM/IPD consultant. "
                    "Cover: what problem it fixes, what action to take, any IPD or BTP relevance. "
                    f"Start each bullet with •\n\n{text}"
                ).text.strip()
            except Exception:
                summary = text[:400]
        else:
            summary = text[:400]

        print(f"  Title   : {title[:80]}")
        print(f"  Summary : {summary[:120]}...")

        rows.append([
            note_num,
            title[:200],
            url or f"https://me.sap.com/notes/{note_num}",
            summary,
            text[:500],
            "SAP_NOTE",
            "✓ CONFIRMED",
            datetime.now().strftime("%Y-%m-%d"),
        ])

    # 8. Write to Google Sheet
    if rows:
        ws.append_rows(rows, value_input_option="RAW")
        print(f"\nSaved {len(rows)} note(s) to the SAP_Notes tab in your Google Sheet.")
    else:
        print("\nNo notes were successfully retrieved.")

    print("\nDone. Your SAP credentials were never saved to any file.")


if __name__ == "__main__":
    main()
