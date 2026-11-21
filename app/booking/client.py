"""Client for the restaurant's booking API (the same API the website uses)."""

from dataclasses import dataclass
from typing import Any

import httpx

from app.business.profile import BusinessProfile, Table


@dataclass(frozen=True)
class ApiResult:
    status: int
    data: dict[str, Any]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error(self) -> dict[str, Any]:
        err = self.data.get("error") or {}
        out: dict[str, Any] = {
            "ok": False,
            "status": self.status,
            "code": err.get("code", "error"),
            "message": err.get("message", "The booking system returned an error."),
        }
        if err.get("fields"):
            out["fields"] = err["fields"]
        return out


def fits(table: Table, guests: int) -> bool:
    """A party sits at a table that fits without wasting more than two seats."""
    return table.seats >= guests and table.seats - guests <= 2


class BookingClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self._http = http
        self._timeout = timeout
        # The API only accepts writes from the restaurant's own site.
        self._headers = {"Origin": self.base_url}

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> ApiResult:
        response = await self._http.request(
            method, f"{self.base_url}{path}", json=body, headers=self._headers, timeout=self._timeout
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        return ApiResult(response.status_code, data if isinstance(data, dict) else {})

    async def availability(self, date: str) -> ApiResult:
        return await self._call("GET", f"/api/availability?date={date}")

    async def create(self, body: dict[str, Any]) -> ApiResult:
        return await self._call("POST", "/api/bookings", body)

    async def lookup(self, code: str, phone: str) -> ApiResult:
        return await self._call("POST", "/api/bookings/lookup", {"code": code, "phone": phone})

    async def cancel(self, code: str, phone: str) -> ApiResult:
        return await self._call("DELETE", f"/api/bookings/{code}", {"phone": phone})

    async def join_waitlist(self, body: dict[str, Any]) -> ApiResult:
        return await self._call("POST", "/api/waitlist", body)


def free_tables(profile: BusinessProfile, taken: set[str], guests: int, area: str | None = None) -> list[Table]:
    return [
        table
        for table in profile.booking.tables
        if fits(table, guests) and table.id not in taken and (not area or table.area == area)
    ]
