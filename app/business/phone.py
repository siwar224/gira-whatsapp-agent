"""Phone numbers in E.164, whatever way guests type them."""

import phonenumbers

from app.business.countries import SUPPORTED_COUNTRIES, Country


def normalize_phone(raw: str | None, default_country: str) -> str | None:
    """Return the number in E.164 ('+21620123456'), or None when it is not a valid number.

    Local formats are read in the restaurant's country: '20 123 456' in Tunisia, '06 12 34 56 78'
    in France, '347 123 4567' in Italy. International formats ('+49…', '0049…') work everywhere.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("00"):
        text = "+" + text[2:]
    try:
        parsed = phonenumbers.parse(text, default_country.upper())
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def from_whatsapp_id(wa_id: str | None) -> str | None:
    """WhatsApp ids are E.164 numbers without the '+'."""
    if not wa_id or not wa_id.isdigit():
        return None
    return normalize_phone(f"+{wa_id}", "FR")


def country_of(e164: str | None) -> Country | None:
    """The supported country a number belongs to, if any."""
    if not e164:
        return None
    try:
        region = phonenumbers.region_code_for_number(phonenumbers.parse(e164))
    except phonenumbers.NumberParseException:
        return None
    return SUPPORTED_COUNTRIES.get(region or "")
