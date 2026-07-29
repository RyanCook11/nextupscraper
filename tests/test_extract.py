from scrapbot import extract
from scrapbot.sources.website import normalize_domain
from tests.fixtures import CONTACT, HOME


def test_company_name_prefers_og_site_name():
    tree = extract.parse(HOME)
    assert extract.company_name(tree, "acme-scaffolding.com.au") == "Acme Scaffolding"


def test_company_name_falls_back_to_domain():
    tree = extract.parse("<html><head></head><body>hi</body></html>")
    assert extract.company_name(tree, "big-blue-plumbing.com") == "Big Blue Plumbing"


def test_title_fallback_drops_generic_segments():
    tree = extract.parse("<html><head><title>Zeta Freight | Home</title></head><body></body></html>")
    assert extract.company_name(tree, "zeta.com") == "Zeta Freight"


def test_description_from_meta():
    desc = extract.description(extract.parse(HOME))
    assert desc is not None and desc.startswith("Acme Scaffolding provides commercial")


def test_emails_ranked_and_noise_filtered():
    tree = extract.parse(CONTACT)
    found = extract.emails(CONTACT, tree, "acme-scaffolding.com.au")
    assert found[0] == "info@acme-scaffolding.com.au"
    assert "accounts@acme-scaffolding.com.au" in found
    assert not any("2x" in addr or addr.endswith(".png") for addr in found)


def test_phones_from_tel_and_text():
    tree = extract.parse(CONTACT)
    digits = {"".join(ch for ch in p if ch.isdigit()) for p in extract.phones(tree, extract.visible_text(tree))}
    assert "61298765432" in digits
    assert "0412345678" in digits


def test_phones_reject_digit_soup():
    """Regression: python.org yielded '5 666666666666' from page furniture."""
    tree = extract.parse("<html><body></body></html>")
    for junk in [
        "Python 3.5 666666666666 downloads",
        "order 1234567890123456 shipped",
        "ISBN 9781234567897",
        "0000 0000 0000",
        "© 2024 2025 all rights",
        "fibonacci: 1 1 2 3 5 8 13 21 34 55",   # python.org, verbatim
        "The result is 8 13 21 34 55 items",    # grouped, but nothing says phone
    ]:
        assert extract.phones(tree, junk) == [], junk


def test_code_samples_are_excluded_from_visible_text():
    tree = extract.parse(
        "<html><body><p>Phone 02 9876 5432</p>"
        "<pre>fib = 8 13 21 34 55</pre><code>tel 5 666666666666</code></body></html>"
    )
    text = extract.visible_text(tree)
    assert "fib" not in text and "666666" not in text
    assert extract.phones(tree, text) == ["02 9876 5432"]


def test_phones_accept_real_shapes():
    tree = extract.parse("<html><body></body></html>")
    for good in ["(02) 9876 5432", "+61 2 9876 5432", "0412 345 678", "555-123-4567",
                 "+1 (555) 123-4567", "0298765432"]:
        assert extract.phones(tree, f"call us on {good} today"), good


def test_socials_are_absolute_and_stripped_of_query():
    socials = extract.socials(extract.parse(HOME), "https://acme-scaffolding.com.au/")
    assert socials["linkedin"] == "https://www.linkedin.com/company/acme-scaffolding"
    assert socials["facebook"] == "https://facebook.com/acmescaffolding"


def test_location_from_json_ld():
    location = extract.location(extract.parse(HOME), "")
    assert location == "12 Vale Rd, Parramatta, NSW, 2150, AU"


def test_industry_hints():
    tree = extract.parse(HOME)
    hints = extract.industry_hints(extract.visible_text(tree), extract.description(tree))
    assert "construction" in hints


def test_open_role_markers():
    assert extract.has_open_roles("Join our team — current vacancies below")
    assert not extract.has_open_roles("We build scaffolding.")


def test_internal_links_scored_and_filtered():
    links = extract.internal_links(
        extract.parse(HOME), "https://acme-scaffolding.com.au/", "acme-scaffolding.com.au"
    )
    urls = [url for _score, url in links]
    assert urls[0].endswith("/contact-us")  # highest weighted hint wins
    assert not any(url.endswith(".pdf") for url in urls)
    assert not any("linkedin.com" in url for url in urls)


def test_normalize_domain():
    assert normalize_domain("https://WWW.Acme.com/about?x=1") == "acme.com"
    assert normalize_domain("acme.com.au") == "acme.com.au"
    assert normalize_domain("http://localhost:8765/x") == "localhost:8765"
    assert normalize_domain("https://acme.com:443/") == "acme.com"
    assert normalize_domain("not a domain") is None
    assert normalize_domain("") is None
