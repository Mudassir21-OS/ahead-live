"""Phase 3c: fixes from the independent review (security and data protection)."""

import os
import re
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from streamlit.testing.v1 import AppTest

from ahead import accounts, calendar_feed, consent, fieldcrypt, mailer, retention
from ahead.platform_data import grant_doctor, notifications, notify, plans, save_plan
from ahead.storage import authenticate, connection, create_user
from tests.test_phase2b_features import PW, Base, plan_values
from tests.test_phase3b_security import APP, KEY_A, KEY_B, field_key, raw

LOOK_ALIKE = "enc1:deadbeef:AAAA"


def _link(message, param):
    found = re.search(rf"\?{param}=([A-Za-z0-9_-]+)", message["text"])
    return found.group(1) if found else None


# =============================================================================
# TEXT THAT LOOKS ENCRYPTED
# =============================================================================

class LookAlikeTextTests(Base):
    def test_seal_encrypts_look_alikes_and_keeps_real_ciphertext(self):
        with field_key(KEY_A):
            sealed = fieldcrypt.seal(LOOK_ALIKE)
            self.assertNotEqual(sealed, LOOK_ALIKE)
            self.assertEqual(fieldcrypt.open_text(sealed), LOOK_ALIKE)
            real = fieldcrypt.seal("Metformin")
            self.assertEqual(fieldcrypt.seal(real), real)                      # still never encrypted twice
            self.assertEqual(fieldcrypt.reveal((LOOK_ALIKE, real)), (LOOK_ALIKE, "Metformin"))

    def test_a_look_alike_name_breaks_nothing(self):
        with field_key(KEY_A):
            create_user("odd@test.com", PW, "Patient", LOOK_ALIKE)
            user = authenticate("odd@test.com", PW, "Patient")
            self.assertEqual(user["name"], LOOK_ALIKE)
            self.assertIn(LOOK_ALIKE, [a["name"] for a in accounts.list_accounts()])
            fieldcrypt.check()                                                  # start-up still works

    def test_a_look_alike_medicine_is_stored_encrypted_and_start_up_still_works(self):
        with field_key(KEY_A):
            grant_doctor(self.p["id"], "d@test.com")
            save_plan(self.d["id"], self.p["id"], plan_values(name="enc1:abcdefgh:xyz"))
            stored = raw("SELECT name FROM medication_plans")[0]["name"]
            self.assertTrue(stored.startswith(f"enc1:{fieldcrypt.current_key_id()}:"))
            self.assertEqual(plans(self.p["id"])[0]["name"], "enc1:abcdefgh:xyz")
            self.assertEqual(fieldcrypt.check(), {fieldcrypt.current_key_id()})

    @pytest.mark.plain_rows
    def test_older_plain_look_alikes_are_encrypted_by_the_background_job(self):
        with field_key(None):
            notify(self.p["id"], "t", "Title", LOOK_ALIKE, dedupe_key="x")
        with field_key(KEY_A):
            self.assertGreaterEqual(fieldcrypt.encrypt_existing(), 1)
            stored = raw("SELECT description FROM notifications")[0]["description"]
            self.assertTrue(stored.startswith(f"enc1:{fieldcrypt.current_key_id()}:"))
            self.assertEqual(notifications(self.p["id"])[0]["description"], LOOK_ALIKE)

    @pytest.mark.plain_rows
    def test_decrypt_all_finishes_and_leaves_look_alikes(self):
        with field_key(KEY_A):
            for n in range(5):
                notify(self.p["id"], "t", "Title", f"notice {n}", dedupe_key=f"n{n}")
        with connection(decrypt=False) as db:   # an old plain look-alike among them
            db.execute("UPDATE notifications SET description=? WHERE dedupe_key='n2'", (LOOK_ALIKE,))
        with field_key(f"{KEY_B},{KEY_A}"):
            self.assertEqual(fieldcrypt.decrypt_existing(batch=2), 4)
            self.assertEqual(fieldcrypt.remaining_encrypted(), 0)
        self.assertIn(LOOK_ALIKE, [r["description"] for r in raw("SELECT description FROM notifications")])


# =============================================================================
# EMAILED LINKS AND ACCOUNT TAKE-OVER
# =============================================================================

class LinkAddressTests(Base):
    def test_real_emails_never_use_the_browsers_address(self):
        os.environ.pop("AHEAD_PUBLIC_URL", None)
        with mock.patch.object(mailer, "backend", return_value="brevo"):
            self.assertEqual(accounts.site_url(), "")

    def test_server_refuses_to_start_without_the_public_address_when_emails_are_on(self):
        os.environ.pop("AHEAD_PUBLIC_URL", None)
        with mock.patch.object(mailer, "backend", return_value="brevo"):
            self.assertIn("AHEAD_PUBLIC_URL", accounts.public_url_problem())
        self.assertEqual(accounts.public_url_problem(), "")                  # emails off (tests): fine
        server_code = open(os.path.join(os.path.dirname(APP), "server.py")).read()
        self.assertIn("accounts.public_url_problem()", server_code)          # and server.py checks it at start-up


class EmailChangeTests(Base):
    def test_new_address_changes_only_after_confirmation(self):
        self.assertEqual(accounts.request_email_change(self.p["id"], "new@test.com"), "sent")
        self.assertEqual(authenticate("p@test.com", PW, "Patient")["email"], "p@test.com")   # not yet
        mail = self.mails("email_change", "new@test.com")[0]
        self.assert_private(mail)
        token = _link(mail, "verify")
        self.assertIsNotNone(accounts.verify_email(token))
        self.assertIsNone(authenticate("p@test.com", PW, "Patient"))
        self.assertEqual(authenticate("new@test.com", PW, "Patient")["id"], self.p["id"])
        self.assertTrue(self.mails("email_changed", "p@test.com"))          # the old address is told
        self.assertIsNone(accounts.verify_email(token))                     # works once

    def test_taken_address_and_reset_still_go_to_the_confirmed_one(self):
        # Same answer as a free address, and nothing is sent to the account that has it.
        self.assertEqual(accounts.request_email_change(self.p["id"], "d@test.com"), "sent")
        self.assertEqual(self.mails("email_change", "d@test.com"), [])
        accounts.request_email_change(self.p["id"], "attacker@evil.test")
        accounts.request_password_reset("attacker@evil.test")
        self.assertEqual(self.mails("reset", "attacker@evil.test"), [])

    def test_address_taken_meanwhile_fails_safely(self):
        accounts.request_email_change(self.p["id"], "later@test.com")
        token = _link(self.mails("email_change", "later@test.com")[0], "verify")
        create_user("later@test.com", PW, "Patient", "Someone Else")
        self.assertIsNone(accounts.verify_email(token))
        self.assertEqual(authenticate("p@test.com", PW, "Patient")["id"], self.p["id"])

    def _settings(self):
        consent.give(self.p["id"])
        at = AppTest.from_file(APP, default_timeout=120)
        for key, value in {"authenticated": True, "user": self.p, "nav": "settings"}.items():
            at.session_state[key] = value
        return at.run()

    def _submit(self, at, email, password=""):
        field = next(t for t in at.text_input if t.label == "Email")
        field.set_value(email)
        next(t for t in at.text_input if t.key == "settings_profile_password").set_value(password)
        next(b for b in at.button if b.label == "Save changes").click()
        return at.run()

    def test_settings_asks_for_the_password_then_waits_for_confirmation(self):
        at = self._settings()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        at = self._submit(at, "new@test.com")
        self.assertTrue(any("current password" in e.value for e in at.error))
        self.assertEqual(self.mails("email_change"), [])
        at = self._submit(at, "new@test.com", PW)
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertTrue(self.mails("email_change", "new@test.com"))
        self.assertEqual(authenticate("p@test.com", PW, "Patient")["id"], self.p["id"])


class EmailsOffChangeTests(Base):
    email_backend = None

    def test_without_emails_the_change_is_immediate_but_needs_the_password(self):
        consent.give(self.p["id"])
        at = AppTest.from_file(APP, default_timeout=120)
        for key, value in {"authenticated": True, "user": self.p, "nav": "settings"}.items():
            at.session_state[key] = value
        at.run()
        next(t for t in at.text_input if t.label == "Email").set_value("moved@test.com")
        next(b for b in at.button if b.label == "Save changes").click()
        at.run()
        self.assertIsNotNone(authenticate("p@test.com", PW, "Patient"))
        next(t for t in at.text_input if t.key == "settings_profile_password").set_value(PW)
        next(b for b in at.button if b.label == "Save changes").click()
        at.run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertIsNotNone(authenticate("moved@test.com", PW, "Patient"))


class AdminResetTests(Base):
    def setUp(self):
        super().setUp()
        create_user("a@test.com", PW, "Admin", "Ada Admin")
        self.a = authenticate("a@test.com", PW, "Admin")

    def _accounts_page(self):
        at = AppTest.from_file(APP, default_timeout=120)
        for key, value in {"authenticated": True, "user": self.a, "nav": "admin"}.items():
            at.session_state[key] = value
        at.run()
        at.selectbox(key="admin_pick").set_value(self.p["id"])
        return at.run()

    def test_with_emails_on_the_admin_never_sees_the_reset_link(self):
        at = self._accounts_page()
        next(b for b in at.button if b.key == "admin_reset").click()
        at.run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        self.assertTrue(self.mails("reset", "p@test.com"))
        token = _link(self.mails("reset", "p@test.com")[0], "reset")
        self.assertFalse([c for c in at.code if token in c.value])
        self.assertTrue(any("emailed" in s.value for s in at.success))


# =============================================================================
# SIGN-UP, SIGN-IN, LINKING
# =============================================================================

class SignUpAndSignInTests(Base):
    def test_sign_up_does_not_say_who_has_an_account(self):
        from ahead.pages.public import _register
        new = _register("New Person", "new@test.com", PW, PW, agreed=True)
        existing = _register("Someone", "p@test.com", PW, PW, agreed=True)
        self.assertTrue(existing[0])
        self.assertEqual(existing[1].replace("p@test.com", "X"), new[1].replace("new@test.com", "X"))
        self.assertTrue(self.mails("signup_exists", "p@test.com"))
        self.assertIsNotNone(authenticate("p@test.com", PW, "Patient"))           # nothing changed
        self.assertIsNone(authenticate("p@test.com", "other-password-123", "Patient"))

    def test_guessing_one_password_from_many_networks_is_limited(self):
        for n in range(accounts.FAILS_PER_EMAIL):
            accounts.record_failed_sign_in("p@test.com", f"10.0.{n}.1")
        self.assertTrue(accounts.sign_in_blocked("p@test.com", "10.9.9.9"))
        self.assertFalse(accounts.sign_in_blocked("d@test.com", "10.9.9.9"))

    def test_switched_off_doctors_cannot_be_linked(self):
        create_user("a@test.com", PW, "Admin", "Ada Admin")
        admin = authenticate("a@test.com", PW, "Admin")
        accounts.set_active(admin["id"], self.d["id"], False)
        self.assertFalse(grant_doctor(self.p["id"], "d@test.com"))
        self.assertEqual(raw("SELECT * FROM care_access"), [])


# =============================================================================
# CONSENT, NOTICES, EMAILS, RETENTION
# =============================================================================

@pytest.mark.consent_gate
class ConsentReachTests(Base):
    def test_calendar_feed_stops_after_withdrawal(self):
        consent.give(self.p["id"])
        token = calendar_feed.new_link(self.p["id"])
        self.assertIsNotNone(calendar_feed.ics_for_token(token))
        consent.withdraw(self.p["id"])
        self.assertIsNone(calendar_feed.ics_for_token(token))

    def test_withdrawal_elsewhere_takes_effect_on_the_next_click(self):
        consent.give(self.p["id"])
        at = AppTest.from_file(APP, default_timeout=120)
        for key, value in {"authenticated": True, "user": self.p, "nav": "overview"}.items():
            at.session_state[key] = value
        at.run()
        self.assertNotIn("Agree and continue", [b.label for b in at.button])
        consent.withdraw(self.p["id"])          # e.g. in another tab
        at.run()
        self.assertIn("Agree and continue", [b.label for b in at.button])


class NoticeAndEmailTextTests(Base):
    def test_names_show_literally_in_notices(self):
        consent.give(self.p["id"])
        with connection() as db:
            db.execute("UPDATE users SET name=? WHERE id=?", ("[Click here](https://evil.test)", self.p["id"]))
        grant_doctor(self.p["id"], "d@test.com")
        at = AppTest.from_file(APP, default_timeout=120)
        for key, value in {"authenticated": True, "user": self.d, "nav": "notifications"}.items():
            at.session_state[key] = value
        at.run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        captions = [c.value for c in at.caption if "care team" in c.value]
        self.assertTrue(captions)
        self.assertTrue(all("\\[Click here\\]" in c for c in captions))
        self.assertTrue(all("https​" in c for c in captions))     # a bare address isn't auto-linked either

    def test_emails_never_greet_by_name(self):
        accounts.request_password_reset("p@test.com")
        mail = self.mails("reset", "p@test.com")[0]
        self.assertNotIn("Pat", mail["text"] + mail["html"] + mail["subject"])
        subject, text, _ = mailer.compose("deactivated", "Pat Test")
        self.assertIn("for a year", text)


class RetentionReviewTests(Base):
    def test_erasing_a_patient_anonymises_the_audit_trail_and_rate_counters(self):
        create_user("a@test.com", PW, "Admin", "Ada Admin")
        admin = authenticate("a@test.com", PW, "Admin")
        accounts.audit("signed_in", actor_id=self.p["id"], target_user_id=self.p["id"], ip="10.1.2.3")
        accounts.record_failed_sign_in("p@test.com", "10.1.2.3")
        accounts.set_active(admin["id"], self.p["id"], False)
        with connection() as db:
            db.execute("UPDATE users SET deactivated_at=? WHERE id=?",
                       ((datetime.now(timezone.utc) - timedelta(days=400)).isoformat(), self.p["id"]))
        self.assertEqual(retention.run(force=True)["accounts"], 1)
        rows = raw("SELECT actor_id,target_user_id,ip,action FROM audit_log WHERE action!='account_erased'")
        self.assertFalse([r for r in rows if self.p["id"] in (r["actor_id"], r["target_user_id"])])
        self.assertFalse([r for r in rows if r["ip"] == "10.1.2.3"])
        self.assertEqual(raw("SELECT * FROM rate_events WHERE subject LIKE 'p@test.com%'"), [])

    def test_old_audit_entries_go_after_a_year(self):
        accounts.audit("signed_in", actor_id=self.p["id"])
        with connection() as db:
            db.execute("UPDATE audit_log SET created_at=?", ((datetime.now(timezone.utc) - timedelta(days=400)).isoformat(),))
        accounts.audit("signed_in", actor_id=self.p["id"])
        self.assertEqual(retention.run(force=True)["audit"], 1)
        self.assertEqual(len(raw("SELECT * FROM audit_log WHERE action='signed_in'")), 1)

    def test_recheck_reminders_are_not_repeated_after_the_clean_up(self):
        stamp = datetime.now(timezone.utc).isoformat()
        with connection() as db:
            db.execute("INSERT INTO prevention_plans(id,patient_id,doctor_id,disease,targets_json,recheck_date,status,"
                       "created_at,updated_at) VALUES(1,?,?,'diabetes','[]','2026-01-01','active',?,?)",
                       (self.p["id"], self.p["id"], stamp, stamp))
        notify(self.p["id"], "plan", "Time to screen again", "due", dedupe_key="prevention-recheck-1-2026-01-01")
        notify(self.p["id"], "t", "Old", "old", dedupe_key="old")
        with connection() as db:
            db.execute("UPDATE notifications SET created_at=?", ((datetime.now(timezone.utc) - timedelta(days=120)).isoformat(),))
        retention.run(force=True)
        self.assertEqual([n["title"] for n in notifications(self.p["id"])], ["Time to screen again"])
        with connection() as db:   # once the plan has stopped, its old reminder follows the 90-day rule
            db.execute("UPDATE prevention_plans SET status='stopped' WHERE id=1")
        retention.run(force=True)
        self.assertEqual(notifications(self.p["id"]), [])


class AssistantPrivacyTests(Base):
    def test_answers_from_the_patients_own_data_stay_out_of_gemini(self):
        from ahead.pages.assistant import _turns
        history = [{"role": "user", "content": "what are my meds today"},
                   {"role": "assistant", "content": "08:00 Metformin 500 mg", "local": True},
                   {"role": "user", "content": "what is a healthy breakfast"}]
        history[0]["local"] = True
        self.assertEqual(_turns(history), [{"role": "user", "text": "what is a healthy breakfast"}])
