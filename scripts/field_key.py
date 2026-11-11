"""AHEAD_FIELD_KEY helper (Phase 3b): make a key, check the database, or decrypt everything for a roll-back.

    python scripts/field_key.py new           print a new random key (paste it into Render, keep a copy safe)
    python scripts/field_key.py status        which keys the stored values use and how many still wait
    python scripts/field_key.py decrypt-all   turn encrypted values back into plain text (see below)

status and decrypt-all work on the database in DATABASE_URL (the Neon string, like scripts/create_account.py)
with the key(s) in AHEAD_FIELD_KEY; without DATABASE_URL they use the local SQLite file.

decrypt-all is only for going back to a version from before Phase 3b (Render → Deploys → Rollback), because
that version can't read encrypted values: roll back FIRST (so nothing encrypts them again), then run this.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ahead import db, fieldcrypt  # noqa: E402


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "new":
        print(fieldcrypt.make_key())
        return
    if command not in ("status", "decrypt-all"):
        sys.exit(__doc__)
    print(f"Database: {db.describe_target() if db.is_postgres() else db.sqlite_path()}")
    print(f"Key(s) in AHEAD_FIELD_KEY: {', '.join(k for k, _ in fieldcrypt.keys()) or 'none'}")
    print(f"Key(s) the stored values use: {', '.join(sorted(fieldcrypt.stored_key_ids())) or 'none (nothing encrypted)'}")
    if command == "status":
        waiting = fieldcrypt.pending()
        print("Still to encrypt with the current key: " + (", ".join(f"{k} {n}" for k, n in waiting.items()) or "nothing"))
        return
    fieldcrypt.check()   # stops here if a key is missing
    if input("Decrypt every stored health value back to plain text? Type yes: ").strip().lower() != "yes":
        sys.exit("Nothing changed.")
    print(f"Decrypted {fieldcrypt.decrypt_existing()} values.")
    left = fieldcrypt.remaining_encrypted()
    if left:   # e.g. the site was still running and wrote new ones: roll back in Render first, then run again
        sys.exit(f"{left} values are still encrypted. Make sure the rollback is done in Render, then run this again.")
    print("Nothing encrypted is left.")


if __name__ == "__main__":
    try:
        main()
    except fieldcrypt.FieldKeyError as error:
        sys.exit(f"Stopped: {error}")
    except KeyboardInterrupt:
        sys.exit("\nCancelled. Nothing changed.")
