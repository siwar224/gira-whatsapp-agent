"""Logging setup. Messages never contain message text or full phone numbers."""

import logging


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Request bodies of the HTTP clients would include tokens and personal data.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def mask_phone(value: str | None) -> str:
    """'+21620123456' -> '+2162*****56'."""
    if not value:
        return "?"
    if len(value) <= 6:
        return "***"
    return f"{value[:5]}{'*' * (len(value) - 7)}{value[-2:]}"
