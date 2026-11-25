"""Browser back/forward between pages (ahead/navigation.py): how st.session_state.nav follows the address."""

import pytest
import streamlit as st

from ahead import navigation

KEYS = ["overview", "screenings", "medications", "settings"]


@pytest.fixture
def env(monkeypatch):
    state = type("State", (dict,), {"__getattr__": dict.get, "__setattr__": dict.__setitem__})()
    monkeypatch.setattr(st, "session_state", state)
    seen = {"url": "overview", "switched": []}
    monkeypatch.setattr(navigation, "register", lambda keys, home, titles=None: seen["url"])
    monkeypatch.setattr(navigation, "switch", lambda key: seen["switched"].append(key))
    return state, seen


def run(state, seen):
    navigation.sync(KEYS, "overview", {})
    return state["nav"], seen["switched"]


def test_back_or_forward_moves_to_the_page_in_the_address(env):
    state, seen = env
    state["nav"] = "screenings"; seen["url"] = "screenings"
    run(state, seen)
    seen["url"] = "medications"           # the browser went to /medications
    assert run(state, seen) == ("medications", [])


def test_a_reload_opens_the_page_in_the_address(env):
    state, seen = env
    state["nav"] = "overview"; seen["url"] = "settings"   # new connection, address /settings
    assert run(state, seen) == ("settings", [])


def test_a_fresh_sign_in_starts_at_home(env):
    state, seen = env
    navigation.mark_fresh_sign_in()
    state["nav"] = "overview"; seen["url"] = "medications"
    assert run(state, seen) == ("overview", ["overview"])


def test_a_page_this_person_cannot_open_goes_home(env):
    state, seen = env
    state["nav"] = "clinical"; seen["url"] = "overview"
    assert run(state, seen) == ("overview", [])


def test_the_address_follows_a_page_chosen_in_the_app(env):
    state, seen = env
    state["nav"] = "overview"; seen["url"] = "overview"
    run(state, seen)
    state["nav"] = "settings"             # e.g. set by a test or an older code path without go_to
    assert run(state, seen) == ("settings", ["settings"])
