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

