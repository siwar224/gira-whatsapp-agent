"""Countries the agent is ready for: dialling codes, time zones and the languages guests write in."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Country:
    code: str
    name: str
    dialling_code: str
    timezone: str
    languages: str
    flag: str


SUPPORTED_COUNTRIES: dict[str, Country] = {
    "TN": Country(
        "TN",
        "Tunisia",
        "+216",
        "Africa/Tunis",
        "Tunisian Arabic (Derja, in Arabic script or Latin letters with numbers such as 3, 7, 9), "
        "French and Modern Standard Arabic",
        "🇹🇳",
    ),
    "MA": Country(
        "MA",
        "Morocco",
        "+212",
        "Africa/Casablanca",
        "Moroccan Arabic (Darija, in Arabic script or Latin letters), French, Modern Standard Arabic and Amazigh",
        "🇲🇦",
    ),
    "FR": Country("FR", "France", "+33", "Europe/Paris", "French", "🇫🇷"),
    "IT": Country("IT", "Italy", "+39", "Europe/Rome", "Italian", "🇮🇹"),
    "DE": Country("DE", "Germany", "+49", "Europe/Berlin", "German", "🇩🇪"),
    "ES": Country("ES", "Spain", "+34", "Europe/Madrid", "Spanish, Catalan, Basque and Galician", "🇪🇸"),
}


def get_country(code: str) -> Country:
    try:
        return SUPPORTED_COUNTRIES[code.upper()]
    except KeyError as exc:
        supported = ", ".join(SUPPORTED_COUNTRIES)
        raise ValueError(f"Unsupported country {code!r}. Supported: {supported}.") from exc
