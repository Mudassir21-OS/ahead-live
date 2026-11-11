"""Small jobs AHEAD asks the browser to do (Phase 3b).

They used to be inline script blocks. The Content-Security-Policy (server.py) now refuses inline scripts, so
they run as Streamlit components instead: the component's code is fixed text in this file, and only data
(a one-time code, a tab id, the text to read aloud) changes from run to run.

* ``post_session(code, tab)`` / ``post_logout(tab)``: set or clear this tab's sign-in cookie (ahead/session.py).
* ``close_phone_menu(count)``: fold the sidebar away on a phone after a page is chosen (app.py).
* ``read_aloud(text, colours)``: speak an assistant answer with the browser's own voice, with a Stop button.
"""

import uuid

import streamlit as st

_ACTIONS_JS = """
const done = new Set();   // each request runs once, even if Streamlit draws the component again
export default function (component) {
  const data = component.data || {};
  if (!data.id || done.has(data.id)) return;
  done.add(data.id);
  const post = (url, body) => fetch(url, { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (data.action === 'session') {
    post('/auth/session', { code: data.code, tab: data.tab });
  } else if (data.action === 'logout') {
    post('/auth/logout', { tab: data.tab });
  } else if (data.action === 'close_menu') {
    if (window.matchMedia('(max-width: 768px)').matches) {
      const button = document.querySelector('[data-testid=stSidebarCollapseButton] button');
      if (button && button.getAttribute('aria-expanded') === 'true') button.click();
    }
  }
}
"""

_SPEAK_HTML = '<div class="bar"><span class="msg">🔊 Reading the answer aloud…</span><button class="stop">Stop</button></div>'
_SPEAK_CSS = """
.bar { display: flex; align-items: center; gap: 10px; padding: 6px 12px; border-radius: 12px; width: max-content;
       font: 13px Inter, system-ui, sans-serif; background: var(--soft); color: var(--accent); }
.stop { border: 0; border-radius: 8px; padding: 4px 10px; cursor: pointer; background: var(--accent); color: white; }
"""
_SPEAK_JS = """
const spoken = new Set();
export default function (component) {
  const { data, parentElement } = component;
  if (!data || !data.id) return;
  const bar = parentElement.querySelector('.bar'), msg = parentElement.querySelector('.msg'),
        stop = parentElement.querySelector('.stop');
  bar.style.setProperty('--soft', data.soft); bar.style.setProperty('--accent', data.accent);
  const synth = window.speechSynthesis;
  if (!synth || !window.SpeechSynthesisUtterance) {
    msg.textContent = 'Reading aloud is not supported in this browser.'; stop.remove(); return;
  }
  if (spoken.has(data.id)) return;
  spoken.add(data.id);
  synth.cancel();
  const utterance = new SpeechSynthesisUtterance(data.text);
  utterance.lang = 'en-US';
  utterance.onend = () => { msg.textContent = '🔊 Finished reading.'; stop.remove(); };
  stop.onclick = () => { synth.cancel(); msg.textContent = 'Stopped.'; stop.remove(); };
  synth.speak(utterance);
}
"""

_registered = {}


def _component(name):
    if name not in _registered:
        if name == "ahead_actions":
            _registered[name] = st.components.v2.component(name, js=_ACTIONS_JS)
        else:
            _registered[name] = st.components.v2.component(name, html=_SPEAK_HTML, css=_SPEAK_CSS, js=_SPEAK_JS)
    return _registered[name]


def _next_id():
    return uuid.uuid4().hex


def _run(box, data):
    """Mount the actions component in a hidden one-pixel box (theme.py hides every ahead_hidden_* box)."""
    with st.container(key=f"ahead_hidden_{box}", height=1, border=False):
        try:
            _component("ahead_actions")(key=f"ahead_action_{box}", data={**data, "id": _next_id()})
        except Exception:   # the test harness has no browser
            pass


def post_session(code, tab):
    _run("session", {"action": "session", "code": code, "tab": tab})


def post_logout(tab):
    _run("logout", {"action": "logout", "tab": tab})


def close_phone_menu():
    _run("menu", {"action": "close_menu"})


def read_aloud(text, soft, accent):
    """Called once per "Read aloud" request (the assistant pops it), so each request gets a new id."""
    try:
        _component("ahead_speak")(key="ahead_speak",
                                  data={"id": _next_id(), "text": text, "soft": soft, "accent": accent})
    except Exception:   # the test harness has no browser
        pass
