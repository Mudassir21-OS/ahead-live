"""Phase 2a: sign-in that belongs to one browser tab, the email outbox and the account emails."""
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from unittest import mock

from streamlit.testing.v1 import AppTest

from ahead import accounts, mailer, session
from ahead.storage import authenticate, connection, create_user, init_db

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
PW = "safe-password-123"
TAB = {"id": "0123456789abcdef", "secret": "S" * 43}
OTHER_TAB = {"id": "fedcba9876543210", "secret": "T" * 43}
HEALTH_WORDS = re.compile(r"diabet|heart|kidney|cardio|glucose|cholesterol|blood|risk|score|medic", re.IGNORECASE)


class Base(unittest.TestCase):
    email_backend = "memory"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ["AHEAD_DB_PATH"] = self.temp.name + "/app.db"
        os.environ["AHEAD_PUBLIC_URL"] = "https://ahead.test"
        if self.email_backend:
            os.environ["AHEAD_EMAIL_BACKEND"] = self.email_backend
        else:
            os.environ.pop("AHEAD_EMAIL_BACKEND", None)
        os.environ.pop("BREVO_API_KEY", None)
        mailer.sent_messages.clear()
        init_db()
        create_user("p@test.com", PW, "Patient", "Pat Test")
        create_user("a@test.com", PW, "Admin", "Ada Admin")
        self.p = authenticate("p@test.com", PW, "Patient")
        self.a = authenticate("a@test.com", PW, "Admin")

    def tearDown(self):
        self.temp.cleanup()
        for name in ("AHEAD_DB_PATH", "AHEAD_PUBLIC_URL", "AHEAD_EMAIL_BACKEND", "AHEAD_EMAIL_DAILY_LIMIT", "BREVO_API_KEY"):
            os.environ.pop(name, None)
        mailer.sent_messages.clear()

    def shift(self, table, column, delta):
        from datetime import datetime
        with connection() as conn:
            for row in conn.execute(f"SELECT id,{column} FROM {table}").fetchall():
                if row[column]:
                    moved = datetime.fromisoformat(row[column]) - delta
                    conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (moved.isoformat(), row["id"]))

    def mails(self, kind=None, to=None):
        return [m for m in mailer.sent_messages if (kind is None or m["kind"] == kind) and (to is None or m["to"] == to)]

    def link(self, message, param):
        return re.search(rf"\?{param}=([A-Za-z0-9_\-]+)", message["text"]).group(1)

    def app(self, tab=TAB, **state):
        at = AppTest.from_file(APP, default_timeout=120)
        if tab is not None:
            at.session_state[session.TAB_KEY] = dict(tab)
        for key, value in state.items():
            at.session_state[key] = value
        return at

    def sign_in(self, at, email, password=PW, account_type=None):
        if account_type:
            at.segmented_control(key="login_account_type").set_value(account_type)
        at.text_input(key="login_email").set_value(email)
        at.text_input(key="login_password").set_value(password)
        next(b for b in at.button if b.label == "Sign In").click().run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        return at


# =============================================================================
# PER-TAB SIGN-IN
# =============================================================================

class TabSessionTests(Base):
    def open_tab_session(self, tab=TAB, user=None):
        session_id, code = accounts.start_session((user or self.p)["id"], tab_secret=tab["secret"])
        token, _ = accounts.complete_handoff(code)
        return session_id, {accounts.cookie_name(tab["id"]): token}

    def test_a_reload_of_the_same_tab_resumes(self):
        session_id, cookies = self.open_tab_session()
        self.assertEqual(session.resume(TAB, cookies)[0], session_id)

    def test_a_new_tab_or_a_reopened_link_does_not(self):
        _, cookies = self.open_tab_session()
        self.assertIsNone(session.resume(OTHER_TAB, cookies))                      # new tab: new identity, no cookie
        stolen = {accounts.cookie_name(OTHER_TAB["id"]): next(iter(cookies.values()))}
        self.assertIsNone(session.resume(OTHER_TAB, stolen))                       # the cookie without the tab's secret
        self.assertIsNone(session.resume({"id": TAB["id"], "secret": "W" * 43}, cookies))
        self.assertIsNone(session.resume(None, cookies))
        self.assertIsNone(session.resume({"id": "", "secret": ""}, cookies))       # a browser without sessionStorage

    def test_two_tabs_have_separate_sessions(self):
        first, cookies_a = self.open_tab_session(TAB)
        second, cookies_b = self.open_tab_session(OTHER_TAB)
        jar = {**cookies_a, **cookies_b}
        accounts.end_session(first)                                # sign out in the first tab
        self.assertIsNone(session.resume(TAB, jar))
        self.assertEqual(session.resume(OTHER_TAB, jar)[0], second)
        accounts.sign_out_everywhere(self.p["id"])                 # "Sign out on all my devices"
        self.assertIsNone(session.resume(OTHER_TAB, jar))

    def test_sessions_from_before_the_tab_change_cannot_resume(self):
        session_id, code = accounts.start_session(self.p["id"])     # Phase 1 style: no tab secret
        token, _ = accounts.complete_handoff(code)
        self.assertIsNone(session.resume(TAB, {accounts.cookie_name(TAB["id"]): token}))
        self.assertIsNotNone(accounts.check_session(session_id))   # still fine in the tab that is open

    def test_idle_limit_still_applies(self):
        _, cookies = self.open_tab_session()
        self.shift("sessions", "last_seen_at", timedelta(minutes=accounts.IDLE_MINUTES + 1))
        self.assertIsNone(session.resume(TAB, cookies))

    def test_only_hashes_are_stored(self):
        _, cookies = self.open_tab_session()
        with connection() as conn:
            stored = str([dict(r) for r in conn.execute("SELECT * FROM sessions")])
        self.assertNotIn(TAB["secret"], stored)
        self.assertNotIn(next(iter(cookies.values())), stored)

    def test_tab_ids_are_checked(self):
        self.assertTrue(accounts.valid_tab_id("0123456789abcdef"))
        for bad in ("", "0123", "0123456789ABCDEF", "../../etc/passwd", None, 5, "0123456789abcdeg"):
            self.assertFalse(accounts.valid_tab_id(bad), bad)


class TabAppTests(Base):
    def test_sign_in_hands_this_tabs_id_to_the_cookie_script(self):
        at = self.sign_in(self.app().run(), "p@test.com")
        self.assertTrue(at.session_state["authenticated"])
        with connection() as conn:
            row = conn.execute("SELECT tab_hash FROM sessions WHERE id=?", (at.session_state["session_id"],)).fetchone()
        self.assertEqual(row["tab_hash"], accounts._hash(TAB["secret"]))
        scripts = " ".join(str(h.proto) for h in at.get("html"))
        self.assertIn(TAB["id"], scripts)   # the POST to /auth/session names this tab

    def test_a_reloaded_tab_is_signed_back_in(self):
        session_id, code = accounts.start_session(self.p["id"], tab_secret=TAB["secret"])
        token, _ = accounts.complete_handoff(code)
        with mock.patch.object(session, "_cookies", return_value={accounts.cookie_name(TAB["id"]): token}):
            at = self.app().run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertTrue(at.session_state["authenticated"])
        self.assertEqual(at.session_state["session_id"], session_id)
        self.assertIn("Log Out", [b.label for b in at.button])

    def test_a_new_tab_with_the_same_browser_cookies_gets_the_login(self):
        _, code = accounts.start_session(self.p["id"], tab_secret=TAB["secret"])
        token, _ = accounts.complete_handoff(code)
        with mock.patch.object(session, "_cookies", return_value={accounts.cookie_name(TAB["id"]): token}):
            at = self.app(tab=OTHER_TAB).run()
        self.assertFalse(at.session_state["authenticated"])
        self.assertIn("Sign In", [b.label for b in at.button])

    def test_waits_for_the_tab_before_choosing_login_or_dashboard(self):
        with mock.patch.object(session, "_cookies", return_value={"ahead_s_0123456789abcdef": "x"}):
            at = self.app(tab=None).run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertNotIn("Sign In", [b.label for b in at.button])
        self.assertIn("Show the sign-in page", [b.label for b in at.button])
        with mock.patch.object(session, "_cookies", return_value={"ahead_s_0123456789abcdef": "x"}):
            next(b for b in at.button if b.label == "Show the sign-in page").click().run()
        self.assertIn("Sign In", [b.label for b in at.button])

    def test_no_cookie_means_no_wait(self):
        at = self.app(tab=None).run()
        self.assertIn("Sign In", [b.label for b in at.button])

    def test_signing_out_keeps_the_tab_identity_and_clears_only_this_tabs_cookie(self):
        at = self.sign_in(self.app().run(), "p@test.com")
        next(b for b in at.button if b.label == "Log Out").click().run()
        self.assertFalse(at.session_state["authenticated"])
        self.assertEqual(at.session_state[session.TAB_KEY]["id"], TAB["id"])
        scripts = " ".join(str(h.proto) for h in at.get("html"))
        self.assertIn("/auth/logout", scripts)
        self.assertIn(TAB["id"], scripts)


# =============================================================================
# THE OUTBOX
# =============================================================================

class OutboxTests(Base):
    def test_off_without_a_key(self):
        os.environ.pop("AHEAD_EMAIL_BACKEND")
        self.assertFalse(mailer.enabled())
        self.assertIsNone(mailer.queue("x@test.com", "reset", link="https://ahead.test/?reset=abc"))
        os.environ["BREVO_API_KEY"] = "xkeysib-test"
        self.assertTrue(mailer.enabled())
        self.assertEqual(mailer.backend(), "brevo")

    def test_sent_email_bodies_are_blanked(self):
        mailer.queue("x@test.com", "reset", link="https://ahead.test/?reset=SECRET-LINK-TOKEN")
        self.assertEqual(len(self.mails("reset")), 1)
        with connection() as conn:
            row = dict(conn.execute("SELECT * FROM email_outbox").fetchone())
        self.assertEqual((row["status"], row["body_text"], row["body_html"]), ("sent", "", ""))
        self.assertNotIn("SECRET-LINK-TOKEN", str(row))

    def test_per_recipient_cap(self):
        for _ in range(mailer.PER_RECIPIENT_HOUR):
            self.assertIsNotNone(mailer.queue("busy@test.com", "password_changed"))
        self.assertIsNone(mailer.queue("busy@test.com", "password_changed"))
        self.assertIsNotNone(mailer.queue("other@test.com", "password_changed"))

    def test_daily_budget_keeps_a_reserve_for_account_emails(self):
        os.environ["AHEAD_EMAIL_DAILY_LIMIT"] = str(mailer.ACCOUNT_RESERVE + 2)
        with mock.patch.object(mailer, "drain"):  # queue everything first, then send in one go
            for i in range(3):
                mailer.queue(f"n{i}@test.com", "password_changed")
            mailer.queue("acct@test.com", "verify", link="https://ahead.test/?verify=x")
        mailer.drain()
        self.assertEqual([m["kind"] for m in mailer.sent_messages], ["verify", "password_changed"])
        with connection() as conn:
            waiting = conn.execute("SELECT COUNT(*) FROM email_outbox WHERE status='pending'").fetchone()[0]
        self.assertEqual(waiting, 2)   # sent tomorrow

    def test_retries_then_gives_up(self):
        with mock.patch.object(mailer, "drain"):
            email_id = mailer.queue("x@test.com", "password_changed")
        with mock.patch.object(mailer, "_deliver", return_value=(False, "HTTP 503", True)):
            for attempt in range(mailer.MAX_ATTEMPTS):
                self.shift("email_outbox", "next_attempt_at", timedelta(days=1))
                mailer.drain()
        with connection() as conn:
            row = conn.execute("SELECT status,attempts,body_text FROM email_outbox WHERE id=?", (email_id,)).fetchone()
        self.assertEqual((row["status"], row["attempts"], row["body_text"]), ("failed", mailer.MAX_ATTEMPTS, ""))

    def test_a_rejected_email_is_not_retried(self):
        with mock.patch.object(mailer, "drain"):
            email_id = mailer.queue("x@test.com", "password_changed")
        with mock.patch.object(mailer, "_deliver", return_value=(False, "HTTP 400: bad sender", False)):
            mailer.drain()
        with connection() as conn:
            self.assertEqual(conn.execute("SELECT status FROM email_outbox WHERE id=?", (email_id,)).fetchone()[0], "failed")

    def test_brevo_request(self):
        os.environ["AHEAD_EMAIL_BACKEND"] = "brevo"
        os.environ["BREVO_API_KEY"] = "xkeysib-test"
        seen = {}

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout):
            seen.update(url=request.full_url, key=request.get_header("Api-key"), body=json.loads(request.data))
            return Reply(b'{"messageId":"<abc@brevo>"}')

        with mock.patch.object(mailer.urllib.request, "urlopen", fake_urlopen):
            mailer.queue("x@test.com", "reset", name="Pat", link="https://ahead.test/?reset=t")
            self.assertEqual(mailer.drain(), 1)
        self.assertEqual(seen["url"], mailer.BREVO_URL)
        self.assertEqual(seen["key"], "xkeysib-test")
        self.assertEqual(seen["body"]["to"], [{"email": "x@test.com"}])
        self.assertEqual(seen["body"]["sender"]["email"], "aurak.events.noreply@gmail.com")
        self.assertIn("https://ahead.test/?reset=t", seen["body"]["htmlContent"])

        def refused(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"code":"unauthorized"}'))
        with mock.patch.object(mailer.urllib.request, "urlopen", refused):
            ok, detail, retry = mailer._deliver({"to_email": "x@test.com", "kind": "reset", "subject": "s",
                                                 "body_text": "t", "body_html": "h"})
        self.assertEqual((ok, retry), (False, False))
        self.assertIn("401", detail)

    def test_no_email_carries_health_information(self):
        for kind in ("verify", "reset", "setup", "password_changed", "email_changed", "deactivated", "reactivated"):
            subject, text, html = mailer.compose(kind, "Pat", "https://ahead.test/?x=1", "https://ahead.test")
            for part in (subject, text, re.sub(r"<[^>]+>", " ", html)):
                self.assertIsNone(HEALTH_WORDS.search(part.replace("not a medical device", "")), (kind, part))


# =============================================================================
# ACCOUNT EMAILS
# =============================================================================

class SignUpTests(Base):
    def register(self, email="new@test.com"):
        at = self.app().run()
        boxes = {t.label: t for t in at.text_input}
        boxes["Your full name"].set_value("New Person")
        boxes["Your email"].set_value(email)
        next(t for t in at.text_input if t.label.startswith("Choose a password")).set_value(PW)
        boxes["Type the password again"].set_value(PW)
        next(b for b in at.button if b.label == "Create account").click().run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        return at

    def test_sign_up_confirm_then_sign_in(self):
        at = self.register()
        self.assertTrue(any("emailed a confirmation link" in s.value for s in at.success))
        message = self.mails("verify", "new@test.com")[0]
        self.assertIn("https://ahead.test/?verify=", message["text"])

        at = self.sign_in(self.app().run(), "new@test.com")
        self.assertFalse(at.session_state["authenticated"])
        self.assertIn("Confirm your email", at.error[0].value)

        page = self.app()
        page.query_params["verify"] = self.link(message, "verify")
        page.run()
        self.assertFalse(page.exception, [e.message for e in page.exception])
        self.assertTrue(any("Email confirmed" in str(h.proto) for h in page.get("html")))
        page.run()   # a rerun of the same page must still say confirmed
        self.assertTrue(any("Email confirmed" in str(h.proto) for h in page.get("html")))

        at = self.sign_in(self.app().run(), "new@test.com")
        self.assertTrue(at.session_state["authenticated"])
        self.assertIn("email_verified", [e["action"] for e in accounts.audit_entries()])

    def test_used_or_late_confirmation_links_fail(self):
        self.register()
        token = self.link(self.mails("verify")[0], "verify")
        self.assertIsNotNone(accounts.verify_email(token))
        self.assertIsNone(accounts.verify_email(token))
        self.assertIsNone(accounts.verify_email("made-up"))

    def test_resend_is_limited(self):
        self.register()
        user_id = next(r["id"] for r in accounts.list_accounts() if r["email"] == "new@test.com")
        results = [accounts.send_verification(user_id, "10.0.0.9") for _ in range(4)]
        self.assertEqual(results, [True, True, False, False])   # the sign-up email was the first of three
        self.assertEqual(len(self.mails("verify")), 3)

    def test_resend_button_on_the_sign_in_card(self):
        self.register()
        at = self.sign_in(self.app().run(), "new@test.com")
        next(b for b in at.button if b.label == "Email me a new confirmation link").click().run()
        self.assertTrue(any("Sent to new@test.com" in s.value for s in at.success))
        self.assertEqual(len(self.mails("verify")), 2)

    def test_existing_accounts_count_as_confirmed(self):
        self.assertTrue(accounts.is_verified(self.p["id"]))
        self.assertTrue(self.sign_in(self.app().run(), "p@test.com").session_state["authenticated"])


class EmailsOffTests(Base):
    email_backend = None

    def test_without_emails_sign_up_works_as_before(self):
        at = SignUpTests.register(self)
        self.assertTrue(any("Sign in above" in s.value for s in at.success))
        self.assertTrue(self.sign_in(self.app().run(), "new@test.com").session_state["authenticated"])
        self.assertEqual(mailer.sent_messages, [])

    def test_forgot_password_falls_back_to_the_team(self):
        at = self.app().run()
        self.assertNotIn("Forgot your password?", [e.label for e in at.expander])
        self.assertTrue(any("Ask the AHEAD team" in str(h.proto) for h in at.get("html")))


class ResetTests(Base):
    def test_forgot_password_end_to_end(self):
        at = self.app().run()
        self.assertIn("Forgot your password?", [e.label for e in at.expander])
        at.text_input(key="forgot_email").set_value("P@Test.com ")
        next(b for b in at.button if b.label == "Email me a reset link").click().run()
        self.assertTrue(any("If an AHEAD account uses that email" in s.value for s in at.success))
        token = self.link(self.mails("reset", "p@test.com")[0], "reset")

        page = self.app()
        page.query_params["reset"] = token
        page.run()
        boxes = [t for t in page.text_input if t.label.startswith("New password") or t.label == "Type it again"]
        for box in boxes:
            box.set_value("a-brand-new-password")
        next(b for b in page.button if b.label == "Save password").click().run()
        self.assertIsNotNone(authenticate("p@test.com", "a-brand-new-password", "Patient"))
        self.assertEqual(len(self.mails("password_changed", "p@test.com")), 1)

    def test_unknown_and_switched_off_accounts_get_nothing_but_the_same_answer(self):
        accounts.request_password_reset("nobody@test.com", "10.0.0.1")
        accounts.set_active(self.a["id"], self.p["id"], False)
        mailer.sent_messages.clear()
        accounts.request_password_reset("p@test.com", "10.0.0.1")
        self.assertEqual(mailer.sent_messages, [])

    def test_reset_requests_are_limited(self):
        for _ in range(5):
            accounts.request_password_reset("p@test.com", "10.0.0.1")
        self.assertEqual(len(self.mails("reset")), accounts.EMAILS_PER_ACCOUNT)
        for i in range(20):                      # one network trying many addresses
            accounts.request_password_reset(f"x{i}@test.com", "10.0.0.2")
        with connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM rate_events WHERE kind='email_request_ip' AND subject='10.0.0.2'").fetchone()[0]
        self.assertEqual(count, accounts.EMAILS_PER_ADDRESS)

    def test_only_the_newest_reset_link_works(self):
        accounts.request_password_reset("p@test.com")
        accounts.request_password_reset("p@test.com")
        first, second = (self.link(m, "reset") for m in self.mails("reset"))
        self.assertIsNone(accounts.link_account(first))
        self.assertIsNotNone(accounts.link_account(second))

    def test_a_confirmation_link_cannot_set_a_password(self):
        user_id = next(r["id"] for r in accounts.list_accounts() if r["email"] == "p@test.com")
        token = accounts.issue_link(user_id, "verify")
        self.assertIsNone(accounts.link_account(token))
        self.assertIsNone(accounts.use_link(token, "sneaky-new-password"))


class StaffEmailTests(Base):
    def test_new_doctor_gets_the_set_up_email_and_is_confirmed_by_using_it(self):
        user_id, token = accounts.create_staff_account(self.a["id"], "doc@test.com", "Dr Doc", "Doctor")
        message = self.mails("setup", "doc@test.com")[0]
        self.assertEqual(self.link(message, "setup"), token)
        self.assertFalse(accounts.is_verified(user_id))
        accounts.use_link(token, "doctor-password-1")
        self.assertTrue(accounts.is_verified(user_id))
        self.assertIsNotNone(authenticate("doc@test.com", "doctor-password-1", "Doctor"))

    def test_switching_an_account_off_and_on_emails_the_person(self):
        accounts.set_active(self.a["id"], self.p["id"], False)
        accounts.set_active(self.a["id"], self.p["id"], False)   # already off: no second email
        accounts.set_active(self.a["id"], self.p["id"], True)
        self.assertEqual([m["kind"] for m in self.mails(to="p@test.com")], ["deactivated", "reactivated"])

    def test_admin_reset_link_is_emailed_too(self):
        token = accounts.admin_reset_link(self.a["id"], self.p["id"])
        self.assertEqual(self.link(self.mails("reset", "p@test.com")[0], "reset"), token)

    def test_admin_emails_tab(self):
        accounts.create_staff_account(self.a["id"], "doc@test.com", "Dr Doc", "Doctor")
        at = self.app(authenticated=True, user=self.a, nav="admin", admin_tabs="Emails").run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        table = at.dataframe[0].value
        self.assertEqual(list(table["To"]), ["doc@test.com"])
        self.assertNotIn("setup=", table.to_string())


if __name__ == "__main__":
    unittest.main()
