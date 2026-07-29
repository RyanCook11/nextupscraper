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

# schools: division, conference, cost, academics — from official APIs
scrapbot run schools --division I
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

## People vs. companies

Most sources produce one `Lead` per company. The `coaches` source produces one
`Contact` per **person** — name, title, sport, work email, work phone, profile
URL — and writes to its own store, because folding a 400-person staff directory
into a single company record would just yield 400 unrelated email addresses in
one field.

It reads a staff directory row by row, so a person's email can only come from
that person's own row. Two layouts are handled, covering most of the platforms
athletics departments run on (Sidearm, PrestoSports, WMT):

- **table view** — one table per sport/department, a header cell naming the
  group, then Name / Title / Email / Phone columns
- **card view** — repeated per-person blocks, each with its own `mailto:`/`tel:`

`is_coach` marks genuine coaching titles; "Executive Assistant to the Head
Coach" and "Director of Athletics" are kept but not marked. Filter at export
time with `--coaches-only` and `--sport`.

`shared_email` marks an address that several people at the same school list —
an executive assistant, or a program inbox like `volleyball@jsu.edu`. Duke
publishes its head basketball coach's assistant this way, so the address is
correct but does not reach that person directly. It is flagged rather than
dropped, because for many programs it is the only route in; use
`--direct-email` to exclude them.

## Schools

The `schools` source builds institution records in the origin database's shape.
It makes **no** web requests — every field has an official machine-readable
source, so scraping one would be slower and less accurate:

| Field | Source |
| --- | --- |
| `school`, `division`, `conference` | NCAA member directory (public JSON, no key) |
| `city`, `state`, `totalYearlyCost`, `privatePublic` | College Scorecard (US Dept. of Education) |
| `academicData.SATMath`, `.ACTComposite`, `.SATReady` | College Scorecard admissions percentiles |
| `region` | Derived from the state — US Census region + division, no lookup |
| `academicData.averageGPA` | **Nothing fills this.** See below. |
| `id`, `logo` | Never set — you assign these |

`scrapbot export --schools --out schools.json` writes exactly the origin schema
and nothing else. The store keeps a few extra fields (`athletics_domain`,
`ncaa_org_id`, timestamps) for its own bookkeeping; they never reach the export.

Scorecard needs a free key for anything beyond a handful of schools — get one
at <https://api.data.gov/signup/> and set `SCRAPBOT_SCORECARD_KEY`. Without it
the shared `DEMO_KEY` allows roughly 30 requests an hour. Use `--no-academics`
to skip Scorecard entirely and take the NCAA fields alone.

### Known gaps

- **`averageGPA` is never filled.** No free authoritative dataset publishes
  average admitted GPA; the sites that do are republishing survey data under
  terms that forbid scraping. Keep whatever you already hold — `merge()` will
  not overwrite a stored value with an empty one.
- **`conference` is the NCAA's parent conference.** The NCAA reports
  "Middle Atlantic Conferences" where the origin database has the more specific
  "Middle Atlantic Freedom". Sub-conference splits are not in the NCAA feed.
- **`SATReady` is a guess.** It is currently the SAT evidence-based
  reading/writing percentile range, which does not reproduce the origin
  database's numbers — tell me what that field means and I'll correct it.
- **Cost and score vintages differ.** Scorecard's "latest" is a fixed reporting
  year, so its figures will not match numbers gathered elsewhere in a different
  year. Treat this source as a refresh, not a reconciliation.

## The two stores together

`School.athletics_domain` is `Contact.school_domain` — the NCAA directory
supplies each school's athletics host, which is exactly what the `coaches`
source scrapes. So the whole pipeline chains:

```powershell
scrapbot run schools --division I                     # 367 schools
scrapbot seeds --out seeds/schools.txt --division I   # 365 athletics hosts
scrapbot run coaches --seeds seeds/schools.txt --coaches-only
```

You never have to assemble a link list by hand.

It also works in reverse: hand `coaches` a **university** host and it resolves
to the athletics host through the school store, with no requests wasted —
`https://www.jsu.edu/` becomes `jaxstatesports.com`. If the school isn't in the
store yet, it falls back to following an "Athletics" link off the university
homepage (one hop, ignoring social and ticketing hosts).

## Tuning

Every flag below also works as an environment variable (`SCRAPBOT_DELAY=3`).

| Flag | Default | Effect |
| --- | --- | --- |
| `--delay` | `1.5` | Minimum seconds between two hits on the same host |
| `--concurrency` | `4` | How many different sites are crawled at once |
| `--max-pages` | `6` | Page budget per site (homepage + best contact/about/careers links) |
| `--render` | `auto` | `never` = HTTP only, `auto` = browser only when the static HTML looks empty, `always` = browser every time |
| `--headful` | off | Show the browser window — useful for debugging a stubborn site |
| `--data-dir` | `./data` | Where to read/write |

`auto` rendering is the right default: it's fast on ordinary sites and still
handles React/Vue marketing sites that ship an empty `<div id="root">`.

## Adding a source

A source turns CLI arguments into a stream of `Lead` objects and gets a
ready-made `Fetcher` (robots.txt, throttling and rendering already handled):

```python
# scrapbot/sources/mysource.py
class MySource(Source):
    name = "mysource"
    help = "What it does."

    @classmethod
    def add_arguments(cls, parser): parser.add_argument("--query", required=True)

    async def run(self, fetcher):
        page = await fetcher.get(f"https://example.com/search?q={self.args.query}")
        ...
        yield Lead(domain="acme.com", company_name="Acme", source=self.name)
```

Register it in `scrapbot/sources/__init__.py` and it appears in the CLI.

## Scope and conduct

- `robots.txt` is respected by default, one host is never hit more than once
  per `--delay` seconds, and the User-Agent identifies the bot. `--ignore-robots`
  exists for sites you own or have written permission to crawl — nothing else.
- Aimed at **company** information published on public company websites. It
  does not target personal profiles. If you point it somewhere that returns
  personal data, that data is subject to privacy law (Australian Privacy Act /
  GDPR where applicable) — have a lawful basis before you collect it.
- The `coaches` source is the one exception, and it is deliberately narrow: it
  collects only the professional details an institution publishes about its own
  staff on its own staff directory — work name, title, work email, work phone.
  That is still personal data. Have a lawful basis, honour opt-out requests,
  and do not point it at student-athlete rosters.
- Many large directories and job boards forbid scraping in their terms of
  service. Check the terms of a target before adding it as a source; prefer an
  official API or data licence where one exists.
