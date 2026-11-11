"""Phase 3d: fixes from the final independent review before submission."""

import os
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

from ahead import accounts, alerts, consent, fieldcrypt, retention, storage
from ahead.platform_data import ai_consent, grant_doctor, notifications, notify, save_plan
from ahead.storage import authenticate, change_password, connection, create_user, delete_screenings, save_screening
from ahead.triage import age_note
from tests.test_phase2b_features import PW, Base, plan_values
from tests.test_phase3b_security import KEY_A, field_key, raw
from tests.test_phase3c_review import _link

NEW_PW = "another-long-password-2026"


def _doctor_notices(doctor):
    return [n["description"] for n in notifications(doctor["id"])]


# =============================================================================
# A PENDING EMAIL CHANGE MUST NOT OUTLIVE A PASSWORD CHANGE
# =============================================================================

class PendingLinkTests(Base):
    def _pending_change(self):
        self.assertEqual(accounts.request_email_change(self.p["id"], "attacker@evil.test"), "sent")
        return _link(self.mails("email_change", "attacker@evil.test")[0], "verify")

    def _still_mine(self, token, password=PW):
        self.assertIsNone(accounts.verify_email(token))
        self.assertEqual(authenticate("p@test.com", password, "Patient")["id"], self.p["id"])

    def test_reset_by_link_cancels_a_pending_email_change(self):
        token = self._pending_change()
        accounts.request_password_reset("p@test.com")
        reset = _link(self.mails("reset", "p@test.com")[0], "reset")
        self.assertIsNotNone(accounts.use_link(reset, NEW_PW))
        self._still_mine(token, NEW_PW)

    def test_changing_the_password_in_settings_cancels_it(self):
        token = self._pending_change()
        self.assertTrue(change_password(self.p["id"], PW, NEW_PW))
        self._still_mine(token, NEW_PW)

    def test_signing_out_everywhere_cancels_it_and_reset_links(self):
        token = self._pending_change()
        accounts.request_password_reset("p@test.com")
        reset = _link(self.mails("reset", "p@test.com")[0], "reset")
        accounts.sign_out_everywhere(self.p["id"])
        self._still_mine(token)
        self.assertIsNone(accounts.link_account(reset))


# =============================================================================
# NOTHING TELLS A VISITOR WHO HAS AN ACCOUNT
# =============================================================================

class EnumerationTests(Base):
    def test_email_change_to_a_taken_address_looks_like_any_other_and_is_rate_limited(self):
        answers = [accounts.request_email_change(self.p["id"], "d@test.com", "10.0.0.5") for _ in range(4)]
        self.assertEqual(answers, ["sent", "sent", "sent", "limit"])
        self.assertEqual(self.mails("email_change", "d@test.com"), [])

    def test_email_change_to_a_reserved_staff_address_sends_nothing(self):
        with mock.patch.dict(os.environ, {"AHEAD_ADMIN_EMAIL": "boss@test.com"}):
            self.assertEqual(accounts.request_email_change(self.p["id"], "boss@test.com"), "sent")
        self.assertEqual(self.mails("email_change", "boss@test.com"), [])

    def test_sign_up_with_an_existing_email_does_the_same_hashing_work(self):
        with mock.patch.object(storage, "password_hash", wraps=storage.password_hash) as hashed:
            self.assertFalse(create_user("p@test.com", PW, "Patient", "Someone"))
        hashed.assert_called_once()

    def test_reserved_staff_address_gets_the_normal_sign_up_reply(self):
        from ahead.pages import public
        with mock.patch.dict(os.environ, {"AHEAD_ADMIN_EMAIL": "boss@test.com"}), \
                mock.patch.object(public, "request_ip", return_value="10.0.0.6"):
            ok, text = public._register("Someone", "boss@test.com", PW, PW, True)
        self.assertTrue(ok)
        self.assertIn("Account created", text)
        self.assertNotIn("reserved", text)
        self.assertIsNone(authenticate("boss@test.com", PW, "Patient"))


# =============================================================================
# CONSENT COVERS SENDING DATA TO GEMINI
# =============================================================================

@pytest.mark.consent_gate
class AiConsentTests(Base):
    def test_withdrawing_consent_switches_off_doctor_ai_summaries(self):
        consent.give(self.p["id"])
        grant_doctor(self.p["id"], "d@test.com")
        self.assertTrue(ai_consent(self.p["id"], self.d["id"], True))
        consent.withdraw(self.p["id"])
        self.assertFalse(ai_consent(self.p["id"], self.d["id"]))
        consent.give(self.p["id"])
        self.assertFalse(ai_consent(self.p["id"], self.d["id"]))   # agreeing again doesn't switch it back on
        self.assertTrue(ai_consent(self.p["id"], self.d["id"], True))

    def test_no_ai_summaries_without_a_current_agreement(self):
        grant_doctor(self.p["id"], "d@test.com")
        with connection() as db:
            db.execute("UPDATE care_access SET ai_allowed=1")
        self.assertFalse(ai_consent(self.p["id"], self.d["id"]))

    def test_consent_text_names_free_tier_use_and_has_a_new_version(self):
        text = " ".join(body for _, body in consent.POINTS)
        self.assertIn("free tier", text)
        self.assertIn("people at Google may read it", text)
        self.assertNotEqual(consent.VERSION, "2026-11")


# =============================================================================
# START-UP CAN'T BE STOPPED BY TEXT THAT LOOKS ENCRYPTED
# =============================================================================

class StartUpCheckTests(Base):
    @pytest.mark.plain_rows
    def test_a_look_alike_saved_without_a_key_does_not_stop_start_up(self):
        with field_key(None):
            fieldcrypt.check()                     # first start: nothing encrypted, recorded as such
            notify(self.p["id"], "t", "Title", "enc1:deadbeef:" + "A" * 60, dedupe_key="look-alike")
            self.assertEqual(fieldcrypt.stored_key_ids(), {"deadbeef"})
            fieldcrypt.check()                     # was: "AHEAD_FIELD_KEY is not set"

    @pytest.mark.plain_rows
    def test_an_older_plain_look_alike_does_not_stop_start_up_with_a_key(self):
        with field_key(KEY_A):
            fieldcrypt.check()
        with connection(decrypt=False) as db:
            db.execute("INSERT INTO notifications(patient_id,type,title,description,created_at) VALUES(?,?,?,?,?)",
                       (self.p["id"], "t", "Old", "enc1:deadbeef:" + "A" * 60, datetime.now(timezone.utc).isoformat()))
        with field_key(KEY_A):
            self.assertIn("deadbeef", fieldcrypt.check())

    def test_a_missing_real_key_still_stops_start_up(self):
        with field_key(KEY_A):
            fieldcrypt.check()
            grant_doctor(self.p["id"], "d@test.com")
            save_plan(self.d["id"], self.p["id"], plan_values())
        with field_key(None), self.assertRaises(fieldcrypt.FieldKeyError):
            fieldcrypt.check()

    def test_a_database_from_before_the_record_trusts_what_is_stored(self):
        with field_key(KEY_A):
            grant_doctor(self.p["id"], "d@test.com")
            save_plan(self.d["id"], self.p["id"], plan_values())
        with connection() as db:
            db.execute("DELETE FROM app_state WHERE name=?", (fieldcrypt.KEY_IDS_STATE,))
        with field_key(None), self.assertRaises(fieldcrypt.FieldKeyError):
            fieldcrypt.check()


# =============================================================================
# ERASURE AND HISTORY DELETION REMOVE WHAT THE DOCTORS WERE TOLD
# =============================================================================

class ErasureTests(Base):
    def _screen(self):
        grant_doctor(self.p["id"], "d@test.com")
        values = {"age": 45, "gender": "Female", "bmi": 31, "smoking_history": "never", "hypertension": "No"}
        screening_id = save_screening(self.p["id"], "diabetes", "RandomForest", 0.6, 1, 0.21, values)
        alerts.new_result(self.p["id"], screening_id, "diabetes", values, 0.6, 0.21)
        self.assertTrue(any("Pat Test" in d for d in _doctor_notices(self.d)))

    def test_deleting_screening_history_removes_the_doctors_result_notices(self):
        self._screen()
        delete_screenings(self.p["id"])
        self.assertFalse(any("screening" in d for d in _doctor_notices(self.d)))

    def test_erasing_a_patient_removes_notices_that_name_them(self):
        self._screen()
        accounts.set_active(self.d2["id"], self.p["id"], False)
        self.assertEqual(retention.erase_account(self.p["id"]), 1)
        self.assertFalse(any("Pat Test" in d for d in _doctor_notices(self.d)))

    def test_the_erasure_row_points_at_no_one_when_the_id_is_reused(self):
        accounts.set_active(self.d2["id"], self.p["id"], False)
        retention.erase_account(self.p["id"])
        row = raw("SELECT target_user_id FROM audit_log WHERE action='account_erased'")[0]
        self.assertIsNone(row["target_user_id"])

    def test_erasing_a_doctor_removes_their_own_screenings(self):
        save_screening(self.d["id"], "diabetes", "RandomForest", 0.6, 1, 0.21, {"age": 50})
        accounts.set_active(self.d2["id"], self.d["id"], False)
        retention.erase_account(self.d["id"])
        self.assertEqual(raw("SELECT id FROM screenings WHERE owner_id=?", (self.d["id"],)), [])


# =============================================================================
# DOCTOR-TYPED TEXT AND CLINICAL WORDING
# =============================================================================

class TextTests(Base):
    def test_a_medicine_name_is_not_a_link_in_the_assistant(self):
        from ahead.pages.assistant import _status_answer
        grant_doctor(self.p["id"], "d@test.com")
        save_plan(self.d["id"], self.p["id"], plan_values(name="[Refill here](https://evil.example)"))
        answer = _status_answer("what are my meds today", self.p["id"]) or ""
        self.assertNotIn("](https://evil", answer)

    def test_lower_risk_notice_doesnt_promise_nothing_is_urgent(self):
        source = open(alerts.__file__).read()
        self.assertNotIn("nothing urgent", source)


class AgeNoteTests(Base):
    def test_age_groups_where_the_flag_says_little_get_a_note(self):
        self.assertIn("60 and over", age_note("diabetes", {"age": 67}))
        self.assertIn("eGFR", age_note("kidney", {"age": 30}, "doctor"))
        self.assertIn("under 40", age_note("heart", {"AgeCategory": "Age 25 to 29"}))

    def test_other_ages_and_missing_ages_get_none(self):
        self.assertEqual(age_note("diabetes", {"age": 45}), "")
        self.assertEqual(age_note("kidney", {"age": 65}), "")
        self.assertEqual(age_note("heart", {"AgeCategory": "Age 40 to 44"}), "")
        self.assertEqual(age_note("heart", {}), "")
        self.assertEqual(age_note("kidney", {"age": None}), "")

    def test_the_pdf_carries_the_note(self):
        from ahead.reports import patient_report
        pdf = patient_report("diabetes", "Diabetes screening", {"age": 70, "bmi": 28}, 0.6, 0.21, "RandomForest", {},
                             audience="patient")
        self.assertTrue(pdf.startswith(b"%PDF"))


class AssistantRoutingTests(Base):
    def test_only_questions_about_my_doses_get_the_dose_list(self):
        from ahead.pages.assistant import _asks_for_doses
        for question in ("what are my meds today", "did i take my pills", "which pills do i take tonight",
                         "what are my medicines"):
            self.assertTrue(_asks_for_doses(question), question)
        for question in ("how do i set up reminders for medicines", "what medicine should i take for a headache",
                         "is metformin safe with coffee", "how do i add my medicine"):
            self.assertFalse(_asks_for_doses(question), question)

    def test_reminder_how_to_gets_a_real_answer(self):
        from ahead.pages.assistant import _local_answer, _status_answer
        question = "how do i set up reminders for medicines"
        self.assertIsNone(_status_answer(question, self.p["id"]))
        self.assertIn("Add reminders to my calendar", _local_answer(question))

    def test_how_to_improve_lifestyle_is_not_answered_with_the_score(self):
        from ahead.pages.assistant import _status_answer
        self.assertIsNone(_status_answer("how do i improve my lifestyle score", self.p["id"]))
        self.assertIsNotNone(_status_answer("what is my lifestyle score", self.p["id"]))
