"""Two-step sign-in: time-based one-time codes (RFC 6238) and recovery codes.

The codes are the ones every authenticator app makes: six digits, a new one
every 30 seconds, from a secret shared once as a QR code. Nothing here needs
more than the standard library.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from flask import current_app

from .db import get_db

STEP = 30
DIGITS = 6
# A code from the step before or after now is taken too, for clocks a little apart.
DRIFT = 1
RECOVERY_CODES = 10
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # nothing that reads as something else


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    start = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[start : start + 4])[0] & 0x7FFFFFFF
    return f"{number % 10**DIGITS:0{DIGITS}d}"


def matching_step(secret: str, text: str, after: int = 0, now: float | None = None) -> int | None:
    """The time step `text` is the code for, if it is current and later than `after`."""
    digits = "".join(text.split())
    if len(digits) != DIGITS or not digits.isdigit():
        return None
    current = int((time.time() if now is None else now) // STEP)
    found = None
    for step in range(current - DRIFT, current + DRIFT + 1):
        # Every candidate is compared, so timing says nothing about which matched.
        if hmac.compare_digest(code_at(secret, step), digits) and step > after:
            found = step
    return found


def uri(secret: str, username: str) -> str:
    """What the QR code says: the address authenticator apps understand."""
    issuer = current_app.config["TWO_FACTOR_ISSUER"]
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"


def spaced(secret: str) -> str:
    """The secret in groups of four, for typing into an app by hand."""
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


def _hashed(code: str) -> str:
    plain = "".join(c for c in code.lower() if c.isalnum())
    return hashlib.sha256(plain.encode()).hexdigest()


def _fresh_codes(user_id: int) -> list[str]:
    codes = []
    for _ in range(RECOVERY_CODES):
        letters = "".join(secrets.choice(ALPHABET) for _ in range(12))
        codes.append("-".join(letters[i : i + 4] for i in range(0, 12, 4)))
    db = get_db()
    db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    db.executemany(
        "INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)",
        [(user_id, _hashed(code)) for code in codes],
    )
    return codes


def enable(user_id: int, secret: str, step: int) -> list[str]:
    """Turn two-step sign-in on; returns the recovery codes, to show this once."""
    db = get_db()
    db.execute(
        "UPDATE users SET totp_secret = ?, totp_step = ? WHERE id = ?", (secret, step, user_id)
    )
    codes = _fresh_codes(user_id)
    db.commit()
    return codes


def disable(user_id: int) -> None:
    db = get_db()
    db.execute("UPDATE users SET totp_secret = NULL, totp_step = 0 WHERE id = ?", (user_id,))
    db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    db.commit()


def new_recovery_codes(user_id: int) -> list[str]:
    codes = _fresh_codes(user_id)
    get_db().commit()
    return codes


def recovery_codes_left(user_id: int) -> int:
    row = get_db().execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (user_id,))
    return row.fetchone()[0]


def check(user, text: str) -> str | None:
    """Is `text` this user's current code, or one of their recovery codes?

    Returns "code" or "recovery", or None. Either kind works once: a code's time
    step is remembered, and a recovery code is deleted.
    """
    db = get_db()
    step = matching_step(user["totp_secret"], text, after=user["totp_step"])
    if step is not None:
        db.execute("UPDATE users SET totp_step = ? WHERE id = ?", (step, user["id"]))
        db.commit()
        return "code"
    used = db.execute(
        "DELETE FROM recovery_codes WHERE user_id = ? AND code_hash = ?",
        (user["id"], _hashed(text)),
    )
    db.commit()
    return "recovery" if used.rowcount else None
