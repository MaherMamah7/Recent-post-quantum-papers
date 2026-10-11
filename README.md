# recent post-quantum papers — code-based & lattice-based cryptography

A tiny static site, in the spirit of [isogeni.es](https://isogeni.es/), that lists
recent papers in **code-based** and **lattice-based** cryptography. It pulls
metadata from two sources and buckets papers into *this week / this month / this year*.

Maintained by Maher Mamah.

- **arXiv** — via the public [Atom API](https://info.arxiv.org/help/api/index.html)
  (categories `cs.CR`, `cs.IT`, `math.NT`, `math.CO`, `quant-ph`).
- **IACR Cryptology ePrint Archive** — via its
  [OAI-PMH](https://www.openarchives.org/pmh/) endpoint (metadata is CC0).

## How it works

```
scraper/scrape.py  ──>  papers_arxiv.txt
                   ──>  papers_eprint.txt
index.html  ──(fetch + render in the browser)──>  the page
```

The scraper classifies each paper by keyword matching over its title and abstract
(see `LATTICE_*` / `CODE_*` in `scraper/scrape.py`) and emits one line per paper:

```
title;;;authors;;;id;;;date_class;;;repo;;;date;;;categories
```

| field        | meaning                                             |
|--------------|-----------------------------------------------------|
| `title`      | paper title                                         |
| `authors`    | comma-separated author names                        |
| `id`         | arXiv id (`2501.01234`) or ePrint id (`2026/0421`)  |
| `date_class` | `last_week` / `last_month` / `last_year`            |
| `repo`       | a github/gitlab/bitbucket URL if found, else `none` |
| `date`       | `YYYY-MM-DD`                                         |
| `categories` | subset of `lattice`, `code` (comma-separated)       |

The page has two filter rows: **topic** (lattice / code / both) and
**source** (ePrint / arXiv / both).

## Running the scraper

No third-party dependencies — standard library only (Python 3.8+).

```bash
python3 scraper/scrape.py            # 400-day lookback (default)
python3 scraper/scrape.py --days 180 # shorter window
python3 scraper/scrape.py --skip-arxiv   # only refresh ePrint
```

## Viewing locally

```bash
python3 -m http.server 8000
# open http://localhost:8000
```

(A plain `file://` open will not work because the page fetches the `.txt`
files, which browsers block over `file://`.)

## Tuning what gets included

Precision/recall is governed entirely by the keyword lists in
`scraper/scrape.py`:

- `LATTICE_CI` / `CODE_CI` — case-insensitive descriptive phrases
  (e.g. `learning with errors`, `syndrome decoding`).
- `LATTICE_CS` / `CODE_CS` — **case-sensitive** acronyms and scheme names
  (e.g. `LWE`, `BIKE`, `LESS`). These are case-sensitive on purpose so they
  don't collide with ordinary words like *less* or *bike*.

Add or remove patterns there to widen or tighten coverage.

## Automatic updates

`.github/workflows/update.yml` re-runs the scraper every 6 hours and commits
the refreshed `.txt` files. Enable GitHub Pages (serving from the repo root) to
publish the site. You can also trigger a refresh on demand from the repo's
**Actions** tab (the workflow has `workflow_dispatch` enabled).
