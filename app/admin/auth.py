"""Sign-in for the team: one account from the environment, a signed session cookie, CSRF tokens."""

import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.core.config import Settings

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60


class LoginRequired(Exception):
    """Raised by protected pages; turned into a redirect to the sign-in page."""


class LoginThrottle:
    """Too many wrong passwords from one address lock it out for a while."""

    def __init__(self) -> None:
        self._failures: defaultdict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str) -> deque[float]:
        attempts = self._failures[key]
        while attempts and time.monotonic() - attempts[0] > LOCKOUT_SECONDS:
            attempts.popleft()
        return attempts

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= MAX_FAILED_ATTEMPTS

    def failed(self, key: str) -> None:
        self._recent(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


def check_credentials(settings: Settings, username: str, password: str) -> bool:
    expected = settings.admin_password.get_secret_value()
    if not expected:
        return False
    user_ok = secrets.compare_digest(username.encode(), settings.admin_username.encode())
    password_ok = secrets.compare_digest(password.encode(), expected.encode())
    return user_ok and password_ok


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def check_csrf(request: Request, submitted: str) -> None:
    expected = request.session.get("csrf")
    if not expected or not secrets.compare_digest(expected, submitted or ""):
        raise HTTPException(status_code=400, detail="The form expired. Reload the page and try again.")


def require_admin(request: Request) -> str:
    user = request.session.get("admin")
    if not user:
        raise LoginRequired
    return user
