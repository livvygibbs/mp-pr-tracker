"""
MP PR/electoral reform tracker - data collection script.

Pulls two datasets and merges them into a single JSON file keyed by MP:

1. APPG for Fair Elections membership (name, party, constituency)
   Source: parallelparliament.co.uk (unofficial aggregator; parliament's own
   register only lists officers, not full membership, so this is currently
   the best public source for the full list)

2. NC31 signatories (Alex Sobel's amendment to the Representation of the
   People Bill, calling for a National Commission on Electoral Reform)
   Source: bills-api.parliament.uk (official JSON API)

IMPORTANT - read before running:

- The NC31 fetch hits bills-api.parliament.uk directly with `requests` and
  works fine. No browser needed.

- The APPG fetch CANNOT use plain `requests` any more: parallelparliament.co.uk
  sits behind Cloudflare's bot-challenge and returns HTTP 403 with
  "Cf-Mitigated: challenge" to a plain GET, even with a normal-looking
  User-Agent (confirmed by testing). It renders fine in a real browser,
  so this script drives one with Playwright instead.

- BILL_ID and the "NC31" search string may need updating as the bill
  progresses. Check https://bills.parliament.uk/bills/4080/stages for the
  bill's current stage.

Usage:
    pip install requests playwright
    playwright install chromium
    python scrape_mp_pr_data.py

Output:
    mp_pr_data.json - list of MP records, e.g.:
    {
      "name": "Alex Sobel",
      "party": "Labour (Co-op)",
      "constituency": "Leeds Central and Headingley",
      "appg_fair_elections_member": true,
      "appg_role": "Chair & Registered Contact",
      "nc31_signatory": true,
      "parliamentary_email": "alex.sobel.mp@parliament.uk",
      "quotes": []
    }
"""

import json
import re
import sys
import time

try:
    import requests
    from requests.adapters import HTTPAdapter, Retry
except ImportError:
    print("Missing dependency. Run: pip install requests")
    sys.exit(1)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (PR-tracker research tool; contact: your-email@example.com)"
}

# The Members API is unauthenticated and has no documented rate limit, but a
# tight loop of 200+ sequential requests (one per MP, for the email lookups
# in main()) reliably gets a handful of connections reset mid-request. A
# shared session with retries absorbs those instead of crashing the run.
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=5, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
)))

APPG_URL = "https://www.parallelparliament.co.uk/APPG/fair-elections"

BILL_ID = 4080  # Representation of the People Bill
AMENDMENT_NUMBER = "NC31"

# Manually-curated quotes: keep these here across re-runs since neither
# source provides them. Keyed by the display name used in the output.
MANUAL_QUOTES = {
    "Alex Sobel": [
        {
            "text": "My amendment to establish a national commission on electoral reform is now the most supported amendment this parliament with 166 signatories from eight parties, half of whom are Labour.",
            "source": "Politics.co.uk",
            "date": "8 July 2026",
        }
    ],
}


def normalize_name(name):
    """Strip common honorific prefixes so the two sources match on the same
    person - the Bills API includes titles like "Dr"/"Mr" that
    parallelparliament.co.uk doesn't, which otherwise creates duplicate
    records for the same MP."""
    return re.sub(r"^(Dr|Mr|Mrs|Ms|Miss|Sir|Dame|Prof)\.?\s+", "", name.strip()).lower()


def fetch_nc31_signatories():
    """Fetch NC31 sponsors from the official Bills API. Finds the bill's
    current stage, then looks up the amendment by its marshalled number
    (e.g. "NC31") within that stage."""
    stages_resp = SESSION.get(
        f"https://bills-api.parliament.uk/api/v1/Bills/{BILL_ID}/Stages",
        headers=HEADERS, timeout=20,
    )
    stages_resp.raise_for_status()
    stages = stages_resp.json()["items"]
    if not stages:
        raise RuntimeError(f"No stages found for bill {BILL_ID}")
    current_stage_id = stages[-1]["id"]

    amendments_resp = SESSION.get(
        f"https://bills-api.parliament.uk/api/v1/Bills/{BILL_ID}/Stages/{current_stage_id}/Amendments",
        headers=HEADERS, params={"AmendmentNumber": AMENDMENT_NUMBER}, timeout=20,
    )
    amendments_resp.raise_for_status()
    amendments = amendments_resp.json()["items"]

    match = next(
        (a for a in amendments if a.get("marshalledListText") == AMENDMENT_NUMBER),
        None,
    )
    if not match:
        print(f"Could not find {AMENDMENT_NUMBER} in stage {current_stage_id} amendments. "
              f"Check the bill's current stage at https://bills.parliament.uk/bills/{BILL_ID}/stages")
        return []

    detail_resp = SESSION.get(
        f"https://bills-api.parliament.uk/api/v1/Bills/{BILL_ID}/Stages/{current_stage_id}/Amendments/{match['amendmentId']}",
        headers=HEADERS, timeout=20,
    )
    detail_resp.raise_for_status()
    sponsors = detail_resp.json()["sponsors"]

    return [
        {
            "name": s["name"],
            "party": s["party"],
            "constituency": s["memberFrom"],
            "member_id": s["memberId"],
        }
        for s in sponsors
    ]


def fetch_appg_members():
    """Scrape the full APPG for Fair Elections membership list using a real
    browser (Playwright), since parallelparliament.co.uk's Cloudflare
    challenge blocks plain HTTP requests.

    Structure as of last check: each officer/member is a
    `div.row` containing a `col-sm-2` photo link and a `col-sm-4` block with
    an `<h5>` name and two `<h6>`s (role, then "party - constituency").
    The membership list ends where a "Former APPG Officers" section begins -
    rows after that point are excluded.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Missing dependency for APPG scraping. Run:")
        print("  pip install playwright")
        print("  playwright install chromium")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(APPG_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("div.col-sm-4 h5", timeout=15000)

        members = page.evaluate(r"""
            () => {
                const cleanText = s => s.replace(/\s+/g, ' ').trim();

                const formerHeading = Array.from(document.querySelectorAll('*')).find(el =>
                    el.children.length === 0 && cleanText(el.textContent) === 'Former APPG Officers'
                );

                const rows = Array.from(document.querySelectorAll('div.row')).filter(r => {
                    const hasPhotoCol = r.querySelector(':scope > a[href^="/mp/"].col-sm-2, :scope > a[href^="/lord/"].col-sm-2');
                    const hasNameCol = r.querySelector(':scope > div.col-sm-4');
                    if (!hasPhotoCol || !hasNameCol) return false;
                    if (formerHeading) {
                        const pos = r.compareDocumentPosition(formerHeading);
                        if (!(pos & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
                    }
                    return true;
                });

                return rows.map(row => {
                    const nameCol = row.querySelector(':scope > div.col-sm-4');
                    const h5 = nameCol.querySelector('h5');
                    const h6s = Array.from(nameCol.querySelectorAll('h6'));
                    const role = h6s[0] ? cleanText(h6s[0].textContent) : null;
                    const partyConstituency = h6s[1] ? cleanText(h6s[1].textContent) : null;
                    let party = null, constituency = null;
                    if (partyConstituency && partyConstituency.includes(' - ')) {
                        const idx = partyConstituency.indexOf(' - ');
                        party = partyConstituency.slice(0, idx).trim();
                        constituency = partyConstituency.slice(idx + 3).trim();
                    } else {
                        party = partyConstituency;
                    }
                    return {
                        name: h5 ? cleanText(h5.textContent) : null,
                        role,
                        party,
                        constituency
                    };
                });
            }
        """)

        browser.close()
        return members


def resolve_member_id(name):
    """Look up a member's ID via the official Members API by name search.
    Used for APPG-only members, since parallelparliament.co.uk doesn't
    expose a parliament.uk member ID directly (only a name-based slug).

    Returns None (rather than guessing) if there isn't exactly one active
    match, so a bad ID never silently ends up in the output."""
    resp = SESSION.get(
        "https://members-api.parliament.uk/api/Members/Search",
        headers=HEADERS, params={"Name": name}, timeout=20,
    )
    resp.raise_for_status()
    items = resp.json()["items"]
    values = [i.get("value") for i in items if i.get("value")]
    active = [
        v for v in values
        if ((v.get("latestHouseMembership") or {}).get("membershipStatus") or {}).get("statusIsActive")
    ]
    if len(active) != 1:
        return None
    return active[0]["id"]


def fetch_parliamentary_email(member_id):
    """Fetch the official @parliament.uk email for a member ID from the
    Members API's Contact endpoint - this is the verified source for the
    firstname.lastname[.mp]@parliament.uk address, which isn't a reliable
    enough pattern to generate from a name alone (accents get stripped,
    hyphens get dropped, Lords use a different format than Commons MPs)."""
    resp = SESSION.get(
        f"https://members-api.parliament.uk/api/Members/{member_id}/Contact",
        headers=HEADERS, timeout=20,
    )
    resp.raise_for_status()
    # Filter by the @parliament.uk domain rather than the "type" label -
    # some offices file this email under "Constituency office" instead of
    # "Parliamentary office", so the label alone isn't a reliable signal.
    for contact in resp.json()["value"]:
        email = (contact.get("email") or "").strip()
        if email.lower().endswith("@parliament.uk"):
            return email
    return None


def main():
    print("Fetching NC31 signatories from bills-api.parliament.uk...")
    nc31 = fetch_nc31_signatories()
    print(f"  Found {len(nc31)} signatories")

    print("Fetching APPG for Fair Elections membership (via headless browser)...")
    appg = fetch_appg_members()
    print(f"  Found {len(appg)} members")

    appg_by_norm = {normalize_name(m["name"]): m for m in appg}
    nc31_by_norm = {normalize_name(s["name"]): s for s in nc31}
    all_keys = set(appg_by_norm) | set(nc31_by_norm)

    print(f"Looking up parliamentary contact emails for {len(all_keys)} people...")
    unresolved = []
    records = []
    for key in all_keys:
        a = appg_by_norm.get(key)
        n = nc31_by_norm.get(key)
        display_name = a["name"] if a else n["name"]

        # NC31 sponsor data already carries an official member ID; APPG-only
        # members need a name lookup against the Members API.
        member_id = n["member_id"] if n else resolve_member_id(display_name)

        email = None
        if member_id is not None:
            email = fetch_parliamentary_email(member_id)
        if member_id is None or email is None:
            unresolved.append(display_name)
        time.sleep(0.05)  # spread out ~234 sequential requests to avoid connection resets

        records.append({
            "name": display_name,
            "party": (a["party"] if a else None) or (n["party"] if n else None),
            "constituency": (a["constituency"] if a else None) or (n["constituency"] if n else None),
            "appg_fair_elections_member": a is not None,
            "appg_role": a["role"] if a else None,
            "nc31_signatory": n is not None,
            "parliamentary_email": email,
            "quotes": MANUAL_QUOTES.get(display_name, []),
        })

    records.sort(key=lambda r: r["name"])

    if unresolved:
        print(f"  Could not resolve a verified email for {len(unresolved)} people "
              f"(ambiguous name match or no email on file) - contact button will be "
              f"hidden for these in the UI:")
        for name in unresolved:
            print(f"    - {name}")

    with open("mp_pr_data.json", "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {len(records)} MP records to mp_pr_data.json")
    print(f"  APPG members: {sum(r['appg_fair_elections_member'] for r in records)}")
    print(f"  NC31 signatories: {sum(r['nc31_signatory'] for r in records)}")
    print("Quotes are only filled in for names in MANUAL_QUOTES - that part stays manual.")


if __name__ == "__main__":
    main()
