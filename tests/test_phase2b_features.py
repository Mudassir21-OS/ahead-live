"""Phase 2b: doctor notifications, the optional emails, email preferences, calendar reminders, the weekly task."""
import json
import os
import re
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

from streamlit.testing.v1 import AppTest

from ahead import alerts, calendar_feed, mailer
from ahead.platform_data import (approve_prevention_plan, grant_doctor, init_platform, local_today, notifications,
                                 plan_status, save_plan)
from ahead.storage import authenticate, connection, create_user, init_db, update_preferences

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
PW = "safe-password-123"
# Words that must never appear in an email: health values, conditions, medicine words and the names used below.
NEVER_IN_EMAIL = re.compile(r"diabet|heart|kidney|cardio|glucose|hba1c|blood|score|metformin|aspirin|500 ?mg|Pat Test|"
                            r"urgent|at risk|flagged", re.IGNORECASE)


def plan_values(**extra):
    today = local_today()
    return {"name": "Metformin", "dosage": "500 mg", "instructions": "With food", "times": ["08:00", "20:00"],
            "days": [0, 2, 4], "start_date": (today - timedelta(days=3)).isoformat(),
            "end_date": (today + timedelta(days=30)).isoformat(), "notes": "", **extra}


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
        init_platform()
        create_user("p@test.com", PW, "Patient", "Pat Test")
        create_user("d@test.com", PW, "Doctor", "Dr Test")
        create_user("d2@test.com", PW, "Doctor", "Dr Two")
        self.p = authenticate("p@test.com", PW, "Patient")
        self.d = authenticate("d@test.com", PW, "Doctor")
        self.d2 = authenticate("d2@test.com", PW, "Doctor")

    def tearDown(self):
        self.temp.cleanup()
        for name in ("AHEAD_DB_PATH", "AHEAD_PUBLIC_URL", "AHEAD_EMAIL_BACKEND", "AHEAD_TASK_KEY"):
            os.environ.pop(name, None)
        mailer.sent_messages.clear()

    def mails(self, kind=None, to=None):
        return [m for m in mailer.sent_messages if (kind is None or m["kind"] == kind) and (to is None or m["to"] == to)]

    def age_outbox(self, delta):
        from datetime import datetime
        with connection() as db:
            for row in db.execute("SELECT id,created_at FROM email_outbox").fetchall():
                db.execute("UPDATE email_outbox SET created_at=? WHERE id=?",
                           ((datetime.fromisoformat(row["created_at"]) - delta).isoformat(), row["id"]))

    def assert_private(self, message, own_name=None):
        """No health words, medicine words or other people's names. A person's own name in the greeting is fine."""
        for part in (message["subject"], message["text"], message["html"]):
            if own_name:
                part = part.replace(f"Hello {own_name},", "Hello,")
            self.assertIsNone(NEVER_IN_EMAIL.search(part), part)


# =============================================================================
# EMAIL PREFERENCES
# =============================================================================

class EmailPreferenceTests(Base):
    def test_defaults_and_roles(self):
        self.assertEqual(alerts.email_prefs(self.p["id"]),
                         {"plan_updates": True, "doctor_reviews": True, "doctor_links": True, "weekly_summary": False})
        self.assertEqual(alerts.emails_for("Patient"), ["plan_updates", "weekly_summary"])
        self.assertEqual(alerts.emails_for("Doctor"), ["doctor_reviews", "doctor_links", "weekly_summary"])
        self.assertEqual(alerts.emails_for("Admin"), [])

    def test_saving_one_choice_keeps_the_others_and_the_page_preferences(self):
        update_preferences(self.p["id"], {"toggles": {"keep_history": False}, "appearance": "Dark"})
        alerts.set_email_pref(self.p["id"], "weekly_summary", True)
        alerts.set_email_pref(self.p["id"], "plan_updates", False)
        update_preferences(self.p["id"], {"toggles": {"keep_history": True}, "appearance": "Light"})  # Settings saving its toggles
        self.assertEqual(alerts.email_prefs(self.p["id"])["weekly_summary"], True)
        self.assertEqual(alerts.email_prefs(self.p["id"])["plan_updates"], False)
        with connection() as db:
            saved = json.loads(db.execute("SELECT preferences_json FROM users WHERE id=?", (self.p["id"],)).fetchone()[0])
        self.assertEqual(saved["appearance"], "Light")
        with self.assertRaises(ValueError):
            alerts.set_email_pref(self.p["id"], "everything", True)


# =============================================================================
# PLAN CHANGES → PATIENT
# =============================================================================

class PlanEmailTests(Base):
    def setUp(self):
        super().setUp()
        grant_doctor(self.p["id"], "d@test.com")
        mailer.sent_messages.clear()

    def test_a_new_plan_emails_the_patient_without_details(self):
        save_plan(self.d["id"], self.p["id"], plan_values())
        sent = self.mails("plan_updated", "p@test.com")
        self.assertEqual(len(sent), 1)
        self.assertIn("medication plan", sent[0]["subject"])
        self.assert_private(sent[0], own_name="Pat Test")
        self.assertIn("https://ahead.test", sent[0]["text"])
        # the in-app notice keeps its detail (it's inside the signed-in app)
        self.assertTrue(any("Metformin" in n["description"] for n in notifications(self.p["id"])))

    def test_at_most_one_plan_email_an_hour(self):
        plan_id = save_plan(self.d["id"], self.p["id"], plan_values())
        save_plan(self.d["id"], self.p["id"], plan_values(dosage="1000 mg"), plan_id)
        plan_status(self.d["id"], plan_id, "paused")
        self.assertEqual(len(self.mails("plan_updated")), 1)
        self.age_outbox(timedelta(minutes=61))
        plan_status(self.d["id"], plan_id, "active")
        self.assertEqual(len(self.mails("plan_updated")), 2)

    def test_prevention_plan_email_says_prevention(self):
        from ahead.storage import save_screening
        screening = save_screening(self.p["id"], "diabetes", "LogisticRegression", 0.7, 1, 0.5,
                                   {"age": 50, "bmi": 31, "weight_kg": 90, "height_cm": 170})
        approve_prevention_plan(self.d["id"], self.p["id"], {
            "disease": "diabetes", "screening_id": screening, "note": "",
            "recheck_date": (local_today() + timedelta(days=90)).isoformat(),
            "targets": [{"key": "activity", "goal": 150}, {"key": "fruit_veg", "goal": 5}]})
        sent = self.mails("plan_updated")
        self.assertEqual(len(sent), 1)
        self.assertIn("prevention plan", sent[0]["subject"])
        self.assert_private(sent[0], own_name="Pat Test")

    def test_patient_can_switch_plan_emails_off(self):
        alerts.set_email_pref(self.p["id"], "plan_updates", False)
        save_plan(self.d["id"], self.p["id"], plan_values())
        self.assertEqual(self.mails("plan_updated"), [])

    def test_switched_off_account_gets_no_email(self):
        with connection() as db:
            db.execute("UPDATE users SET is_active=0 WHERE id=?", (self.p["id"],))
        alerts.plan_changed(self.p["id"], "medication")
        self.assertEqual(self.mails(), [])


class EmailsOffTests(Base):
    email_backend = ""

    def test_nothing_is_queued_and_actions_still_work(self):
        grant_doctor(self.p["id"], "d@test.com")
        save_plan(self.d["id"], self.p["id"], plan_values())
        with connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0], 0)
        self.assertEqual(alerts.weekly_summaries(), {"emails": "off", "queued": 0, "checked": 0})
        # the doctor's in-app notice doesn't need emails
        self.assertEqual([n["type"] for n in notifications(self.d["id"])], ["care_team"])


# =============================================================================
# DOCTORS: NEW LINK AND RESULTS TO REVIEW
# =============================================================================

class DoctorNoticeTests(Base):
    def test_a_new_link_tells_the_doctor_once(self):
        self.assertTrue(grant_doctor(self.p["id"], "d@test.com"))
        notes = notifications(self.d["id"])
        self.assertEqual([n["type"] for n in notes], ["care_team"])
        self.assertIn("Pat Test", notes[0]["description"])          # in-app: the name is fine
        sent = self.mails("doctor_linked", "d@test.com")
        self.assertEqual(len(sent), 1)
        self.assert_private(sent[0])                                  # email: no name
        grant_doctor(self.p["id"], "d@test.com")                      # linking again is not news
        self.assertEqual(len(notifications(self.d["id"])), 1)
        self.assertEqual(len(self.mails("doctor_linked")), 1)
        self.assertEqual(notifications(self.p["id"]), [])            # the patient isn't told about their own action

    def test_doctor_can_switch_link_emails_off(self):
        alerts.set_email_pref(self.d["id"], "doctor_links", False)
        grant_doctor(self.p["id"], "d@test.com")
        self.assertEqual(self.mails(), [])
        self.assertEqual(len(notifications(self.d["id"])), 1)

    def _result(self, flagged=True, screening_id=1):
        score = 0.9 if flagged else 0.05
        values = {"age": 55, "gender": "Male", "bmi": 33, "hypertension": 1, "smoking_history": "current"} if flagged \
            else {"age": 30, "gender": "Female", "bmi": 22, "hypertension": 0, "smoking_history": "never"}
        return alerts.new_result(self.p["id"], screening_id, "diabetes", values, score, 0.5)

    def test_a_result_that_needs_review_reaches_every_linked_doctor(self):
        grant_doctor(self.p["id"], "d@test.com")
        grant_doctor(self.p["id"], "d2@test.com")
        mailer.sent_messages.clear()
        told = self._result(flagged=True, screening_id=7)
        self.assertEqual(sorted(told), sorted([self.d["id"], self.d2["id"]]))
        for doctor in (self.d, self.d2):
            review = [n for n in notifications(doctor["id"]) if n["type"] == "review"]
            self.assertEqual(len(review), 1)
            self.assertEqual(review[0]["action"], "intelligence")
            self.assertIn("Pat Test", review[0]["description"])
        sent = self.mails("doctor_review")
        self.assertEqual(sorted(m["to"] for m in sent), ["d2@test.com", "d@test.com"])
        for message in sent:
            self.assert_private(message)
        # the same screening never notifies twice; another result within the hour adds a notice but no email
        self._result(flagged=True, screening_id=7)
        self._result(flagged=True, screening_id=8)
        self.assertEqual(len([n for n in notifications(self.d["id"]) if n["type"] == "review"]), 2)
        self.assertEqual(len(self.mails("doctor_review")), 2)

    def test_a_result_that_is_not_flagged_says_nothing(self):
        grant_doctor(self.p["id"], "d@test.com")
        mailer.sent_messages.clear()
        self.assertEqual(self._result(flagged=False), [])
        self.assertEqual([n["type"] for n in notifications(self.d["id"])], ["care_team"])
        self.assertEqual(self.mails(), [])

    def test_unlinked_doctors_hear_nothing(self):
        self._result(flagged=True)
        self.assertEqual(notifications(self.d["id"]), [])


# =============================================================================
# WEEKLY SUMMARY
# =============================================================================

class WeeklyTests(Base):
    def test_only_people_who_asked_and_have_unread_updates(self):
        grant_doctor(self.p["id"], "d@test.com")                    # doctor: 1 unread notice
        save_plan(self.d["id"], self.p["id"], plan_values())        # patient: 1 unread notice
        mailer.sent_messages.clear()
        self.assertEqual(alerts.weekly_summaries()["queued"], 0)    # nobody turned it on
        alerts.set_email_pref(self.p["id"], "weekly_summary", True)
        alerts.set_email_pref(self.d2["id"], "weekly_summary", True)  # nothing unread: no email
        result = alerts.weekly_summaries()
        self.assertEqual((result["queued"], result["checked"]), (1, 2))
        message = self.mails("weekly", "p@test.com")[0]
        self.assertIn("1 update", message["subject"])
        self.assert_private(message, own_name="Pat Test")
        self.assertEqual(alerts.weekly_summaries()["queued"], 0)    # never twice in a week

    def test_read_updates_are_not_counted(self):
        save_plan_and_read = grant_doctor(self.p["id"], "d@test.com")
        self.assertTrue(save_plan_and_read)
        save_plan(self.d["id"], self.p["id"], plan_values())
        from ahead.platform_data import mark_notification
        for note in notifications(self.p["id"]):
            mark_notification(self.p["id"], note["id"])
        alerts.set_email_pref(self.p["id"], "weekly_summary", True)
        self.assertEqual(alerts.weekly_summaries()["queued"], 0)


# =============================================================================
# CALENDAR
# =============================================================================

class CalendarTests(Base):
    def setUp(self):
        super().setUp()
        grant_doctor(self.p["id"], "d@test.com")
        self.plan = save_plan(self.d["id"], self.p["id"], plan_values())

    def events(self, ics):
        unfolded = ics.replace("\r\n ", "")
        return [block for block in unfolded.split("BEGIN:VEVENT")[1:]]

    def test_generic_by_default_with_stable_ids(self):
        ics = calendar_feed.build_ics(self.p["id"], site="https://ahead.test")
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n"))
        events = self.events(ics)
        self.assertEqual(len(events), 2)                              # 08:00 and 20:00
        self.assertNotIn("Metformin", ics)
        self.assertNotIn("500", ics)
        self.assertIn("SUMMARY:AHEAD medicine reminder", ics)
        self.assertIn(f"UID:ahead-med-{self.plan}-0800@ahead", ics)
        self.assertIn(f"UID:ahead-med-{self.plan}-2000@ahead", ics)
        self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=", ics)
        self.assertIn("DTSTART;TZID=Asia/Dubai:", ics)
        self.assertIn("TRIGGER:PT0M", ics)
        first = re.search(r"DTSTART;TZID=Asia/Dubai:(\d{8})T0800", ics).group(1)
        self.assertIn(date(int(first[:4]), int(first[4:6]), int(first[6:])).weekday(), (0, 2, 4))
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75, line)
        # editing the plan keeps the same events (same UIDs) with a higher SEQUENCE
        before = re.search(r"SEQUENCE:(\d+)", ics).group(1)
        save_plan(self.d["id"], self.p["id"], plan_values(times=["08:00", "20:00"], dosage="1000 mg"), self.plan)
        after = calendar_feed.build_ics(self.p["id"])
        self.assertIn(f"UID:ahead-med-{self.plan}-0800@ahead", after)
        self.assertGreater(int(re.search(r"SEQUENCE:(\d+)", after).group(1)), int(before))

    def test_names_only_when_the_patient_chooses(self):
        ics = calendar_feed.build_ics(self.p["id"], names=True)
        self.assertIn("SUMMARY:Metformin · 500 mg", ics)
        self.assertIn("With food", ics)

    def test_paused_and_finished_plans_drop_out(self):
        plan_status(self.d["id"], self.plan, "paused")
        self.assertEqual(self.events(calendar_feed.build_ics(self.p["id"])), [])
        plan_status(self.d["id"], self.plan, "active")
        self.assertEqual(len(self.events(calendar_feed.build_ics(self.p["id"]))), 2)
        with connection() as db:
            db.execute("UPDATE medication_plans SET end_date=? WHERE id=?",
                       ((local_today() - timedelta(days=1)).isoformat(), self.plan))
        self.assertEqual(self.events(calendar_feed.build_ics(self.p["id"])), [])

    def test_text_is_escaped(self):
        save_plan(self.d["id"], self.p["id"], plan_values(name="A; B, C\\D", times=["09:00"]))
        ics = calendar_feed.build_ics(self.p["id"], names=True)
        self.assertIn("SUMMARY:A\\; B\\, C\\\\D · 500 mg", ics.replace("\r\n ", ""))

    def test_private_link(self):
        self.assertIsNone(calendar_feed.feed(self.p["id"]))
        token = calendar_feed.new_link(self.p["id"])
        self.assertTrue(calendar_feed.valid_token(token))
        self.assertIn("BEGIN:VEVENT", calendar_feed.ics_for_token(token, "https://ahead.test"))
        self.assertIsNotNone(calendar_feed.feed(self.p["id"])["last_fetched_at"])
        webcal, https = calendar_feed.urls(token, "https://ahead.test")
        self.assertEqual((webcal, https), (f"webcal://ahead.test/calendar/{token}.ics", f"https://ahead.test/calendar/{token}.ics"))
        calendar_feed.set_show_names(self.p["id"], True)
        self.assertIn("Metformin", calendar_feed.ics_for_token(token))
        newer = calendar_feed.new_link(self.p["id"])               # the old link stops; the names choice stays
        self.assertIsNone(calendar_feed.ics_for_token(token))
        self.assertIn("Metformin", calendar_feed.ics_for_token(newer))
        for bad in ("", "short", "x" * 100, "../../etc/passwd", None):
            self.assertIsNone(calendar_feed.ics_for_token(bad))
        with connection() as db:
            db.execute("UPDATE users SET is_active=0 WHERE id=?", (self.p["id"],))
        self.assertIsNone(calendar_feed.ics_for_token(newer))      # a switched-off account's link stops too
        calendar_feed.remove_link(self.p["id"])
        self.assertIsNone(calendar_feed.feed(self.p["id"]))


# =============================================================================
# HTTP ROUTES
# =============================================================================

class RouteTests(Base):
    @classmethod
    def setUpClass(cls):
        os.environ["AHEAD_WARM_UP"] = "0"
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient
        import server
        cls.client = TestClient(Starlette(routes=[
            Route("/calendar/{token}", server.calendar, methods=["GET", "HEAD"]),
            Route("/tasks/weekly", server.weekly_task, methods=["POST"])]), base_url="https://ahead.test")

    def test_calendar_route(self):
        grant_doctor(self.p["id"], "d@test.com")
        save_plan(self.d["id"], self.p["id"], plan_values())
        token = calendar_feed.new_link(self.p["id"])
        response = self.client.get(f"/calendar/{token}.ics")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/calendar"))
        self.assertIn("private", response.headers["cache-control"])
        self.assertIn("BEGIN:VEVENT", response.text)
        self.assertEqual(self.client.get("/calendar/" + "A" * 32 + ".ics").status_code, 404)

    def test_weekly_route_needs_the_key(self):
        os.environ.pop("AHEAD_TASK_KEY", None)
        self.assertEqual(self.client.post("/tasks/weekly").status_code, 404)    # not configured: no route
        os.environ["AHEAD_TASK_KEY"] = "short"
        self.assertEqual(self.client.post("/tasks/weekly", headers={"x-ahead-task-key": "short"}).status_code, 404)
        os.environ["AHEAD_TASK_KEY"] = "k" * 40
        self.assertEqual(self.client.post("/tasks/weekly").status_code, 403)
        self.assertEqual(self.client.post("/tasks/weekly", headers={"x-ahead-task-key": "k" * 39}).status_code, 403)
        response = self.client.post("/tasks/weekly", headers={"x-ahead-task-key": "k" * 40})
        self.assertEqual((response.status_code, response.json()["emails"]), (200, "on"))


# =============================================================================
# PAGES
# =============================================================================

class PageTests(Base):
    def run_page(self, user, nav):
        at = AppTest.from_file(APP, default_timeout=120)
        for key, value in {"authenticated": True, "user": user, "nav": nav}.items():
            at.session_state[key] = value
        at.run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        return at

    def text(self, at):
        return " ".join([m.value for m in at.markdown] + [c.value for c in at.caption] +
                        [str(h.proto) for h in at.get("html")] + [i.value for i in at.info])

    def test_doctor_has_notifications(self):
        grant_doctor(self.p["id"], "d@test.com")
        at = self.run_page(self.d, "notifications")
        self.assertIn("New linked patient", self.text(at))
        self.assertTrue(any(b.label.startswith("Notifications (1)") for b in at.button))

    def test_settings_email_toggles_replace_the_old_notice(self):
        for user, expected in ((self.p, ["Plan changes", "Weekly summary"]),
                               (self.d, ["Results to review", "New linked patients", "Weekly summary"])):
            at = self.run_page(user, "settings")
            page = self.text(at)
            self.assertNotIn("not available in this prototype", page)
            self.assertEqual([t.label for t in at.toggle if t.key and t.key.startswith("email_pref_")], expected)
        at = self.run_page(self.p, "settings")
        at.toggle(key="email_pref_weekly_summary").set_value(True).run()
        self.assertTrue(alerts.email_prefs(self.p["id"])["weekly_summary"])
        self.assertIn("Make my private calendar link", [b.label for b in at.button])

    def test_my_plan_offers_calendar_reminders(self):
        grant_doctor(self.p["id"], "d@test.com")
        save_plan(self.d["id"], self.p["id"], plan_values())
        at = self.run_page(self.p, "medications")
        self.assertIn("📅  Add reminders to my calendar", [e.label for e in at.expander])
        self.assertNotIn("not configured", self.text(at))
        next(b for b in at.button if b.label == "Make my private calendar link").click().run()
        self.assertFalse(at.exception)
        self.assertIsNotNone(calendar_feed.feed(self.p["id"]))
        self.assertTrue(any("/calendar/" in c.value for c in at.code))

    def test_saving_a_flagged_screening_notifies_linked_doctor(self):
        grant_doctor(self.p["id"], "d@test.com")
        mailer.sent_messages.clear()
        import streamlit as st
        from ahead import components
        state = {"user": self.p, "screening_history": [], "prefs": {"keep_history": True}}
        fake = type("State", (dict,), {"__getattr__": dict.get, "__setattr__": dict.__setitem__})(state)
        with mock.patch.object(st, "session_state", fake):
            components.record_screening("diabetes", "LogisticRegression", 0.9, 1, 0.5,
                                        {"age": 55, "gender": "Male", "bmi": 33, "hypertension": 1,
                                         "smoking_history": "current"})
        self.assertEqual([n["type"] for n in notifications(self.d["id"])], ["review", "care_team"])
        self.assertEqual(len(self.mails("doctor_review")), 1)


if __name__ == "__main__":
    unittest.main()


class StyleSafetyTests(unittest.TestCase):
    def test_no_tags_inside_style_blocks(self):
        """st.html sanitises its HTML: a tag-like word inside <style> (even in a CSS comment) ends the stylesheet
        early and the whole app loses its look. Found once with a comment that mentioned the <a> element."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for folder, _, files in os.walk(os.path.join(root, "ahead")):
            for name in files:
                if not name.endswith((".py", ".css")):
                    continue
                text = open(os.path.join(folder, name), encoding="utf-8").read()
                for block in re.findall(r"<style>(.*?)</style>", text, re.DOTALL):
                    self.assertIsNone(re.search(r"<\s*/?\s*[a-zA-Z]", block), f"{name}: {re.search(r'<[^>]{0,30}', block)}")
        for css in ("login.css", "ahead/redesign.css"):
            self.assertIsNone(re.search(r"<\s*/?\s*[a-zA-Z]", open(os.path.join(root, css), encoding="utf-8").read()), css)
