"""
ahead/pages/public.py
=====================
Everything a visitor sees before signing in: the login card and the
About / Contact / Help information pages.  All share login.css.
"""

from html import escape
import os

import streamlit as st

from ahead import accounts, consent, mailer
from ahead.components import markdown_literal, username_hint
from ahead.config import BASE, ROLE_ADMIN, ROLE_DOCTOR, ROLE_PATIENT
from ahead.resources import icon_uri, image_data_uri, static_url
from ahead.session import begin, request_ip
from ahead.storage import MIN_PASSWORD, authenticate, create_user, has_doctor_account
from ahead.platform_data import event


@st.cache_data(show_spinner=False)
def _login_css() -> str:
    return (BASE / "login.css").read_text(encoding="utf-8")


def _public_chrome(active: str) -> None:
    """Background, stylesheet and the top navigation bar shared by public pages."""
    st.html(
        f"""
<style>
{_login_css()}
html, body {{ background: #EEF5F9 !important; }}
.stApp, [data-testid="stAppViewContainer"] {{ background: transparent !important; }}
/* One fixed layer for the picture instead of `background-attachment: fixed`, which made the browser
   repaint the whole 1.2 MB image on every scroll step. The image is now a 23 KB WebP. */
.stApp::before {{
    content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background:
        linear-gradient(rgba(241,248,251,0.38), rgba(241,248,251,0.38)),
        url("{static_url('login_bg.webp')}") center / cover no-repeat;
}}
</style>
"""
    )
    links = [("login", "Sign In")] + [(key, content["nav"]) for key, content in PUBLIC_CONTENT.items()]
    nav = "".join(
        f'<a href="?page={key}" target="_self" class="{"active" if key == active else ""}">{label}</a>'
        for key, label in links
    )
    st.html(
        f"""
<div class="login-topbar">
    <div class="login-links">{nav}</div>
</div>
"""
    )


def _footer() -> None:
    st.html(
        """
<div class="login-footer">
    <span>© AHEAD · Senior Design Project</span>
    <span>Educational screening prototype · Not a medical device</span>
</div>
"""
    )


# =============================================================================
# LOGIN
# =============================================================================

def _sign_in(email: str, password: str, account_type: str) -> str | None:
    """Return an error message, or None on success (session is updated)."""
    email = (email or "").strip().lower()
    wanted = (ROLE_PATIENT,) if account_type == "Patient" else (ROLE_DOCTOR, ROLE_ADMIN)
    ip = request_ip()
    if accounts.sign_in_blocked(email, ip):
        return "Too many failed attempts. Wait 15 minutes, then try again."
    account = authenticate(email, password, wanted)
    if not account:
        accounts.record_failed_sign_in(email, ip)
        return "Incorrect email, password or account type."
    if accounts.needs_verification(account["id"]):
        st.session_state.unverified_user = {"id": account["id"], "email": account["email"]}
        return "Confirm your email address first: open the link we emailed you when you signed up."
    st.session_state.pop("unverified_user", None)
    begin(account)
    if account["role"] == ROLE_PATIENT:
        event(account['id'],'login')
    return None


def _reserved(email: str) -> bool:
    """Emails kept for the accounts created from the server settings."""
    email = email.strip().lower()
    return any(email == os.environ.get(name, "").strip().lower() for name in ("AHEAD_DOCTOR_EMAIL", "AHEAD_ADMIN_EMAIL"))


def _register(name: str, email: str, password: str, confirm: str, agreed: bool = False) -> tuple[bool, str]:
    email = (email or "").strip().lower()
    local, _, domain = email.partition("@")
    if not (name or "").strip() or not local or "." not in domain or len(email) > 254:
        return False, "Enter your name and a valid email address."
    if len(password or "") < MIN_PASSWORD:
        return False, f"Choose a password of at least {MIN_PASSWORD} characters."
    if password != confirm:
        return False, "The two passwords don't match."
    if not agreed:
        return False, "Please read how AHEAD uses your information and tick the box to agree."
    ip = request_ip()
    if not accounts.sign_up_allowed(ip):
        return False, "Too many new accounts from this network. Try again in an hour."
    accounts.record_sign_up(ip)
    confirm_by_email = mailer.enabled()
    created_text = (f"Account created. We've emailed a confirmation link to {email}. Open it, then sign in. "
                    "It can take a minute, and it may land in Junk or Spam.")
    if _reserved(email):
        # The server-set staff addresses answer like any registered one, so the page doesn't reveal them.
        return (True, created_text) if confirm_by_email else (False, "That email is already registered.")
    if not create_user(email, password, ROLE_PATIENT, name.strip()[:120], verified=not confirm_by_email):
        if confirm_by_email:
            # Same reply as a new account, so sign-up can't be used to find out who has one; the owner hears instead.
            accounts.notify_sign_up_attempt(email, ip)
            return True, created_text
        return False, "That email is already registered."
    from ahead.storage import connection
    with connection() as db:
        user_id = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    accounts.audit("patient_registered", target_user_id=user_id, ip=ip)
    consent.give(user_id, ip)
    if confirm_by_email:
        accounts.send_verification(user_id, ip)
        return True, created_text
    return True, "Account created. Sign in above."


def _consent_summary() -> str:
    """The consent text in the sign-up form (ahead/consent.py), in a fold-out so the form stays short."""
    items = "".join(f"<li><strong>{escape(title)}.</strong> {escape(copy)}</li>" for title, copy in consent.POINTS)
    return (f'<details class="signup-consent"><summary>{escape(consent.TITLE)} (please read)</summary>'
            f'<ul>{items}</ul></details>')


def render_login() -> None:
    _public_chrome("login")

    with st.container(key="login_shell"):
        left, right = st.columns([0.92, 1.08], gap="small")

        with left:
            feature_rows = "".join(
                f"""
<div class="ahead-feature">
    <div class="ahead-feature-icon"><img src="{icon_uri(icon)}" class="feature-svg"></div>
    <div class="ahead-feature-title">{title}</div>
</div>"""
                for icon, title in [
                    ("shield-check.svg", "Detect disease risk early"),
                    ("chart-simple.svg", "Get AI-powered health insights"),
                    ("users.svg", "Support better healthcare decisions"),
                ]
            )
            st.html(
                f"""
<div class="ahead-login-brand">
    <div class="ahead-login-main">
        <div class="ahead-login-logo">
            <img class="ahead-login-logo-icon" src="{image_data_uri('logo.svg')}" alt="AHEAD logo">
            <div class="ahead-login-logo-text">AHEAD</div>
        </div>
        <h1>Advanced Health<br>Early Awareness and<br>Disease Detection System</h1>
        <div class="ahead-login-description">
            AI-powered health screening designed to support earlier awareness and smarter healthcare decisions.
        </div>
        {feature_rows}
    </div>
    <div class="ahead-login-tagline">Early Detection. Healthier Tomorrows.</div>
</div>
"""
            )

        with right:
            with st.container(key="login_form"):
                st.html('<div class="login-heading"><h2>Welcome to AHEAD</h2><p>Sign in to continue.</p></div>')
                if st.session_state.pop("signed_out_notice", False):
                    st.info("You've been signed out. Please sign in again.")

                account_type = st.segmented_control(
                    "Account Type",
                    ["Patient", "Doctor / Admin"],
                    default="Patient",
                    selection_mode="single",
                    label_visibility="collapsed",
                    key="login_account_type",
                ) or "Patient"

                # A real form so that pressing Enter submits.
                with st.form("login_submit", border=False):
                    email = st.text_input("Email", placeholder="name@example.com", key="login_email", autocomplete="username")
                    password = st.text_input(
                        "Password", type="password", placeholder="Enter your password",
                        key="login_password", autocomplete="current-password",
                    )
                    submitted = st.form_submit_button("Sign In", width="stretch", type="primary")

                if submitted:
                    error = _sign_in(email, password, account_type)
                    if error:
                        st.error(error)
                    else:
                        st.query_params.clear()
                        st.rerun()

                pending = st.session_state.get("unverified_user")
                if pending:
                    if st.button("Email me a new confirmation link", key="resend_verification", width="stretch"):
                        if accounts.send_verification(pending["id"], request_ip()):
                            st.success(f"Sent to {markdown_literal(pending['email'])}. It can take a minute; check Junk or Spam too.")
                        else:
                            st.warning("We couldn't send another link just now. Wait an hour and try again.")

                if account_type != "Patient" and not has_doctor_account():
                    st.html(
                        '<div class="login-note">No doctor account is set up yet. '
                        'Doctor and admin accounts are created by the AHEAD admin.</div>'
                    )
                if mailer.enabled():
                    with st.expander("Forgot your password?"):
                        with st.form("forgot_password", clear_on_submit=True):
                            forgot_email = st.text_input("Your account email", max_chars=254, key="forgot_email",
                                                         autocomplete="username")
                            if st.form_submit_button("Email me a reset link"):
                                accounts.request_password_reset(forgot_email, request_ip())
                                st.session_state.forgot_sent = True
                        if st.session_state.pop("forgot_sent", False):
                            st.success("If an AHEAD account uses that email, a reset link is on its way. "
                                       "It works for 60 minutes. Check Junk or Spam too.")
                else:
                    st.html('<div class="login-note">Forgot your password? Ask the AHEAD team for a reset link.</div>')

                with st.expander("Create a patient account"):
                    with st.form("patient_registration"):
                        name = st.text_input("Your full name", max_chars=120)
                        new_email = st.text_input("Your email", max_chars=254, autocomplete="username")
                        new_password = st.text_input(f"Choose a password ({MIN_PASSWORD}+ characters)", type="password",
                                                     autocomplete="new-password")
                        confirm = st.text_input("Type the password again", type="password", autocomplete="new-password")
                        with st.container(key="signup_consent"):
                            st.html(_consent_summary())
                            agreed = st.checkbox("I have read how AHEAD uses my information and agree.",
                                                 key="signup_agree")
                        if st.form_submit_button("Create account"):
                            ok, message = _register(name, new_email, new_password, confirm, agreed)
                            (st.success if ok else st.error)(message)

    _footer()


# =============================================================================
# INFORMATION PAGES
# =============================================================================

PUBLIC_CONTENT = {
    "about": {
        "nav": "About",
        "eyebrow": "ABOUT THE PROJECT",
        "title": "About AHEAD",
        "intro": (
            "Advanced Health Early Awareness and Disease Detection is a Senior Design "
            "machine-learning project focused on early-awareness screening for chronic disease."
        ),
        "cards": [
            ("✚", "What AHEAD does",
             ["AHEAD accepts lifestyle, medical-history and clinical information and uses disease-specific "
              "machine-learning models to generate an early-awareness screening result.",
              "The current system includes diabetes, cardiovascular and chronic kidney disease assessments, "
              "each benchmarked across six model families."]),
            ("i", "How results should be used",
             ["Results are designed for education and early awareness. They are not diagnoses and should not "
              "be used to make medication or treatment decisions.",
              "Concerning symptoms or abnormal laboratory results should be evaluated by a qualified "
              "healthcare professional."]),
        ],
    },
    "contact": {
        "nav": "Contact",
        "eyebrow": "GET IN TOUCH",
        "title": "Contact the AHEAD team",
        "intro": "AHEAD is a university Senior Design prototype. Reach the project team through the channels below.",
        "cards": [
            ("✉", "Project team", ["For questions about the platform, its models or the datasets, contact the "
                                   "Senior Design team through your course supervisor or the project repository."]),
            ("⚑", "Report a problem", ["Found a bug or an incorrect result? Open an issue on the project repository "
                                       "with the screening condition, the model selected and the values entered."]),
            ("♥", "Medical concerns", ["AHEAD cannot answer medical questions. If you are worried about your health, "
                                      "please contact a healthcare professional or local emergency services."]),
        ],
    },
    "help": {
        "nav": "Help",
        "eyebrow": "HELP CENTRE",
        "title": "How to use AHEAD",
        "intro": "A short guide to signing in, running a screening and reading the result.",
        "cards": [
            ("1", "Sign in", ["Patients can register with \"Create a patient account\" on the sign-in card, then "
                             "sign in with Patient selected. Doctor accounts are added by the AHEAD admin, and doctors choose "
                             "their own password through a one-time set-up link.",
                             "Your screenings, lifestyle check-ins and medication records are saved to your account. "
                             "You stay signed in when you reload the page, but closing the tab signs you out, and a new tab "
                             "asks you to sign in again. AHEAD also signs you out after 2 hours without activity."]),
            ("2", "Run a screening", ["Open Screenings, pick a condition and fill in the form. Lab values are optional unless "
                                     "marked as needed. "
                                     "AHEAD scores the answers with the recommended model (you can compare alternatives)."]),
            ("3", "Read the result", ["The gauge shows the model score against its decision threshold. Below it you will find "
                                     "next steps, the factors AHEAD noticed in your inputs, and an AI-generated explanation."]),
            ("4", "Ask the assistant", ["Use the floating assistant button or the AI Assistant page for general questions "
                                       "about AHEAD, the three conditions or common health measurements."]),
            ("5", "Clinicians", ["Doctor accounts also get a Clinical Dashboard to upload a CSV/XLSX of patient records "
                                "and screen a whole file or a single record."]),
            ("!", "Important", ["AHEAD is an educational prototype. It does not diagnose disease and must not be used "
                               "to start, stop or change any treatment."]),
        ],
    },
}


def render_public_page(kind: str) -> None:
    content = PUBLIC_CONTENT[kind]
    _public_chrome(kind)
    grid_class = "three" if len(content["cards"]) % 3 == 0 else ""
    cards = "".join(
        f"""
<div class="public-info-card">
    <div class="icon">{icon}</div>
    <h3>{escape(title)}</h3>
    {''.join(f'<p>{escape(p)}</p>' for p in paragraphs)}
</div>"""
        for icon, title, paragraphs in content["cards"]
    )
    st.html(
        f"""
<div class="public-card">
    <div class="public-eyebrow">{escape(content['eyebrow'])}</div>
    <h1 class="public-title">{escape(content['title'])}</h1>
    <div class="public-intro">{escape(content['intro'])}</div>
    <div class="public-grid {grid_class}">{cards}</div>
</div>
"""
    )
    _footer()


# =============================================================================
# ONE-TIME LINKS: ACCOUNT SET-UP AND PASSWORD RESET
# =============================================================================

def render_account_link(token: str) -> None:
    """/?setup=<token> or /?reset=<token>: the person chooses a new password, once."""
    _public_chrome("login")
    found = accounts.link_account(token)
    with st.container(key="login_shell"):
        _, middle, _ = st.columns([0.25, 0.5, 0.25])
        with middle, st.container(key="login_form"):
            if st.session_state.get("link_done"):
                st.html('<div class="login-heading"><h2>Password saved</h2><p>You can sign in now.</p></div>')
                if st.button("Go to sign in", type="primary", width="stretch"):
                    st.session_state.pop("link_done", None)
                    st.query_params.clear()
                    st.rerun()
                return
            if not found:
                again = ("Use “Forgot your password?” on the sign-in page for a new reset link, or ask the AHEAD team."
                         if mailer.enabled() else "Ask the AHEAD team for a new one.")
                st.html('<div class="login-heading"><h2>This link has expired</h2>'
                        f'<p>Links work once. Set-up links last 3 days and reset links 60 minutes. {escape(again)}</p></div>')
                if st.button("Go to sign in", width="stretch"):
                    st.query_params.clear()
                    st.rerun()
                return
            user = found["user"]
            title = "Set up your AHEAD account" if found["purpose"] == "setup" else "Choose a new password"
            st.html(f'<div class="login-heading"><h2>{escape(title)}</h2>'
                    f'<p>{escape(user["name"])} · {escape(user["email"])}</p></div>')
            with st.form("account_link_form", border=False):
                username_hint(user["email"])
                password = st.text_input(f"New password ({MIN_PASSWORD}+ characters)", type="password",
                                         autocomplete="new-password")
                confirm = st.text_input("Type it again", type="password", autocomplete="new-password")
                submitted = st.form_submit_button("Save password", type="primary", width="stretch")
            if submitted:
                if len(password or "") < MIN_PASSWORD:
                    st.error(f"Use at least {MIN_PASSWORD} characters.")
                elif password != confirm:
                    st.error("The two passwords don't match.")
                elif accounts.use_link(token, password, request_ip()):
                    st.session_state.link_done = True
                    st.rerun()
                else:
                    st.error("This link has expired. Ask the AHEAD team for a new one.")
    _footer()


# =============================================================================
# EMAIL CONFIRMATION LINK AND THE MOMENT AFTER A RELOAD
# =============================================================================

def render_verify_link(token: str) -> None:
    """/?verify=<token>: confirms the email address once, then the person signs in."""
    _public_chrome("login")
    key = "verify_result_" + token[:16]
    if key not in st.session_state:  # reruns on this page must not use the link a second time
        st.session_state[key] = bool(accounts.verify_email(token, request_ip()))
    with st.container(key="login_shell"):
        _, middle, _ = st.columns([0.25, 0.5, 0.25])
        with middle, st.container(key="login_form"):
            if st.session_state[key]:
                st.html('<div class="login-heading"><h2>Email confirmed</h2><p>Thank you. You can sign in now.</p></div>')
            else:
                st.html('<div class="login-heading"><h2>This link has expired</h2><p>Confirmation links work once and '
                        'last 24 hours. Sign in with your email and password to get a new one.</p></div>')
            if st.button("Go to sign in", type="primary", width="stretch"):
                st.session_state.pop(key, None)
                st.query_params.clear()
                st.rerun()
    _footer()


def render_tab_wait() -> None:
    """Shown for a moment after a reload while this tab tells AHEAD which sign-in is its own."""
    _public_chrome("login")
    with st.container(key="login_shell"):
        _, middle, _ = st.columns([0.3, 0.4, 0.3])
        with middle, st.container(key="login_form"):
            st.html('<div class="login-heading"><h2>Opening AHEAD…</h2><p>Checking your sign-in.</p></div>')
            if st.button("Show the sign-in page", width="stretch", key="skip_tab_wait_button"):
                st.session_state.skip_tab_wait = True
                st.rerun()
