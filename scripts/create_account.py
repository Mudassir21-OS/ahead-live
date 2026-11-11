"""Create a doctor or admin account, or set a new password for one, from your own computer.

    python scripts/create_account.py --email you@example.com --name "Your Name" --role Admin

It uses DATABASE_URL if set (the live Neon database), otherwise the local SQLite file. The password is
typed at a prompt (shown as *), twice; it is never accepted on the command line, where it would be saved
in your shell history. For an email that already exists it offers to set a new password instead.

Usually you only need this once, for the first admin. After that, add doctors from Accounts & Security.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ahead import db  # noqa: E402
from ahead.config import ROLE_ADMIN, ROLE_DOCTOR  # noqa: E402
from ahead.storage import MIN_PASSWORD, connection, create_user, init_db, password_hash  # noqa: E402


def masked_input(prompt):
    """Read a line showing * for each character; Backspace removes one."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    chars = []
    if os.name == "nt":
        import msvcrt
        while True:
            key = msvcrt.getwch()
            if key in ("\r", "\n"):
                break
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\b":
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
            elif key in ("\x00", "\xe0"):
                msvcrt.getwch()  # arrow and function keys come as two characters
            else:
                chars.append(key)
                sys.stdout.write("*")
            sys.stdout.flush()
    else:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                key = sys.stdin.read(1)
                if key in ("\r", "\n"):
                    break
                if key == "\x03":
                    raise KeyboardInterrupt
                if key in ("\x7f", "\b"):
                    if chars:
                        chars.pop()
                        sys.stdout.write("\b \b")
                else:
                    chars.append(key)
                    sys.stdout.write("*")
                sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    sys.stdout.write("\n")
    return "".join(chars)


def ask_password():
    while True:
        first = masked_input(f"Password ({MIN_PASSWORD}+ characters): ")
        if len(first) < MIN_PASSWORD:
            print(f"Too short: use at least {MIN_PASSWORD} characters.")
            continue
        if masked_input("Type it again: ") != first:
            print("The two passwords don't match. Try again.")
            continue
        return first


def main():
    if any(arg.startswith("--password") for arg in sys.argv[1:]):
        sys.exit("For safety the password is typed at the prompt, not given on the command line.")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=[ROLE_ADMIN, ROLE_DOCTOR], default=ROLE_ADMIN)
    parser.add_argument("--institution", default="")
    args = parser.parse_args()

    print(f"Database: {db.describe_target() if db.is_postgres() else db.sqlite_path()}")
    if input("Continue? Type yes: ").strip().lower() != "yes":
        sys.exit("Nothing changed.")
    init_db()
    email = args.email.strip().lower()
    with connection() as conn:
        existing = conn.execute("SELECT id,role FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        print(f"{email} already exists ({existing['role']}).")
        if input("Set a new password for it (signs it out everywhere)? Type yes: ").strip().lower() != "yes":
            sys.exit("Nothing changed.")
        password = ask_password()
        with connection() as conn:
            conn.execute("UPDATE users SET password_hash=?,token_version=token_version+1,is_active=1,deactivated_at=NULL WHERE id=?",
                         (password_hash(password), existing["id"]))
        print("Password updated.")
        return
    password = ask_password()
    create_user(email, password, args.role, args.name, args.institution)
    print(f"Created {args.role} account for {email}. Sign in with 'Doctor / Admin' selected.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled. Nothing changed.")
