"""
ahead/pages/assistant.py
========================
"Ask AHEAD" — the conversational assistant page: hero with mascot, trait
chips, suggested prompts and a chat transcript backed by Gemini.
"""

import re
from html import escape
from urllib.parse import quote

import streamlit as st

from ahead.browser import read_aloud
from ahead.components import markdown_literal, current_user, go_to, pref
from ahead.config import ICONS_DIR, NAV_ITEMS
from ahead.platform_data import local_now
from ahead.resources import chat_reply, chat_stream, gemini_available, gemini_error_message, icon_uri, static_url, transcribe_audio
from ahead.theme import is_dark, tokens

WELCOME = "Hi! I'm AHEAD Assistant. What would you like to know about AHEAD or its screening conditions?"
DOCTOR_WELCOME = ("Hi! I'm AHEAD Assistant. Ask me about AHEAD's screening models, the Clinical Dashboard or "
                  "Patient Intelligence.")

# (material icon, prompt)
SUGGESTIONS = [
    (":material/lightbulb:", "What is AHEAD?"),
    (":material/cardiology:", "Tell me about the screening conditions"),
    (":material/explore:", "How do I use the platform?"),
]
DOCTOR_SUGGESTIONS = [
    (":material/link:", "How do patients link to me?"),
    (":material/upload_file:", "How do I score an uploaded file?"),
    (":material/insights:", "What does Patient Intelligence show?"),
]
DOCTOR_NOTICE = ("This assistant is for general questions about AHEAD and health topics. Do not paste identifiable "
                 "patient records here; the consent-gated summary for a linked patient is in Patient Intelligence.")

SYSTEM_PROMPT = (
    "You are AHEAD Assistant inside a university healthcare screening prototype called AHEAD "
    "(Advanced Health Early Awareness and Disease Detection). AHEAD screens for diabetes, cardiovascular "
    "disease and chronic kidney disease using machine-learning models trained on public CDC survey data (NHANES "
    "for diabetes and kidney, BRFSS for heart), and shows the result as an educational screening estimate, never a "
    "diagnosis. The diabetes and kidney models read only answers (age, sex, BMI, smoking and health history); lab "
    "values the person enters are checked against fixed ranges instead: an HbA1c, eGFR or urine ACR result sets the "
    "result in place of the model's estimate, while a blood glucose or blood pressure reading counts only in the "
    "severe range (glucose 200 mg/dL, blood pressure 180/120 or more). The heart model compares answers with survey respondents who reported a past heart attack, so its "
    "score is not a 10-year or future heart risk; anyone who reports a past heart attack, angina or stroke is asked "
    "to see their doctor instead. Answer questions about AHEAD, the "
    "three conditions, common screening measurements (HbA1c, blood glucose, BMI, blood pressure, eGFR, ACR, "
    "creatinine, BUN, hemoglobin), and how to use the website (Overview, Screenings, Health & Lifestyle Tracker, "
    "Food Nutrition Analyzer, My plan, Notifications, Data & Analytics, Settings, and for doctors the "
    "Clinical Dashboard and Patient Intelligence). Medicine reminders: My plan → Add reminders to my calendar (a private "
    "calendar link the phone or computer subscribes to); doctors add medication plans, patients can't. Users may speak their questions; their words arrive already "
    "transcribed, and your answer may be read aloud, so prefer plain sentences over tables. Always reply in English. "
    "Give general educational information only. Do not diagnose "
    "a person, do not claim a screening result confirms disease, and do not recommend starting, stopping or "
    "changing prescription medication. For urgent symptoms advise appropriate urgent medical care. "
    "Keep answers concise, warm and patient-friendly; use short paragraphs or bullet points."
)

DOCTOR_SYSTEM_PROMPT = SYSTEM_PROMPT.rsplit("Keep answers", 1)[0] + (
    "The person asking is a signed-in clinician using a doctor account. Their menu has Overview, Screenings, "
    "Data & Analytics, Clinical Dashboard, Advanced Analytics & Patient Intelligence, AI Assistant and Settings; "
    "the My plan, Health & Lifestyle Tracker, Food Nutrition Analyzer and Notifications pages are for "
    "patient accounts only, so do not send the clinician there. Patients link to a doctor themselves under "
    "Settings, Preferences, Care team access, by entering the doctor's account email. Patient Intelligence shows "
    "only linked patients: their saved assessments and score history, activity, medication plans the doctor creates "
    "with the dose log, and a record-based pre-consultation summary (plus an optional Gemini summary only with the "
    "patient's permission). The Clinical Dashboard imports a CSV or Excel file of "
    "de-identified records for one condition, checks it, scores the eligible rows and records a review decision "
    "per row. You cannot see any patient's records. If the clinician pastes identifiable patient details, do not "
    "repeat them; remind them that the consent-gated summary is in Patient Intelligence. "
    "Use a concise, professional, clinician-oriented tone with standard clinical terminology; refer to "
    "\"the patient\" in the third person; use short paragraphs or bullet points."
)

# (label, svg paths) — rendered as <img> data URIs because st.html strips inline <svg>
TRAITS = [
    ("Evidence-based",
     '<path d="M2 4h6a4 4 0 0 1 4 4v12a3 3 0 0 0-3-3H2z"/><path d="M22 4h-6a4 4 0 0 0-4 4v12a3 3 0 0 1 3-3h7z"/>'),
    ("Supportive",
     '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8z"/>'),
    ("Practical guidance",
     '<path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7V17h8v-2.3A7 7 0 0 0 12 2z"/>'),
]


def _trait_icon(paths: str) -> str:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{tokens()["accent"]}" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{paths}</svg>'
    )
    return f'<img src="data:image/svg+xml;utf8,{quote(svg)}" alt="">'


# Offline answers for the suggested prompts so the page is useful without an API key.
LOCAL_ANSWERS = {
    "what is ahead": (
        "**AHEAD** (Advanced Health Early Awareness and Disease Detection) is a Senior Design prototype that "
        "screens for **diabetes**, **cardiovascular disease** and **chronic kidney disease** using machine-learning "
        "models trained on public CDC survey data.\n\nYou enter lifestyle, medical-history and clinical values; AHEAD "
        "returns an *educational* screening estimate, practical next steps and the factors it noticed — never a diagnosis."
    ),
    "screening conditions": (
        "AHEAD currently screens three conditions:\n\n"
        "- **Diabetes** — the model estimates whether your HbA1c is likely to be in the prediabetes range or higher, "
        "from age, sex, BMI, smoking and history of high blood pressure / heart disease. It is for people who have "
        "not been told by a doctor that they have diabetes. An HbA1c result is optional and, if entered, sets the "
        "result; a blood glucose result counts only at 200 mg/dL or more.\n"
        "- **Cardiovascular** — the model compares your answers on general health, activity, sleep, smoking, alcohol, "
        "BMI and prior conditions with adults in a CDC survey who reported a heart attack. It is not your 10-year "
        "risk. If you have had a heart attack, angina or a stroke, AHEAD asks you to see your doctor instead.\n"
        "- **Chronic kidney disease** — the model estimates whether a kidney test is likely to be outside the usual "
        "range, from age, sex, BMI, smoking, activity and history of high blood pressure, diabetes and heart disease. "
        "Creatinine, eGFR and urine ACR are optional: a low eGFR or a high ACR is flagged for a doctor, and both in "
        "the usual range clear the result; a severely high blood pressure (180/120 or more) is flagged.\n\nEach "
        "condition has its own recommended model chosen by benchmark."
    ),
    "how do i use": (
        "1. Open **Screenings** and pick a condition.\n"
        "2. Fill in the form (lab values are optional) and press **Generate Screening Result**.\n"
        "3. Read the gauge, the recommended next steps and the factors AHEAD identified.\n"
        "4. Explore **Data & Analytics** for datasets and model performance, or adjust your **Settings**.\n\n"
        "Doctor accounts also get a **Clinical Dashboard** for uploading patient files."
    ),
}


# Doctor versions, checked before LOCAL_ANSWERS so "how do i use" describes the doctor tools.
DOCTOR_LOCAL_ANSWERS = {
    "link to me": (
        "Patients link themselves. In their own **Settings → Preferences → Care team access** they enter your doctor "
        "account email and choose **Add doctor**.\n\nOnce linked, the patient appears in **Advanced Analytics & "
        "Patient Intelligence**. The patient can remove your access at any time, and separately decides whether "
        "their linked records may be sent to Gemini for the optional AI summary."
    ),
    "upload": (
        "1. Open the **Clinical Dashboard** and choose the condition and model.\n"
        "2. Download the example upload template if needed, then upload a CSV or Excel file of de-identified records "
        "(a patient ID column plus the form's columns, lab columns optional; a DOB can stand in for age).\n"
        "3. Check the import preview and choose **Save records to review queue**.\n"
        "4. Under **Review and score**, score the eligible records, then open each record to correct its inputs, "
        "read the model output and save a review decision."
    ),
    "patient intelligence": (
        "**Advanced Analytics & Patient Intelligence** shows only patients who linked their account to you:\n\n"
        "- **Overview** and **Disease Analytics**: linked patients, saved assessments and recent activity.\n"
        "- **Patient Records & History**: each patient's saved assessments, score history and entered values, with "
        "the test to ask for when a lab such as HbA1c was left blank.\n"
        "- **Medication Monitoring**: the medication plans you create and the patient's dose log.\n"
        "- **Prevention Plan**: targets you approve for an at-risk patient (weight, activity, fruit and vegetables, "
        "sleep, stopping smoking), their progress from the patient's tracker, and a re-check date.\n"
        "- **Clinical Insights**: a record-based pre-consultation summary, and an optional Gemini summary only with "
        "the patient's permission."
    ),
    "how do i use": (
        "1. **Clinical Dashboard**: upload a file of de-identified patient records, correct flagged values, score "
        "the records and save a review decision for each.\n"
        "2. **Patient Intelligence**: follow the patients who linked their account to you (saved assessments, "
        "score history and activity) and create or adjust **medication plans**.\n"
        "3. **Screenings**: score one set of entered values; these results are not attached to a patient record.\n"
        "4. **Data & Analytics**: datasets, model performance and decision thresholds."
    ),
}


def _local_answer(prompt: str, is_doctor: bool = False) -> str | None:
    text = prompt.lower()
    pairs = [*DOCTOR_LOCAL_ANSWERS.items(), *LOCAL_ANSWERS.items()] if is_doctor else LOCAL_ANSWERS.items()
    for key, answer in pairs:
        if key in text:
            return answer
    return None


# ----------------------------------------------------------------------------- how-to answers
# Questions about using AHEAD's own pages are answered here, instantly and with the real button names, instead of
# by Gemini (which can only guess at the screens). A question needs a how-to cue ("how", "where", "can I", ...) and
# a topic; the first matching topic wins, so the more specific ones come first. (pattern, answer, who): who is
# "patient", "doctor" or "both".
HOW_CUE = re.compile(r"\b(how|where|can i|can you|could i|help|steps?|want to|way to|need to|i'?d like to|is there|"
                     r"what do i do|where do i|show me how)\b")
_PASSWORD_FORGOT = (
    "On the **sign-in page**, open **Forgot your password?**, type your account email and choose **Email me a reset "
    "link**. The link arrives in a minute or two (check Junk or Spam) and works once.")
_PASSWORD_CHANGE = (
    "Open **Settings → Account → Change password**, type your current password and the new one twice, then choose "
    "**Update password**. Your other browsers are signed out; this one stays signed in.")
_EMAIL_CHANGE = (
    "Open **Settings → Profile → Account information**, type the new email and your **current password**, then "
    "**Save changes**. AHEAD emails a confirmation link to the new address; your email only changes once you open it, "
    "and the old address is told.")
_DARK_MODE = "Open **Settings → Appearance → Display mode** and choose the light or dark interface."
_SESSIONS = ("Open **Settings → Account → Manage sessions** and choose **Sign out on all my devices**. Changing your "
             "password also signs your other browsers out.")
HOW_TOS = [
    # ---- both
    (r"\b(forgot|forgotten|reset|lost|recover)\b.*\bpassword", _PASSWORD_FORGOT, "both"),
    (r"\b(change|update|new)\b.*\bpassword", _PASSWORD_CHANGE, "both"),
    (r"\b(change|update)\b.*\bemail\b(?! ?(notif|settings|prefer))(?!s)", _EMAIL_CHANGE, "both"),
    (r"\b(dark|light)\s*(mode|theme)\b|\btheme\b|\bdisplay mode\b", _DARK_MODE, "both"),
    (r"\b(sign|log)\s*out\b.*\b(all|every|other)\b|\bsessions?\b", _SESSIONS, "both"),
    # ---- patients
    (r"\b(remove|unlink|revoke|stop sharing|take away)\b.*\bdoctors?\b",
     "Open **Settings → Preferences → Care team access** and choose **Remove access** next to the doctor. They stop "
     "seeing your results at once, and plans that doctor wrote for you stop.", "patient"),
    (r"\b(gemini|ai)\b.*\b(summar|doctor)|\bdoctor\b.*\b(ai|gemini)\b",
     "Doctors can draft an optional AI summary of your records only if you allow it. Under **Settings → Preferences → "
     "Care team access**, tick or untick **Allow sending my linked records to Gemini for optional AI summaries** for "
     "each doctor. It is off until you turn it on.", "patient"),
    (r"\b(link|add|connect|share|invite)\b.*\bdoctors?\b|\bdoctors?\b.*\b(see|access|view)\b",
     "1. Ask your doctor for the email of their AHEAD doctor account.\n"
     "2. Open **Settings → Preferences → Care team access**, type it under **Doctor account email** and choose "
     "**Add doctor** (or tap **Share with your doctor** under a screening result).\n\n"
     "You can add several doctors and remove any of them at any time. They then see your saved results and can "
     "add medication and prevention plans.", "patient"),
    (r"\b(delete|clear|erase|remove|wipe)\b.*\b(history|results?|screenings?|data)\b",
     "Open **Settings → Preferences → Data & privacy** and choose **Delete my saved screening history**. This "
     "permanently deletes every saved screening, including what your doctors could see. To stop saving new "
     "results, switch off **Keep screening history** just above it.", "patient"),
    (r"\b(withdraw|take back|revoke|cancel)\b.*\b(consent|agreement)\b|\bconsent\b",
     "Open **Settings → Preferences → Data & privacy** and choose **Withdraw**. AHEAD then asks you to agree again "
     "before you can use it, and doctors' AI summaries are switched off. What is already saved stays until you delete "
     "it or the account is erased.", "patient"),
    (r"\b(pdf|download|export|print)\b",
     "Run a screening (**Screenings → Begin screening**). Under the result, choose **Download screening PDF**. It "
     "explains the score, the factors AHEAD noticed and what to ask your doctor.", "patient"),
    (r"\b(reminders?|remind|calendar|alarm)\b",
     "Medicine reminders go in your own phone or computer calendar, so they ring at each dose time even when "
     "AHEAD is closed:\n\n"
     "1. Open **My plan** and choose **Add reminders to my calendar** (or **Settings → Preferences → Reminders "
     "in my calendar**).\n"
     "2. Choose **Make my private calendar link**, then **Subscribe on this device**. For Google Calendar, paste "
     "the link under Other calendars → + → From URL.\n\n"
     "The reminders follow your doctor's changes by themselves. Doses only appear once a doctor has added a "
     "medication plan for you.", "patient"),
    (r"\b(add|enter|record|mark|log|track)\b.*\b(medicines?|medications?|meds|doses?|pills?|tablets?)\b|\bmark\b.*\btaken\b",
     "Medicines are added by your doctor, not by you, so AHEAD always matches what was prescribed. Once a linked "
     "doctor adds a plan, open **My plan** to see today's doses and mark each one **taken**. To link a doctor: "
     "**Settings → Preferences → Care team access → Add doctor**.", "patient"),
    (r"\b(stop|turn off|unsubscribe|fewer|less|choose|change|which)\b.*\bemails\b|\bweekly summary\b",
     "Open **Settings → Preferences → Emails** and switch each optional email on or off (plan changes, the weekly "
     "summary). Sign-up, password and security emails are always sent. Emails never contain results or medicine "
     "names.", "patient"),
    (r"\b(food|meal|meals|calories|nutrition|eat)\b",
     "Open the **Food Nutrition Analyzer**, use **Upload or take a food photo** and choose **Analyze Food**. Check "
     "the foods, portion and nutrient estimates, correct them if needed, then **Confirm and save meal**. Your day's totals and meal history appear "
     "below. The photo is sent to Gemini for the estimate.", "patient"),
    (r"\b(lifestyle|wellness|check-?in|goals?|challenge|30\s*[x×]\s*30)\b",
     "Open the **Health & Lifestyle Tracker**. **Check-in** records sleep, water, food habits, stress and time spent "
     "sitting (**Save lifestyle check-in**); **Progress** shows your trend; **Goals** sets your own targets; the "
     "**30×30 Challenge** tab logs each day's active minutes over 30 days.", "patient"),
    (r"\b(start|do|take|run|begin|get)\b.*\b(screening|test|assessment|check)\b|\bscreen(ing)? (for|my)\b",
     "Open **Screenings**, pick diabetes, cardiovascular or kidney health and choose **Begin screening**. Answer the "
     "questions (lab values are optional) and choose **Generate Screening Result**. The result is an educational "
     "estimate, not a diagnosis; you can download it as a PDF and share it with a linked doctor.", "patient"),
    (r"\bnotifications?\b",
     "Open **Notifications** for plan changes, reminders to screen again and messages from your doctors. **Show "
     "unread only** hides the ones you've seen. Notifications are deleted after 90 days.", "patient"),
    # ---- doctors
    (r"\b(upload|import)\b|\bcsv\b|\bexcel\b",
     "1. Open the **Clinical Dashboard** and choose the condition and model.\n"
     "2. Download the example upload template if needed, then **Upload patient records** (CSV or Excel, "
     "de-identified) and choose **Save records to review queue**.\n"
     "3. Under **Review and score**, score the ready records, open each one to check it, and save your **Review "
     "decision**. **Download patient PDF** gives a one-page summary.", "doctor"),
    (r"\b(prescribe|medication plan|medicine plan|assign)\b|\b(add|create|change|stop)\b.*\b(medicines?|medications?|meds|doses?)\b",
     "Open **Advanced Analytics & Patient Intelligence**, choose the patient, then the **Medication Monitoring** tab. "
     "Fill in the plan and choose **Save medication plan**. The patient is notified (by email without the medicine "
     "name, if they allow emails), and you can pause or stop the plan there later.", "doctor"),
    (r"\bprevention\b",
     "Open **Advanced Analytics & Patient Intelligence**, choose the patient and the **Prevention Plan** tab. Start "
     "from AHEAD's standard advice for the condition, adjust the targets, then **Approve and send** (or **Save "
     "draft**). The model never chooses the targets.", "doctor"),
    (r"\b(ai|gemini)\b.*\bsummar|\bsummar\w*\b",
     "Open **Advanced Analytics & Patient Intelligence**, choose the patient and the **Clinical Insights** tab. The "
     "record summary there uses no AI. The optional Gemini summary appears only if the patient allowed it in their "
     "Settings and their consent is current.", "doctor"),
    (r"\b(only|fewer|less|which|stop)\b.*\b(notifications?|results|alerts|emails)\b",
     "Open **Settings → Preferences → Emails**. Under **Tell me about**, choose every new result (the default) or "
     "only results that need attention; switch the optional emails on or off there too.", "doctor"),
    (r"\b(link|add|connect|find|see|view|open)\b.*\bpatients?\b|\bpatients?\b.*\b(link|add|connect)\b|\bmy patients\b",
     "Patients link themselves: they enter your doctor account email under **Settings → Preferences → Care team "
     "access → Add doctor**. Linked patients then appear in **Advanced Analytics & Patient Intelligence**, where you "
     "choose one to see their records, plans and history.", "doctor"),
]
_HOW_TOS = [(re.compile(pattern), answer, who) for pattern, answer, who in HOW_TOS]


def how_to_answer(prompt: str, is_doctor: bool = False) -> str | None:
    """Steps for using AHEAD's own pages ("how do I link my doctor"), or None for anything else."""
    text = prompt.lower()
    if not HOW_CUE.search(text):
        return None
    role = "doctor" if is_doctor else "patient"
    for pattern, answer, who in _HOW_TOS:
        if who in (role, "both") and pattern.search(text):
            return answer
    return None


# ----------------------------------------------------------------------------- app intents
# Answered on-device (no Gemini call): page navigation and the patient's own status.
PAGE_ALIASES = [
    ("screenings", ("screening", "screenings", "assessment", "assessments", "test", "tests")),
    ("wellness", ("lifestyle", "wellness", "health tracker", "tracker", "check-in", "check in")),
    ("food", ("food", "nutrition", "meal", "meals")),
    ("medications", ("medication", "medications", "medicine", "medicines", "meds", "pills",
                     "plan", "my plan", "prevention plan", "prevention plans")),
    ("notifications", ("notification", "notifications", "reminders")),
    ("analytics", ("analytics", "data", "datasets", "data and analytics", "data & analytics")),
    ("clinical", ("clinical dashboard", "clinical")),
    ("intelligence", ("intelligence", "patient intelligence", "patients")),
    ("settings", ("settings", "account", "profile")),
    ("overview", ("overview", "home", "dashboard")),
]
NAV_PATTERN = re.compile(r"^(?:please\s+|can you\s+|could you\s+)?(?:open|go to|go|show me|show|take me to|navigate to)\s+(?:the\s+|my\s+)?(.+?)(?:\s+page)?[.!?]*$")
PATIENT_ONLY = {"wellness", "food", "medications", "notifications"}


def _allowed_pages(is_doctor: bool) -> set:
    return {key for key, _label, _icon, doctor_only in NAV_ITEMS
            if (not doctor_only or is_doctor) and not (is_doctor and key in PATIENT_ONLY)}


PAGE_SUFFIX = r"(?:\s+(?:page|screen|section|tab|analy[sz]er|tracker|monitoring|dashboard|center|centre|history))*"


def _navigation_target(text: str, is_doctor: bool) -> str | None:
    """Page key for requests like "open medication" / "take me to the food analyzer", else None.

    The whole requested name must be one page's alias (plus words like "page" or "tracker"), the most
    specific alias wins, and a page this user cannot open is never chosen.
    """
    match = NAV_PATTERN.match(text.lower().strip())
    if not match:
        return None
    wanted = re.sub(r"^(?:a|an|the|my)\s+", "", match.group(1).strip())
    best = None
    for key, aliases in PAGE_ALIASES:
        for alias in aliases:
            if re.fullmatch(re.escape(alias) + PAGE_SUFFIX, wanted) and (best is None or len(alias) > len(best[1])):
                best = (key, alias)
    if best and best[0] in _allowed_pages(is_doctor):
        return best[0]
    return None


MEDS = r"(?:medications?|medicines?|meds|doses?|pills?|tablets?)"
# "How do I set up reminders for medicines" is a how-to question, not a request for today's doses.
HOW_TO = re.compile(r"\b(how|where|can i|could i|set ?up|setup|remind(?:er|ers)?|calendar|add|change|edit|stop|"
                    r"should i|safe|side effects?|what is|what are the)\b")
DOSE_QUESTION = re.compile(
    rf"\b(?:my|today'?s?|tonight'?s?)\s+(?:\w+\s+)?{MEDS}\b"                       # my meds, today's doses
    rf"|\b{MEDS}\b.*\b(?:today|tonight|now|this (?:morning|evening))\b"             # which pills ... today
    rf"|\bdid i (?:take|miss)\b|\bwhat do i take\b|\bwhen do i take\b")


def _asks_for_doses(lowered: str) -> bool:
    """True for questions about the patient's own doses ("what are my meds today", "did I take my pills"), not for
    questions that only mention medicines ("how do I set up reminders for medicines", "is metformin safe")."""
    if not DOSE_QUESTION.search(lowered):
        return False
    return not HOW_TO.search(lowered) or bool(re.search(r"\bwhat are my\b|\bdid i\b", lowered))


def _status_answer(text: str, user_id: int) -> str | None:
    """The patient's own lifestyle score or today's doses, read from their account."""
    from ahead.platform_data import lifestyle_history, local_today, scheduled_doses
    lowered = text.lower()
    if not re.search(r"\b(my|i|me)\b", lowered):
        return None
    about_score = re.search(r"\b(lifestyle|wellness)\b", lowered) or re.search(r"\bmy (latest |last )?score\b", lowered)
    if about_score and (not HOW_TO.search(lowered) or re.search(r"\bwhat(?:'s| is) my\b", lowered)):
        latest = lifestyle_history(user_id)
        if not latest:
            return "You haven't saved a lifestyle check-in yet. Open the **Health & Lifestyle Tracker** to record your first one."
        return (f"Your latest lifestyle score is **{latest[0]['scores']['overall']} out of 100**. "
                "Open the Health & Lifestyle Tracker to see what makes it up.")
    if _asks_for_doses(lowered):
        doses = scheduled_doses(user_id, local_today(), local_today())
        if not doses:
            return "You have no medication doses scheduled today."
        # The doctor typed the name and dose: shown as text, never as links or formatting.
        lines = [f"- {d['time']} · {markdown_literal(d['name'])} {markdown_literal(d['dosage'] or '')} — {d['status']}" for d in sorted(doses, key=lambda d: d["time"])]
        return "Here are today's doses:\n\n" + "\n".join(lines) + "\n\nYou can record them on **My plan**."
    return None


# ----------------------------------------------------------------------------- read aloud
def _speakable(markdown: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", markdown)        # links -> their text
    text = re.sub(r"[*_`#>]+", "", text)                                 # emphasis, code, headings, quotes
    text = re.sub(r"^\s*(?:[-•]|\d+\.)\s+", "", text, flags=re.M)      # list markers
    return re.sub(r"\s+", " ", text).strip()


def _read_aloud(text: str) -> None:
    """Speak through the browser's own speechSynthesis (free, on-device) with a Stop control (ahead/browser.py)."""
    t = tokens()
    read_aloud(_speakable(text), t["accent_soft"], t["accent"])


def _reply(history: list, is_doctor: bool = False) -> str:
    """Send the transcript as proper user/model turns with a system instruction."""
    return chat_reply(DOCTOR_SYSTEM_PROMPT if is_doctor else SYSTEM_PROMPT, _turns(history))


def _turns(history: list) -> list:
    """The last 12 messages as Gemini turns. Questions answered here from the patient's own data (today's doses,
    their score) are left out, with their answers: that data never goes to Gemini."""
    return [{"role": "user" if m["role"] == "user" else "model", "text": m["content"]}
            for m in history if not m.get("local")][-12:]


def _reply_stream(history: list, is_doctor: bool = False):
    """_reply, streamed piece by piece (same turns and instructions)."""
    return chat_stream(DOCTOR_SYSTEM_PROMPT if is_doctor else SYSTEM_PROMPT, _turns(history))


def _safe_stream(pieces):
    """Yield the answer as it arrives. A failure never shows a traceback: before any text it becomes the usual
    message; part way, the text so far stays and a short note follows."""
    wrote = False
    try:
        for piece in pieces:
            wrote = True
            yield piece
    except Exception as exc:
        message = gemini_error_message(exc)
        yield f"\n\n_{message}_" if wrote else message


def _first_and_rest(pieces):
    """Wait (under the spinner) for the first piece, then hand back a stream that starts with it."""
    import itertools
    pieces = iter(pieces)
    first = next(pieces, None)
    return itertools.chain([] if first is None else [first], pieces)


def _append(role: str, content: str, voice: bool = False, local: bool = False) -> None:
    # Asia/Dubai like every other page; datetime.now() would follow the server clock (UTC when hosted).
    st.session_state.chat_messages.append({"role": role, "content": content, "time": local_now(), "voice": voice,
                                           "local": local})


def _sync_speak_pref() -> None:
    from ahead.pages.settings import _save_prefs
    st.session_state.prefs["speak_replies"] = st.session_state["assistant_speak_replies"]
    _save_prefs()


def render_assistant() -> None:
    robot = str(ICONS_DIR / "chatbot.svg")
    doctor = current_user()["is_doctor"]

    # page background (soft waves) in light mode; dark mode keeps the plain dark canvas
    if not is_dark():
        st.html(
            f"""
<style>
/* A fixed layer instead of `background-attachment: fixed` (repainted on every scroll). `isolation` keeps the
   layer above the app's page colour but under the content. */
[data-testid="stAppViewContainer"] {{ background: transparent !important; isolation: isolate; }}
[data-testid="stAppViewContainer"]::before {{
    content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background: url("{static_url('chatbot_bg.webp')}") center / cover no-repeat;
}}
[data-testid="stMain"] {{ background: transparent !important; }}
[data-testid="stBottomBlockContainer"] {{ background: transparent !important; }}
</style>
"""
        )

    if st.button("← Back", key="assistant_back", type="tertiary"):
        # return to where the user came from, including a screening that was open
        go_to(st.session_state.get("assistant_return", "overview"), keep_screening=True)

    traits = "".join(f'<div class="assistant-trait">{_trait_icon(paths)}{escape(label)}</div>' for label, paths in TRAITS)
    st.html(
        f"""
<div class="ahead-card assistant-shell" style="padding: 30px 34px 22px;">
    <div class="assistant-hero">
        <div>
            <div class="eyebrow">AHEAD Assistant</div>
            <h1>Ask <span>AHEAD</span></h1>
            <p>Ask general questions about AHEAD, its three screening conditions, health measurements,
            or how to use the platform. Type, or tap the microphone in the message bar and just ask.</p>
        </div>
        <div class="assistant-mascot"><img src="{icon_uri('chatbot.svg')}" alt="AHEAD Assistant"></div>
    </div>
    <div class="assistant-traits">{traits}</div>
    <div class="assistant-try">Try asking…</div>
</div>
"""
    )

    if not st.session_state.chat_messages:
        _append("assistant", DOCTOR_WELCOME if doctor else WELCOME)

    # suggestion chips, side by side (wrap on narrow screens)
    with st.container(key="assistant_suggestions", horizontal=True, gap="small"):
        for index, (icon, text) in enumerate(DOCTOR_SUGGESTIONS if doctor else SUGGESTIONS):
            if st.button(f"{text}  →", key=f"suggest_{index}", icon=icon):
                st.session_state.pending_prompt = text
                st.rerun()

    if doctor:
        st.info(DOCTOR_NOTICE, icon=":material/privacy_tip:")
    if not gemini_available():
        st.info(
            "AHEAD Assistant needs a Gemini API key to answer. Set `GEMINI_API_KEY` in the environment or "
            "add it to `.streamlit/secrets.toml`, then restart the app."
        )

    # transcript
    for index, message in enumerate(st.session_state.chat_messages):
        is_user = message["role"] == "user"
        with st.chat_message("user" if is_user else "assistant", avatar=None if is_user else robot):
            st.markdown(message["content"])
            stamp = message["time"].strftime("%I:%M %p").lstrip("0")
            badge = '<span class="assistant-voice-badge">🎙 Voice</span>' if message.get("voice") else ""
            st.html(f'<div class="assistant-time">{badge}{stamp}</div>')
            if not is_user and index > 0:
                if st.button("Read aloud", key=f"speak_{index}", icon=":material/volume_up:", type="tertiary"):
                    st.session_state.speak_now = message["content"]
                    st.rerun()

    speak_now = st.session_state.pop("speak_now", None)
    if speak_now:
        _read_aloud(speak_now)

    voice_ready = gemini_available()
    if voice_ready:
        with st.container(key="assistant_voice_controls", horizontal=True, gap="small"):
            st.toggle("Read answers to voice questions aloud", value=pref("speak_replies"),
                      key="assistant_speak_replies", on_change=_sync_speak_pref)

    # input: typed, spoken (mic in the bar) or a suggestion chip
    submitted = st.chat_input("Ask AHEAD — type, or tap the mic to speak…", accept_audio=voice_ready)
    pending = st.session_state.pop("pending_prompt", None)   # always consume a queued suggestion
    prompt, spoken = pending, False
    if submitted is not None:
        prompt = submitted if isinstance(submitted, str) else (submitted.text or "").strip()
        audio = None if isinstance(submitted, str) else submitted.audio
        if audio is not None and not prompt:
            with st.spinner("Listening to your question…"):
                try:
                    prompt = transcribe_audio(audio.getvalue(), audio.type or "audio/wav")
                except Exception as exc:
                    _append("assistant", f"I couldn't process that recording. {gemini_error_message(exc)}")
                    st.rerun()
            if not prompt:
                _append("assistant", "I didn't catch any words in that recording. Please try again, a little closer to the microphone, or type your question.")
                st.rerun()
            spoken = True

    if prompt:
        user = current_user()
        _append("user", prompt, voice=spoken)
        target = _navigation_target(prompt, user["is_doctor"])
        if target:
            label = next(label for key, label, _i, _d in NAV_ITEMS if key == target)
            _append("assistant", f"Opening **{label}**…")
            go_to(target)
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar=robot):
            answer = (None if user["is_doctor"] else _status_answer(prompt, st.session_state.user["id"])) \
                or how_to_answer(prompt, user["is_doctor"])
            local = bool(answer)
            if local:
                st.session_state.chat_messages[-1]["local"] = True   # this question and its answer stay out of Gemini
            if not answer and gemini_available():
                # Stream: the spinner shows only until the first words arrive, then the answer writes itself out.
                with st.spinner("Thinking…"):
                    stream = _first_and_rest(_safe_stream(_reply_stream(st.session_state.chat_messages, user["is_doctor"])))
                answer = st.write_stream(stream)
                if not isinstance(answer, str):
                    answer = "".join(map(str, answer or []))
            else:
                if not answer:
                    answer = _local_answer(prompt, user["is_doctor"]) or (
                        "AHEAD Assistant is not available right now (no Gemini API key or the service could not be "
                        "reached). Try one of the suggested questions above — the screening tools work normally without me."
                    )
                st.markdown(answer)
        _append("assistant", answer, local=local)
        if spoken and pref("speak_replies"):
            st.session_state.speak_now = answer
        st.rerun()

    if len(st.session_state.chat_messages) > 1:
        if st.button("Clear conversation", key="assistant_clear", type="tertiary"):
            st.session_state.chat_messages = []
            st.rerun()
