# scrapbot

Company / lead data scraper for NextUp Recruitment. Give it a list of company
domains (or a directory page to harvest them from) and it returns structured
leads — company name, emails, phones, location, socials, industry hints and
whether the site shows signs of hiring — as JSON + CSV, with a local dashboard
for review.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

# Only needed for JavaScript-rendered sites. Both packages are required:
# headless runs use chrome-headless-shell, which `install chromium` alone omits.
playwright install chromium chromium-headless-shell
```

Optional, for the test suite: `pip install -e ".[dev]"` then `pytest`.

## Use

```powershell
# 1. put your targets in seeds/domains.txt (one domain per line), then:
scrapbot run website --seeds seeds/domains.txt

# quick one-off, nothing written to disk
scrapbot run website --domains acme.com.au globex.com.au --dry-run

# harvest companies off a directory / member-list page, then crawl each one
scrapbot run directory --listing https://example-chamber.org/members --limit 25

# schools: division, conference, cost, academics — from official sources
scrapbot run schools                                  # NCAA + NAIA + NJCAA
scrapbot run schools --association njcaa              # junior colleges only
scrapbot seeds --out seeds/schools.txt --division I   # -> athletics hosts

# people, not companies: athletics staff directories, one record per person
scrapbot run coaches --seeds seeds/schools.txt
scrapbot run coaches --sites goduke.com --coaches-only

# see what you have
scrapbot stats
scrapbot stats --contacts
scrapbot export --out hot-leads.csv --has-email --industry construction
scrapbot export --contacts --coaches-only --sport basketball --out coaches.csv

# browse and filter in the browser — tabs for Coaches / Schools / Companies
scrapbot dashboard        # http://127.0.0.1:8000
```

`scrapbot sources` lists the sources; `scrapbot run <source> --help` shows the
flags for one.

## Output

| Path | What it is |
| --- | --- |
| `data/leads.json` | Merged, de-duplicated store — one record per domain, the source of truth |
| `data/leads.csv` | The same store as a spreadsheet |
| `data/contacts.json` | People store — one record per person, written by the `coaches` source |
| `data/contacts.csv` | The same store as a spreadsheet |
| `data/schools.json` | Institution store, written by the `schools` source |
| `data/schools.csv` | The same store as a spreadsheet |
| `data/runs/<run-id>/` | Immutable snapshot of a single run: `leads.json`, `leads.csv`, `meta.json` |

Re-running a domain **updates** its record rather than duplicating it: scalars
take the newest non-empty value, while emails, phones, socials and industry
hints are unioned, so nothing found on an earlier run is lost. `first_seen` and
`last_seen` track the record's history. Contacts merge the same way, keyed on
the staff profile URL, so a person listed under two departments is one record
carrying both.


```powershell
scrapbot run coaches --seeds data/runs/<run-id>/failed.txt
```

