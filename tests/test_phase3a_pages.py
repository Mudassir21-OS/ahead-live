"""Phase 3a pages: features that were hard to find are one click away; doctors choose which results they hear about;
screening questions line up row by row; the assistant streams its answer."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from ahead import alerts
from ahead.platform_data import grant_doctor, init_platform
from ahead.storage import authenticate, create_user, init_db

APP = str(Path(__file__).resolve().parents[1] / "app.py")
PW = "test-password-123"


class PageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ["AHEAD_DB_PATH"] = self.temp.name + "/pages.db"
        init_db(); init_platform()
        create_user("p@test.local", PW, "Patient", "Amy Lee")
        create_user("d@test.local", PW, "Doctor", "Dr. Layla Hassan")
        self.patient = authenticate("p@test.local", PW, "Patient")
        self.doctor = authenticate("d@test.local", PW, "Doctor")

    def tearDown(self):
        self.temp.cleanup(); os.environ.pop("AHEAD_DB_PATH", None)

    def page(self, user, nav, **state):
        at = AppTest.from_file(APP, default_timeout=120)
        for key, value in {"authenticated": True, "user": user, "nav": nav, **state}.items():
            at.session_state[key] = value
        at.run()
        self.assertFalse(at.exception, [e.message for e in at.exception])
        return at

    def button(self, at, label):
        return next(b for b in at.button if b.label == label)

    def test_home_offers_adding_a_doctor_before_and_after_linking(self):
        at = self.page(self.patient, "overview")
        self.assertIn("No doctor can see your results yet", " ".join(c.value for c in at.caption))
        self.button(at, "Add your doctor").click().run()
        self.assertEqual(at.session_state["nav"], "settings")
        grant_doctor(self.patient["id"], "d@test.local")
        at = self.page(self.patient, "overview")
        from ahead.components import markdown_literal
        self.assertIn("Your care team: " + markdown_literal("Dr. Layla Hassan"), " ".join(c.value for c in at.caption))
        self.assertTrue(self.button(at, "Add a doctor"))

    def test_my_plan_offers_another_doctor_once_one_is_linked(self):
        grant_doctor(self.patient["id"], "d@test.local")
        at = self.page(self.patient, "medications")
        self.button(at, "Add another doctor").click().run()
        self.assertEqual(at.session_state["nav"], "settings")

    def test_doctor_chooses_which_results_they_hear_about(self):
        at = self.page(self.doctor, "settings")
        radio = next(r for r in at.radio if r.label == "Tell me about")
        self.assertEqual(radio.value, "all")
        radio.set_value("attention").run()
        self.assertEqual(alerts.review_scope(self.doctor["id"]), "attention")
        # Patients don't get the choice.
        at = self.page(self.patient, "settings")
        self.assertFalse([r for r in at.radio if r.label == "Tell me about"])

    def test_doctor_notifications_filter_and_settings_link(self):
        from ahead.platform_data import notify
        notify(self.doctor["id"], "review", "New result to review", "Amy Lee saved a screening: At risk.", "intelligence", "a")
        notify(self.doctor["id"], "result", "New result", "Amy Lee saved a screening: Not flagged.", "intelligence", "b")
        at = self.page(self.doctor, "notifications")
        self.assertEqual(sum("New result" in m.value for m in at.markdown), 2)
        self.assertTrue(any("⚠️" in m.value and "New result to review" in m.value for m in at.markdown))
        next(t for t in at.toggle if t.label == "Needs attention only").set_value(True).run()
        self.assertEqual(sum("New result" in m.value for m in at.markdown), 1)
        self.button(at, "Email and notice settings").click().run()
        self.assertEqual(at.session_state["nav"], "settings")
        (tabs,) = at.get("tab_container")
        self.assertEqual(tabs.proto.tab_container.default_tab_index, 2)   # opens on Preferences
        # Patients get the settings link but not the doctor-only filter.
        at = self.page(self.patient, "notifications")
        self.assertFalse([t for t in at.toggle if t.label == "Needs attention only"])

    def test_screening_questions_are_drawn_row_by_row_bottom_aligned(self):
        import inspect
        from ahead import screening
        from ahead.config import DISEASES
        for disease in DISEASES:   # every form still draws with the row-by-row layout
            self.page(self.patient, "screenings", active_screening=disease)
        source = inspect.getsource(screening)
        self.assertIn('st.columns(3, vertical_alignment="bottom")', source)
        self.assertIn("if index % 3 == 0:", source)

    def test_assistant_streams_the_answer(self):
        from ahead.pages import assistant
        pieces = iter(["Diabetes is ", "a condition."])
        with mock.patch.object(assistant, "gemini_available", lambda: True), \
             mock.patch.object(assistant, "chat_stream", lambda system, turns: pieces):
            at = self.page(self.patient, "assistant")
            at.chat_input[0].set_value("What is diabetes?").run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["chat_messages"][-1]["content"], "Diabetes is a condition.")


if __name__ == "__main__":
    unittest.main()
