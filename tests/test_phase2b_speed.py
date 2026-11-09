"""Phase 2b speed fixes: saved form statistics, model warm-up, lighter pages."""

import json
import re
from pathlib import Path

import pytest

from ahead import resources
from ahead.config import DISEASES

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("disease", list(DISEASES))
def test_saved_form_statistics_match_the_dataset(disease):
    """models/feature_summary.json must give exactly what the dataset gives (rebuild it if this fails:
    python scripts/build_feature_summary.py)."""
    saved = resources._saved_summary(disease)
    assert saved is not None, "feature_summary.json is missing or out of date"
    assert saved == resources.compute_feature_summary(disease)


def test_a_changed_dataset_falls_back_to_computing(monkeypatch, tmp_path):
    entry = json.loads(resources.SUMMARY_FILE.read_text())
    entry["kidney"]["dataset_bytes"] += 1
    stale = tmp_path / "feature_summary.json"
    stale.write_text(json.dumps(entry))
    monkeypatch.setattr(resources, "SUMMARY_FILE", stale)
    assert resources._saved_summary("kidney") is None
    assert resources._saved_summary("diabetes") is not None


def test_missing_file_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(resources, "SUMMARY_FILE", tmp_path / "nope.json")
    assert resources._saved_summary("heart") is None


def test_screening_form_never_parses_heart_csv(monkeypatch):
    resources.feature_summary.clear()
    monkeypatch.setattr(resources, "load_dataset", lambda d: pytest.fail("dataset was parsed"))
    for disease in DISEASES:
        assert resources.feature_summary(disease)
    resources.feature_summary.clear()


# --------------------------------------------------------------------------- warm-up

def test_warm_up_loads_the_recommended_models(monkeypatch):
    loaded = []
    monkeypatch.setattr(resources, "load_pipeline", lambda d, m: loaded.append((d, m)))
    resources.warm_up()
    assert sorted(loaded) == sorted((d, resources.best_model(d)) for d in DISEASES)


def test_server_starts_the_warm_up_unless_turned_off():
    source = (ROOT / "server.py").read_text()
    assert 'threading.Thread(target=_warm_up' in source and 'AHEAD_WARM_UP' in source


# --------------------------------------------------------------------------- lighter pages

def test_backgrounds_are_small_webp_without_fixed_attachment():
    for name in ("login_bg", "chatbot_bg"):
        assert (ROOT / "static" / f"{name}.webp").stat().st_size < 100_000
        assert not (ROOT / "static" / f"{name}.png").exists()
    for page in ("ahead/pages/public.py", "ahead/pages/assistant.py"):
        source = (ROOT / page).read_text()
        assert "no-repeat fixed" not in source and ".png')" not in source
    assert "backdrop-filter" not in (ROOT / "login.css").read_text()


def test_only_fields_with_icons_get_the_wide_left_padding():
    css = (ROOT / "login.css").read_text()
    wide = [line for line in css.splitlines() if "padding-left: 52px" in line]
    assert len(wide) == 1 and ".st-key-login_email input" in wide[0] and ".st-key-login_password input" in wide[0]


def test_db_skips_the_alive_check_for_recently_used_connections(monkeypatch):
    from ahead import db

    class Conn:
        closed = False

    checked = []
    monkeypatch.setattr(db.ConnectionPool, "check_connection", staticmethod(lambda c: checked.append(c)), raising=False)
    conn = Conn()
    db._check_if_idle(conn)                 # never used: checked
    db._mark_used(conn)
    db._check_if_idle(conn)                 # just used: not checked
    conn.closed = True
    db._check_if_idle(conn)                 # closed: checked
    monkeypatch.setitem(db._last_used, id(conn), 0.0)
    conn.closed = False
    db._check_if_idle(conn)                 # used long ago: checked
    assert len(checked) == 3


# --------------------------------------------------------------------------- password managers

def test_username_hint_is_escaped_and_hidden(monkeypatch):
    import streamlit as st
    from ahead import components
    shown = []
    monkeypatch.setattr(st, "html", lambda body, **_: shown.append(body))
    components.username_hint('a"b@test.local')
    html = shown[0]
    assert 'autocomplete="username"' in html and 'value="a&quot;b@test.local"' in html and "readonly" in html


def test_password_forms_have_autocomplete_hints():
    public = (ROOT / "ahead/pages/public.py").read_text()
    settings = (ROOT / "ahead/pages/settings.py").read_text()
    assert public.count('autocomplete="new-password"') >= 4      # sign-up (2) and set-up/reset (2)
    assert 'username_hint(user["email"])' in public and 'username_hint(user["email"])' in settings
    assert settings.count('autocomplete="new-password"') == 2 and 'autocomplete="current-password"' in settings


def test_choosing_a_page_asks_the_phone_menu_to_close(monkeypatch):
    import streamlit as st
    from ahead import components
    monkeypatch.setattr(st, "rerun", lambda: None)
    state = type("State", (dict,), {"__getattr__": dict.get, "__setattr__": dict.__setitem__})()
    monkeypatch.setattr(st, "session_state", state)
    components.go_to("wellness")
    first = state["close_menu"]
    components.go_to("overview")
    assert state["close_menu"] == first + 1 and state["nav"] == "overview"
