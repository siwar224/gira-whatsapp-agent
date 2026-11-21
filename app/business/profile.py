"""The restaurant's business profile: loaded from YAML, validated, and rendered for the agent."""

from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.business.countries import Country, get_country

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class Contact(BaseModel):
    address: str
    map_url: str | None = None
    phone: str
    email: str | None = None
    website: str | None = None


class Links(BaseModel):
    booking: str | None = None
    manage_booking: str | None = None
    build_your_plate: str | None = None


class MenuItem(BaseModel):
    name: str
    price: float
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class MenuCategory(BaseModel):
    name: str
    intro: str = ""
    items: list[MenuItem]


class Menu(BaseModel):
    note: str = ""
    categories: list[MenuCategory]


class Table(BaseModel):
    id: str
    label: str
    area: str
    seats: int = Field(ge=1)


class BookingRules(BaseModel):
    min_guests: int = 1
    max_guests: int = 6
    large_party: str = ""
    days_ahead: int = 14
    table_hold_minutes: int = 120
    occasions: list[str] = Field(default_factory=list)
    areas: dict[str, str]
    tables: list[Table]

    @model_validator(mode="after")
    def tables_use_known_areas(self) -> "BookingRules":
        unknown = {t.area for t in self.tables} - set(self.areas)
        if unknown:
            raise ValueError(f"Tables use unknown areas: {sorted(unknown)}")
        return self


class BusinessProfile(BaseModel):
    name: str
    description: str = ""
    country: str
    timezone: str | None = None
    currency: str = "EUR"
    contact: Contact
    links: Links = Field(default_factory=Links)
    opening_hours: dict[int, list[tuple[str, str]]]
    menu: Menu
    booking: BookingRules
    facts: list[str] = Field(default_factory=list)
    never_mention: list[str] = Field(default_factory=list)

    @field_validator("country")
    @classmethod
    def country_is_supported(cls, value: str) -> str:
        return get_country(value).code

    @model_validator(mode="after")
    def default_timezone(self) -> "BusinessProfile":
        if not self.timezone:
            self.timezone = get_country(self.country).timezone
        ZoneInfo(self.timezone)  # fails fast on a typo
        return self

    @property
    def country_info(self) -> Country:
        return get_country(self.country)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone or self.country_info.timezone)

    def area_label(self, area: str) -> str:
        return self.booking.areas.get(area, area)

    def render_facts(self) -> str:
        """The profile as plain text for the agent's instructions."""
        c, b = self.contact, self.booking
        lines = [f"RESTAURANT: {self.name}", self.description.strip(), f"Address: {c.address}"]
        if c.map_url:
            lines.append(f"Map: {c.map_url}")
        lines.append(f"Phone: {c.phone}" + (f" · Email: {c.email}" if c.email else ""))
        if c.website:
            lines.append(f"Website: {c.website}")

        lines += ["", f"OPENING HOURS ({self.timezone} time)"]
        for day, label in enumerate(WEEKDAYS):
            ranges = self.opening_hours.get(day, [])
            lines.append(f"{label}: " + (" and ".join(f"{a}–{z}" for a, z in ranges) if ranges else "closed"))

        lines += ["", f"MENU (prices in {self.currency}). {self.menu.note}".rstrip()]
        for category in self.menu.categories:
            lines.append(f"{category.name} — {category.intro}".rstrip(" —"))
            for item in category.items:
                price = f"{item.price:g}"
                tags = f" ({', '.join(item.tags)})" if item.tags else ""
                lines.append(f"- {item.name}, {price}{tags} — {item.description}")

        lines += [
            "",
            "BOOKINGS",
            f"- Parties of {b.min_guests} to {b.max_guests} guests. {b.large_party}".rstrip(),
            f"- Bookable up to {b.days_ahead} days ahead, only on open days. A table is held for "
            f"{b.table_hold_minutes} minutes.",
            "- Areas: " + "; ".join(f"{key} = {label}" for key, label in b.areas.items()),
        ]
        if b.occasions:
            lines.append("- Occasions the team prepares for: " + ", ".join(b.occasions))
        links = self.links
        if links.booking:
            lines.append(f"- Book online: {links.booking}")
        if links.manage_booking:
            lines.append(f"- Manage or cancel online: {links.manage_booking}")
        if links.build_your_plate:
            lines.append(f"- Build your own plate: {links.build_your_plate}")
        if self.facts:
            lines += ["", "GOOD TO KNOW"] + [f"- {fact}" for fact in self.facts]
        return "\n".join(line for line in lines if line is not None)


def load_profile(path: Path) -> BusinessProfile:
    with path.open(encoding="utf-8") as handle:
        return BusinessProfile.model_validate(yaml.safe_load(handle))
