"""Encryption of health values in the database (Phase 3b).

What is encrypted
-----------------
The columns in ``SENSITIVE`` below: screening and lifestyle answers, clinic record values, dates of birth and
notes, food logs, tracker goals, medicine names, doses and instructions, prevention targets and notes, their
audit trails, and the text of in-app notices (which can name a medicine). They are encrypted in Python before
they are written, so the database (and any backup or leaked copy of it) only holds ciphertext.

Left readable on purpose: risk scores, thresholds and model names (the doctor pages sort and filter on them in
SQL), dates, statuses and ids, and account details (names, emails). Empty values ('', '{}', '[]') stay as they
are: they say only that nothing was entered.

How
---
AES-256-GCM with a random 96-bit nonce per value. A stored value looks like ``enc1:<key id>:<base64>``. Reads
are decrypted automatically by ahead/db.py (any text value starting ``enc1:``), so pages see plain values;
writes go through ``seal()`` at the few places that save these columns (ahead/storage.py, ahead/platform_data.py).

The key
-------
``AHEAD_FIELD_KEY``: 32 random bytes in base64. Without it AHEAD stores values unencrypted, as before.
LOSING THE KEY LOSES THE ENCRYPTED DATA: keep a copy in a password manager.
To change the key, put the new one first and the old one after a comma (``new,old``): new values use the new
key, the background job re-encrypts old ones, and once the admin page shows nothing left on the old key the
old one can be removed. If the database holds values for a key AHEAD doesn't have, start-up stops with an
error instead of showing broken pages (Render then keeps the previous deploy running).
scripts/field_key.py makes a key, shows which keys the stored values use, and can decrypt everything for a
roll-back to a version without encryption.
"""

import base64
import binascii
import hashlib
import logging
import os
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

PREFIX = "enc1:"
ENV = "AHEAD_FIELD_KEY"
_AAD = b"ahead/field/v1"
EMPTY = ("", "{}", "[]")   # stored as they are

# table -> (key column, encrypted columns)
SENSITIVE = {
    "records": ("id", ("values_json", "dob", "issues_json", "note")),
    "record_predictions": ("id", ("values_json",)),
    "screenings": ("id", ("values_json",)),
    "lifestyle": ("id", ("values_json", "scores_json")),
    "lifestyle_goals": ("patient_id", ("goals_json",)),
    "food_entries": ("id", ("foods_json", "nutrients_json")),
    "medication_plans": ("id", ("name", "dosage", "instructions", "notes")),
    "medication_audit": ("id", ("details_json",)),
    "prevention_plans": ("id", ("targets_json", "note")),
    "prevention_audit": ("id", ("details_json",)),
    "notifications": ("id", ("description",)),
}


class FieldKeyError(RuntimeError):
    """The key is missing, malformed or not the one the stored values were encrypted with."""


# =============================================================================
# KEYS
# =============================================================================

_cache = {"raw": None, "keys": ()}
_cache_lock = threading.Lock()


def key_id(key):
    return hashlib.sha256(b"ahead-field-key-id:" + key).hexdigest()[:8]


def _decode(text):
    text = text.strip()
    try:
        key = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_" if ("-" in text or "_" in text) else None,
                               validate=True)
    except (binascii.Error, ValueError):
        raise FieldKeyError(f"{ENV} is not valid base64. Make a key with the command in the handover note.") from None
    if len(key) != 32:
        raise FieldKeyError(f"{ENV} must be 32 bytes (44 base64 characters); this one is {len(key)} bytes.")
    return key


def keys():
    """[(key id, AESGCM)] from AHEAD_FIELD_KEY, the key for new values first. Empty when no key is set."""
    raw = os.environ.get(ENV, "").strip()
    if raw == _cache["raw"]:
        return _cache["keys"]
    with _cache_lock:
        parsed = []
        for part in (p for p in raw.split(",") if p.strip()):
            key = _decode(part)
            parsed.append((key_id(key), AESGCM(key)))
        _cache["raw"], _cache["keys"] = raw, tuple(parsed)
        return _cache["keys"]


def enabled():
    return bool(keys())


def current_key_id():
    found = keys()
    return found[0][0] if found else None


def make_key():
    """A new random key, ready to paste into Render."""
    return base64.b64encode(os.urandom(32)).decode()


# =============================================================================
# VALUES
# =============================================================================

def is_sealed(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(value):
    """Encrypt one text value with the current key. None, empty values and already-encrypted values pass through,
    and so does everything when no key is set."""
    if value is None or not isinstance(value, str) or value in EMPTY or value.startswith(PREFIX):
        return value
    found = keys()
    if not found:
        return value
    kid, cipher = found[0]
    nonce = os.urandom(12)
    data = cipher.encrypt(nonce, value.encode("utf-8"), _AAD)
    return f"{PREFIX}{kid}:{base64.urlsafe_b64encode(nonce + data).decode().rstrip('=')}"


def open_text(value):
    """The plain text of a stored value (unencrypted values are returned as they are)."""
    if not is_sealed(value):
        return value
    try:
        kid, body = value[len(PREFIX):].split(":", 1)
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, binascii.Error):
        raise FieldKeyError("A stored value is damaged (not valid encrypted text).") from None
    cipher = dict(keys()).get(kid)
    if cipher is None:
        raise FieldKeyError(f"A stored value was encrypted with key {kid}, which is not in {ENV}.")
    try:
        return cipher.decrypt(blob[:12], blob[12:], _AAD).decode("utf-8")
    except Exception:
        raise FieldKeyError("A stored value could not be decrypted (damaged, or the key doesn't match).") from None


def reveal(values):
    """A database row's values with any encrypted text decrypted (ahead/db.py calls this for every row)."""
    for value in values:
        if isinstance(value, str) and value.startswith(PREFIX):
            return tuple(open_text(v) if isinstance(v, str) and v.startswith(PREFIX) else v for v in values)
    return values


# =============================================================================
# START-UP CHECK, BACKGROUND ENCRYPTION AND KEY CHANGES
# =============================================================================

def _columns():
    for table, (pk, columns) in SENSITIVE.items():
        for column in columns:
            yield table, pk, column


def stored_key_ids():
    """Every key id that encrypted values in the database use."""
    from ahead import db as database
    found = set()
    with database.connection(decrypt=False) as conn:
        for table, _, column in _columns():
            for row in conn.execute(f"SELECT DISTINCT substr({column},6,8) kid FROM {table} WHERE {column} LIKE ?",
                                    (PREFIX + "%",)).fetchall():
                found.add(row["kid"])
    return found


def check():
    """Run at start-up: stop with FieldKeyError when AHEAD can't read what is stored. Returns the stored key ids."""
    have = {kid for kid, _ in keys()}   # also raises for a malformed key
    stored = stored_key_ids()
    missing = stored - have
    if missing:
        if not have:
            raise FieldKeyError(f"The database holds encrypted health values but {ENV} is not set. Add the key in "
                                "Render → Environment (the one saved in your password manager).")
        raise FieldKeyError(f"The database holds values encrypted with key {', '.join(sorted(missing))}, which is "
                            f"not in {ENV} (it has {', '.join(sorted(have))}). Put the old key back, after a comma.")
    return stored


def _needs_work(value, kid):
    if value is None or not isinstance(value, str) or value in EMPTY:
        return False
    return not value.startswith(f"{PREFIX}{kid}:")


def pending():
    """{table.column: number of values not yet encrypted with the current key} (only the ones with work left)."""
    kid = current_key_id()
    if kid is None:
        return {}
    from ahead import db as database
    counts = {}
    with database.connection(decrypt=False) as conn:
        for table, _, column in _columns():
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL AND {column} NOT IN ('','{{}}','[]') "
                             f"AND {column} NOT LIKE ?", (f"{PREFIX}{kid}:%",)).fetchone()[0]
            if n:
                counts[f"{table}.{column}"] = n
    return counts


def encrypt_existing(batch=200, max_rounds=10_000):
    """Encrypt values saved before the key was set, and re-encrypt values under an older key. Safe to run while
    the site is in use: each value is replaced only if it hasn't changed since it was read. Returns the count."""
    kid = current_key_id()
    if kid is None:
        return 0
    from ahead import db as database
    done = 0
    for table, pk, column in _columns():
        for _ in range(max_rounds):
            with database.connection(decrypt=False) as conn:
                rows = conn.execute(f"SELECT {pk} pk,{column} val FROM {table} WHERE {column} IS NOT NULL "
                                    f"AND {column} NOT IN ('','{{}}','[]') AND {column} NOT LIKE ? ORDER BY {pk} LIMIT ?",
                                    (f"{PREFIX}{kid}:%", batch)).fetchall()
            if not rows:
                break
            # Encrypted outside the write transaction, so the site's own writes are never held up for long.
            updates = [(seal(open_text(row["val"])), row["pk"], row["val"]) for row in rows if _needs_work(row["val"], kid)]
            with database.connection(decrypt=False) as conn:
                for new, key, old in updates:
                    done += conn.execute(f"UPDATE {table} SET {column}=? WHERE {pk}=? AND {column}=?", (new, key, old)).rowcount
            if len(rows) < batch:
                break
    if done:
        log.info("encrypted %d stored health values", done)
    return done


def decrypt_existing(batch=200):
    """Turn every encrypted value back into plain text (scripts/field_key.py decrypt-all), for going back to a
    version of AHEAD without encryption. Needs every key the values were encrypted with. Returns the count."""
    from ahead import db as database
    done = 0
    for table, pk, column in _columns():
        while True:
            with database.connection(decrypt=False) as conn:
                rows = conn.execute(f"SELECT {pk} pk,{column} val FROM {table} WHERE {column} LIKE ? ORDER BY {pk} LIMIT ?",
                                    (PREFIX + "%", batch)).fetchall()
            if not rows:
                break
            updates = [(open_text(row["val"]), row["pk"], row["val"]) for row in rows]
            changed = 0
            with database.connection(decrypt=False) as conn:
                for plain, key, old in updates:
                    changed += conn.execute(f"UPDATE {table} SET {column}=? WHERE {pk}=? AND {column}=?", (plain, key, old)).rowcount
            done += changed
            if not changed:   # every row changed meanwhile (the site is still running): stop rather than spin
                break
    return done

