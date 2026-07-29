"""A tiny fake company site, served over HTTP for end-to-end tests."""

from __future__ import annotations

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
          <td><a href="tel:+15551110001">(555) 111-0001</a></td></tr>
    </tbody>
  </table>
  <table>
    <thead><tr><th>Men's Basketball</th><th>Name</th><th>Title</th>
      <th>Email</th><th>Phone</th></tr></thead>
    <tbody>
      <tr><td><a href="/staff-directory/chris-vance/34">Chris Vance</a></td>
          <td>Head Coach</td>
          <td><a href="mailto:chris.vance@state.edu">chris.vance@state.edu</a></td>
          <td><a href="tel:+15551110002">(555) 111-0002</a></td></tr>
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

# Card layout, for sites that don't use tables at all.
STAFF_CARDS = """<!doctype html>
<html><head><title>Coaches | Cardinal Athletics</title></head><body>
  <h2>Women's Soccer</h2>
  <ul>
    <li class="staff-card"><h3>Robin Ellis</h3>
      <p>Head Coach</p>
      <a href="/staff-directory/robin-ellis/7">Bio</a>
      <a href="mailto:robin.ellis@cardinal.edu">Email</a>
      <a href="tel:+15552220003">(555) 222-0003</a></li>
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

PAGES = {
    "/": HOME,
    "/contact-us": CONTACT,
    "/careers": CAREERS,
    "/about": ABOUT,
    "/members": MEMBERS,
    "/staff-directory": STAFF_DIRECTORY,
    "/coaches": STAFF_CARDS,
    "/malformed": MALFORMED,
}

ROBOTS = "User-agent: *\nDisallow: /admin\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/robots.txt":
            self._send(ROBOTS, "text/plain; charset=utf-8")
            return
        body = PAGES.get(path)
        if body is None:
            self._send("<h1>404</h1>", "text/html; charset=utf-8", status=404)
            return
        self._send(body, "text/html; charset=utf-8")

    def _send(self, body: str, ctype: str, status: int = 200) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        pass  # keep the test output clean


class FixtureSite:
    """Context manager yielding the ``host:port`` of a running fixture site."""

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
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
