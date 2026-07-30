"""US state names and Census regions.

``region`` in the school schema is not scraped — it is a pure function of the
state, using the Census Bureau's four regions and nine divisions, which is
exactly the ``"Northeast (Middle Atlantic)"`` / ``"South (East South Central)"``
form the origin database already uses.
"""

from __future__ import annotations

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "VI": "U.S. Virgin Islands", "GU": "Guam",
    # NAIA membership reaches into Canada.
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador", "NS": "Nova Scotia",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan",
}

_DIVISIONS = {
    "Northeast (New England)": ("CT", "ME", "MA", "NH", "RI", "VT"),
    "Northeast (Middle Atlantic)": ("NJ", "NY", "PA"),
    "Midwest (East North Central)": ("IL", "IN", "MI", "OH", "WI"),
    "Midwest (West North Central)": ("IA", "KS", "MN", "MO", "NE", "ND", "SD"),
    "South (South Atlantic)": ("DE", "DC", "FL", "GA", "MD", "NC", "SC", "VA", "WV"),
    "South (East South Central)": ("AL", "KY", "MS", "TN"),
    "South (West South Central)": ("AR", "LA", "OK", "TX"),
    "West (Mountain)": ("AZ", "CO", "ID", "MT", "NV", "NM", "UT", "WY"),
    "West (Pacific)": ("AK", "CA", "HI", "OR", "WA"),
    # Not Census regions, but the schema needs something for them.
    "Territories": ("PR", "VI", "GU"),
    "Canada": ("AB", "BC", "MB", "NB", "NL", "NS", "ON", "PE", "QC", "SK"),
}

REGION_BY_STATE = {
    code: region for region, codes in _DIVISIONS.items() for code in codes
}

_CODE_BY_NAME = {name.lower(): code for code, name in STATE_NAMES.items()}


def state_code(value: str | None) -> str | None:
    """Accept ``"NJ"`` or ``"New Jersey"`` and return the two-letter code."""
    if not value:
        return None
    text = value.strip()
    if len(text) == 2 and text.upper() in STATE_NAMES:
        return text.upper()
    return _CODE_BY_NAME.get(text.lower())


def state_name(value: str | None) -> str | None:
    """The full state name the origin database stores, e.g. ``"New Jersey"``."""
    code = state_code(value)
    return STATE_NAMES.get(code) if code else None


def region_for(value: str | None) -> str | None:
    code = state_code(value)
    return REGION_BY_STATE.get(code) if code else None
