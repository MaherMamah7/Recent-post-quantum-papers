#!/usr/bin/env python3
"""
Harvest recent code-based and lattice-based cryptography papers from
arXiv (Atom API) and the IACR Cryptology ePrint Archive (OAI-PMH), and
write them into the flat text files consumed by index.html.

Output format (one paper per line), mirroring the isogeni.es layout but
with a trailing category field:

    title;;;authors;;;id;;;date_class;;;repo;;;date;;;categories

  - title       paper title (single line)
  - authors     comma-separated author names
  - id          arXiv id (e.g. 2501.01234) or ePrint id (e.g. 2026/0421)
  - date_class  one of: last_week, last_month, last_year
  - repo        source-code URL if we could find one, else "none"
  - date        YYYY-MM-DD (announced / submitted date)
  - categories  comma-separated subset of {lattice, code}

Run:  python scrape.py            # writes papers_arxiv.txt / papers_eprint.txt
      python scrape.py --days 400 # change the lookback window
"""

import argparse
import datetime as dt
import html
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ARXIV_API = "https://export.arxiv.org/api/query"
EPRINT_OAI = "https://eprint.iacr.org/oai"

# arXiv subject classes where PQC work actually appears.
ARXIV_CATEGORIES = ["cs.CR", "cs.IT", "math.NT", "math.CO", "quant-ph"]

USER_AGENT = "pqc-papers/1.0 (recent code-based & lattice-based crypto index)"

# How far back to look, in days. Anything older than ~1 year is dropped
# because the front-end only has week/month/year buckets.
DEFAULT_LOOKBACK_DAYS = 400

# Rolling-window thresholds (in days) used to assign a date_class.
WEEK_DAYS = 7
MONTH_DAYS = 30
YEAR_DAYS = 365

# --------------------------------------------------------------------------
# Keyword / topic matching
# --------------------------------------------------------------------------
#
# Each entry is a compiled regular expression. A paper is tagged with a
# category if ANY of that category's patterns match its title or abstract.
# Patterns are intentionally specific to crypto so that, e.g., "lattice"
# in a physics paper or "LDPC" in a channel-coding paper is not swept in.

def _ci(*words):
    """Case-insensitive patterns: safe for descriptive multi-word phrases."""
    return [re.compile(w, re.IGNORECASE) for w in words]


def _cs(*words):
    """Case-sensitive patterns: for acronyms / scheme names that would
    otherwise collide with ordinary words (e.g. LESS vs. 'less',
    BIKE vs. 'bike', Saber vs. 'saber')."""
    return [re.compile(w) for w in words]


# Descriptive phrases are distinctive enough to match case-insensitively.
LATTICE_CI = _ci(
    r"lattice[- ]based crypt",
    r"learning with errors",
    r"ring[- ]?lwe",
    r"module[- ]?lwe",
    r"short integer solution",
    r"ring[- ]?sis",
    r"module[- ]?sis",
    r"lattice reduction",
    r"shortest vector problem",
    r"closest vector problem",
    r"lattice trapdoor",
    r"lattice isomorphism",
    r"lattice sieving",
    r"fully homomorphic encryption",
    r"gadget decomposition",
)

# Acronyms / scheme names: match the canonical capitalisation only.
LATTICE_CS = _cs(
    r"\bLWE\b",
    r"\bR-?LWE\b",
    r"\bM-?LWE\b",
    r"\bNTRU\b",
    r"\bKyber\b",
    r"\bML-?KEM\b",
    r"\bDilithium\b",
    r"\bML-?DSA\b",
    r"\bFalcon\b",
    r"\bFrodoKEM\b",
    r"\bFrodo\b",
    r"\bSaber\b",
    r"\bHawk\b",
    r"\bHAWK\b",
    r"\bBKZ\b",
    r"\bLLL\b",
    r"\bSVP\b",
    r"\bCVP\b",
    r"\bFHE\b",
    r"\bTFHE\b",
    r"\bCKKS\b",
    r"\bBGV\b",
    r"\bBFV\b",
)

CODE_CI = _ci(
    r"code[- ]based crypt",
    r"mceliece",
    r"niederreiter",
    r"goppa code",
    r"syndrome decoding",
    r"information set decoding",
    r"rank[- ]metric",
    r"rank syndrome decoding",
    r"regular syndrome decoding",
    r"restricted syndrome decoding",
    r"lee metric code",
    r"decoding (random )?linear codes",
    r"wave signature",
)

CODE_CS = _cs(
    r"\bQC-?MDPC\b",
    r"\bQC-?LDPC\b",
    r"\bMDPC\b",
    r"\bBIKE\b",
    r"\bHQC\b",
    r"\bRQC\b",
    r"\bROLLO\b",
    r"\bLESS\b",
)

GITHUB_RE = re.compile(r"https?://(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org)/[^\s)\]\}<>\"']+",
                       re.IGNORECASE)


def _matches(text, ci_patterns, cs_patterns):
    return (any(p.search(text) for p in ci_patterns) or
            any(p.search(text) for p in cs_patterns))


def classify(text):
    """Return the set of categories ({'lattice','code'}) matched by text."""
    cats = set()
    if _matches(text, LATTICE_CI, LATTICE_CS):
        cats.add("lattice")
    if _matches(text, CODE_CI, CODE_CS):
        cats.add("code")
    return cats


def find_repo(text):
    m = GITHUB_RE.search(text or "")
    if not m:
        return "none"
    # strip common trailing punctuation
    return m.group(0).rstrip(".,);]")


def date_class_for(date, now):
    age = (now - date).days
    if age < 0:
        age = 0
    if age <= WEEK_DAYS:
        return "last_week"
    if age <= MONTH_DAYS:
        return "last_month"
    if age <= YEAR_DAYS:
        return "last_year"
    return None


def clean(text):
    """Collapse whitespace and strip; remove the ;;; delimiter if present."""
    if not text:
        return ""
    text = html.unescape(text)
    text = text.replace(";;;", "; ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------

def http_get(url, retries=4):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 - network flakiness, retry
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}\n{last_err}")


# --------------------------------------------------------------------------
# arXiv
# --------------------------------------------------------------------------

ATOM_NS = {"a": "http://www.w3.org/2005/Atom",
           "arxiv": "http://arxiv.org/schemas/atom"}


def arxiv_query_string():
    """Build the OR-joined keyword query, scoped to relevant categories."""
    # Strong, phrase-level terms arXiv can match server-side. We keep this
    # list a touch broader than the regexes; the regexes re-filter afterwards.
    terms = [
        "lattice-based cryptography", "learning with errors", "ring-LWE",
        "module-LWE", "short integer solution", "NTRU", "Kyber", "Dilithium",
        "Falcon", "FrodoKEM", "lattice reduction", "fully homomorphic encryption",
        "lattice isomorphism", "lattice sieving", "shortest vector problem",
        "code-based cryptography", "McEliece", "Niederreiter", "Goppa code",
        "syndrome decoding", "information set decoding", "QC-MDPC", "BIKE",
        "HQC", "rank metric code", "decoding random linear codes",
    ]
    abs_q = " OR ".join(f'abs:"{t}"' for t in terms)
    cat_q = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
    return f"({cat_q}) AND ({abs_q})"


def fetch_arxiv(now, lookback_days):
    cutoff = now - dt.timedelta(days=lookback_days)
    search = arxiv_query_string()
    papers = {}
    start = 0
    page = 100
    while True:
        params = {
            "search_query": search,
            "start": start,
            "max_results": page,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = ARXIV_API + "?" + urllib.parse.urlencode(params)
        xml = http_get(url)
        root = ET.fromstring(xml)
        entries = root.findall("a:entry", ATOM_NS)
        if not entries:
            break
        oldest_on_page = None
        for e in entries:
            pub = e.findtext("a:published", default="", namespaces=ATOM_NS)
            try:
                pdate = dt.datetime.strptime(pub[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            oldest_on_page = pdate if oldest_on_page is None else min(oldest_on_page, pdate)

            raw_id = e.findtext("a:id", default="", namespaces=ATOM_NS)
            # e.g. http://arxiv.org/abs/2501.01234v2  ->  2501.01234
            m = re.search(r"abs/([^v]+?)(v\d+)?$", raw_id)
            if not m:
                continue
            aid = m.group(1)

            title = clean(e.findtext("a:title", default="", namespaces=ATOM_NS))
            summary = clean(e.findtext("a:summary", default="", namespaces=ATOM_NS))
            comment = clean(e.findtext("arxiv:comment", default="", namespaces=ATOM_NS))

            cats = classify(title + " " + summary)
            if not cats:
                continue

            repo = find_repo(summary + " " + comment)
            authors = [clean(a.findtext("a:name", default="", namespaces=ATOM_NS))
                       for a in e.findall("a:author", ATOM_NS)]

            papers[aid] = {
                "title": title,
                "authors": [a for a in authors if a],
                "id": aid,
                "repo": repo,
                "date": pdate,
                "cats": cats,
            }

        start += page
        if oldest_on_page is not None and oldest_on_page < cutoff:
            break
        if start > 5000:  # safety valve
            break
        time.sleep(3)  # be polite to the arXiv API
    return list(papers.values())


# --------------------------------------------------------------------------
# IACR ePrint (OAI-PMH)
# --------------------------------------------------------------------------

OAI_NS = {"oai": "http://www.openarchives.org/OAI/2.0/",
          "dc": "http://purl.org/dc/elements/1.1/",
          "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/"}


def fetch_eprint(now, lookback_days):
    cutoff = now - dt.timedelta(days=lookback_days)
    base = {
        "verb": "ListRecords",
        "metadataPrefix": "oai_dc",
        "from": cutoff.isoformat(),
    }
    url = EPRINT_OAI + "?" + urllib.parse.urlencode(base)
    papers = {}
    while True:
        xml = http_get(url)
        root = ET.fromstring(xml)
        for rec in root.findall(".//oai:record", OAI_NS):
            header = rec.find("oai:header", OAI_NS)
            if header is not None and header.get("status") == "deleted":
                continue
            md = rec.find(".//oai_dc:dc", OAI_NS)
            if md is None:
                continue

            identifier = ""
            for ident in md.findall("dc:identifier", OAI_NS):
                if ident.text and "eprint.iacr.org" in ident.text:
                    identifier = ident.text
                    break
            m = re.search(r"(\d{4}/\d+)", identifier)
            if not m:
                continue
            eid = m.group(1)

            title = clean(md.findtext("dc:title", default="", namespaces=OAI_NS))
            abstract = clean(md.findtext("dc:description", default="", namespaces=OAI_NS))
            authors = [clean(c.text) for c in md.findall("dc:creator", OAI_NS) if c.text]

            dates = [d.text for d in md.findall("dc:date", OAI_NS) if d.text]
            pdate = None
            for d in dates:
                try:
                    pdate = dt.datetime.strptime(d[:10], "%Y-%m-%d").date()
                    break
                except ValueError:
                    continue
            if pdate is None:
                continue

            cats = classify(title + " " + abstract)
            if not cats:
                continue

            repo = find_repo(abstract)
            papers[eid] = {
                "title": title,
                "authors": authors,
                "id": eid,
                "repo": repo,
                "date": pdate,
                "cats": cats,
            }

        token_el = root.find(".//oai:resumptionToken", OAI_NS)
        token = token_el.text if token_el is not None else None
        if not token:
            break
        url = EPRINT_OAI + "?" + urllib.parse.urlencode(
            {"verb": "ListRecords", "resumptionToken": token})
        time.sleep(1)
    return list(papers.values())


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def to_line(p, now):
    dclass = date_class_for(p["date"], now)
    if dclass is None:
        return None
    fields = [
        p["title"],
        ", ".join(p["authors"]),
        p["id"],
        dclass,
        p["repo"],
        p["date"].isoformat(),
        ",".join(sorted(p["cats"])),
    ]
    return ";;;".join(fields)


def write_file(path, papers, now):
    papers = sorted(papers, key=lambda p: p["date"], reverse=True)
    lines = []
    for p in papers:
        line = to_line(p, now)
        if line:
            lines.append(line)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")
    return len(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help="lookback window in days (default: %(default)s)")
    ap.add_argument("--arxiv-out", default="papers_arxiv.txt")
    ap.add_argument("--eprint-out", default="papers_eprint.txt")
    ap.add_argument("--skip-arxiv", action="store_true")
    ap.add_argument("--skip-eprint", action="store_true")
    args = ap.parse_args()

    now = dt.date.today()

    if not args.eprint_out and not args.arxiv_out:
        print("nothing to do", file=sys.stderr)
        return

    if not args.skip_eprint:
        print("Harvesting IACR ePrint (OAI-PMH)...", file=sys.stderr)
        eprint = fetch_eprint(now, args.days)
        n = write_file(args.eprint_out, eprint, now)
        print(f"  wrote {n} ePrint papers -> {args.eprint_out}", file=sys.stderr)

    if not args.skip_arxiv:
        print("Harvesting arXiv (Atom API)...", file=sys.stderr)
        arxiv = fetch_arxiv(now, args.days)
        n = write_file(args.arxiv_out, arxiv, now)
        print(f"  wrote {n} arXiv papers -> {args.arxiv_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
