# Cold-Email Job Agent — PROJECT.md

## 1. Goal

Semi-automated pipeline that finds product-based companies in India, discovers a real
verified contact at each, classifies the company by niche, generates a personalized
cold email (with resume attached) matched to the most relevant part of Arjun's
background, queues it for human review, sends approved emails in small daily batches
from a personal Gmail account, and tracks replies + follow-ups.

**Human-in-the-loop is a hard requirement.** Nothing is sent without explicit approval
in the review UI.

## 2. Decisions locked in

| Question | Decision |
|---|---|
| Sending account | Personal Gmail (via Gmail API, OAuth) |
| Budget | Free tier first; small paid spend allowed if a free tier becomes a real blocker |
| Company sourcing | Public directories/APIs only (YC, ProductHunt, govt open data) — no LinkedIn/Crunchbase scraping |
| Review interface | Simple local web UI (Streamlit) |
| Resume | Single resume, attached to every email |
| Daily volume | Start ~10–15/day, ramp up gradually (see §6) |
| Follow-up cadence | **2 follow-ups** per contact (raised from 1, 2026-08-29), each sent 5 business days after the previous email, then stop |
| Follow-up approval | **Scoped exception to the human-in-the-loop rule above (2026-08-29):** once due, follow-ups are drafted *and sent* automatically -- no Approve click. Arjun's explicit choice, made after being told it deviates from "nothing sends without approval." Initial emails are untouched by this -- they still require approval as always. |
| Niche priority on ties | Consumer/marketplace preferred (Arjun's stated preference) |
| Active discovery scope | Contact discovery (Hunter.io lookups) restricted to `consumer`-niche companies for now (`niches.active_for_discovery` in settings.yaml). Fintech/data-SaaS/AI-devtools companies are still sourced and classified so no data is lost, just not looked up yet — expand the list when ready. |

## 3. Arjun's background → niche story bank

The email generator does **not** try to auto-parse the resume with NLP. Instead, a small
hand-written `story_bank.yaml` maps each niche to the 2-3 best resume bullets to use as
the "why I'm relevant" paragraph. This is far more reliable than automated extraction for
a single, unchanging resume.

| Niche | Signal to draw on |
|---|---|
| Consumer / Marketplace | Rapido: order-propagation layer (-3% cancellations, +2% net orders), rejection-nudge growth loop (+11% rides/rider), reverse-bid flow fix (+2.5% fulfillment) |
| Fintech / Payments | FreeCharge: BNPL user research (75+ customers), Hindi-first onboarding fix (+8% activation), segment analysis (fuel/e-commerce) |
| Data-heavy B2B SaaS | Indus Insights: unified SQL repo for 40k+ agent behavior analytics, hypothesis-driven migration of 40% of workflows, Power BI/SQL depth |
| AI / Dev-tools / Automation | AI-agentic browser extension for chatbot QA (saved 50+ hrs/quarter), self-built GMAT simulator web app (1K+ questions, adaptive pooling) |

Consumer wins ties because it's Arjun's stated preference, not just because it's his most
recent role.

## 4. Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌────────────────┐
│ 1. Company        │ →  │ 2. Classifier      │ →  │ 3. Contact       │
│    Sourcing        │    │    (niche tag)      │    │    Discovery      │
└─────────────────┘     └──────────────────┘     └────────────────┘
                                                              │
                                                              ▼
┌─────────────────┐     ┌──────────────────┐     ┌────────────────┐
│ 7. Tracking &      │ ←  │ 6. Scheduled       │ ←  │ 5. Review Queue  │ ←── 4. Email
│    Follow-ups       │    │    Sending          │    │    (Streamlit)   │      Generation
└─────────────────┘     └──────────────────┘     └────────────────┘
```

All modules read/write a single SQLite datastore (`agent.db`). No module talks directly
to another — each reads its input state from the DB and writes its output state back.
This keeps every stage independently re-runnable and inspectable.

Classification runs **before** contact discovery, not after, even though it's easy to
assume niche-tagging happens once you already have a contact. It has to come first: rule-
based classification is free (no external API), while contact discovery burns Hunter.io's
25-lookups/month free tier — so niche has to be known *before* discovery in order for
`niches.active_for_discovery` (§2) to actually gate anything. Running discovery first
would mean spending a lookup on a company before knowing whether it's even in-scope.

### 4.1 Company Sourcing

Pulls candidate companies from **public, ToS-compliant sources only**:
- YC company directory (public JSON behind ycombinator.com/companies, filterable by
  location/tag — no auth needed, it's public data meant for browsing)
- ProductHunt API (official, free, OAuth) — filter by topic/maker location where available
- Startup India government open registry (public dataset, India-specific, explicitly
  open data — lowest legal risk of any source)
- Manual CSV import — a hatch for Arjun to drop in companies he's already found by hand
  (e.g. from Wellfound browsing, news, personal network) so the pipeline isn't blocked
  waiting on API coverage gaps

Output: row per company in `companies` table with `name, domain, source, raw_tags,
status=new`.

### 4.2 Classification

Rule-based keyword/tag classifier against the four niches in §3 (consumer, fintech,
data-SaaS, AI/dev-tools) using the company's stated industry tags and name (no scraped
domain description exists in the schema — classification works off `raw_tags` +
`name` only). No ML model needed at this scale — a scored, whole-word keyword match
per niche, highest score wins, ties broken toward consumer per Arjun's preference.
Unclassifiable companies (`status=skipped`, no keyword hits at all) are left for manual
tagging rather than guessed.

Runs immediately after sourcing (§4.1) and before contact discovery (§4.3) — see the
note in §4 architecture on why the order matters.

Output: `companies.niche` set and `companies.status` moved from `new` to `classified`
(or `skipped` if unclassifiable).

### 4.3 Contact Discovery (real API, not scraping)

Given a company domain, finds a named contact with a **verified** email — never a
guessed or scraped address. Even though Arjun is targeting product roles specifically,
contact discovery deliberately does **not** just look for the most senior PM — it looks
for whoever is most likely to actually reply:

- **Niche-gated**: only runs against companies whose classified niche (§4.2) is in
  `settings.yaml`'s `niches.active_for_discovery` (currently `consumer` only —
  see §2). Fintech/data-SaaS/AI-devtools companies still get sourced and classified,
  they just sit at `status=classified` without an API lookup spent on them until that
  list is expanded.
- **Role search priority: HR/Talent/Recruiting first, Product Manager second.**
  The finder API is queried for HR/Talent/People/Recruiting titles at the company first
  (highest reply-rate role for cold outreach); if none turn up, it falls back to
  PM/APM/Product Lead/founder titles. This is a search-order preference, not a hard
  filter — either role type is an acceptable outcome, HR is just tried first.
- Each contact is tagged `role_category` (`hr` | `product` | `other`) so reply rates by
  role category can be queried later from `email_queue`/`contacts` — same idea as the
  resume-attachment tracking in §4.4, just another variable worth knowing which one wins.
- Start on **Hunter.io free tier** (25 lookups/month) behind a thin `EmailFinder`
  interface, so swapping to Apollo/Prospeo/Snov.io later (if free tier is a bottleneck)
  is a one-file change, not a rewrite.
- Only accept results the API marks as verified/deliverable-confidence above a threshold;
  anything lower gets flagged `needs_manual_check` instead of auto-queued.
- No LinkedIn scraping, no guessing `first.last@domain.com` patterns without
  verification — this is the #1 deliverability and ToS risk in this whole system (see §7).

Output: row per contact in `contacts` table with `company_id, name, role, role_category,
email, verification_confidence, source_api`.

### 4.3.1 Apollo.io Contact Import (parallel path, not Hunter-gated)

Arjun switched to sourcing contacts primarily via Apollo.io himself (exported from
Apollo's UI, not this pipeline's own lookup) — more control/coverage than Hunter's
25-lookups/month free tier, and he prefers curating the list by hand. This is a
**third path into `contacts`**, alongside §4.1 (companies only, no contact yet) and
§4.3 above (contact via a live Hunter lookup):

- Apollo's People Export gives a company AND an already-resolved contact/email in
  the *same* CSV row, so `agent/apollo_import.py` does both steps at once:
  find-or-create the company (deduped by the `Website` column's domain — not the
  contact's email domain, which can differ when a person's email uses a parent
  company's domain, e.g. a Zomato-brand employee with a `@zomato.com` address),
  then insert the contact directly.
- **Not gated by `niches.active_for_discovery`** — that setting exists to conserve
  Hunter's free-tier lookup quota; Apollo already resolved the email, so there's no
  API cost left to gate, and Arjun hand-picked these companies/contacts himself in
  Apollo regardless of niche. The company still needs `scripts/classify_companies.py`
  run (or re-run) afterward before `scripts/generate_emails.py` picks up its contacts.
- Only Apollo's own `"Verified"` email status is trusted enough to auto-queue
  (`status='verified'`); anything else (e.g. `"Verifying"`) is flagged
  `needs_manual_check` — same never-guess discipline as §4.3/§7, applied to Apollo's
  status field instead of a numeric confidence score (Apollo's `Email Confidence`
  column was blank on every row of Arjun's real export, so status is the only usable
  signal).
- `role_category` prefers Apollo's own `Departments` column over the existing
  title-keyword heuristic (`categorize_role`) — more reliable: a real example,
  "Product Operations Manager", contains none of `ROLE_KEYWORDS`' literal phrases
  ("product manager", "product owner", …) and would classify as `other` by title
  alone, but `Departments` explicitly says "Product, Operations". Falls back to
  `categorize_role(title)` when `Departments` is blank or doesn't mention HR/Product.
- `Industry` + `Keywords` columns feed `companies.raw_tags`, so the *existing*
  rule-based niche classifier (§4.2) runs on Apollo-sourced companies unchanged —
  no separate classification path. Real result importing Arjun's first Apollo batch
  (16 contacts / 6 companies, 2026-08-23): JioHotstar/Zomato/District → `consumer`,
  Pixxel → `ai_devtools`, LinkedIn → `data_saas`, Blinkit → unclassified (Apollo's
  own `Industry`/`Keywords` data for it was too generic — "technology, information &
  internet" — to hit any niche keyword, even though a human would obviously call it
  quick-commerce/consumer). Left `status='skipped'` rather than guessed, per §4.2's
  existing discipline — Arjun can hand-tag it if he wants those 3 contacts to reach
  email generation.

CLI: `py scripts/import_apollo_contacts.py path/to/apollo-export.csv`. Required
columns: `Company Name`, `Email`. Used when present: `First Name`, `Last Name`,
`Title`, `Website`, `Industry`, `Keywords`, `Departments`, `Email Status`. Extra
columns (Apollo exports ~70) are ignored. 24 tests in `tests/test_apollo_import.py`
against a small fixture CSV mirroring Apollo's real headers.

Verified end-to-end against Arjun's real export (2026-08-23): 16 rows → 6 companies,
16 contacts (11 `verified`, 5 `needs_manual_check`) → classify step → **9 real email
drafts generated** for the 9 verified contacts at classified companies (2 verified
Blinkit contacts correctly skipped — unclassified company, no niche template to use).

### 4.4 Email Generation

- One template per niche (4 templates total), each with a placeholder for the
  company name, contact first name, and the story bullets (§3).
- Resume attached as a **PDF** to every generated draft by default (converted once,
  up front, from the source `.docx` — see setup note below). `email_queue` stores an
  `attached_resume` flag per row so if a no-attachment variant is ever tried later for
  comparison, reply-rate differences are queryable rather than guessed at.
- Output is a **draft**, not a sent email — written to the `email_queue` table with
  status `pending_review`. Nothing here touches the Gmail API.

**Content redesign (2026-08-24).** Arjun rewrote the subject/body brief directly
after testing the Streamlit review queue for the first time:

- **Subject is now a fixed personal-branding line**, not per-company:
  `"Seeking Product Roles | Rapido (Marketplace) | Ashoka University"` — no
  `{{company_name}}` placeholder in it anymore (still appears in the body).
- **Every email now leads with the Rapido story, regardless of the target
  company's niche** (`RESUME_STORY_NICHE = "consumer"` in
  `agent/email_generation.py`) — a deliberate simplification over the prior
  per-niche story swap (fintech targets got FreeCharge bullets, data/SaaS got
  Indus Insights, AI/dev-tools got personal projects). Rapido is Arjun's
  strongest, most recent, most quantified experience, and leading with it
  everywhere was judged better than diluting across 4 different pitches.
  `niche` is still required (a company must be classified before an email
  generates) and still recorded on the `email_queue` row — it just no longer
  selects which story or template content is used.
- **Body is now: a "won't take much of your time" opener → one-line self-intro
  (Product Intern at Rapido, marketplace team, matching) → interest line
  naming the target company → 2 short bullet points → a closing ask + resume
  mention.** New `build_story_bullets` (agent/email_generation.py) renders
  bullets as short dash-prefixed lines instead of `build_story_paragraph`'s
  stitched prose — the two are kept as separate functions (the old one still
  tested/working, just no longer wired into `render_email`) since they produce
  genuinely different shapes of text, not interchangeable via a flag.
  `story_bank.yaml`'s consumer bullets were tightened to short one-liners to
  suit bullet-point display (the fintech/data_saas/ai_devtools bullets are
  unused by templates now but left in place, not deleted).
- **All 4 template files now hold identical content** (same subject, same
  intro, same Rapido bullets) — a deliberate, low-risk choice over collapsing
  the per-niche template lookup in code: `generate_pending_emails` still
  resolves `templates_dir / f"{niche}.txt"` unchanged, it's just that all 4
  files render the same text now. Editing the wording means editing all 4
  files identically; a single-shared-template refactor is a reasonable future
  cleanup if that duplication becomes a real maintenance problem.
- Regenerated all pending drafts against the new template (the 9 real Apollo-
  sourced drafts from §4.3.1, plus 2 older dev/test-fixture drafts predating
  this session that were sitting in the queue) — verified a real rendered
  draft matches the brief exactly. One pre-existing `approved` row (a Zepto
  draft from 2026-08-10, clearly old dev/test data) was left untouched rather
  than touched without being asked.

**Content revision (2026-08-29), prompted by reverse-engineering a friend's
cold-email tracker** (`data/Email Mastersheet.xlsx`) to design module 8 below:

- **`{{company_name}}` is back in the subject line**, reversing the
  2026-08-24 fixed-branding-subject decision above. Sending one identical
  subject to dozens of recipients from a single personal Gmail account is a
  real spam-pattern/deliverability risk on top of losing personalization —
  worth catching before any real sends have gone out (none had, at either
  revision).
- **A low-friction opt-out line was added to the body** (`"No worries if
  this isn't the right fit or timing — feel free to let me know either
  way."`). §7 already claimed every send had one; it didn't in the actual
  template text until now.
- **Two new follow-up templates**, `config/templates/followup1.txt` /
  `followup2.txt` — short bumps, not niche-specific (every initial email
  already uses the same Rapido content regardless of niche, so a follow-up
  needs no niche variant either), rendered by the new
  `render_followup_email()` in `agent/email_generation.py`.

**Setup note**: the resume you shared is a `.docx`
(`Arjun_Khanna_CV_V4.docx`). It needs a one-time export to PDF (Word/Google Docs →
"Save as PDF") before build starts, since re-generating a PDF from the docx on every
send is unnecessary work for a file that doesn't change per-email.

### 4.5 Review Queue (Streamlit)

Local web app, run with `streamlit run review_app.py`, now two tabs:
- **Review Queue tab**: table/list view of pending *initial-email* drafts: company,
  contact, niche, subject, body preview, resume attachment confirmation. Per-row:
  **Approve**, **Edit** (inline body edit before approving), **Reject**. Approving sets
  status `approved` (eligible for the next send batch); nothing sends from here
  directly — sending is a separate scheduled step, so there's always a buffer between
  "approved" and "actually left the outbox." Follow-ups never appear here — see §4.7,
  they auto-approve.
- **Tracking Dashboard tab** (added 2026-08-29, §4.7): summary metrics, a per-contact
  automated-stage table, and the manual per-company outcome editor.

### 4.6 Scheduled Sending (daily caps + deliverability safeguards)

- A script run once/day (Windows Task Scheduler, or manually) picks up to
  `daily_cap` rows with status `approved`, sends via **Gmail API** (not raw SMTP —
  gives proper threading, and reply tracking for free), and marks them `sent` with
  Gmail `thread_id` stored for §4.7.
- **Warm-up ramp**: cap starts at 10/day, +2-3/day per week as long as bounce/complaint
  rate stays low, capped at a ceiling you set once comfortable (e.g. 25-30/day). Ramp
  state lives in a small `send_config` table, not hardcoded.
- **Circuit breaker**: if bounce rate in the last batch exceeds a threshold (e.g. >5%),
  the sender halts and flags for manual review instead of continuing to send.
- Sends are spaced out within the day (not fired as one burst) to look human.
- Every send includes a simple opt-out line — cheap insurance against spam complaints
  even where not strictly legally required (see §7).

### 4.7 Tracking & Follow-ups

**Origin (2026-08-29)**: designed by reverse-engineering a friend's cold-email
spreadsheet (`data/Email Mastersheet.xlsx`) to see how he tracked sends/follow-ups/
replies. One important finding shaped this design: his `Outreach.Stage` column showed
"Replied" on 851 of 852 sent rows — not a real reply-detection signal, it turned out
to be a mislabeled "follow-up sequence finished" flag. His actual reply/outcome
tracking lived entirely in a separate, hand-maintained `Company` sheet (Did Not Reply /
Rejected / Got Referral / Interview stages / Offer). This module builds the real
thing — genuine Gmail-thread reply detection — while also adopting his sheet's good
idea: a manual per-company outcome tracker, since only Arjun can know the real
conversation outcome, same as his friend could only know his by reading his own inbox.

- **Gmail threading** (`agent/gmail_client.py`): follow-ups are sent onto the same
  Gmail thread as the contact's initial email — `GmailApiSender.send()` takes an
  optional `thread_id`, looks up the thread's last message for its `Message-ID`
  header, sets `In-Reply-To`/`References` and a `Re:`-prefixed subject, and passes
  `threadId` to the Gmail API. No extra column stores the prior Message-ID; it's
  fetched live from the thread each time.
- **Reply detection** (`agent/tracking.py` + `GmailReplyChecker` in
  `agent/gmail_client.py`): `check_replies` polls each contact's active thread; a
  message in the thread not from `sender_email` means a real reply, which marks *all*
  of that contact's `email_queue` rows `replied` — retiring them from follow-up
  consideration for good.
- **Bounce detection** (same module): `check_bounces` searches the sender's own inbox
  for a mailer-daemon delivery-failure message addressed to the recipient (bounce mail
  doesn't land in the original thread, so this can't be a thread check — a heuristic
  search, same "not perfect precision by design" tradeoff as the niche classifier and
  Hiring Posts relevance filter). Marks the row `bounced` and increments
  `send_log.bounce_count` for its original send date — this is the piece that was
  missing for §4.6's circuit breaker, which read a permanently-zero `bounce_count`
  until now.
- **Follow-up generation** (`agent/followups.py`): **2 follow-ups** per contact (raised
  from 1, 2026-08-29), each `business_days_wait` (5) business days after the previous
  email, using `config/templates/followup{1,2}.txt`. **Follow-ups auto-send — they skip
  the review queue entirely**, inserted directly as `status='approved'` rather than
  `pending_review` (Arjun's explicit choice; see the decision table in §2 for the
  human-in-the-loop exception this carves out). They feed straight into the existing
  `send_approved_emails` path (§4.6) on the next `send_batch.py` run — no separate send
  code. After the 2nd follow-up's wait period elapses with no reply, status →
  `no_response` and the contact is retired (no repeat pestering).
- **Daily entry point**: `scripts/check_replies.py`, meant to run once a day via
  Windows Task Scheduler *before* `send_batch.py` (same manual-registration pattern as
  that script — not auto-registered). Order inside it matters: bounces → replies →
  follow-up generation, so a contact whose bounce/reply was just detected doesn't also
  get a follow-up queued in the same run.
- **Manual per-company outcome tracker** (`agent/tracking_dashboard.py`,
  `companies.outcome_status`/`outcome_notes`): mirrors the friend's `Company` sheet —
  `did_not_reply | rejected | got_referral | intern_call | interview | on_hold |
  offer_received`, set by hand via the Tracking Dashboard tab (§4.5). Deliberately
  never inferred from the automated stage above (e.g. a `no_response` contact isn't
  auto-marked `did_not_reply` — Arjun might have a referral in progress the automation
  can't see).
- **Gmail OAuth scope**: `gmail.readonly` was added alongside `gmail.send` in
  `agent/gmail_client.SCOPES` for the `threads().get`/`messages().list` calls above —
  free to change now since no real Gmail credentials were configured yet;
  `scripts/gmail_auth.py` needs a (re-)run once they are.
- **Known gap**: no live Gmail send/check has happened yet (same caveat as §8's module
  7 entry) — reply/bounce detection and follow-up auto-send are verified against a
  fake `ReplyChecker`/`EmailSender` only so far.

## 5. Tech stack

- **Python 3.11+** — best-supported ecosystem for Gmail API, Streamlit, and scheduling
  glue; no reason to reach for anything else at this scope.
- **SQLite** (via `sqlalchemy` or plain `sqlite3`) — single-file datastore, zero ops
  overhead, plenty for hundreds-to-low-thousands of companies/contacts. Easy to inspect
  by hand (DB Browser for SQLite) when debugging.
- **Gmail API** (`google-api-python-client` + OAuth) — sending, threading, reply
  detection. Chosen over SMTP specifically because thread/reply tracking is native.
- **Streamlit** — review queue UI; fastest path to an approve/edit/reject dashboard.
- **Hunter.io free tier** — contact discovery/verification, behind an interface so it's
  swappable.
- **Windows Task Scheduler** — daily trigger for the send-batch and reply-check scripts
  (no need for a persistent server/cron daemon for a personal tool like this).
- **PyYAML** — story bank and templates as human-editable YAML, not buried in code.

## 6. Datastore schema (SQLite, `agent.db`)

```
companies      id, name, domain, source, raw_tags, niche, status,
               outcome_status, outcome_notes (manual per-company outcome
               tracker, §4.7 -- did_not_reply|rejected|got_referral|
               intern_call|interview|on_hold|offer_received, NULL until
               triaged by hand), created_at
contacts       id, company_id, name, role, role_category (hr|product|other), email,
               verification_confidence, source_api, status, created_at
email_queue    id, contact_id, company_id, niche, subject, body, kind
               (initial|followup), followup_number (0=initial, 1/2=which
               follow-up round), attached_resume (bool), status (pending_review|
               approved|rejected|sent|bounced|replied|no_response), gmail_thread_id,
               created_at, sent_at
send_log       id, date, sent_count, bounce_count   -- drives the warm-up ramp
               and circuit breaker
send_config    daily_cap, ramp_step, ramp_ceiling    -- single-row config table
```

`outcome_status`/`outcome_notes` and `followup_number` were added after the original
schema shipped (2026-08-29) — `agent/db.py`'s `init_db` migrates an existing `agent.db`
in place via idempotent `ALTER TABLE` (additive-only, no data loss), not just
`CREATE TABLE IF NOT EXISTS`.

## 7. Legal / deliverability / ToS risks and mitigations

| Risk | Mitigation in this design |
|---|---|
| **LinkedIn/Crunchbase scraping** — ToS violation, account ban risk, legally contested territory (hiQ v. LinkedIn line of cases) | Not used at all. Sourcing restricted to public APIs/open data (§4.1); contact discovery restricted to a licensed email-finder API (§4.3), never scraped from LinkedIn profiles. |
| **Guessed email addresses** (`first.last@domain.com` patterns) — high bounce rate, hurts sender reputation, sometimes hits wrong/uninvolved people | Only verified-confidence results from the finder API are auto-queued; low-confidence ones are routed to manual check instead of guessed. |
| **Personal Gmail spam/suspension risk** — cold outreach from a personal account can trigger Google's abuse detection | Low starting cap + gradual ramp (§4.6), spaced sends (not bursts), circuit breaker on bounce rate, human-approved content (reduces spammy-pattern risk vs. templated blasts), Gmail API (not raw SMTP relay, which Google trusts less). |
| **Spam complaints** — recipients marking as spam damages both deliverability and the personal Gmail account's standing | Every email includes a low-friction opt-out/"let me know if not relevant" line (§4.4); strictly 2 follow-ups (§4.7), then automatic retirement — no repeated unsolicited contact. |
| **India DPDP Act 2023** — processing personal data (name + email) of individuals | Data collected is limited to what's needed for outreach (name, role, email, company) and sourced from data the individual/company has made available for business contact (verified business email finder, not scraped personal profiles). No sensitive personal data categories involved. Keep the DB local, not shared/sold, and support deleting a contact's record on request. |
| **CAN-SPAM / general spam law exposure** | Even though individual job-outreach emails are lower-risk than commercial marketing blasts, the design still includes sender identification and an opt-out line as cheap, standard-practice insurance. |

## 8. Module build order

1. ✅ **Datastore + config** — schema (§6), `story_bank.yaml`, niche templates, resume file
   path config. Nothing works without this; build and sanity-check it first.
   Implemented in `agent/config.py` (typed, validating `Settings`/`NicheStory` loaders)
   and `agent/db.py` (schema init + seeded `send_config` + connection helper), with a
   pytest suite in `tests/` and a manual sanity-check script at `scripts/init_db.py`.
2. 🟡 **Company sourcing** — start with manual CSV import (unblocks everything downstream
   immediately) then wire up YC/ProductHunt/Startup-India pulls.
   Manual CSV import is implemented: `agent/sourcing.py` (`CompanySource` ABC,
   `ManualCsvSource`, `import_companies` with domain-dedup against the DB's unique
   index) and `scripts/import_companies_csv.py` as the CLI entry point. Drop a CSV
   with `name` (required), `domain`, `tags` (semicolon-separated) columns anywhere
   and run `py scripts/import_companies_csv.py path/to/file.csv`.
   YC/ProductHunt/Startup India pulls are **not built yet** — each is a real
   third-party API/endpoint that needs its response shape verified live before
   writing a parser against it, rather than guessed from memory.
   **Apollo.io contact import** (2026-08-23, see §4.3.1) is a parallel path that
   feeds `contacts` directly alongside company sourcing — Arjun switched to Apollo
   as his primary contact source over Hunter's free-tier limits. Verified end-to-
   end against his real 16-contact export: 6 companies created, 16 contacts
   inserted, 9 real drafts generated after classification.
3. ✅ **Classification** — rule-based niche tagger. Reordered ahead of contact discovery
   (was step 4) since it's free and has to run first for `niches.active_for_discovery`
   to gate anything — see the note in §4.
   Implemented in `agent/classification.py` (whole-word keyword scoring, ties broken
   by `niches.order`) with keywords in the editable `config/niche_keywords.yaml`
   (same pattern as `story_bank.yaml`) and a CLI entry point at
   `scripts/classify_companies.py`. Unclassifiable companies land at
   `status=skipped` rather than being guessed.
4. ✅ **Contact discovery** — Hunter.io free-tier integration behind the `EmailFinder`
   interface.
   Implemented in `agent/contact_discovery.py` (`EmailFinder` ABC, `HunterEmailFinder`
   against Hunter's documented Domain Search endpoint, `select_contact` applying
   `role_search_order`, `discover_contacts_for_pending_companies` doing the DB
   orchestration with a `max_lookups` safety cap) and `scripts/discover_contacts.py`
   as the CLI entry point (reads `HUNTER_API_KEY` from the environment — never
   committed). Tested entirely against a fake HTTP session/finder (33 tests) so
   development didn't spend any of the real 25-lookups/month free tier; nothing has
   been run against the live API yet since there's no key configured.
5. ✅ **Email generation** — template + story-bank + resume-attach logic, writes to
   `email_queue` as `pending_review`.
   Implemented in `agent/email_generation.py` (`build_story_paragraph` stitches the
   first 2 story-bank bullets per niche, `render_email` fills the 4 template
   placeholders, `generate_pending_emails` does the DB orchestration — only
   `status='verified'` contacts are picked up, `needs_manual_check` ones are
   deliberately skipped per §4.3) and `scripts/generate_emails.py` as the CLI entry
   point. Wrote the two remaining templates (`data_saas.txt`, `ai_devtools.txt`)
   so all four niches now render, even though only `consumer` currently reaches
   this stage. Verified end-to-end against the real pipeline (import → classify →
   discover with a fake finder → generate) — a real rendered draft read correctly
   and its UTF-8 (e.g. em dashes) round-tripped intact through SQLite.
6. ✅ **Review queue UI** — Streamlit approve/edit/reject app.
   Implemented in `agent/review_queue.py` (`list_pending_drafts`,
   `approve_draft`/`reject_draft` — both guarded so acting on an
   already-actioned row is a safe no-op, e.g. two browser tabs open on the same
   draft) with `review_app.py` at the repo root as the Streamlit entry point
   (`streamlit run review_app.py`): one card per pending draft with company/
   contact/niche, a read-only subject, an editable body, and Approve/Reject
   buttons. Approving only flips status to `approved` — sending is a separate
   step (module 7), so there's always a review buffer.
   DB logic has 9 passing tests. The Streamlit page itself was launched
   headless against real seeded data (full pipeline: import → classify →
   discover → generate) and confirmed to boot without exceptions with a
   passing health check; no browser-automation tool was available in this
   environment to click through it, so the exact `approve_draft` call the
   Approve button makes was separately exercised directly against the same
   live DB and confirmed to transition status correctly. Visual
   rendering/click-through in an actual browser has **not** been confirmed —
   worth a quick manual check (`streamlit run review_app.py`) before relying
   on it.
7. ✅ **Scheduled sending** — daily-cap batch sender with warm-up ramp + circuit breaker.
   Implemented in `agent/sending.py` (`EmailSender` ABC — same swap pattern as
   `CompanySource`/`EmailFinder` — `apply_ramp_if_due` growing `send_config.daily_cap`
   by `ramp_step` once `ramp_interval_days` elapse, a pre-flight bounce-rate circuit
   breaker checked against the most recent day with sends, `send_approved_emails`
   doing the batch orchestration with a small randomized delay between sends) and
   `agent/gmail_client.py` (MIME construction + resume attachment, `GmailApiSender`
   wrapping `users.messages.send`, with token refresh handled automatically). Send-
   time failures leave a row at `status='approved'` for retry rather than being
   conflated with a bounce — bounces are an async signal module 8 detects later, not
   something a synchronous send call can know. `scripts/gmail_auth.py` runs the
   one-time OAuth flow (needs a Google Cloud OAuth client you create yourself);
   `scripts/send_batch.py` is the daily entry point.
   google-api-python-client's dependency chain wanted a newer `protobuf` than
   Streamlit (module 6) tolerates — pinned `protobuf==5.29.6` after confirming both
   import together, since the REST/discovery-based Gmail client doesn't touch the
   `google-api-core`/`proto-plus` code paths that wanted the newer version.
   Tested with 21 cases (ramp math, cap accounting, circuit breaker, send-failure
   handling, delay spacing, MIME/attachment round-trip) against a fake `EmailSender`
   — no real Gmail credentials exist yet, so nothing has been sent live. Verified the
   full pipeline end-to-end (import → classify → discover → generate → approve →
   send) with a fake sender standing in for Gmail; all three approved drafts came out
   correctly marked `sent` with thread ids.
8. ✅ **Tracking + follow-ups** — reply/bounce polling, 2-round follow-up generation,
   tracking dashboard. See §4.7 for the full design (including the friend's-spreadsheet
   origin) and §4.5 for the dashboard UI.
   Implemented in `agent/tracking.py` (`check_replies`/`check_bounces` against the
   `ReplyChecker` interface — same swappable-interface pattern as `EmailSender`/
   `EmailFinder`/`CompanySource`), `agent/followups.py` (`business_days_since`,
   `generate_due_followups` — auto-approves rather than queuing for review, see §2's
   decision table), `agent/gmail_client.py` (`GmailReplyChecker`, plus threading
   support added to `GmailApiSender.send()`), and `agent/tracking_dashboard.py`
   (`dashboard_rows`, `summarize`, `set_company_outcome` — DB access only, same
   Streamlit-free split as `agent/review_queue.py`). `scripts/check_replies.py` is the
   daily entry point (Windows Task Scheduler, same manual-registration pattern as
   `send_batch.py`). `review_app.py` gained a second tab for the dashboard.
   28 new tests across `tests/test_followups.py`, `tests/test_tracking.py`, and
   `tests/test_tracking_dashboard.py`, plus additions to `tests/test_sending.py` and
   `tests/test_gmail_client.py` for the threading changes (fake `ReplyChecker`/
   `EmailSender`, no real Gmail credentials touched — same as the rest of the sending
   pipeline, nothing has been verified against live Gmail yet). Full suite: 237 passed.

Each module is runnable and testable in isolation against the shared SQLite DB before
the next one is built — no module should require a later one to exist to be verified.

## 8.1 LinkedIn Job Lookup (standalone, separate from the pipeline above)

A second, independent tool: search LinkedIn job postings and export matches to an
Excel file. Built alongside the pipeline but deliberately **not wired into it** —
output is a spreadsheet Arjun works from by hand, not `agent.db` rows.

- **Stays logged out of LinkedIn.** Uses the Apify Actor
  `curious_coder/linkedin-jobs-scraper`, which scrapes LinkedIn's public jobs-search
  page (the same one a signed-out visitor sees) via Apify's own infrastructure — no
  cookies, no account, so Arjun's personal LinkedIn login/session is never touched and
  carries none of the ToS/ban risk that account-based scraping would (see §7's
  LinkedIn risk note, which is about *account* scraping specifically).
- **Search scope does not auto-parse the resume.** Same discipline as the
  hand-curated `story_bank.yaml` (§3): a fixed list of PM-track job titles is
  searched instead — "Product Manager", "Associate Product Manager", "APM",
  "Product Lead", "Growth Product Manager" (`linkedin_jobs.keywords` in
  `settings.yaml`).
- **Scope locked in (2026-08-23)**: location = all of India in one search (not split
  by city), `datePosted = pastWeek`, and a combined cap of 100 postings per run
  across all keywords together — keeps Apify spend predictable (~$0.002/result, so
  ~$0.20/run at the cap) regardless of how many keywords are configured.
- Output columns: Company, Job Title, Job Link, Contact Name, Contact Title, Contact
  LinkedIn Profile, Other Contact Details (any email regex-matched out of the job
  description text), Location, Posted At, Applicants. LinkedIn doesn't always surface
  a job poster — expect `Contact Name` blank on a meaningful fraction of rows, not a
  bug.
- Implemented in `agent/linkedin_jobs.py` (`LinkedInJobSearchClient` ABC +
  `ApifyLinkedInJobSearch` — same swappable-interface pattern as
  `EmailFinder`/`CompanySource`/`EmailSender` — calling Apify's
  `run-sync-get-dataset-items` REST endpoint directly via `requests`, plus an
  `openpyxl`-based Excel writer) and `scripts/find_linkedin_jobs.py` as the CLI entry
  point (reads `APIFY_API_TOKEN` from the environment — never committed, same
  pattern as `HUNTER_API_KEY`). Output field names were verified against a live
  sample Actor run rather than guessed from the README.

### 8.1.1 Hiring Posts (second source, same workbook)

LinkedIn's formal Jobs tab (above) misses postings that never got filed as a proper
job listing — a PM or HR person just announcing "we're hiring" in a regular post.
Added as a second source into the same Excel file rather than folded into the Jobs
sheet, because the data shape is genuinely different and much noisier:

- Uses a second Apify Actor, `harvestapi/linkedin-post-search` — also public,
  logged-out, no cookies/account, same no-ban-risk property as the Jobs Actor.
- **No clean job-title/company/location field exists in post data** — it's free
  text. Manual sampling during design surfaced recruiter-agency blasts, job-
  aggregator bot accounts (generic "N followers" as their whole profile), and
  candidates-seeking-work posts mixed in with genuine hiring announcements.
- Two client-side filters cut that noise (in `filter_relevant_posts`,
  `agent/linkedin_posts.py`) — deliberately client-side rather than via the Actor's
  own `authorKeywords` input, which zeroed out real results in manual testing:
  - `looks_like_hiring_post`: the post's **opening** (first 300 chars) must contain
    both a hiring-intent phrase ("hiring", "looking for a", "referral", …) and one
    of the configured job-title keywords (reused from `linkedin_jobs.keywords`).
    Windowed to the opening specifically — an early version matched the title
    keyword anywhere in the post and let through a Frontend Developer hiring post
    that only mentioned "product managers" incidentally deep in the body
    ("...you'll work with backend engineers and product managers"); windowing to
    where the actual role is announced fixed that in a live re-run (18 → 5 results,
    all genuinely on-target, confirmed by hand against a real Apify run 2026-08-23).
  - `author_matches_role_signal`: the post author's headline must match the same
    HR/Product role-signal keywords already used for contact discovery
    (`agent/contact_discovery.ROLE_KEYWORDS`) — this alone dropped the bot/aggregator
    accounts in testing (no real headline to match against).
  - Not perfect precision by design (a keyword heuristic, not NLP/LLM extraction —
    same discipline as the rule-based company classifier in §4.2) — the output is
    small enough for Arjun to skim and judge, consistent with this whole project's
    human-in-the-loop philosophy.
- Scope locked in with the Jobs search: `posted_limit = month` ("past week" returned
  zero results in manual testing — LinkedIn's post search has no reliable recency
  density at that window), combined RAW-fetch cap of 100 posts across all queries
  (raised from 50 — see below; this cap drives Apify spend, the relevance filter
  above runs after fetching so the final sheet has fewer rows than 100).
- **Query broadening (2026-08-23)**, driven by 3 real posts Arjun shared from his
  own feed: the original queries appended `"India"` as a literal search term, but
  two of his three examples named a specific city ("Bangalore") or no location
  keyword at all — LinkedIn's post search has no true geo filter (unlike the Jobs
  Actor), so `"India"` was a text-match requirement silently cutting recall, not a
  location filter. `build_post_search_queries` now generates a location-free
  variant per title plus variants across `DEFAULT_QUERY_LOCATIONS` (India +
  8 major tech-hub cities), ordered location-slot-major so a limited budget samples
  every title broadly before drilling into one title's city variants. One of his
  three examples (Kirana Club, "roles open across Product, Growth, Engineering and
  Business") stays an accepted miss — it never says "Product Manager" literally,
  and loosening the title match to bare "Product" would catch far more unrelated
  posts than it would rescue; fixing that reliably needs semantic understanding
  this rule-based filter deliberately doesn't reach for.
- Output columns (second sheet, "Hiring Posts"): Contact Name, Contact Headline,
  Contact LinkedIn Profile, Company (if detected — only populated when LinkedIn
  tagged a company mention in the post), Post Excerpt (truncated to 600 chars), Post
  Link, Posted At.
- Implemented in `agent/linkedin_posts.py` (mirrors `linkedin_jobs.py`'s
  `LinkedInPostSearchClient` ABC / `ApifyLinkedInPostSearch` / Excel-sheet pattern)
  and `agent/linkedin_export.py` (`write_linkedin_excel` — combines both sources'
  sheets into one workbook; two sheets, not one merged table, since forcing the
  structured Jobs columns and the free-text Posts columns into a shared schema would
  mean misleading blanks or guessed values on one side or the other).
  `scripts/find_linkedin_jobs.py` runs both searches and writes the combined file.
  33 tests across `tests/test_linkedin_posts.py` and `tests/test_linkedin_export.py`
  (fake client/session, no real Apify credits spent in tests).

**Gotcha found and fixed during a real end-to-end run (2026-08-23)**: Apify's
`run-sync-get-dataset-items` endpoint replies **HTTP 201**, not 200, on a normal
successful run (it's creating a run resource, not just returning data) — both
Actor clients originally only accepted 200 and raised on every real call. Fixed to
accept any 2xx.

**Hiring Posts seniority filtering (added 2026-08-23, in two passes).** A live run
surfaced an "8-12 Years" listing that slipped through the relevance filter. First
pass added `has_seniority_mismatch` — a keyword check reusing
`seniority_mismatch_keywords` from the resume profile — but a second live run
showed the same "8-12 Years" post *still* getting through: that listing names no
seniority *word* ("Senior", "Principal", …), only a numeric bar, which a keyword
check can't catch. Second pass added `has_experience_gap`, reusing
`agent.resume_match.extract_min_years_required` directly (not re-implemented) to
drop posts whose stated minimum-years bar exceeds Arjun's actual experience by more
than 3 years. A third live run confirmed the fix — 100 raw posts fetched, 0 passed
the relevance filter that run (an empty "Hiring Posts" sheet, not a crash; workbook
still wrote cleanly with all 100 Jobs rows intact). Zero results is consistent with
the volatility already noted above (5 → 2 → 0 matches across four same-day runs
with a shrinking, more accurate filter each time) rather than a new bug — LinkedIn's
post-search index shifts between runs, and this stage was already known to return
few results. Both Jobs and Hiring Posts have now been run for real against the live
API multiple times.

### 8.1.2 Resume Match Scoring (Jobs sheet only)

After the first real run, Arjun flagged that keyword-search alone wasn't enough —
many scraped listings ask for Senior/Principal/5+ years, which he doesn't have, and
he had no way to tell that from the spreadsheet without reading every row. Added a
0-100 "Match Score" + "Match Notes" column pair to the **LinkedIn Jobs** sheet
(the Hiring Posts sheet doesn't get one — no clean job-title/requirements field to
score against, and Arjun didn't ask for one there).

**Deliberately not an LLM call.** The first design called Claude per job to judge
fit; Arjun pushed back (2026-08-23) — his resume barely changes, so paying an API
cost to re-read it on every run made no sense, and he specifically didn't want to
keep spending more money on this tool. Landed on the same discipline already used
for `story_bank.yaml` (§3): read the resume **once**, by hand, into a small config
file, then score every job against that fixed profile with plain rule-based logic.
Zero per-run API cost, no `ANTHROPIC_API_KEY` needed at all.

- `config/resume_profile.yaml`: hand-curated from `data/resume.pdf`, confirmed with
  Arjun 2026-08-23. Key facts: continuous full-time employment since 2024-06-01
  (Indus Insights → Rapido, no gap — Rapido's "Product Intern" title undersells it;
  Arjun confirmed it's full-time, substantive work and should count toward total
  years of experience despite the short tenure so far), target level
  `entry_level_pm`/APM (not Senior/Staff/Principal/Lead/Director/VP/Head of
  Product), core skills (SQL, Excel/VBA, JIRA, Metabase, Power BI, Tableau), and a
  domain-fit ranking — consumer (Rapido) > data_saas (Indus Insights) > fintech
  (FreeCharge) > ai_devtools (personal projects) — that reuses the same 4 niches
  already defined in `story_bank.yaml`/`niche_keywords.yaml`, not a separate list.
  `total_experience_years()` is computed dynamically from the start date on every
  run, so the file doesn't need editing just because time passes.
- `agent/resume_match.py`: four independent scoring dimensions, weighted and summed
  to 0-100 —
  - **Seniority (35%)**: any of the profile's mismatch keywords ("senior",
    "principal", "director", …) anywhere in the title/description drops this to 10.
  - **Experience (30%)**: regex-extracts the smallest "X+ years" figure in the
    description and compares it to `total_experience_years()` — full score if met,
    a shrinking score as the gap grows, neutral (70) if the JD states no figure at
    all (so an unstated requirement is never guessed at and penalized).
  - **Skills (20%)**: keyword overlap between the profile's core skills/strengths
    and the job description.
  - **Domain (15%)**: reuses `agent.classification.classify_text` (the same rule-
    based niche classifier module 3 uses for companies) to tag the job's niche, then
    scores by where that niche sits in the profile's `domain_fit_order`.
  - Not perfect precision by design — a keyword/regex heuristic, not semantic
    understanding, same tradeoff already accepted for the niche classifier and the
    Hiring Posts relevance filter.
- `LinkedInJobPosting` gained `description_text` (the raw JD, previously only used
  transiently for email-regex extraction) and `match_score`/`match_reasons` fields,
  filled in by `attach_match_scores` (rebuilds each frozen record via
  `dataclasses.replace`). `scripts/find_linkedin_jobs.py` sorts the Jobs sheet by
  score descending after scoring, so the best-fit roles surface at the top without
  Arjun needing to sort the spreadsheet himself.
- 26 tests in `tests/test_resume_match.py` cover each scoring dimension plus two
  end-to-end scenarios (a good-fit APM/marketplace role scores 70+; a "Senior PM,
  8+ years, fintech" role scores ≤40) — not yet re-verified against a fresh live
  Apify run as of this writing (Arjun rotated his Apify token right after the first
  live test, so a new one is needed to confirm real-world scores look right).

## 9. Explicitly out of scope (for now)

- Fully autonomous sending (review step is permanent, not a v1-only training wheel).
- ML-based classification (rule-based is sufficient at this volume/niche count).
- Multi-resume matching (single resume, per your preference).
- Any LinkedIn/Crunchbase automation.
