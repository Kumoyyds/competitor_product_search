"""Human-readable names for country codes exposed by search providers.

Provider modules own the actual country-to-API mappings.  This file is only
used when rendering those mappings in documentation.
"""

COUNTRY_NAMES: dict[str, str] = {
    "uk": "United Kingdom",
    "gb": "United Kingdom",
    "de": "Germany",
    "fr": "France",
    "us": "United States",
    "nl": "Netherlands",
    "jp": "Japan",
    "es": "Spain",
    "it": "Italy",
    "pt": "Portugal",
    "se": "Sweden",
    "pl": "Poland",
    "br": "Brazil",
    "au": "Australia",
    "ca": "Canada",
}
