"""Phase 3b: encrypted health values, patient consent, data retention and the Content-Security-Policy."""

import os
import re
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from streamlit.testing.v1 import AppTest

from ahead import accounts, consent, db, fieldcrypt, mailer, retention
from ahead.platform_data import (doctors_for, grant_doctor, lifestyle_history, notifications, notify, plans, save_food,
                                 save_lifestyle, save_plan, food_history)
from ahead.storage import connection, create_user, authenticate, load_screenings, save_screening
from tests.test_phase2b_features import PW, Base, plan_values

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "app.py")
KEY_A, KEY_B = fieldcrypt.make_key(), fieldcrypt.make_key()


@contextmanager
def field_key(value):
    """Run with AHEAD_FIELD_KEY set to value (None: unset), then put back whatever the test run had."""
    before = os.environ.get(fieldcrypt.ENV)
    if value is None:
        os.environ.pop(fieldcrypt.ENV, None)
    else:
        os.environ[fieldcrypt.ENV] = value
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(fieldcrypt.ENV, None)
        else:
            os.environ[fieldcrypt.ENV] = before


def raw(sql, params=()):
    with connection(decrypt=False) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def stored_health_values():
    """Every non-empty stored value in the encrypted columns, exactly as stored."""
    values = []
    for table, (_, columns) in fieldcrypt.SENSITIVE.items():
        for column in columns:
            values += [r["v"] for r in raw(f"SELECT {column} v FROM {table}") if r["v"] not in (None, *fieldcrypt.EMPTY)]
    return values


# =============================================================================
# ENCRYPTION: VALUES
# =============================================================================

class ValueTests(unittest.TestCase):
    def test_round_trip_random_nonce_and_pass_through(self):
        with field_key(KEY_A):
            one, two = fieldcrypt.seal("Metformin 500 mg"), fieldcrypt.seal("Metformin 500 mg")
            self.assertTrue(one.startswith("enc1:") and one != two)          # same text, different ciphertext
            self.assertNotIn("Metformin", one)
            self.assertEqual(fieldcrypt.open_text(one), "Metformin 500 mg")
            self.assertEqual(fieldcrypt.seal(one), one)                       # never encrypted twice
            for empty in (None, "", "{}", "[]"):
                self.assertEqual(fieldcrypt.seal(empty), empty)
            self.assertEqual(fieldcrypt.open_text('{"age": 30}'), '{"age": 30}')   # older plain values still read
            self.assertEqual(fieldcrypt.reveal((1, one, None)), (1, "Metformin 500 mg", None))
        with field_key(None):
            self.assertEqual(fieldcrypt.seal("plain"), "plain")
            with self.assertRaises(fieldcrypt.FieldKeyError):
                fieldcrypt.open_text(one)

    def test_bad_keys_and_damaged_values_are_refused(self):
        for bad in ("not base64!!", "c2hvcnQ=", fieldcrypt.make_key()[:20]):
            with field_key(bad), self.assertRaises(fieldcrypt.FieldKeyError):
                fieldcrypt.keys()
        with field_key(KEY_A):
            sealed = fieldcrypt.seal("secret")
            body = sealed[-10:]
            flipped = sealed[:-10] + ("A" if body[0] != "A" else "B") + body[1:]
            with self.assertRaises(fieldcrypt.FieldKeyError):
                fieldcrypt.open_text(flipped)
        with field_key(KEY_B), self.assertRaises(fieldcrypt.FieldKeyError):
            fieldcrypt.open_text(sealed)                                       # another key can't open it

    def test_url_safe_keys_work_too(self):
        url_safe = KEY_A.replace("+", "-").replace("/", "_").rstrip("=")
        with field_key(url_safe):
            sealed = fieldcrypt.seal("x")
        with field_key(KEY_A):
            self.assertEqual(fieldcrypt.open_text(sealed), "x")


# =============================================================================
# ENCRYPTION: IN THE DATABASE
# =============================================================================

class DatabaseEncryptionTests(Base):
    def fill(self):
        """One of everything that holds health values."""
        grant_doctor(self.p["id"], "d@test.com")
        save_plan(self.d["id"], self.p["id"], plan_values(name="Atorvastatin", notes="after dinner"))
        save_screening(self.p["id"], "diabetes", "xgboost", 0.7, 1, 0.5, {"age": 51, "bmi": 33.1})
        save_lifestyle(self.p["id"], {"steps": 4321}, {"overall": 61})
        save_food(self.p["id"], ["Biryani"], {"calories": 820})
        notify(self.p["id"], "test", "Title", "Your Atorvastatin plan changed")

    def test_every_listed_column_exists(self):
        for table, (pk, columns) in fieldcrypt.SENSITIVE.items():
            raw(f"SELECT {pk},{','.join(columns)} FROM {table} LIMIT 1")

    def test_saved_values_are_ciphertext_and_pages_get_plain_values(self):
        with field_key(KEY_A):
            self.fill()
            stored = stored_health_values()
            self.assertTrue(stored and all(v.startswith("enc1:") for v in stored), stored[:3])
            dump = " ".join(stored)
            for secret in ("Atorvastatin", "Biryani", "4321", "33.1", "after dinner"):
                self.assertNotIn(secret, dump)
            self.assertEqual(plans(self.p["id"])[0]["name"], "Atorvastatin")
            self.assertEqual(load_screenings(self.p["id"])[0]["values_json"], '{"age": 51, "bmi": 33.1}')
            self.assertEqual(lifestyle_history(self.p["id"])[0]["values"], {"steps": 4321})
            self.assertEqual(food_history(self.p["id"])[0]["foods"], ["Biryani"])
            self.assertIn("Atorvastatin", notifications(self.p["id"])[0]["description"])
            self.assertEqual(fieldcrypt.pending(), {})

    def test_older_plain_values_are_encrypted_in_the_background(self):
        with field_key(None):
            self.fill()
            self.assertTrue(any("Atorvastatin" in v for v in stored_health_values()))
            self.assertEqual(fieldcrypt.encrypt_existing(), 0)            # no key: nothing to do
        with field_key(KEY_A):
            self.assertTrue(fieldcrypt.pending())
            done = fieldcrypt.encrypt_existing(batch=2)                   # small batches: several rounds
            self.assertGreater(done, 5)
            self.assertEqual(fieldcrypt.pending(), {})
            self.assertTrue(all(v.startswith("enc1:") for v in stored_health_values()))
            self.assertEqual(plans(self.p["id"])[0]["name"], "Atorvastatin")
            self.assertEqual(fieldcrypt.encrypt_existing(), 0)            # safe to run again

    def test_changing_the_key(self):
        with field_key(KEY_A):
            self.fill()
            old_id = fieldcrypt.current_key_id()
        with field_key(f"{KEY_B},{KEY_A}"):                                # new key first, old one after a comma
            self.assertEqual(fieldcrypt.check(), {old_id})
            self.assertEqual(plans(self.p["id"])[0]["name"], "Atorvastatin")
            self.assertTrue(fieldcrypt.pending())
            fieldcrypt.encrypt_existing()
            self.assertEqual(fieldcrypt.stored_key_ids(), {fieldcrypt.current_key_id()})
        with field_key(KEY_B):                                             # the old key can now go
            fieldcrypt.check()
            self.assertEqual(plans(self.p["id"])[0]["name"], "Atorvastatin")

    def test_decrypt_all_for_a_roll_back(self):
        with field_key(f"{KEY_B},{KEY_A}"):
            self.fill()
        with field_key(KEY_A):
            save_food(self.p["id"], ["Older key"], {"calories": 1})
        with field_key(f"{KEY_B},{KEY_A}"):
            self.assertEqual(len(fieldcrypt.stored_key_ids()), 2)
            self.assertGreater(fieldcrypt.decrypt_existing(batch=2), 5)
            self.assertEqual(fieldcrypt.stored_key_ids(), set())
        with field_key(None):
            self.assertEqual(plans(self.p["id"])[0]["name"], "Atorvastatin")
            self.assertTrue(any("Atorvastatin" in v for v in stored_health_values()))
            with field_key(KEY_A):
                fieldcrypt.encrypt_existing()   # the test run's own check expects encrypted values at the end

    def test_start_up_refuses_a_missing_or_wrong_key(self):
        with field_key(None):
            self.assertEqual(fieldcrypt.check(), set())                    # nothing encrypted, no key: fine
        with field_key(KEY_A):
            self.fill()
            self.assertEqual(len(fieldcrypt.check()), 1)
        with field_key(None), self.assertRaisesRegex(fieldcrypt.FieldKeyError, "not set"):
            fieldcrypt.check()
        with field_key(KEY_B), self.assertRaisesRegex(fieldcrypt.FieldKeyError, "Put the old key back"):
            fieldcrypt.check()

    def test_a_value_changed_meanwhile_is_not_overwritten(self):
        with field_key(None):
            self.fill()
        with field_key(KEY_A):
            real_open = fieldcrypt.open_text

            def change_first(value):   # the site saves a new value while the job is between reading and writing
                with connection() as conn:
                    conn.execute("UPDATE medication_plans SET notes=? WHERE patient_id=?",
                                 (fieldcrypt.seal("changed meanwhile"), self.p["id"]))
                return real_open(value)

            with mock.patch.object(fieldcrypt, "open_text", side_effect=change_first):
                fieldcrypt.encrypt_existing()
            fieldcrypt.encrypt_existing()
            self.assertEqual(plans(self.p["id"])[0]["notes"], "changed meanwhile")


# =============================================================================
# CONSENT
# =============================================================================

@pytest.mark.consent_gate
class ConsentTests(Base):
    def test_give_withdraw_and_new_text(self):
        pid = self.p["id"]
        self.assertFalse(consent.has_consent(pid))
        self.assertTrue(consent.give(pid, "10.0.0.1"))
        self.assertFalse(consent.give(pid))                                  # already current: no second row
        self.assertTrue(consent.has_consent(pid))
        self.assertEqual(consent.status_by_user()[pid][0], "given")
        self.assertTrue(consent.withdraw(pid))
        self.assertFalse(consent.has_consent(pid))
        self.assertEqual(consent.status_by_user()[pid][0], "withdrawn")
        consent.give(pid)
        with mock.patch.object(consent, "VERSION", "2099-01"):
            self.assertFalse(consent.has_consent(pid))                       # a new text asks again
            self.assertEqual(consent.status_by_user()[pid][0], "old version")
        actions = [e["action"] for e in accounts.audit_entries(50)]
        self.assertEqual(actions.count("consent_given"), 2)
        self.assertIn("consent_withdrawn", actions)
        self.assertEqual(raw("SELECT ip FROM consents ORDER BY id")[0]["ip"], "10.0.0.1")

    def test_sign_up_needs_the_tick_and_records_it(self):
        from ahead.pages.public import _register
        ok, message = _register("New Person", "new@test.com", PW, PW, agreed=False)
        self.assertFalse(ok)
        self.assertIn("agree", message)
        self.assertIsNone(authenticate("new@test.com", PW, "Patient"))
        ok, _ = _register("New Person", "new@test.com", PW, PW, agreed=True)
        self.assertTrue(ok)
        self.assertTrue(consent.has_consent(authenticate("new@test.com", PW, "Patient")["id"]))


@pytest.mark.consent_gate
class ConsentPageTests(Base):
    def page(self, user, nav="overview"):
        at = AppTest.from_file(APP, default_timeout=120)
        for key, value in {"authenticated": True, "user": user, "nav": nav}.items():
            at.session_state[key] = value
        return at.run()

    def test_patients_agree_before_anything_else(self):
        at = self.page(self.p, "medications")
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertEqual([b.label for b in at.button if (b.key or "").startswith("nav_")], [])   # no menu, no page
        self.assertIn("Agree and continue", [b.label for b in at.button])
        next(b for b in at.button if b.label == "Agree and continue").click().run()
        self.assertTrue(any("Tick the box" in e.value for e in at.error))
        self.assertFalse(consent.has_consent(self.p["id"]))
        at.checkbox[0].check()
        next(b for b in at.button if b.label == "Agree and continue").click().run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertTrue(consent.has_consent(self.p["id"]))
        self.assertIn("nav_medications", [b.key for b in at.button])

    def test_doctors_are_not_asked(self):
        at = self.page(self.d, "intelligence")
        self.assertFalse(at.exception)
        self.assertNotIn("Agree and continue", [b.label for b in at.button])

    def test_withdrawing_in_settings_asks_again(self):
        consent.give(self.p["id"])
        at = self.page(self.p, "settings")
        self.assertFalse(at.exception, [e.message for e in at.exception])
        next(b for b in at.button if b.key == "consent_withdraw").click().run()
        self.assertFalse(consent.has_consent(self.p["id"]))
        self.assertIn("Agree and continue", [b.label for b in at.button])


# =============================================================================
# RETENTION
# =============================================================================

def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class RetentionTests(Base):
    def setUp(self):
        super().setUp()
        create_user("a@test.com", PW, "Admin", "Ada Admin")
        self.a = authenticate("a@test.com", PW, "Admin")

    def test_old_notices_and_email_records_go(self):
        notify(self.p["id"], "t", "Old", "old notice", dedupe_key="old")
        notify(self.p["id"], "t", "New", "new notice", dedupe_key="new")
        mailer.queue("p@test.com", "plan_updated", self.p["id"], "Pat", site="https://ahead.test", detail="medication")
        with connection() as conn:
            conn.execute("UPDATE notifications SET created_at=? WHERE dedupe_key='old'", (_ago(91),))
            conn.execute("UPDATE email_outbox SET status='sent',created_at=?", (_ago(31),))
        done = retention.run(force=True)
        self.assertEqual((done["notices"], done["emails"], done["accounts"]), (1, 1, 0))
        self.assertEqual([n["title"] for n in notifications(self.p["id"])], ["New"])
        self.assertEqual(retention.last_run()["at"], done["at"])
        self.assertIn("skipped", retention.run())                          # ran less than 20 hours ago
        self.assertNotIn("skipped", retention.run(force=True))

    def test_deactivation_dates_and_reactivation(self):
        accounts.set_active(self.a["id"], self.p["id"], False)
        when = raw("SELECT deactivated_at FROM users WHERE id=?", (self.p["id"],))[0]["deactivated_at"]
        self.assertTrue(when)
        self.assertGreater(retention.erase_on(when), datetime.now(timezone.utc) + timedelta(days=364))
        accounts.set_active(self.a["id"], self.p["id"], True)
        self.assertIsNone(raw("SELECT deactivated_at FROM users WHERE id=?", (self.p["id"],))[0]["deactivated_at"])

    def _switched_off_long_ago(self, user_id, days=366):
        accounts.set_active(self.a["id"], user_id, False)
        with connection() as conn:
            conn.execute("UPDATE users SET deactivated_at=? WHERE id=?", (_ago(days), user_id))

    def test_a_patient_switched_off_for_a_year_is_erased_completely(self):
        pid = self.p["id"]
        grant_doctor(pid, "d@test.com")
        save_plan(self.d["id"], pid, plan_values())
        save_screening(pid, "diabetes", "xgboost", 0.7, 1, 0.5, {"age": 51})
        save_lifestyle(pid, {"steps": 1}, {"overall": 50})
        save_food(pid, ["x"], {"calories": 1})
        consent.give(pid)
        create_user("p2@test.com", PW, "Patient", "Still Here")
        p2 = authenticate("p2@test.com", PW, "Patient")
        self._switched_off_long_ago(p2["id"], days=200)                    # not due yet
        self._switched_off_long_ago(pid)
        self.assertEqual(retention.run(force=True)["accounts"], 1)
        self.assertEqual(raw("SELECT id FROM users WHERE id=?", (pid,)), [])
        for table, column in (("medication_plans", "patient_id"), ("screenings", "owner_id"), ("lifestyle", "patient_id"),
                              ("food_entries", "patient_id"), ("notifications", "patient_id"), ("care_access", "patient_id"),
                              ("consents", "user_id"), ("sessions", "user_id"), ("email_outbox", "user_id")):
            self.assertEqual(raw(f"SELECT 1 FROM {table} WHERE {column}=?", (pid,)), [], table)
        self.assertEqual(raw("SELECT 1 FROM email_outbox WHERE to_email='p@test.com'"), [])
        self.assertTrue(raw("SELECT id FROM users WHERE id=?", (p2["id"],)))
        self.assertIn("account_erased", [e["action"] for e in accounts.audit_entries(20)])

    def test_an_erased_doctor_keeps_a_renamed_row_and_their_patients_are_told(self):
        grant_doctor(self.p["id"], "d@test.com")
        save_plan(self.d["id"], self.p["id"], plan_values(name="Lisinopril"))
        self._switched_off_long_ago(self.d["id"])
        retention.run(force=True)
        row = raw("SELECT email,name,erased_at,is_active FROM users WHERE id=?", (self.d["id"],))[0]
        self.assertEqual((row["name"], row["is_active"]), ("Removed account", 0))
        self.assertTrue(row["email"].endswith("@erased.invalid") and row["erased_at"])
        self.assertIsNone(authenticate("d@test.com", PW, ("Doctor",)))
        self.assertEqual(doctors_for(self.p["id"]), [])
        self.assertEqual(plans(self.p["id"])[0]["status"], "stopped")
        self.assertTrue(any("Lisinopril" in n["description"] for n in notifications(self.p["id"])))
        with self.assertRaisesRegex(ValueError, "erased"):
            accounts.set_active(self.a["id"], self.d["id"], True)


class MigrationTests(unittest.TestCase):
    def test_005_dates_accounts_switched_off_before_it(self):
        import shutil
        import tempfile
        from ahead.storage import init_db
        temp = tempfile.TemporaryDirectory()
        old_dir = Path(temp.name) / "old"
        old_dir.mkdir()
        for path in db.migration_files():
            if path.name < "005":
                shutil.copy(path, old_dir / path.name)
        os.environ["AHEAD_DB_PATH"] = temp.name + "/app.db"
        try:
            with mock.patch.object(db, "MIGRATIONS_DIR", old_dir):
                db.reset_migration_cache()
                db.migrate()
            with connection() as conn:
                conn.execute("INSERT INTO users(email,password_hash,role,name,is_active) VALUES('x@t.com','h','Patient','X',0)")
                conn.execute("INSERT INTO users(email,password_hash,role,name,is_active) VALUES('y@t.com','h','Patient','Y',0)")
                conn.execute("INSERT INTO users(email,password_hash,role,name,is_active) VALUES('z@t.com','h','Patient','Z',1)")
                ids = {r["email"]: r["id"] for r in conn.execute("SELECT id,email FROM users").fetchall()}
                conn.execute("INSERT INTO audit_log(action,target_user_id,created_at) VALUES('account_deactivated',?,?)",
                             (ids["x@t.com"], "2026-05-01T10:00:00+00:00"))
            db.reset_migration_cache()
            init_db()
            dates = {r["email"]: r["deactivated_at"] for r in raw("SELECT email,deactivated_at FROM users")}
            self.assertEqual(dates, {"x@t.com": "2026-05-01T10:00:00+00:00", "y@t.com": "2026-11-12T00:00:00+00:00",
                                     "z@t.com": None})
        finally:
            os.environ.pop("AHEAD_DB_PATH", None)
            db.reset_migration_cache()
            temp.cleanup()


# =============================================================================
# CONTENT-SECURITY-POLICY AND NO INLINE SCRIPTS
# =============================================================================

class HeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["AHEAD_WARM_UP"] = "0"
        import server
        cls.server = server

    def directives(self, policy):
        return dict((part.strip().split(" ", 1) + [""])[:2] for part in policy.split(";"))

    def test_policy_is_strict(self):
        policy = self.directives(self.server.content_security_policy(True, "ahead-hepa.onrender.com"))
        self.assertNotIn("'unsafe-inline'", policy["script-src"])
        self.assertNotIn("'unsafe-eval'", policy["script-src"])
        self.assertRegex(policy["script-src"], r"'sha256-[A-Za-z0-9+/=]{44}'")    # Streamlit's one inline line
        self.assertEqual(policy["frame-ancestors"], "'none'")
        self.assertEqual(policy["object-src"], "'none'")
        self.assertEqual(policy["connect-src"], "'self' wss://ahead-hepa.onrender.com")
        self.assertIn("upgrade-insecure-requests", self.server.content_security_policy(True))
        self.assertNotIn("wss://", self.server.content_security_policy(True, "bad host\r\nx"))

    def client(self):
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient
        app = Starlette(routes=[Route("/", lambda request: PlainTextResponse("ok"))],
                        middleware=[Middleware(self.server.SecurityHeaders)])
        return TestClient(app, base_url="https://ahead.test")

    def test_headers_and_the_report_only_switch(self):
        os.environ.pop("AHEAD_CSP", None)
        headers = self.client().get("/", headers={"x-forwarded-proto": "https"}).headers
        self.assertIn("script-src 'self' blob:", headers["content-security-policy"])
        self.assertNotIn("content-security-policy-report-only", headers)
        self.assertEqual(headers["cross-origin-opener-policy"], "same-origin")
        self.assertIn("max-age", headers["strict-transport-security"])
        os.environ["AHEAD_CSP"] = "report"
        try:
            headers = self.client().get("/").headers
            self.assertIn("script-src", headers["content-security-policy-report-only"])
            self.assertEqual(headers["content-security-policy"], "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
        finally:
            os.environ.pop("AHEAD_CSP", None)

    def test_no_inline_scripts_left_in_the_app(self):
        sources = [ROOT / "app.py", *sorted((ROOT / "ahead").rglob("*.py"))]
        for path in sources:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("unsafe_allow_javascript", text, path.name)
            self.assertIsNone(re.search(r"<script[\s>]", text), path.name)

    def test_start_up_warms_the_models_at_once_and_runs_maintenance_later(self):
        import tempfile
        temp = tempfile.TemporaryDirectory()
        env = {"AHEAD_DB_PATH": temp.name + "/app.db", "AHEAD_WARM_UP": "1", "AHEAD_STARTUP_MAINTENANCE": "1"}
        try:
            with mock.patch.dict(os.environ, env), mock.patch.object(self.server.threading, "Thread") as thread:
                self.server._startup()
            targets = [call.kwargs.get("target") for call in thread.call_args_list]
            self.assertIn(self.server._warm_up, targets)
            self.assertIn(self.server._delayed_maintenance, targets)
            with mock.patch.dict(os.environ, {"AHEAD_MAINTENANCE_DELAY": "0"}), \
                    mock.patch.object(self.server, "_maintenance") as maintenance:
                self.server._delayed_maintenance()
            maintenance.assert_called_once_with()
        finally:
            db.reset_migration_cache()
            temp.cleanup()

    def test_the_weekly_task_also_runs_maintenance(self):
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient
        import tempfile
        from ahead.storage import init_db
        temp = tempfile.TemporaryDirectory()
        os.environ.update({"AHEAD_DB_PATH": temp.name + "/app.db", "AHEAD_TASK_KEY": "k" * 40})
        try:
            init_db()
            client = TestClient(Starlette(routes=[Route("/tasks/weekly", self.server.weekly_task, methods=["POST"])]))
            body = client.post("/tasks/weekly", headers={"x-ahead-task-key": "k" * 40}).json()
            self.assertEqual(set(body["maintenance"]), {"encrypted", "retention"})
            self.assertIn("at", body["maintenance"]["retention"])
        finally:
            for name in ("AHEAD_DB_PATH", "AHEAD_TASK_KEY"):
                os.environ.pop(name, None)
            temp.cleanup()
