# AHEAD risk register (Phase 3c, November 2026)

What could go wrong with AHEAD as it runs today (test data only, Render free + Neon Frankfurt), how likely and how
bad it is, and what has been done about it. Built from an independent review of the whole codebase in Phase 3c
(two reviewers who had not seen the code being written: one on sign-in, permissions and the server, one on
encryption, consent, retention and emails), plus the known limits from Phases 1–3b.

**Likelihood / impact:** High, Medium or Low, for the current prototype with test accounts only. Several would rise
sharply with real patients; the last column says what has to change first.

## 1. Fixed in Phase 3c (found by the review)

| # | Risk | Was | What changed |
|---|---|---|---|
| F1 | Anyone could sign up with a name looking like encrypted data (`enc1:…`), which broke the admin Accounts page; a doctor typing such text as a medicine name stored it unencrypted and stopped every later deploy from starting | High / High | Such text is now encrypted like any other; reads show it as typed instead of failing; the background job and `decrypt-all` cope with older rows like it |
| F2 | Changing the email needed no password and no confirmation, so two minutes at an open tab gave a permanent takeover (change email → reset password) | Medium / High | Current password required; with emails on, the new address only replaces the old one after its confirmation link is opened (migration 006); the old address is told |
| F3 | An admin could create a password-reset link for any patient, see it on screen and sign in as them, which broke "admins see no health data" | Medium / High | With emails on, admin reset links are only emailed to the account's own address and never shown |
| F4 | Emailed links fell back to the address the browser claimed, so a forged client could get a real reset email pointing at another site | Low (live sets `AHEAD_PUBLIC_URL`) / High | Real emails use only `AHEAD_PUBLIC_URL`; the server refuses to start with emails on and no public address |
| F5 | A patient's name could carry a clickable link (Markdown or a bare web address) into a doctor's notifications | Medium / Medium | Names and plan names are shown literally everywhere `markdown_literal` is used, with web addresses broken so they can't auto-link |
| F6 | Sign-up said "That email is already registered", revealing who has an account | Medium / Low | Same reply as a new account; the existing owner gets an email instead |
| F7 | Password guessing spread over many networks had no limit | Low / Medium | 30 failed sign-ins per email per 15 minutes, across all networks (high enough that a stranger can't easily lock someone out) |
| F8 | Patients could link a switched-off or erased doctor account | Low / Medium | Only active doctors can be linked |
| F9 | Withdrawn consent took up to a minute to apply in other tabs, and the calendar feed kept serving the medicine schedule | Low / Medium | Consent checked on every page load; the calendar feed stops when consent is withdrawn |
| F10 | Erasure left the person's sign-in IP addresses and rate counters behind; the audit log was kept forever | Medium / Low | Erasure blanks addresses and details in their audit entries (and unlinks a patient's); audit log kept 1 year |
| F11 | The "Time to screen again" reminder came back every 90 days after the notice clean-up | Medium / Low | Those notices are kept while their plan exists |
| F12 | The assistant's answer listing today's doses (with medicine names) was sent to Gemini in later turns, without the AI consent | Medium / Low | Answers built from the patient's own data, and the questions behind them, are never sent to Gemini |
| F13 | Emails greeted people by name, so an inbox preview paired a name with "your doctor updated your plan" | Medium / Low | No names in any email; the "switched off" email now mentions erasure after a year |
| F14 | The admin Emails list showed which patient got "Plan changed" or which doctor got "Result to review" | Medium / Low | Those show only as "Care notice" |
| F15 | `decrypt-all` could stop early and still report success | Low / Medium | Walks each table once in order, then checks nothing encrypted is left and fails loudly if something is |

## 2. Open risks (accepted for the prototype, or for the IT handover)

| # | Risk | Likelihood / impact | Mitigation now | Before real patients |
|---|---|---|---|---|
| R1 | **Real patient data on non-UAE hosting** would breach Federal Law No. 2 of 2019 (data must stay in the UAE) | Low (rule: test data only) / High | Consent text and the site say "test data only, servers in Frankfurt"; no real patients | Move to UAE hosting (AWS me-central-1, Azure UAE North); ethics approval; signed consent |
| R2 | **Models not clinically validated**: a wrong score could mislead | Medium / High | Disclaimers on every page and email; results sorted into tracks, no model-chosen treatment; doctors approve plans | Doctor review of model accuracy; ask about medical-device approval |
| R3 | **Anyone with Render access can read the data**: the encryption key lives in Render's environment | Low / High | Encryption protects the database, its backups and leaks of it; 2FA on Render, Neon, GitHub, Google | Hosting accounts owned by AURAK IT; key in a managed secret store; rotate with `new,old` |
| R4 | **Losing `AHEAD_FIELD_KEY` loses the encrypted data** | Low / High | Key in a password manager; start-up refuses a missing or wrong key, so a mistake never shows broken pages | Hand the key over with the database; test a restore |
| R5 | **Hosting runs on personal and borrowed accounts** (Brevo on the AURAK events account, a friend's Gemini key, Render/Neon on a student account) | High / Medium | Restricted Brevo key with a 100/day cap; Gemini sees test data only | AURAK-owned accounts and sending domain; rotate every secret |
| R6 | **Gemini free tier**: Google may use inputs and reviewers may read them | High (if used) / Medium with real data | Test data only; doctor AI summaries need the patient's consent; dose answers stay local (F12) | Paid or enterprise tier with no training on inputs, or no AI on patient data |
| R7 | **An admin can still take over an account when emails are off** (the reset link is shown so it can be passed on) | Low / High | Emails are on live; every reset is in the audit log and the person gets a "password changed" notice once emails are on | Keep emails on; Sign in with Microsoft for staff |
| R8 | **Patients link doctors without the doctor agreeing**, so a script could flood a doctor's list and inbox | Low / Medium | Sign-up limits, per-recipient email caps, at most one review email an hour, names shown literally (F5) | A pending/accept step on the doctor's side |
| R9 | **Proxy hop count (`AHEAD_TRUST_PROXY=3`) is not documented by Render**; if it is ever wrong, rate limits and audit IPs could use a client-supplied address | Low / Medium | Value found by testing; the per-email limit (F7) does not depend on IPs | Log the raw header once after a hosting move and set the count again |
| R10 | **Someone with database write access but no key** could copy one value's ciphertext into another field and have the app show it decrypted (the same associated data is used for every value) | Low / Medium | Needs database write access, which already means a serious breach | Bind each value to its table, column and owner (a new `enc2` format with re-encryption) |
| R11 | **Some health-related fields are readable on purpose**: risk scores, thresholds, model names, dates; also the clinic CSV's patient identifier, the upload file name, activity minutes, dose check-offs | Medium / Low (test data) | Documented; doctor pages sort and filter on scores in SQL | Encrypt the CSV identifier and file name; consider deterministic encryption or a separate store for scores |
| R12 | **Doctor review emails can rarely double** when two results arrive at the same moment, and a follow-up can be dropped if the recipient's email cap is reached (the in-app notice always arrives) | Low / Low | Per-recipient caps; in-app notices are never dropped | Lock per doctor when deciding to email |
| R13 | **Google Fonts sees visitors' IP addresses** | High / Low | Allowed in the CSP; no other third party loads in the browser | Self-host the font |
| R14 | **The CSP must allow inline styles and `blob:` scripts** (Streamlit needs them) | Medium / Low | No inline scripts or `eval`; `AHEAD_CSP=report` as an escape hatch | Re-check after Streamlit upgrades |
| R15 | **Free-tier speed and availability**: 0.1 CPU, ~1 min wake after 15 idle minutes, no uptime guarantee | High / Medium | Warm-up, caching, streamed AI answers; wake the site before demos | Paid plan or university hosting |
| R16 | **GitHub pauses the weekly-summary schedule after 60 days without commits** | Medium / Low | Noted for the handover | Re-enable or move the timer to the hosting platform |
| R17 | **Not checked on real devices**: Safari/iOS with the CSP, real phone tab-close sign-out, calendar subscribe on iPhone/Android | Medium / Medium | Chrome headless (desktop and phone sizes) passes every page with no CSP errors | Test on real devices before the demo |
| R18 | **Emails from a Gmail sender may land in Junk** | High / Low | Users told to check Junk | AURAK sending domain with SPF/DKIM |
| R19 | **"Duplicate tab" copies the sign-in** (how browsers work) | Medium / Low | 2 h idle and 12 h maximum; sign out everywhere in Settings | None needed beyond user guidance |
| R20 | **Withdrawing consent doesn't remove doctors' access by itself** | Medium / Low | Patients remove doctors in Settings; consent text says so | Ask on withdrawal whether to remove doctors too |

## 3. Checked and found sound by the review

Session tokens (256-bit, stored hashed, compared in constant time, revoked on password change and deactivation); the
2 h idle / 12 h maximum; cookie flags and the Origin check on the sign-in endpoints; the weekly task's secret
header; PBKDF2 passwords with a dummy hash for unknown emails; one-time links (single use, purpose-scoped, expiring);
every doctor read and write checks the patient link; patient data scoped to the signed-in account; parameterised
SQL throughout; escaped HTML; CSV export protected against formula injection; no secrets or health values in logs;
every write of an encrypted column goes through encryption; random nonces; key rotation; retention dates and
erasure without orphan rows; email bodies deleted once sent; budgets and caps; migrations tracked and locked.
