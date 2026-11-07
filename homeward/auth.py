"""Role-based access: who may see and act on which patient.

  * care desk (tele-nurse)  - every home, plus demo / resilience controls
  * family / home carer     - ONLY their own patient: vitals, alerts, notes, notifications, audit

Design choices (privacy by default):
  * PINs are stored only as salted PBKDF2 hashes; sessions are random 256-bit tokens kept
    server-side, expiring after 12 h. Logout destroys the session.
  * 5 wrong PINs lock the account for 60 s.
  * The acting user is ALWAYS taken from the session, never from the request body, so the
    audit trail can't be forged ("acknowledged by Nurse Asha" means Nurse Asha did it).
  * Opening a patient's record is itself written to the audit log.
  * Browsers use an HttpOnly, SameSite=Strict cookie; the caregiver phone app uses a bearer token.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time

SESSION_TTL_S = 12 * 3600
MAX_FAILS, LOCK_S = 5, 60


def _hash(pin: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 120_000)


# Demo accounts. In production these come from a user store; PINs can be overridden with
# env vars HOMEWARD_PIN_<ID> (e.g. HOMEWARD_PIN_ASHA=8264).
_ACCOUNTS = [
    {"id": "asha", "name": "Nurse Asha (care desk)", "role": "desk", "patients": "*", "pin": "1234"},
    {"id": "sunita", "name": "Sunita (daughter-in-law)", "role": "family", "patients": ["P-101"], "pin": "1111"},
    {"id": "meena", "name": "Meena (wife)", "role": "family", "patients": ["P-102"], "pin": "2222"},
    {"id": "joseph", "name": "Home nurse Joseph", "role": "family", "patients": ["P-103"], "pin": "3333"},
]


class Auth:
    def __init__(self):
        self.users = {}
        for a in _ACCOUNTS:
            salt = os.urandom(16)
            pin = os.environ.get(f"HOMEWARD_PIN_{a['id'].upper()}", a["pin"])
            self.users[a["id"]] = {k: v for k, v in a.items() if k != "pin"} | {"salt": salt, "hash": _hash(pin, salt)}
        self.sessions: dict[str, dict] = {}
        self.fails: dict[str, list] = {}
        self.lock = threading.Lock()

    def directory(self) -> list[dict]:
        """Public list for the login screen (names and roles only)."""
        return [{"id": u["id"], "name": u["name"], "role": u["role"]} for u in self.users.values()]

    def login(self, user_id: str, pin: str) -> tuple[str | None, str]:
        with self.lock:
            u = self.users.get(user_id)
            f = self.fails.setdefault(user_id, [0, 0.0])
            if f[1] > time.time():
                return None, f"Too many attempts. Try again in {int(f[1] - time.time()) + 1} s."
            if not u or not hmac.compare_digest(_hash(pin, u["salt"]), u["hash"]):
                f[0] += 1
                if f[0] >= MAX_FAILS:
                    f[0], f[1] = 0, time.time() + LOCK_S
                return None, "Wrong name or PIN."
            f[0] = 0
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {"user": u["id"], "expires": time.time() + SESSION_TTL_S, "viewed": set()}
            return token, "ok"

    def logout(self, token: str | None) -> None:
        with self.lock:
            self.sessions.pop(token or "", None)

    def resolve(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.lock:
            s = self.sessions.get(token)
            if not s or s["expires"] < time.time():
                self.sessions.pop(token, None)
                return None
            u = self.users[s["user"]]
            return {"id": u["id"], "name": u["name"], "role": u["role"], "patients": u["patients"], "_session": s}

    @staticmethod
    def can_see(user: dict, pid: str) -> bool:
        return user["patients"] == "*" or pid in user["patients"]
