"""Accounts, sessions that survive a reload, sign-in limits, one-time links, the admin page and the HTTP routes."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from streamlit.testing.v1 import AppTest

from ahead import accounts, db
from ahead.storage import authenticate, change_password, connection, create_user, init_db

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
PW = "safe-password-123"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ["AHEAD_DB_PATH"] = self.temp.name + "/app.db"
        init_db()
        for email, role in [("p@test.com", "Patient"), ("d@test.com", "Doctor"), ("a@test.com", "Admin"),
                            ("a2@test.com", "Admin")]:
            create_user(email, PW, role, email.split("@")[0].upper())
        self.p = authenticate("p@test.com", PW, "Patient")
        self.d = authenticate("d@test.com", PW, "Doctor")
        self.a = authenticate("a@test.com", PW, "Admin")
        self.a2 = authenticate("a2@test.com", PW, "Admin")

    def tearDown(self):
        self.temp.cleanup()
        os.environ.pop("AHEAD_DB_PATH", None)
        os.environ.pop("AHEAD_TRUST_PROXY", None)

    def session(self, user):
        session_id, code = accounts.start_session(user["id"], "test-agent", "10.0.0.1")
        token, _ = accounts.complete_handoff(code)
        return session_id, token

    def shift(self, table, column, delta, where="1=1", args=()):
        """Move stored timestamps back in time."""
        with connection() as conn:
            for row in conn.execute(f"SELECT id,{column} FROM {table} WHERE {where}", args).fetchall():
                if row[column]:
                    moved = datetime.fromisoformat(row[column]) - delta
                    conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (moved.isoformat(), row["id"]))


class SignInTests(Base):
    def test_roles_and_deactivated_accounts(self):
        self.assertIsNone(authenticate("p@test.com", PW, "Doctor"))
        self.assertEqual(authenticate("d@test.com", PW, ("Doctor", "Admin"))["role"], "Doctor")
        self.assertEqual(authenticate("a@test.com", PW, ("Doctor", "Admin"))["role"], "Admin")
        self.assertIsNone(authenticate("nobody@test.com", PW, "Patient"))
        accounts.set_active(self.a["id"], self.p["id"], False)
        self.assertIsNone(authenticate("p@test.com", PW, "Patient"))
        accounts.set_active(self.a["id"], self.p["id"], True)
        self.assertIsNotNone(authenticate("p@test.com", PW, "Patient"))

    def test_short_passwords_and_unknown_roles_are_refused(self):
        with self.assertRaises(ValueError):
            create_user("x@test.com", "short", "Patient", "X")
        with self.assertRaises(ValueError):
            create_user("x@test.com", PW, "Superuser", "X")
        self.assertFalse(create_user("P@Test.com ", PW, "Patient", "Dup"))  # same email, any case

    def test_failed_sign_ins_block_that_account_from_that_address_only(self):
        for _ in range(accounts.FAILS_PER_ACCOUNT - 1):
            accounts.record_failed_sign_in("p@test.com", "10.0.0.1")
        self.assertFalse(accounts.sign_in_blocked("p@test.com", "10.0.0.1"))
        accounts.record_failed_sign_in("P@test.com", "10.0.0.1")
        self.assertTrue(accounts.sign_in_blocked("p@test.com", "10.0.0.1"))
        self.assertFalse(accounts.sign_in_blocked("p@test.com", "10.0.0.2"))   # someone else on another network
        self.assertFalse(accounts.sign_in_blocked("d@test.com", "10.0.0.1"))   # a classmate on the same Wi-Fi
        self.assertEqual([e["action"] for e in accounts.audit_entries(action="sign_in_blocked")], ["sign_in_blocked"])
        self.assertNotIn("p@test.com", str(accounts.audit_entries()))          # the email is partly hidden
        self.shift("rate_events", "created_at", accounts.FAIL_WINDOW)
        self.assertFalse(accounts.sign_in_blocked("p@test.com", "10.0.0.1"))

    def test_one_address_with_many_failures_is_blocked(self):
        for i in range(accounts.FAILS_PER_ADDRESS):
            accounts.record_failed_sign_in(f"user{i}@test.com", "10.9.9.9")
        self.assertTrue(accounts.sign_in_blocked("fresh@test.com", "10.9.9.9"))

    def test_sign_up_limit_per_address(self):
        for _ in range(accounts.SIGNUPS_PER_ADDRESS):
            self.assertTrue(accounts.sign_up_allowed("10.1.1.1"))
            accounts.record_sign_up("10.1.1.1")
        self.assertFalse(accounts.sign_up_allowed("10.1.1.1"))
        self.assertTrue(accounts.sign_up_allowed("10.1.1.2"))

    def test_client_address_trusts_only_configured_proxies(self):
        headers = {"X-Forwarded-For": "6.6.6.6, 203.0.113.7, 10.0.0.2"}
        self.assertEqual(accounts.client_ip(headers, "10.0.0.3"), "10.0.0.3")  # default: header ignored
        os.environ["AHEAD_TRUST_PROXY"] = "2"
        self.assertEqual(accounts.client_ip(headers, "10.0.0.3"), "203.0.113.7")  # a faked first entry is skipped
        os.environ["AHEAD_TRUST_PROXY"] = "9"
        self.assertEqual(accounts.client_ip(headers, "10.0.0.3"), "6.6.6.6")


class SessionTests(Base):
    def test_handoff_code_works_once_and_expires(self):
        _, code = accounts.start_session(self.p["id"])
        token, max_age = accounts.complete_handoff(code)
        self.assertTrue(token and 0 < max_age <= accounts.MAX_HOURS * 3600)
        self.assertIsNone(accounts.complete_handoff(code))
        _, late = accounts.start_session(self.p["id"])
        self.shift("sessions", "handoff_expires_at", timedelta(seconds=accounts.HANDOFF_SECONDS + 1))
        self.assertIsNone(accounts.complete_handoff(late))
        self.assertIsNone(accounts.complete_handoff(""))

    def test_only_a_hash_of_the_token_is_stored(self):
        _, token = self.session(self.p)
        with connection() as conn:
            stored = [tuple(r[k] for k in ("token_hash", "handoff_hash")) for r in conn.execute("SELECT * FROM sessions")]
        self.assertNotIn(token, str(stored))

    def test_valid_session_and_every_way_it_ends(self):
        session_id, token = self.session(self.p)
        self.assertEqual(accounts.session_user(token)[1]["email"], "p@test.com")
        self.assertEqual(accounts.check_session(session_id)["id"], self.p["id"])
        # deactivation signs out at once, and reactivation doesn't bring old sessions back
        accounts.set_active(self.a["id"], self.p["id"], False)
        self.assertIsNone(accounts.session_user(token))
        accounts.set_active(self.a["id"], self.p["id"], True)
        self.assertIsNone(accounts.session_user(token))
        # sign out one browser
        session_id, token = self.session(self.p)
        accounts.end_session(session_id)
        self.assertIsNone(accounts.check_session(session_id))
        # idle too long
        session_id, token = self.session(self.p)
        self.shift("sessions", "last_seen_at", timedelta(minutes=accounts.IDLE_MINUTES + 1), "id=?", (session_id,))
        self.assertIsNone(accounts.session_user(token))
        # past the maximum lifetime
        session_id, token = self.session(self.p)
        self.shift("sessions", "expires_at", timedelta(hours=accounts.MAX_HOURS), "id=?", (session_id,))
        self.assertIsNone(accounts.check_session(session_id))
        self.assertIsNone(accounts.session_user("not-a-token"))

    def test_activity_keeps_a_session_alive(self):
        session_id, token = self.session(self.p)
        self.shift("sessions", "last_seen_at", timedelta(minutes=accounts.IDLE_MINUTES - 10), "id=?", (session_id,))
        self.assertIsNotNone(accounts.session_user(token))  # refreshes last_seen_at
        self.shift("sessions", "last_seen_at", timedelta(minutes=30), "id=?", (session_id,))
        self.assertIsNotNone(accounts.session_user(token))

    def test_password_change_signs_out_other_browsers_only(self):
        here, here_token = self.session(self.p)
        there, there_token = self.session(self.p)
        self.assertTrue(change_password(self.p["id"], PW, "another-password-456"))
        accounts.keep_session_after_password_change(here, self.p["id"])
        self.assertIsNotNone(accounts.session_user(here_token))
        self.assertIsNone(accounts.session_user(there_token))
        self.assertFalse(change_password(self.p["id"], "wrong-password!!", "x" * 12))

    def test_sign_out_everywhere(self):
        _, one = self.session(self.d)
        _, two = self.session(self.d)
        accounts.sign_out_everywhere(self.d["id"])
        self.assertIsNone(accounts.session_user(one))
        self.assertIsNone(accounts.session_user(two))


class LinkAndAdminTests(Base):
    def test_admin_adds_a_doctor_who_sets_their_own_password_once(self):
        user_id, token = accounts.create_staff_account(self.a["id"], "New.Doc@Clinic.ae", "Dr New", "Doctor", "Clinic")
        self.assertIsNone(authenticate("new.doc@clinic.ae", PW, "Doctor"))  # nobody knows the random password
        self.assertEqual(accounts.link_account(token)["purpose"], "setup")
        with self.assertRaises(ValueError):
            accounts.use_link(token, "short")
        self.assertEqual(accounts.use_link(token, "doctor-chosen-pass", "10.0.0.1")["id"], user_id)
        self.assertEqual(authenticate("new.doc@clinic.ae", "doctor-chosen-pass", "Doctor")["id"], user_id)
        self.assertIsNone(accounts.use_link(token, "second-try-password"))
        self.assertIsNone(accounts.link_account(token))
        actions = [e["action"] for e in accounts.audit_entries()]
        self.assertIn("account_created", actions)
        self.assertIn("password_set_from_link", actions)

    def test_staff_account_rules(self):
        with self.assertRaises(ValueError):
            accounts.create_staff_account(self.a["id"], "x@test.com", "X", "Patient")
        with self.assertRaises(ValueError):
            accounts.create_staff_account(self.a["id"], "d@test.com", "Dup", "Doctor")
        with self.assertRaises(ValueError):
            accounts.create_staff_account(self.a["id"], "not-an-email", "X", "Doctor")

    def test_links_expire_and_new_ones_replace_old(self):
        token = accounts.admin_reset_link(self.a["id"], self.p["id"])
        newer = accounts.admin_reset_link(self.a["id"], self.p["id"])
        self.assertIsNone(accounts.link_account(token))
        self.assertEqual(accounts.link_account(newer)["user"]["id"], self.p["id"])
        self.shift("account_tokens", "expires_at", timedelta(minutes=accounts.RESET_MINUTES + 1))
        self.assertIsNone(accounts.link_account(newer))
        self.assertIsNone(accounts.use_link(newer, "brand-new-password"))

    def test_a_reset_link_signs_out_everywhere_and_dies_with_deactivation(self):
        _, token = self.session(self.p)
        link = accounts.admin_reset_link(self.a["id"], self.p["id"])
        accounts.use_link(link, "brand-new-password")
        self.assertIsNone(accounts.session_user(token))
        link = accounts.admin_reset_link(self.a["id"], self.p["id"])
        accounts.set_active(self.a["id"], self.p["id"], False)
        self.assertIsNone(accounts.link_account(link))
        with self.assertRaises(ValueError):
            accounts.admin_reset_link(self.a["id"], self.p["id"])

    def test_admins_cant_change_themselves_or_remove_the_last_admin(self):
        with self.assertRaises(ValueError):
            accounts.set_active(self.a["id"], self.a["id"], False)
        accounts.set_active(self.a["id"], self.a2["id"], False)
        with self.assertRaises(ValueError):  # a2 is gone, so `a` is the last active admin
            accounts.set_active(self.a2["id"], self.a["id"], False)
        counts = accounts.account_counts()
        self.assertEqual((counts["Admin"], counts["inactive"]), (1, 1))

    def test_admin_listing_holds_no_health_data(self):
        row = accounts.list_accounts()[0]
        self.assertEqual(set(row), {"id", "email", "role", "name", "institution", "is_active", "created_at", "last_login_at"})


class TranslationTests(unittest.TestCase):
    def test_placeholders_quotes_and_insert_or_ignore(self):
        sql, table = db.translate("INSERT OR IGNORE INTO care_access(a,b) VALUES(?,?)")
        self.assertEqual((sql, table), ("INSERT INTO care_access(a,b) VALUES(%s,%s) ON CONFLICT DO NOTHING", "care_access"))
        sql, _ = db.translate("SELECT * FROM t WHERE x LIKE '%a?b%' AND y=?")
        self.assertEqual(sql, "SELECT * FROM t WHERE x LIKE '%%a?b%%' AND y=%s")
        self.assertEqual(db.translate("SELECT '50%'", has_params=False)[0], "SELECT '50%'")

    def test_migration_statements_split_outside_quotes_and_comments(self):
        parts = db.split_statements("CREATE TABLE a(x TEXT DEFAULT ';'); -- a; comment\nCREATE INDEX i ON a(x);")
        self.assertEqual(parts, ["CREATE TABLE a(x TEXT DEFAULT ';')", "CREATE INDEX i ON a(x)"])

    def test_every_migration_is_recorded_once(self):
        temp = tempfile.TemporaryDirectory()
        os.environ["AHEAD_DB_PATH"] = temp.name + "/m.db"
        try:
            init_db()
            db.reset_migration_cache()
            init_db()
            with connection() as conn:
                versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
            self.assertEqual(versions, [p.stem for p in db.migration_files()])
        finally:
            temp.cleanup()
            os.environ.pop("AHEAD_DB_PATH", None)


class DemoAccountTests(unittest.TestCase):
    def test_demo_accounts_never_reach_postgres(self):
        temp = tempfile.TemporaryDirectory()
        os.environ["AHEAD_DB_PATH"] = temp.name + "/demo.db"
        try:
            with mock.patch.dict(os.environ, {"AHEAD_ENABLE_DEMO_ACCOUNTS": "1"}):
                init_db()
                if db.is_postgres():
                    self.assertIsNone(authenticate("doctor@ahead.demo", "doctor123", "Doctor"))
                else:  # local classroom runs only
                    self.assertIsNotNone(authenticate("doctor@ahead.demo", "doctor123", "Doctor"))
        finally:
            temp.cleanup()
            os.environ.pop("AHEAD_DB_PATH", None)


class AppPageTests(Base):
    def page(self, user, nav):
        at = AppTest.from_file(APP, default_timeout=120)
        for key, value in {"authenticated": True, "user": user, "nav": nav}.items():
            at.session_state[key] = value
        return at.run()

    def labels(self, at):
        return [b.label for b in at.button if (b.key or "").startswith("nav_")]

    def test_admin_sees_only_accounts_and_security(self):
        at = self.page(self.a, "clinical")
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertEqual(self.labels(at), ["Accounts & Security"])
        self.assertEqual(at.session_state.nav, "admin")
        self.assertEqual([t.label for t in at.tabs], ["Accounts", "Add a doctor or admin", "Audit log", "My password"])

    def test_patients_and_doctors_cant_open_the_admin_page(self):
        for user in (self.p, self.d):
            at = self.page(user, "admin")
            self.assertFalse(at.exception, [e.message for e in at.exception])
            self.assertNotIn("Accounts & Security", self.labels(at))
            self.assertEqual(at.session_state.nav, "overview")

    def test_admin_creates_a_doctor_and_gets_a_one_time_link(self):
        at = AppTest.from_file(APP, default_timeout=120)
        for key, value in {"authenticated": True, "user": self.a, "nav": "admin",
                           "admin_tabs": "Add a doctor or admin"}.items():
            at.session_state[key] = value
        at.run()
        form_inputs = [t for t in at.text_input if t.label in ("Full name", "Email", "Clinic or institution (optional)")]
        values = {"Full name": "Dr Test", "Email": "dr.test@clinic.ae", "Clinic or institution (optional)": "Clinic"}
        for widget in form_inputs:
            widget.set_value(values[widget.label])
        next(b for b in at.button if b.label == "Create account").click().run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertTrue(any("Set-up link created" in s.value for s in at.success))
        self.assertTrue(any("/?setup=" in c.value for c in at.code))
        self.assertEqual(at.session_state["admin_tabs"], "Add a doctor or admin")  # the link stays on screen
        self.assertIsNotNone(next(r for r in accounts.list_accounts("Doctor") if r["email"] == "dr.test@clinic.ae"))

    def test_deactivating_someone_signs_their_open_tab_out(self):
        at = self.page(self.p, "overview")
        self.assertIn("Log Out", [b.label for b in at.button])
        accounts.set_active(self.a["id"], self.p["id"], False)
        at.run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertFalse(at.session_state["authenticated"])
        self.assertTrue(any("signed out" in i.value for i in at.info))

    def test_sign_in_through_the_form_opens_a_cookie_session(self):
        at = AppTest.from_file(APP, default_timeout=120).run()
        at.text_input(key="login_email").set_value("p@test.com")
        at.text_input(key="login_password").set_value(PW)
        next(b for b in at.button if b.label == "Sign In").click().run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        session_id = at.session_state["session_id"]
        self.assertEqual(accounts.check_session(session_id)["id"], self.p["id"])
        next(b for b in at.button if b.label == "Log Out").click().run()
        self.assertIsNone(accounts.check_session(session_id))
        self.assertFalse(at.session_state["authenticated"])

    def test_wrong_passwords_are_limited(self):
        at = AppTest.from_file(APP, default_timeout=120).run()
        for _ in range(accounts.FAILS_PER_ACCOUNT + 1):
            at.text_input(key="login_email").set_value("p@test.com")
            at.text_input(key="login_password").set_value("wrong-password!")
            next(b for b in at.button if b.label == "Sign In").click().run()
        self.assertIn("Too many failed attempts", at.error[0].value)
        at.text_input(key="login_password").set_value(PW)  # even the right password waits now
        next(b for b in at.button if b.label == "Sign In").click().run()
        self.assertFalse(at.session_state["authenticated"])

    def test_setup_link_page(self):
        _, token = accounts.create_staff_account(self.a["id"], "linkdoc@test.com", "Dr Link", "Doctor")
        at = AppTest.from_file(APP, default_timeout=120)
        at.query_params["setup"] = token
        at.run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        boxes = [t for t in at.text_input if t.label.startswith("New password") or t.label == "Type it again"]
        boxes[0].set_value("linkdoc-password")
        boxes[1].set_value("linkdoc-password")
        next(b for b in at.button if b.label == "Save password").click().run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertIsNotNone(authenticate("linkdoc@test.com", "linkdoc-password", "Doctor"))
        expired = AppTest.from_file(APP, default_timeout=120)
        expired.query_params["setup"] = token
        expired.run()
        self.assertTrue(any("expired" in m.value for m in expired.markdown) or
                        any("expired" in str(h.proto) for h in expired.get("html")))


class ServerRouteTests(Base):
    """The plain HTTP routes in server.py, called like a browser would."""

    @classmethod
    def setUpClass(cls):
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Route
        from starlette.testclient import TestClient
        import server
        routes = [Route("/healthz", server.healthz), Route("/auth/session", server.auth_session, methods=["POST"]),
                  Route("/auth/logout", server.auth_logout, methods=["POST"])]
        cls.client = TestClient(Starlette(routes=routes, middleware=[Middleware(server.SecurityHeaders)]),
                                base_url="https://ahead.test")

    def test_health_check(self):
        response = self.client.get("/healthz")
        self.assertEqual((response.status_code, response.json()["database"]), (200, "ok"))
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("max-age", response.headers["strict-transport-security"])

    def test_handoff_sets_an_httponly_strict_cookie_and_logout_clears_it(self):
        session_id, code = accounts.start_session(self.p["id"])
        bad = self.client.post("/auth/session", json={"code": code}, headers={"origin": "https://evil.test"})
        self.assertEqual(bad.status_code, 403)
        ok = self.client.post("/auth/session", json={"code": code}, headers={"origin": "https://ahead.test"})
        self.assertEqual(ok.status_code, 204)
        cookie = ok.headers["set-cookie"].lower()
        for part in ("ahead_session=", "httponly", "samesite=strict", "secure", "path=/"):
            self.assertIn(part, cookie)
        token = ok.cookies.get("ahead_session") or self.client.cookies.get("ahead_session")
        self.assertEqual(accounts.session_user(token)[0], session_id)
        again = self.client.post("/auth/session", json={"code": code}, headers={"origin": "https://ahead.test"})
        self.assertEqual(again.status_code, 400)
        out = self.client.post("/auth/logout", headers={"origin": "https://ahead.test"})
        self.assertEqual(out.status_code, 204)
        self.assertIsNone(accounts.check_session(session_id))
        self.assertIn('ahead_session=""', out.headers["set-cookie"])


if __name__ == "__main__":
    unittest.main()
