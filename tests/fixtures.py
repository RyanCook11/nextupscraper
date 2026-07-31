"""A tiny fake company site, served over HTTP for end-to-end tests."""

from __future__ import annotations

import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = """<!doctype html>
<html><head>
  <title>Acme Scaffolding | Home</title>
  <meta name="description" content="Acme Scaffolding provides commercial scaffolding and labour hire across Sydney." />
  <meta property="og:site_name" content="Acme Scaffolding" />
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"Organization","name":"Acme Scaffolding Pty Ltd",
   "address":{"@type":"PostalAddress","streetAddress":"12 Vale Rd","addressLocality":"Parramatta",
              "addressRegion":"NSW","postalCode":"2150","addressCountry":"AU"}}
  </script>
</head><body>
  <h1>Commercial scaffolding, done properly</h1>
  <p>We are a construction services business with 40 staff. Our fabrication team
     supports major civil works projects across NSW.</p>
  <nav>
    <a href="/contact-us">Contact us</a>
    <a href="/about">About</a>
    <a href="/careers">Careers</a>
    <a href="/brochure.pdf">Brochure</a>
    <a href="https://www.linkedin.com/company/acme-scaffolding">LinkedIn</a>
    <a href="https://facebook.com/acmescaffolding?ref=nav">Facebook</a>
  </nav>
</body></html>
"""

CONTACT = """<!doctype html>
<html><head><title>Contact us | Acme Scaffolding</title></head><body>
  <h1>Contact</h1>
  <address>12 Vale Rd, Parramatta NSW 2150</address>
  <p>Email <a href="mailto:info@acme-scaffolding.com.au">info@acme-scaffolding.com.au</a>
     or accounts@acme-scaffolding.com.au</p>
  <p>Phone <a href="tel:+61298765432">(02) 9876 5432</a> — mobile 0412 345 678</p>
  <img src="/logo@2x.png" alt="logo" />
</body></html>
"""

CAREERS = """<!doctype html>
<html><head><title>Careers | Acme Scaffolding</title></head><body>
  <h1>Careers</h1>
  <p>We are hiring! Current vacancies for riggers and scaffolders.</p>
  <a href="/apply">Apply now</a>
  <p>Send your CV to careers@acme-scaffolding.com.au</p>
</body></html>
"""

ABOUT = """<!doctype html>
<html><head><title>About us | Acme Scaffolding</title></head><body>
  <h1>About</h1><p>Founded 1998, family owned, servicing the construction sector.</p>
</body></html>
"""

MEMBERS = """<!doctype html>
<html><head><title>Member directory</title></head><body>
  <h1>Our members</h1>
  <ul>
    <li><a href="https://www.acme-scaffolding.com.au/">Acme Scaffolding</a></li>
    <li><a href="http://globex-freight.com.au/about">Globex Freight</a></li>
    <li><a href="https://facebook.com/ourchamber">Follow us</a></li>
    <li><a href="https://www.google.com/maps?q=us">Map</a></li>
    <li><a href="https://business.gov.au/grants">Grants</a></li>
    <li><a href="/about">About this directory</a></li>
  </ul>
  <a rel="next" href="/members?page=2">Next</a>
</body></html>
"""

# Modelled on the Sidearm table layout: one table per sport/department, the
# group name in the leading header cell, then Name / Title / Email / Phone.
STAFF_DIRECTORY = """<!doctype html>
<html><head><title>Staff Directory | State University Athletics</title>
  <meta property="og:site_name" content="State University Athletics" />
</head><body>
  <h1>Staff Directory</h1>
  <table>
    <thead><tr><th>Senior Administration</th><th>Name</th><th>Title</th>
      <th>Email</th><th>Phone</th></tr></thead>
    <tbody>
      <tr><td><a href="/staff-directory/dana-reyes/12">Dana Reyes</a></td>
          <td>Director of Athletics</td>
          <td><a href="mailto:dana.reyes@state.edu">dana.reyes@state.edu</a></td>
          <td><a href="tel:+12315550101">(231) 555-0101</a></td></tr>
    </tbody>
  </table>
  <table>
    <thead><tr><th>Men's Basketball</th><th>Name</th><th>Title</th>
      <th>Email</th><th>Phone</th></tr></thead>
    <tbody>
      <tr><td><a href="/staff-directory/chris-vance/34">Chris Vance</a></td>
          <td>Head Coach</td>
          <td><a href="mailto:chris.vance@state.edu">chris.vance@state.edu</a></td>
          <td><a href="tel:+12315550102">(231) 555-0102</a></td></tr>
      <tr><td><a href="/staff-directory/pat-oduya/35">Pat Oduya</a></td>
          <td>Assistant Coach</td>
          <td><a href="mailto:pat.oduya@state.edu">pat.oduya@state.edu</a></td>
          <td></td></tr>
      <tr><td><a href="/staff-directory/sam-webb/36">Sam Webb</a></td>
          <td>Equipment Manager</td>
          <td><a href="mailto:sam.webb@state.edu">sam.webb@state.edu</a></td>
          <td></td></tr>
    </tbody>
  </table>
  <table>
    <thead><tr><th>Track &amp; Field</th><th>Name</th><th>Title</th>
      <th>Email</th><th>Phone</th></tr></thead>
    <tbody>
      <!-- Same person as Senior Administration above: must merge, not duplicate. -->
      <tr><td><a href="/staff-directory/dana-reyes/12">Dana Reyes</a></td>
          <td>Director of Athletics</td>
          <td><a href="mailto:dana.reyes@state.edu">dana.reyes@state.edu</a></td>
          <td></td></tr>
    </tbody>
  </table>
</body></html>
"""

# One long table whose sections are single-cell rows rather than separate
# tables per sport. Kentucky's directory is built this way.
STAFF_SECTIONED = """<!doctype html>
<html><head><title>Staff Directory | Wildcat Athletics</title></head><body>
  <h1>Staff Directory</h1>
  <table>
    <thead><tr><th>Name</th><th>Title</th><th>Phone</th><th>Email</th></tr></thead>
    <tbody>
      <tr><td>Administration</td></tr>
      <tr><td><a href="/staff-directory/dale-nash/1">Dale Nash</a></td>
          <td>Athletics Director</td><td>(555) 333-0001</td>
          <td><a href="mailto:dale.nash@wildcat.edu">dale.nash@wildcat.edu</a></td></tr>
      <tr><td>Football</td></tr>
      <tr><td><a href="/staff-directory/rob-vance/2">Rob Vance</a></td>
          <td>Head Coach</td><td>(555) 333-0002</td>
          <td><a href="mailto:football@wildcat.edu">football@wildcat.edu</a></td></tr>
      <tr><td><a href="/staff-directory/kim-doyle/3">Kim Doyle</a></td>
          <td>Offensive Coordinator</td><td></td>
          <td><a href="mailto:football@wildcat.edu">football@wildcat.edu</a></td></tr>
      <tr><td>Volleyball</td></tr>
      <tr><td><a href="/staff-directory/ana-reyes/4">Ana Reyes</a></td>
          <td>Head Coach</td><td></td>
          <td><a href="mailto:ana.reyes@wildcat.edu">ana.reyes@wildcat.edu</a></td></tr>
    </tbody>
  </table>
</body></html>
"""

# Card layout, for sites that don't use tables at all.
STAFF_CARDS = """<!doctype html>
<html><head><title>Coaches | Cardinal Athletics</title></head><body>
  <h2>Women's Soccer</h2>
  <ul>
    <li class="staff-card"><h3>Robin Ellis</h3>
      <p>Head Coach</p>
      <a href="/staff-directory/robin-ellis/7">Bio</a>
      <a href="mailto:robin.ellis@cardinal.edu">Email</a>
      <a href="tel:+12315550103">(231) 555-0103</a></li>
    <li class="staff-card"><h3>Jamie Fox</h3>
      <p>Assistant Coach</p>
      <a href="mailto:jamie.fox@cardinal.edu">Email</a></li>
  </ul>
</body></html>
"""

# A <script> containing markup makes the parser nest the rest of the document
# underneath it. Deleting the subtree would take the whole page with it.
MALFORMED = """<!doctype html>
<html><head><title>Broken | Acme</title></head><body>
  <script>var tpl = "<div class='x'>";</script>
  <p>Phone: (02) 9876 5432</p>
  <p>Real visible copy that must survive the parse, repeated for length.
     Real visible copy that must survive the parse, repeated for length.</p>
  <pre><code>fib = 8 13 21 34 55</code></pre>
</body></html>
"""

# A university's general faculty/staff page: the shape that fooled the bot on
# aquinas.edu. No sport column, mostly professors, a couple of coaches buried in
# it. "Department" is deliberately a trailing unknown column — that combination
# used to map a named column to a negative cell index and raise IndexError.
FACULTY_DIRECTORY = """<!doctype html>
<html><head><title>Faculty &amp; Staff | Aquinas</title></head><body>
  <h1>Faculty &amp; Staff Directory</h1>
  <table>
    <tr><th>Name</th><th>Title</th><th>Department</th><th>Email</th></tr>
    <tr><td>Alan Reed</td><td>Professor of Biology</td><td>Biology</td>
        <td>alan.reed@aq.edu</td></tr>
    <tr><td>Bea Lowe</td><td>Registrar</td><td>Records</td>
        <td>bea.lowe@aq.edu</td></tr>
    <tr><td>Cal Ives</td><td>Associate Professor of History</td><td>History</td>
        <td>cal.ives@aq.edu</td></tr>
    <tr><td>Dana Poe</td><td>Director of Financial Aid</td><td>Financial Aid</td>
        <td>dana.poe@aq.edu</td></tr>
    <tr><td>Eve Marsh</td><td>Head Coach</td><td>Athletics</td>
        <td>eve.marsh@aq.edu</td></tr>
    <tr><td>Finn Doyle</td><td>Assistant Coach</td><td>Athletics</td></tr>
    <tr><td>Gus Hale</td><td>Librarian</td><td>Library</td>
        <td>gus.hale@aq.edu</td></tr>
    <tr><td>Hana Vue</td><td>Professor of Chemistry</td><td>Chemistry</td>
        <td>hana.vue@aq.edu</td></tr>
  </table>
</body></html>
"""

# The Sidearm layout, as served by aqsaints.com. Three things here broke the
# parser at once: sections marked with a one-cell <th> (which table.css("th")
# swept into the header list), a headerless-looking image column that must NOT
# shift the mapping, and emails assembled by a per-row <script>.
SIDEARM_DIRECTORY = """<!doctype html>
<html><head><title>Staff Directory - Aquinas College</title></head><body>
  <h1>Staff Directory</h1>
  <table>
    <thead>
      <tr><th>Image</th><th>Name</th><th>Title</th><th>Email Address</th><th>Phone</th></tr>
    </thead>
    <tbody>
      <tr><th>Adminstration</th></tr>
      <tr>
        <td><img src="/images/2025/8/25/Damon_Bouwkamp_headshot.jpeg" alt="Damon Bouwkamp"></td>
        <td>Damon Bouwkamp</td><td>Director of Intercollegiate Athletics (AD)</td>
        <td><a id="staff_email_0" href="#"></a>
          <script type="text/javascript">
            var placeholder = document.getElementById("staff_email_0");
            var firstHalf = "bouwkdam";
            var secondHalf = "aquinas.edu";
            placeholder.href = 'mailto:' + firstHalf + '@' + secondHalf;
          </script></td>
        <td></td>
      </tr>
      <tr><th>Men's Basketball</th></tr>
      <tr>
        <td><img src="/images/2024/9/12/Ryan_Bertoia.jpg" alt="Ryan Bertoia"></td>
        <td>Ryan Bertoia</td><td>Head Coach</td>
        <td><a id="staff_email_1" href="#"></a>
          <script type="text/javascript">
            var placeholder = document.getElementById("staff_email_1");
            var firstHalf = "rmb004";
            var secondHalf = "aquinas.edu";
            placeholder.href = 'mailto:' + firstHalf + '@' + secondHalf;
          </script></td>
        <td>(616) 632-2478</td>
      </tr>
      <tr>
        <td><img src="/images/placeholder-silhouette.png" alt="No photo available"></td>
        <td>Dana Poe</td><td>Assistant Coach</td><td></td><td></td>
      </tr>
    </tbody>
  </table>
</body></html>
"""

# Reports back what the page looks like from *inside* the browser. This is the
# same handful of signals a commercial bot-detector reads first, so rendering
# this page tells us whether the stealth launch flags actually took.
WHOAMI = """<!doctype html>
<html><head><title>Who am I</title></head><body>
  <h1>Fingerprint probe</h1>
  <pre id="out">pending</pre>
  <script>
    document.addEventListener('DOMContentLoaded', function () {
      var uaData = navigator.userAgentData;
      document.getElementById('out').textContent = JSON.stringify({
        webdriver: navigator.webdriver === true,
        ua: navigator.userAgent,
        languages: (navigator.languages || []).join(','),
        plugins: navigator.plugins.length,
        innerWidth: window.innerWidth,
        chromeObject: typeof window.chrome !== 'undefined',
        uaDataBrands: uaData ? uaData.brands.map(function (b) {
          return b.brand + '=' + b.version;
        }).join(',') : ''
      });
    });
  </script>
</body></html>
"""

# Sidearm's web-component staff directory, as served by georgiadogs.com and
# texaslonghorns.com. The two things that made every large athletics site read
# as "no staff directory found": there is no <table>/<tr> anywhere, and not a
# single mailto — the person's address lives on their profile page instead.
SIDEARM_PERSON_CARDS = """<!doctype html>
<html><head><title>Staff Directory - Bulldog Athletics</title></head><body>
  <h1>Staff Directory</h1>
  <h2>Administration</h2>
  <div data-test-id="s-person-card-list__root" class="s-person-card s-person-card--list">
    <div class="s-person-card__content">
      <div data-test-id="s-person-details__root" class="s-person-details">
        <div class="s-person-details__personal">
          <a data-test-id="s-person-details__personal-single-line"
             href="/staff-directory/josh-brooks/3">Josh Brooks</a>
        </div>
        <div class="s-person-details__position s-text-details">Director of Athletics</div>
      </div>
    </div>
  </div>
  <h2>Men's Basketball</h2>
  <div data-test-id="s-person-card-list__root" class="s-person-card s-person-card--list">
    <div class="s-person-card__content">
      <div data-test-id="s-person-details__root" class="s-person-details">
        <div class="s-person-details__personal">
          <a data-test-id="s-person-details__personal-single-line"
             href="/staff-directory/mike-white/1201">Mike White</a>
        </div>
        <div class="s-person-details__position s-text-details">Head Coach</div>
      </div>
    </div>
  </div>
  <div data-test-id="s-person-card-list__root" class="s-person-card s-person-card--list">
    <div class="s-person-card__content">
      <div data-test-id="s-person-details__root" class="s-person-details">
        <div class="s-person-details__personal">
          <a data-test-id="s-person-details__personal-single-line"
             href="/staff-directory/chad-dollar/1202">Chad Dollar</a>
        </div>
        <div class="s-person-details__position s-text-details">Assistant Coach</div>
      </div>
    </div>
  </div>
  <h2>Volleyball</h2>
  <div data-test-id="s-person-card-list__root" class="s-person-card s-person-card--list">
    <div class="s-person-card__content">
      <div data-test-id="s-person-details__root" class="s-person-details">
        <div class="s-person-details__personal">
          <a data-test-id="s-person-details__personal-single-line"
             href="/staff-directory/tom-black/1203">Tom Black</a>
        </div>
        <div class="s-person-details__position s-text-details">Head Coach</div>
      </div>
    </div>
  </div>
  <div data-test-id="s-person-card-list__root" class="s-person-card s-person-card--list">
    <div class="s-person-card__content">
      <div data-test-id="s-person-details__root" class="s-person-details">
        <div class="s-person-details__personal">
          <a data-test-id="s-person-details__personal-single-line"
             href="/staff-directory/kim-doyle/1204">Kim Doyle</a>
        </div>
        <div class="s-person-details__position s-text-details">Assistant Coach</div>
      </div>
    </div>
  </div>
</body></html>
"""

# A directory that exists only after JavaScript runs. Fetched statically it is
# an empty shell that parses to nobody; rendered, it is an ordinary staff
# table. This is the case `render=auto`'s visible-text heuristic misses — the
# shell carries plenty of prose, so it never looks "too short to be real".
JS_BUILT_DIRECTORY = """<!doctype html>
<html><head><title>Staff Directory | Script State</title></head><body>
  <h1>Staff Directory</h1>
  <p>Our staff directory lists every coach and administrator across all of our
     varsity programs. Use the filters below to narrow by sport or department.
     Contact details are provided for media enquiries only, and are updated at
     the start of each competitive season by the athletics communications
     office. Please direct general questions to the main athletics switchboard
     rather than to individual staff members during championship weeks.</p>
  <p>Prospective student-athletes should not use this page to contact coaching
     staff directly. Recruiting correspondence is handled through the compliance
     office, which reviews every enquiry against conference and national
     association rules before passing it on. Questionnaires submitted through
     the recruiting portal reach the relevant coaching staff far faster than
     email, and are the only route that guarantees a reply during a dead
     period. Ticketing, parking and hospitality questions go to the box office,
     whose staff are not listed here. Media requesting interviews should copy
     the communications office on any approach to a coach, including during
     the postseason, so that availability can be coordinated around travel.</p>
  <div id="directory"></div>
  <script>
    var STAFF = [
      ["Dana Reyes", "Director of Athletics", "dana.reyes@script.edu"],
      ["Chris Vance", "Head Coach", "chris.vance@script.edu"],
      ["Pat Oduya", "Assistant Coach", "pat.oduya@script.edu"],
      ["Sam Webb", "Head Coach", "sam.webb@script.edu"],
      ["Robin Ellis", "Assistant Coach", "robin.ellis@script.edu"],
      ["Jamie Fox", "Head Coach", "jamie.fox@script.edu"]
    ];
    document.addEventListener('DOMContentLoaded', function () {
      var rows = STAFF.map(function (p) {
        return '<tr><td>' + p[0] + '</td><td>' + p[1] +
               '</td><td><a href="mailto:' + p[2] + '">' + p[2] + '</a></td></tr>';
      }).join('');
      document.getElementById('directory').innerHTML =
        '<table><thead><tr><th>Men\\'s Basketball</th><th>Name</th><th>Title</th>' +
        '<th>Email</th></tr></thead><tbody>' + rows + '</tbody></table>';
    });
  </script>
</body></html>
"""

PAGES = {
    "/": HOME,
    "/whoami": WHOAMI,
    "/sidearm-cards": SIDEARM_PERSON_CARDS,
    "/js-directory": JS_BUILT_DIRECTORY,
    "/faculty-staff": FACULTY_DIRECTORY,
    "/sidearm": SIDEARM_DIRECTORY,
    "/contact-us": CONTACT,
    "/careers": CAREERS,
    "/about": ABOUT,
    "/members": MEMBERS,
    "/staff-directory": STAFF_DIRECTORY,
    "/coaches": STAFF_CARDS,
    "/malformed": MALFORMED,
}

ROBOTS = "User-agent: *\nDisallow: /admin\n"

# Smallest thing that is genuinely a JPEG: SOI, a comment segment, EOI. Enough
# for the download path, which checks the content type rather than the bytes.
FAKE_JPEG = b"\xff\xd8\xff\xfe\x00\x10headshot fixture\xff\xd9"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        # Every request is recorded so tests can assert on what actually went
        # out on the wire — the anti-bot headers are only worth anything if
        # they survive the trip through httpx.
        self.server.requests.append((self.path, dict(self.headers)))  # type: ignore[attr-defined]

        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/robots.txt":
            self._send(ROBOTS, "text/plain; charset=utf-8")
            return
        if path == "/gzipped":
            self._send_gzipped(ABOUT)
            return
        if path == "/rate-limited":
            # What a host does when we are asking too often. Retry-After is
            # short so the backoff tests stay fast.
            self._send(
                "<h1>Slow down</h1>",
                "text/html; charset=utf-8",
                status=429,
                extra={"Retry-After": "1"},
            )
            return
        if path.startswith("/images/"):
            self._send_bytes(FAKE_JPEG, "image/jpeg")
            return
        body = PAGES.get(path)
        if body is None:
            self._send("<h1>404</h1>", "text/html; charset=utf-8", status=404)
            return
        self._send(body, "text/html; charset=utf-8")

    def _send_gzipped(self, body: str, status: int = 200) -> None:
        """Serve gzip only if the client actually asked for it.

        Mirrors what a real server does with ``Accept-Encoding``, so a client
        that over-advertises its codecs gets caught out here.
        """
        raw = body.encode("utf-8")
        encoding = None
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            raw = gzip.compress(raw)
            encoding = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, raw: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send(
        self,
        body: str,
        ctype: str,
        status: int = 200,
        extra: dict[str, str] | None = None,
    ) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        pass  # keep the test output clean


class FixtureSite:
    """Context manager yielding the ``host:port`` of a running fixture site."""

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def requests(self) -> list[tuple[str, dict[str, str]]]:
        """``(path, headers)`` for every request served, in order."""
        if self._server is None:
            return []
        return list(self._server.requests)  # type: ignore[attr-defined]

    def headers_for(self, path: str) -> list[dict[str, str]]:
        """Headers of every request whose path matches, ignoring the query."""
        return [h for p, h in self.requests if p.split("?")[0].rstrip("/") == path.rstrip("/")]

    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.requests = []  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def __exit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


# Bay College: an <h2> per sport with its own table underneath — the layout the
# campus-directory salvage was hiding, because the athletics host was never read.
HEADING_PER_SPORT = """<!doctype html>
<html><head><title>Staff Directory - Bay College Norse</title></head><body>
  <h1>Staff Directory</h1>
  <h2>Administration</h2>
  <table><tr><th>Name</th><th>Title</th><th>Phone</th><th>E-Mail</th></tr>
    <tr><td>Matt Johnson</td><td>Director of Athletics</td><td>906-217-4134</td>
        <td><a href="mailto:matt.c.johnson@baycollege.edu">matt.c.johnson@baycollege.edu</a></td></tr>
  </table>
  <h2>Baseball</h2>
  <table><tr><th>Name</th><th>Title</th><th>Phone</th><th>E-Mail</th></tr>
    <tr><td>Travis Derrick</td><td>Assistant Baseball Coach</td><td></td><td></td></tr>
  </table>
  <h2>Women's Basketball</h2>
  <table><tr><th>Name</th><th>Title</th><th>Phone</th><th>E-Mail</th></tr>
    <tr><td>James Fassett</td><td>Head Women's Basketball Coach</td><td>906-217-4285</td>
        <td><a href="mailto:james.fassett@baycollege.edu">james.fassett@baycollege.edu</a></td></tr>
  </table>
</body></html>
"""
