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

Some directories assemble the address in a per-row `<script>` (`firstHalf` +
`secondHalf`) so it never appears in the HTML as text. Those are reassembled
from the script — no browser needed. Without it, aqsaints.com yields 86 people
and zero emails.

### Athletics directory vs. campus directory

A university's `/faculty-staff/` page looks exactly like a staff directory from
the outside. Discovery therefore judges a candidate by what it **parses to**,
not by its URL: a page counts as athletics only if at least 10% of its people
are attributed to a sport. Real athletics directories score 46–60%; campus
directories score 0%, because they have no such column.

Coach *ratio* cannot make this call — Duke is 24.5% coaches and Andrew College's
combined faculty list is 25.9%. Only sport separates them.

If nothing athletic is found, the campus directory is used as a last resort but
**only for the people whose titles say they coach**. Andrew College publishes 54
people, 14 of them coaches, and has no athletics site: keeping all 54 would file
professors as athletics staff, keeping none would lose 14 real coaches.

Without this, aquinas.edu matched its own faculty page and contributed 151
professors, never reaching aqsaints.com.

### Headshots

`photo_url` is always recorded when a directory publishes a real headshot; stock
silhouettes, logos and spacers are skipped. The dashboard shows it inline.

Pass `--save-photos` to download the files into `data/photos/<domain>/`, with
`photo_file` holding the relative path. It is off by default because it roughly
doubles the requests per site. Downloads are sequential per site, under the same
robots and crawl-delay rules as pages.

`shared_email` marks an address that several people at the same school list —
an executive assistant, or a program inbox like `volleyball@jsu.edu`. Duke
publishes its head basketball coach's assistant this way, so the address is
correct but does not reach that person directly. It is flagged rather than
dropped, because for many programs it is the only route in; use
`--direct-email` to exclude them.

## Sites that refuse automated requests

Some athletics platforms (PrestoSports behind CloudFront) answer **403 to any
client that identifies itself as a crawler**, even where robots.txt allows the
page. That is a decision the operator made, and scrapbot does not work around
it: it does not spoof a browser User-Agent, and `--render always` keeps sending
the project's real UA rather than Chromium's.

Measured on this project: driving those hosts through real Chromium still
returned 403 on 5 of 5, because the identification is what triggers the block.

Two things do help.

**The college's own site.** A blocked athletics host falls back to the
university, which usually publishes the coaches among its staff. That is how
Cisco College's 15 coaches arrive, via `cisco.edu`, after `wranglersports.net`
refuses us.

**Pages you save yourself.** The page is public and a person can open it, so
open it, save it (`Ctrl+S`, "Webpage, HTML Only"), and let scrapbot parse the
file:

    .\scripts\manual-folders.ps1 -Top 25          # make a folder per blocked host
    # ...save each staff-directory page into its folder as .html...
    scrapbot run coaches --manual-dir data\manual

Layout is `data/manual/<athletics-domain>/<anything>.html` — the folder name
says which school the page belongs to, so no extra flags are needed. Parsing is
identical to a live scrape (same columns, sports, headshots, script-obfuscated
emails) and costs **zero requests**. Saved pages and live sites can run in the
same command.

## Knowing what failed

A site that blocks bots and a site with no staff directory both yield zero
people. Without a reason they look identical, and a run that quietly returned
nothing looks like a run that found nothing. So every run reports per site:

```
  sites attempted : 4
    succeeded     : 1 (25%)
    failed        : 3
      blocked the bot (HTTP 403/429)         1
        - naiastats.prestosports.com: server refused automated requests (HTTP 403)
      no staff directory found               1
        - london.edu: site reachable but no staff directory found in 9 request(s)
      network failure / timeout              1
        - dead-domain.com: ConnectError: [Errno 11001] getaddrinfo failed
```

| Status | Meaning | Worth retrying |
| --- | --- | --- |
| `ok` | Scraped, people found | — |
| `empty` | Directory fetched but no rows recognised | No — parser gap, tell me |
| `blocked` | Server refused the client (401/403/405/406/429/451) | **Yes** |
| `robots` | robots.txt disallows the pages needed | No — a decision, not a glitch |
| `network` | Timeouts, DNS failures, nothing fetched | **Yes** |
| `no_directory` | Site reachable, genuinely has no staff directory | No |
| `error` | Unhandled crash on that site | **Yes** |

Each run directory gets three files beside the data:

| File | What it is |
| --- | --- |
| `sites.json` | Every site with status, reason, URL and people count |
| `succeeded.txt` | The sites that worked |
| `failed.txt` | The ones that didn't, with reasons — **in seed-file format** |

So retrying a bad network day is one command:

```powershell
scrapbot run coaches --seeds data/runs/<run-id>/failed.txt
```

The same report is the dashboard's **Runs** tab: pick a run (or "All runs"),
filter by outcome, and every failure shows its reason. Colour follows severity —
green for scraped, red for blocked, amber for network.

Retryable failures carry a **retry button**, and there is a **Retry all failed**
button beside the filters. Clicking one starts a background scrape; the page
polls it and refreshes when it finishes, writing a new run report as usual.

Two limits are deliberate:

- **One retry at a time.** Two concurrent retries of the same host would fight
  each other's rate limiting, and a retry should be gentler, not more
  aggressive. A second request gets `409`.
- **Only domains already in a run report, with a retryable outcome.** The
  dashboard cannot be used to point the scraper at an arbitrary host — which
  matters the moment it is bound to anything but localhost.

DNS failures are not retried within a run: a name that doesn't resolve won't
resolve a second later, and a list of a few hundred schools usually holds a few
dead domains. Timeouts and 5xx still get the normal backoff.

## Schools

The `schools` source builds institution records in the origin database's shape.
It makes **no** web requests — every field has an official machine-readable
source, so scraping one would be slower and less accurate:

| Field | Source |
| --- | --- |
| `school`, `division`, `conference` | NCAA member directory (public JSON, no key) / NAIA member-institutions PDF |
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

### NAIA

`--association naia` adds the 233 NAIA member institutions. `division` is set
to `NAIA`, which is accurate — the NAIA dropped divisions in 2020, so the
association is the tier.

The NAIA publishes no member API. Its own school finder is a third-party SPA
with neither robots.txt nor sitemap, and Wikipedia's transcription is
second-hand and rate-limits bots. So the source is the NAIA's own
**member-institutions PDF**, on a host whose robots.txt allows everything:

    https://www.naia.org/wp-content/uploads/2026/07/2026-2027_NAIA_Institutions.pdf

That path carries the year it was posted, so it changes each season — pass
`--naia-pdf URL` when it moves. The parser checks its row count against the
"Total Schools (N)" line in the PDF itself and warns on a mismatch, so a
changed layout surfaces instead of silently dropping schools.

Two NAIA members are Canadian (British Columbia), so `state` and `region`
handle provinces; their region reads `Canada`, not a US Census division.

Several NAIA names repeat across states — two Bethel Universities, two Columbia
Colleges, two Universities of Saint Francis. NCAA schools de-duplicate on the
NCAA's org id, but NAIA records have none, so their store key is name + state.

### NJCAA (junior colleges)

`--association njcaa` adds the 489 NJCAA member junior colleges — the two-year
pathway, and a significant recruiting route.

`njcaa.org` returns **403 to any non-browser client** (it sits behind bot
protection, robots.txt notwithstanding), so its own member directory is not
available. The source is instead Wikipedia's three per-division articles, which
are CC BY-SA and reachable through the MediaWiki API. Wikimedia rejects clients
whose User-Agent carries no contact address, so the API client sends the
project's — keep a real contact in `SCRAPBOT_USER_AGENT`.

**Every NJCAA college is stored as one division: `NJCAA`.** The three articles
are still read separately, but the tier is dropped on the way in.

That is not a simplification, it is the accurate shape. An NJCAA tier is a
*per-sport* designation — a college plays DI basketball and DII baseball — so it
is a property of a team, not of the institution. Keeping it produced 29 colleges
stored as `"NJCAA DI; NJCAA DII"`, a value no division filter ever matched, and
made a school's division depend on which list happened to be read last.

A bare `DI` still means NCAA Division I; `normalize_division` accepts any of
`njcaa`, `njcaa 1`, `NJCAA DII` and returns `NJCAA`.

### Junior colleges outside the NJCAA

Not every two-year college is an NJCAA member. California's **CCCAA** (97
colleges) and the Pacific Northwest's **NWAC** (32) run their own championships
and eligibility rules. A supplied list calls all three "Junior College", so the
importer reads the conference column to tell them apart, and they are stored as
their own division and association:

    DI 404 · DII 334 · DIII 483 · NAIA 284 · NJCAA 601 · CCCAA 97 · NWAC 32

`association` is part of the store key, so a CCCAA college and an NJCAA college
of the same name in the same state stay separate records.

### Importing a supplied list

The official sources say who exists but not always where their athletics site
is: the NCAA feed publishes both hosts, the NAIA PDF and the NJCAA articles
publish neither. That left 726 schools the bot had to enter through the
university homepage — the case it gets wrong most often.

    scrapbot import-schools Mens_Basketball_Schools_List.xlsx --add-new

Matching is on normalised name **plus** state, never name alone: the store holds
two Bethel Universities and two Columbia Colleges, and pairing one with the
other's athletics site is worse than leaving both blank.

Three rules decide what happens on a disagreement:

- nothing on record → take the list's host
- on record, but it is the university's own domain (`beloit.edu`) → take the
  list's dedicated host (`beloitcollegeathletics.com`), which cannot wander into
  the faculty directory
- on record as a different dedicated host (`sundevils.com` vs
  `thesundevils.com`) → keep the official one and print the disagreement

`--add-new` also creates schools the list has and the store does not. Off by
default, because the official directories decide membership.

### Identity across associations

`association` (NCAA / NAIA / NJCAA) is part of a school's store key. Cottey
College and Marian University each appear in **both** the NAIA and the NJCAA
lists as different institutions; keyed on name and state alone they overwrote
one another and the NAIA count silently fell from 233 to 231.

Keys, in order of preference: NCAA org id → name + state + association. `association` is backfilled from `division` when an older record
lacks it, so existing rows keep their key rather than splitting into duplicates.

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
- **NAIA schools carry no athletics URL.** The NCAA directory supplies one per
  member, so `scrapbot seeds` can chain straight into `coaches`. The NAIA PDF
  does not, so NAIA seeds fall back to the university host from Scorecard —
  which means NAIA needs the Scorecard key before `seeds` can emit them.

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
