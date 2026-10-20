"""Shared Alexa locale display names, used by the status and setup UIs.

Kept in app/ (not endpoints/) so non-endpoint modules can import it without a
circular dependency. Keys are the raw locale codes (option values); values are
the human-readable display names.
"""

LOCALE_NAMES = {
    "de-DE": "German (Germany)",
    "en-AU": "English (Australia)",
    "en-CA": "English (Canada)",
    "en-GB": "English (UK)",
    "en-IN": "English (India)",
    "en-US": "English (US)",
    "es-ES": "Spanish (Spain)",
    "es-MX": "Spanish (Mexico)",
    "fr-CA": "French (Canada)",
    "fr-FR": "French (France)",
    "it-IT": "Italian (Italy)",
    "nl-NL": "Dutch (Netherlands)",
    "pt-BR": "Portuguese (Brazil)",
}
