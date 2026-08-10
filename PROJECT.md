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
| Follow-up cadence | 1 follow-up per contact, sent 5 business days after the initial email, then stop |
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
│ 1. Company        │ →  │ 2. Contact         │ →  │ 3. Classifier    │
│    Sourcing        │    │    Discovery        │    │    (niche tag)   │
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

### 4.2 Contact Discovery (real API, not scraping)

Given a company domain, finds a named contact with a **verified** email — never a
guessed or scraped address. Even though Arjun is targeting product roles specifically,
contact discovery deliberately does **not** just look for the most senior PM — it looks
for whoever is most likely to actually reply:

- **Niche-gated**: only runs against companies whose classified niche is in
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

### 4.3 Classification

Rule-based keyword/tag classifier against the four niches in §3 (consumer, fintech,
data-SaaS, AI/dev-tools) using the company's stated industry tags, YC/ProductHunt topic
tags, and domain description. No ML model needed at this scale — a scored keyword match
per niche, highest score wins, ties broken toward consumer per Arjun's preference.
Unclassifiable companies are flagged for manual tagging rather than guessed.

### 4.4 Email Generation

- One template per niche (4 templates total), each with a placeholder for the
  niche-specific story-bank paragraph (§3), company name, and contact first name.
- Resume attached as a **PDF** to every generated draft by default (converted once,
  up front, from the source `.docx` — see setup note below). `email_queue` stores an
  `attached_resume` flag per row so if a no-attachment variant is ever tried later for
  comparison, reply-rate differences are queryable rather than guessed at.
- Output is a **draft**, not a sent email — written to the `email_queue` table with
  status `pending_review`. Nothing here touches the Gmail API.

**Setup note**: the resume you shared is a `.docx`
(`Arjun_Khanna_CV_V4.docx`). It needs a one-time export to PDF (Word/Google Docs →
"Save as PDF") before build starts, since re-generating a PDF from the docx on every
send is unnecessary work for a file that doesn't change per-email.

### 4.5 Review Queue (Streamlit)

Local web app, run with `streamlit run review_app.py`:
- Table/list view of pending drafts: company, contact, niche, subject, body preview,
  resume attachment confirmation.
- Per-row: **Approve**, **Edit** (inline body edit before approving), **Reject**.
- Approving sets status `approved` (eligible for the next send batch); nothing sends
  from here directly — sending is a separate scheduled step, so there's always a buffer
  between "approved" and "actually left the outbox."

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

- A daily check polls the Gmail thread for each `sent` email; if a reply exists in the
  thread, status → `replied` and it's pulled out of the follow-up pipeline.
- If no reply after 5 business days, exactly **one** follow-up is generated (short,
  references the original thread) and dropped into the review queue like any other
  draft — it still needs approval before sending.
- After the one follow-up, no further automated action — status → `no_response` and
  the contact is retired from the pipeline (no repeat pestering).

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
companies      id, name, domain, source, raw_tags, niche, status, created_at
contacts       id, company_id, name, role, role_category (hr|product|other), email,
               verification_confidence, source_api, status, created_at
email_queue    id, contact_id, company_id, niche, subject, body, kind
               (initial|followup), attached_resume (bool), status (pending_review|
               approved|rejected|sent|bounced|replied|no_response), gmail_thread_id,
               created_at, sent_at
send_log       id, date, sent_count, bounce_count   -- drives the warm-up ramp
               and circuit breaker
send_config    daily_cap, ramp_step, ramp_ceiling    -- single-row config table
```

## 7. Legal / deliverability / ToS risks and mitigations

| Risk | Mitigation in this design |
|---|---|
| **LinkedIn/Crunchbase scraping** — ToS violation, account ban risk, legally contested territory (hiQ v. LinkedIn line of cases) | Not used at all. Sourcing restricted to public APIs/open data (§4.1); contact discovery restricted to a licensed email-finder API (§4.2), never scraped from LinkedIn profiles. |
| **Guessed email addresses** (`first.last@domain.com` patterns) — high bounce rate, hurts sender reputation, sometimes hits wrong/uninvolved people | Only verified-confidence results from the finder API are auto-queued; low-confidence ones are routed to manual check instead of guessed. |
| **Personal Gmail spam/suspension risk** — cold outreach from a personal account can trigger Google's abuse detection | Low starting cap + gradual ramp (§4.6), spaced sends (not bursts), circuit breaker on bounce rate, human-approved content (reduces spammy-pattern risk vs. templated blasts), Gmail API (not raw SMTP relay, which Google trusts less). |
| **Spam complaints** — recipients marking as spam damages both deliverability and the personal Gmail account's standing | Every email includes a low-friction opt-out/"let me know if not relevant" line; strictly one follow-up, then automatic retirement — no repeated unsolicited contact. |
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
3. **Contact discovery** — Hunter.io free-tier integration behind the `EmailFinder`
   interface.
4. **Classification** — rule-based niche tagger.
5. **Email generation** — template + story-bank + resume-attach logic, writes to
   `email_queue` as `pending_review`.
6. **Review queue UI** — Streamlit approve/edit/reject app.
7. **Scheduled sending** — daily-cap batch sender with warm-up ramp + circuit breaker.
8. **Tracking + follow-ups** — reply polling, single follow-up generation.

Each module is runnable and testable in isolation against the shared SQLite DB before
the next one is built — no module should require a later one to exist to be verified.

## 9. Explicitly out of scope (for now)

- Fully autonomous sending (review step is permanent, not a v1-only training wheel).
- ML-based classification (rule-based is sufficient at this volume/niche count).
- Multi-resume matching (single resume, per your preference).
- Any LinkedIn/Crunchbase automation.
