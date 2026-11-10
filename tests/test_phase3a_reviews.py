"""Phase 3a: doctors hear about every new result by default, attention results stand out, and the hourly email cap
never drops a result (a follow-up email is scheduled for when the hour is up)."""

import unittest
from datetime import datetime, timedelta, timezone

from ahead import alerts, mailer
from ahead.platform_data import grant_doctor, notifications
from ahead.storage import connection
from tests.test_phase2b_features import Base


def _shift(delta):
    """Move every outbox time back by delta, as if that much time had passed."""
    with connection() as db:
        for row in db.execute("SELECT id,created_at,sent_at,next_attempt_at FROM email_outbox").fetchall():
            back = lambda v: (datetime.fromisoformat(v) - delta).isoformat() if v else v
            db.execute("UPDATE email_outbox SET created_at=?,sent_at=?,next_attempt_at=? WHERE id=?",
                       (back(row["created_at"]), back(row["sent_at"]), back(row["next_attempt_at"]), row["id"]))


class ReviewTests(Base):
    def setUp(self):
        super().setUp()
        grant_doctor(self.p["id"], "d@test.com")
        mailer.sent_messages.clear()

    def result(self, flagged, screening_id):
        score = 0.9 if flagged else 0.05
        values = {"age": 55, "gender": "Male", "bmi": 33, "hypertension": 1, "smoking_history": "current"} if flagged \
            else {"age": 30, "gender": "Female", "bmi": 22, "hypertension": 0, "smoking_history": "never"}
        return alerts.new_result(self.p["id"], screening_id, "diabetes", values, score, 0.5)

    def outbox(self, kind=None):
        with connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM email_outbox ORDER BY id").fetchall()]
        return [r for r in rows if kind is None or r["kind"] == kind]

    def test_every_result_is_the_default(self):
        self.assertEqual(alerts.review_scope(self.d["id"]), "all")
        self.assertEqual(self.result(False, 1), [self.d["id"]])
        notes = [n for n in notifications(self.d["id"]) if n["type"] == "result"]
        self.assertEqual(len(notes), 1)
        self.assertIn("Pat Test", notes[0]["description"])
        self.assertIn("Not flagged", notes[0]["description"])
        sent = self.mails("doctor_review")
        self.assertEqual(len(sent), 1)
        self.assert_private(sent[0], own_name="Dr Test")
        self.assertIn("saved a new screening result", sent[0]["text"])

    def test_attention_results_are_labelled_differently(self):
        self.result(True, 1)
        note = [n for n in notifications(self.d["id"]) if n["type"] in ("review", "result")]
        self.assertEqual([n["type"] for n in note], ["review"])
        self.assertIn("marked for attention", self.mails("doctor_review")[0]["text"])

    def test_attention_only_doctors_still_hear_about_flagged_results(self):
        alerts.set_review_scope(self.d["id"], "attention")
        self.assertEqual(self.result(False, 1), [])
        self.assertEqual(self.result(True, 2), [self.d["id"]])
        self.assertEqual(alerts.review_scope(self.d["id"]), "attention")
        with self.assertRaises(ValueError):
            alerts.set_review_scope(self.d["id"], "nonsense")

    def test_scope_and_email_toggles_do_not_overwrite_each_other(self):
        alerts.set_review_scope(self.d["id"], "attention")
        alerts.set_email_pref(self.d["id"], "weekly_summary", True)
        self.assertEqual(alerts.review_scope(self.d["id"]), "attention")
        alerts.set_review_scope(self.d["id"], "all")
        self.assertTrue(alerts.email_prefs(self.d["id"])["weekly_summary"])

    def test_results_within_the_hour_get_one_follow_up_when_the_hour_is_up(self):
        self.result(False, 1)                       # email now
        for sid in range(2, 12):                    # ten more in the same hour: notices, one follow-up waiting
            self.result(sid % 2 == 0, sid)
        self.assertEqual(len([n for n in notifications(self.d["id"]) if n["type"] in ("review", "result")]), 11)
        self.assertEqual(len(self.mails("doctor_review")), 1)
        waiting = self.outbox("doctor_review_more")
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["status"], "pending")
        first = datetime.fromisoformat(self.outbox("doctor_review")[0]["sent_at"])
        due = datetime.fromisoformat(waiting[0]["next_attempt_at"])
        self.assertAlmostEqual((due - first).total_seconds(), 3600, delta=5)
        mailer.drain()
        self.assertEqual(self.mails("doctor_review_more"), [])          # not yet: the hour isn't up
        _shift(timedelta(minutes=61))
        mailer.drain()
        more = self.mails("doctor_review_more")
        self.assertEqual(len(more), 1)
        self.assert_private(more[0], own_name="Dr Test")
        # Right after the follow-up went, a new result waits for the next hour again (no burst).
        self.result(False, 20)
        self.assertEqual(len(self.mails("doctor_review")), 1)
        self.assertEqual(len([r for r in self.outbox("doctor_review_more") if r["status"] == "pending"]), 1)
        _shift(timedelta(hours=3))
        mailer.drain()
        self.assertEqual(len(self.mails("doctor_review_more")), 2)       # the waiting follow-up went
        # After a quiet hour with nothing waiting, the next result is emailed at once.
        _shift(timedelta(hours=3))
        self.result(False, 21)
        self.assertEqual(len(self.mails("doctor_review")), 2)

    def test_switching_the_email_off_cancels_a_waiting_follow_up(self):
        self.result(False, 1)
        self.result(False, 2)
        alerts.set_email_pref(self.d["id"], "doctor_reviews", False)
        self.assertEqual(self.outbox("doctor_review_more")[0]["status"], "cancelled")
        _shift(timedelta(hours=2))
        mailer.drain()
        self.assertEqual(self.mails("doctor_review_more"), [])
        self.result(False, 3)                        # off: notice only
        self.assertEqual(len(self.mails("doctor_review")), 1)
        self.assertEqual(len([n for n in notifications(self.d["id"]) if n["type"] == "result"]), 3)

    def test_a_follow_up_for_a_switched_off_account_is_not_sent(self):
        self.result(False, 1)
        self.result(False, 2)
        with connection() as db:
            db.execute("UPDATE users SET is_active=0 WHERE id=?", (self.d["id"],))
        _shift(timedelta(hours=2))
        mailer.drain()
        self.assertEqual(self.mails("doctor_review_more"), [])
        self.assertEqual(self.outbox("doctor_review_more")[0]["status"], "cancelled")

    def test_emails_off_on_the_server_changes_nothing_in_app(self):
        import os
        os.environ.pop("AHEAD_EMAIL_BACKEND", None)
        self.assertFalse(mailer.enabled())
        self.assertEqual(self.result(False, 1), [self.d["id"]])
        self.assertEqual(self.outbox("doctor_review") + self.outbox("doctor_review_more"), [])


if __name__ == "__main__":
    unittest.main()
