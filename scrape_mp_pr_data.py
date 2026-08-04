"""
MP PR/electoral reform tracker - data collection script.

Pulls three datasets and merges them into a single JSON file covering every
current MP, not just the ones who happen to support electoral reform:

1. Every current House of Commons MP (name, party, constituency)
   Source: members-api.parliament.uk (official JSON API) - this is the base
   list; APPG/NC31 status is layered on top of it, defaulting to false for
   anyone not on either list.

2. APPG for Fair Elections membership (name, party, constituency)
   Source: parallelparliament.co.uk (unofficial aggregator; parliament's own
   register only lists officers, not full membership, so this is currently
   the best public source for the full list). Includes a handful of Lords,
   who aren't part of the Commons roster above and are added separately.

3. NC31 signatories (Alex Sobel's amendment to the Representation of the
   People Bill, calling for a National Commission on Electoral Reform)
   Source: bills-api.parliament.uk (official JSON API)

Every person in the output also gets a verified @parliament.uk contact
email where the Members API has one on file (see fetch_parliamentary_email),
so constituents can email their MP regardless of their reform stance.

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
      "house": "Commons",
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
        },
    ],
    "Andrew Lewin": [
        {
            "text": "I want to start by putting on record that I am a long-standing advocate of a more proportional electoral system for our general elections.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Andrew Ranger": [
        {
            "text": "This is fundamentally an argument about fairness. Everyone's vote should be equal, and should count.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Anna Dixon": [
        {
            "text": "I turn to the benefits of PR, for which I am a strong advocate. I saw as a young politics student in Germany how PR led to more stable government.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Cameron Thomas": [
        {
            "text": "The only way to ensure that the next election returns a representative Parliament is to transition to a proportional representation electoral system.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Claire Young": [
        {
            "text": "Does the hon. Gentleman agree that, in our current system, people vote for what can actually be very loose coalitions? Our electoral system forces us to have very large coalitions in order to form a Government, but voters do not know which parts of those coalitions they are going to get after an election.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Clive Jones": [
        {
            "text": "We need to fundamentally change our electoral system. It is undemocratic that under the UK's electoral system, not all votes count in the same way.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Florence Eshalomi": [
        {
            "text": "Turnout at the general election in July last year dropped to below 60%, which means that two in every five people did not participate. Does my hon. Friend agree that that shows we need change, so that more people engage in our democratic system?",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Jeevun Sandher": [
        {
            "text": "Proportional representation ensures that we have an equal say in how we our governed and, what is crucially missed, also leads to less poverty and more growth.",
            "source": "Progress Online",
            "date": "13 January 2023",
        },
    ],
    "Joe Powell": [
        {
            "text": "Our current system is failing to command public trust. That is the foundation of my belief in electoral reform.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Lisa Smart": [
        {
            "text": "The case for electoral reform is urgent and undeniable. First past the post is a system that no longer functions as a fair or effective mechanism for translating the will of the electorate into parliamentary representation.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Luke Akehurst": [
        {
            "text": "All voters should have equal value wherever they live in the UK, but first past the post condemns millions of voters to living in electoral deserts where just one party dominates all Commons representation.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Marie Goldman": [
        {
            "text": "Our antiquated first-past-the-post system can be incredibly demoralising, even for a committed political campaigner like myself.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Noah Law": [
        {
            "text": "Under the current system, many voters feel that their vote fails to express those nuances, which can lead to disengagement, disillusionment and a sense that the political system does not serve them.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Olly Glover": [
        {
            "text": "The United Kingdom is highly anomalous in retaining first past the post. Very few other European countries do so.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Rachel Gilmour": [
        {
            "text": "The system is not fair, and it is not proportional. I and my colleagues in my party will continue to fight hard to raise awareness about its unfairness, not because it is the politically expedient thing to do—as has been pointed out, we did rather well under first past the post at the last general election—but because it is the right thing to do.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Sarah Olney": [
        {
            "text": "First past the post is a broken and unfair system. Last summer, the Labour party won a landslide election victory, securing 63% of seats in the House of Commons in return for just 34% of the vote.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Siân Berry": [
        {
            "text": "I am not here to make arguments that are only in my own self-interest. Proportionality is not the goal here; a better politics is.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Steve Race": [
        {
            "text": "I too wish to take this opportunity to put on record my support for electoral reform, to ensure that the composition of our representatives better reflects the wishes of voters and that voters can exercise more choice.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Tim Roca": [
        {
            "text": "I hope all of us here are committed to the fundamental principle that we should have a functioning, representative democracy; and that elections should reflect the will of the people.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
    "Tom Hayes": [
        {
            "text": "On Monday morning, I visited the year 10 citizenship class at St Peter's school in Bournemouth, where I spoke with several young people, including Ozzie, who was only just born at the time of the last vote. He asked me whether I agreed that too many people feel their vote does not count, that too many younger people feel disconnected from democracy, and that the continuation of first past the post will leave more people—particularly younger people—disconnected from democracy.",
            "source": "Hansard, House of Commons",
            "date": "30 January 2025",
        },
    ],
}


HONORIFIC_RE = re.compile(r"^(Dr|Mr|Mrs|Ms|Miss|Sir|Dame|Prof)\.?\s+")


def normalize_name(name):
    """Strip common honorific prefixes so the two sources match on the same
    person - the Bills API includes titles like "Dr"/"Mr" that
    parallelparliament.co.uk doesn't, which otherwise creates duplicate
    records for the same MP."""
    return HONORIFIC_RE.sub("", name.strip()).lower()


def display_name_for(name):
    """Same prefix-stripping as normalize_name, but case-preserving - used
    for the name actually shown in the UI. The official Members API's
    nameDisplayAs field includes a title for some MPs (e.g. "Mr Bayo Alaba",
    "Sir Julian Lewis") but not others, which otherwise makes the tracker
    look inconsistently formatted between MPs from different sources."""
    return HONORIFIC_RE.sub("", name.strip())


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
                    const photoLink = row.querySelector(':scope > a[href^="/mp/"], :scope > a[href^="/lord/"]');
                    const house = photoLink.getAttribute('href').startsWith('/lord/') ? 'Lords' : 'Commons';
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
                        house,
                        role,
                        party,
                        constituency
                    };
                });
            }
        """)

        browser.close()
        return members


def fetch_all_current_commons_mps():
    """Fetch every current House of Commons MP from the official Members
    API, so the tracker can show all ~650 MPs - not just the ones who
    happen to be APPG members or NC31 signatories - each labelled with
    both flags, defaulting to false where neither applies.

    The Search endpoint caps at 20 results per page regardless of the
    `take` value requested, so this pages through with `skip` until it's
    collected everything the API reports in `totalResults`."""
    members = []
    skip = 0
    page_size = 20
    while True:
        resp = SESSION.get(
            "https://members-api.parliament.uk/api/Members/Search",
            headers=HEADERS,
            params={"House": 1, "IsCurrentMember": "true", "skip": skip, "take": page_size},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data["items"]:
            v = item["value"]
            hm = v.get("latestHouseMembership") or {}
            members.append({
                "id": v["id"],
                "name": v["nameDisplayAs"],
                "party": (v.get("latestParty") or {}).get("name"),
                "constituency": hm.get("membershipFrom"),
            })
        skip += page_size
        if skip >= data["totalResults"]:
            break
        time.sleep(0.05)
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

    print("Fetching the full current House of Commons roster...")
    commons = fetch_all_current_commons_mps()
    print(f"  Found {len(commons)} current MPs")

    appg_by_norm = {normalize_name(m["name"]): m for m in appg}
    nc31_by_norm = {normalize_name(s["name"]): s for s in nc31}
    commons_by_norm = {normalize_name(m["name"]): m for m in commons}

    # Base the output on every current Commons MP (so all ~650 show up, not
    # just APPG members / NC31 signatories), plus any APPG members who are
    # actually Lords - those wouldn't be in the Commons roster at all, and
    # dropping them would lose data this tracker already had. Filtering by
    # house (not just "missing from the Commons roster") matters: someone
    # who's *left* the Commons since parallelparliament.co.uk last updated
    # (e.g. lost a by-election) is also missing from commons_by_norm, and
    # should be dropped, not resurrected as if they were a peer.
    lords_only_keys = {
        k for k, m in appg_by_norm.items()
        if m.get("house") == "Lords" and k not in commons_by_norm
    }
    all_keys = set(commons_by_norm) | lords_only_keys

    print(f"Looking up parliamentary contact emails for {len(all_keys)} people...")
    unresolved = []
    records = []
    for key in all_keys:
        c = commons_by_norm.get(key)
        a = appg_by_norm.get(key)
        n = nc31_by_norm.get(key)
        display_name = display_name_for(c["name"] if c else (a["name"] if a else n["name"]))

        # The Commons roster and NC31 sponsor data both carry an official
        # member ID already; Lords-only APPG members need a name lookup.
        # A transient network blip mid-lookup shouldn't blow up a run that's
        # already 20+ minutes into ~650 sequential requests - fall back to
        # "unresolved" for this one person and keep going.
        try:
            if c is not None:
                member_id = c["id"]
            elif n is not None:
                member_id = n["member_id"]
            else:
                member_id = resolve_member_id(display_name)
            email = fetch_parliamentary_email(member_id) if member_id is not None else None
        except requests.exceptions.RequestException as e:
            print(f"    (network error looking up {display_name}, leaving unresolved: {e})")
            member_id = None
            email = None
        if member_id is None or email is None:
            unresolved.append(display_name)
        time.sleep(0.05)  # spread out ~650 sequential requests to avoid connection resets

        records.append({
            "name": display_name,
            "house": "Commons" if c is not None else "Lords",
            "party": (c["party"] if c else None) or (a["party"] if a else None) or (n["party"] if n else None),
            "constituency": (c["constituency"] if c else None) or (a["constituency"] if a else None) or (n["constituency"] if n else None),
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
