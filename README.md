# Molete CMS

Customer Management System for **MOLETE (IBADAN) BEST CHOICE MULTIPURPOSE CO-OPERATIVE SOCIETY LTD**.

A single Flask app covering member onboarding, loan applications, the voting portal, and end-of-year
gift preferences — replacing/absorbing the earlier separate voting & gift-preference app.

## Features

- **New member signup** — full name, account number, phone number, work position, email, passport
  photo, signature, and **NIN (National Identification Number) document** are all **compulsory**
  (no field is optional). There's no password field on the form; a shared default password is
  assigned automatically, same as bulk-imported members. Account is `Pending` until an admin
  approves it. On approval, the next Membership Code in the cooperative's existing sequence is
  generated (see "Membership code numbering" below), and — since email is now compulsory — an
  email is always sent with the Membership Code and default password. On first login, the member
  is forced through **Complete Profile** to set their own real password and provide their
  **Office Number** (this is always asked at this step, for every member, since it isn't
  collected at signup) — plus anything else still missing. Bulk-imported/manually-added members
  go through the same Complete Profile step, but see the full set of fields since nothing was
  pre-collected for them, including NIN.
- **Bulk import of existing members** (CSV/Excel: `full_name`, `membership_code`, `account_number`,
  optional `work_position`) — deliberately minimal. Email and everything else is filled in by the
  member themselves. They get a shared default password and are **forced** to complete their
  profile (photo, signature, email, position, new password) on first login — and can't do anything
  else in the portal until they do (see "Profile completion is mandatory" below).
- **Loans**: members apply for one of five loan types, each with its own eligibility clause,
  amount cap, and tenure/interest options (see "Loan Types" below), must upload a photo/scan of
  their **signed physical loan application form receipt** (compulsory — image or PDF), and must
  nominate **two guarantors** by membership code — matching the cooperative's official loan
  application form — each vouching for a specific portion of the loan (the two amounts must add
  up to the total). Both guarantors see the request in their own portal and must accept before
  the admin can approve the loan; either one declining auto-declines the loan. Admin can otherwise
  decline with a mandatory reason too. The admin can view the receipt (and the member can view
  their own) via a secure, authenticated link — never exposed publicly.
- **Voting**: admin creates positions + candidates and controls status with three states —
  **Open / Paused / Closed** (admin-only; members cannot change this) — live results via
  Flask-SocketIO + Chart.js. Members can only cast votes while status is Open; Paused temporarily
  blocks new votes without losing existing ones, Closed ends it. One vote per member per position,
  enforced at the database level.
- **Gift Preference**: admin-gated — members can only choose Physical or Monetized while the admin
  has explicitly **opened** selection; closed by default. Admin views/exports the full list and
  toggles it open/closed from the dashboard.
- **UI**: both the member and admin dashboards use a sidebar navigation layout (collapses to a
  horizontal scrollable bar on mobile) instead of top tabs, with live badges for pending items
  (pending signups, pending loans, guarantor requests) and status dots for open voting/gifts.
- **Member Directory**: search, edit, delete (with confirmation). Two export options per member/list:
  - **Download PDF** — a filled copy of the cooperative's actual official Membership Application
    Form (letterhead, passport photo embedded in the "affix passport" box, Section A fields filled
    with whatever the system has — name, profession/department from work position, email, phone,
    office number, account number — the member's signature embedded at the bottom, and a page 2
    Membership Certificate with their name and today's date). Fields the system doesn't collect
    (marital status, sex, employer, next of kin, bankers/branch, starting contribution, deduction
    date) are left blank exactly as they'd be on a partially-filled paper form.
  - **Download CSV** (list-level, from the Members tab) — a deliberately minimal export with just
    **Full Name, Membership Code, Account Number, Office Number**. Separate from the full
    **Export Excel** button, which includes every field (phone, email, savings, status, etc).
- **Security**: passport photos, signatures, and NIN documents are stored **outside** `static/`, in
  `instance/uploads/`, and are only ever served through an authenticated route
  (`/secure-file/<user_id>/<kind>`) — a member can see their own files, an admin can see anyone's.

## Email notifications

Email is optional per member — collected at signup (or profile completion, for bulk-imported
members) and editable later by the admin. Two things trigger an email, only if the relevant
person has one on file:

1. **Member approved** — they get an email with their Membership Code and login instructions.
2. **Named as a loan guarantor** — the guarantor gets an email saying who named them, for how
   much, and to check their "Guarantor Requests" tab.

Sending uses Python's built-in `smtplib` against Gmail's SMTP server, configured via `.env`:

```
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USERNAME=bestchoicecorperative@gmail.com
MAIL_PASSWORD=your-16-character-app-password
```

**Gmail requires an "App Password"**, not the account's normal password — Gmail blocks plain
password SMTP logins. To generate one:
1. Turn on 2-Step Verification for the Google account (required first)
2. Go to https://myaccount.google.com/apppasswords
3. Generate a password for "Mail", copy the 16-character code into `MAIL_PASSWORD`

If `MAIL_USERNAME`/`MAIL_PASSWORD` aren't set, or sending fails for any reason (wrong password,
no internet, Gmail rate limit, etc.), the app **never crashes or blocks the action** — approval
and loan application still go through normally; the email is just silently skipped (logged to
the console). The admin's approval flash message tells you whether the email actually sent.

## Loan Types

Seeded automatically on first run with the cooperative's official terms:

| Loan Type | Max amount | Interest | Tenure | Notes |
|---|---|---|---|---|
| Personal Loan | 2x savings balance | 10% or 15% | 12 or 18 months | Member picks either option |
| Emergency Loan | 1x savings balance | 5% flat | 6 months | |
| Telephone Loan | ₦150,000 | 10% flat p.a. | 12 months | |
| Household Loan | Set by admin | 10% flat p.a. | 12 months | Closed by default — open it every December |
| Commodity & Seasonal Loan | Set by admin | 5% flat (adjustable) | 3 months (adjustable) | Closed by default — open per season (Christmas, Ileya, Itunu-Awe) with a season label |

A member's "savings balance" is tracked per-member (editable by the admin from the member's Edit
button) and drives the max amount for Personal/Emergency loans automatically.

From the admin dashboard's **Loan Types** tab you can:
- Edit the clause text shown to members, the multiplier/max amount, and the season label
- Open/close any loan type (essential for the seasonal ones — they start closed)
- Add or remove tenure/interest-rate options per loan type

When a member applies, the interest rate and tenure are locked in as a snapshot on that loan
record, so changing a loan type's terms later doesn't retroactively affect existing applications.

## Login

- **Admin** logs in with username `admin` (configurable via `ADMIN_USERNAME`) and `ADMIN_PASSWORD`.
- **Members** log in with their **full name** and password (membership code and account number still
  work too, as fallbacks).

Since members can log in by name, **full names must be unique** across the system — the app blocks
creating or renaming a member to an exact name (case-insensitive) that's already taken, at signup,
manual add, bulk import (duplicates are skipped and counted), and admin edits. If two real members
share a name, add a middle name or initial to tell them apart.

## Profile completion is mandatory

Every member starts with `is_profile_complete = False` and must set their own password (replacing
the shared default) before they can do anything else in the portal. A `before_request` hook blocks
**every** route for an incomplete-profile member except the complete-profile page itself, logout,
and the two file endpoints needed to render it — so they can't apply for a loan, vote, set a gift
preference, respond to a guarantor request, change their password, or view any dashboard until
they've completed their profile. This applies to every request method, not just page views, so it
can't be bypassed by submitting a form directly.

Complete Profile now covers **every field on the official Membership Application Form** — the page
only asks for whatever is still missing on that member's record, so nobody re-enters something
they've already provided:

- Work Position, Office Number, Employer
- Marital Status, Sex (dropdowns)
- Email (optional)
- Bankers/Branch, Application & Development Fee, Starting Contribution, Deduction Due Date,
  Preferred Month of Deduction and Year
- Next of Kin — Name, Address, Tel
- Passport photo, Signature, NIN document
- New password

Bulk-imported/manually-added members arrive with just a name, code, and account number, so they'll
see the full set. Self-registered members (via signup) already provide work position, email,
phone, passport, signature, and NIN at signup, so their Complete Profile step covers the rest:
Employer, Marital Status, Sex, Bankers/Branch, the financial/deduction fields, Next of Kin, and the
new password.

**Starting Contribution** is added directly to the member's tracked `savings_balance` once
submitted, since that balance is what drives Personal/Emergency loan caps.

## Membership form fields & the PDF

The **Download PDF** button on a member's profile recreates the cooperative's actual Membership
Application Form (letterhead, Section A, Next of Kin, signature, and the page-2 Membership
Certificate) filled with whatever the system has on file for that member. With the fields above now
collected, the only things still left blank on a completed member's PDF are the two signature lines
for President/Secretary — which are meant to be signed by hand regardless.

## Guarantor workflow

This matches the cooperative's official loan application form, which requires **two guarantors**,
each vouching for a stated amount:

1. Applicant fills the loan form, entering **two** guarantors by membership code, each with the
   amount that guarantor is vouching for. The two amounts must add up to the total loan amount.
2. The system validates both guarantors are existing **active members** — not pending, not the
   applicant themselves, and not the same person twice.
3. Each guarantor sees their own pending request on their dashboard under **"Guarantor Requests"**
   (badge count included): *"You are &lt;applicant&gt;'s guarantor (Membership Code: &lt;code&gt;)
   for &#8358;X of their &#8358;Y &lt;loan type&gt; loan application ... Accept or decline?"*
4. **Both accept** → the loan becomes eligible for admin approval.
5. **Either declines** → the loan is automatically marked Declined, with the reason
   "Guarantor (&lt;name&gt;) declined to guarantee this loan."
6. The admin's Loans tab shows both guarantors' names, guaranteed amounts, and statuses on every
   application, and the **Approve** button is disabled until both have accepted.

## Membership code numbering

New signups get an auto-generated code that **continues the cooperative's existing orderly
sequence** — it does not pick randomly. The format is `BC` + an 8-digit number (e.g. `BC08210002`,
`BC08210003`, ...), matching the historical member register.

On each approval, the app scans all existing membership codes matching that `BC########` pattern,
takes the highest number found, and assigns the next one. So once you've bulk-imported your real
member register (which tops out around `BC08211614` at time of writing), the very next new member
approved gets `BC08211615`, then `BC08211616`, and so on — no gaps, no randomness.

If the database has no `BC########` codes yet (e.g. a brand-new install before any import), it
starts from `BC08210002` to match where your historical numbering actually begins. This only
matters before your first bulk import — once that's done, numbering picks up from your real data.

## Project layout

```
molete_cms/
├── app.py              # routes, app factory
├── models.py           # SQLAlchemy models
├── extensions.py       # db / login_manager / socketio singletons
├── utils.py            # file upload, PDF, Excel export, bulk-import parsing
├── requirements.txt
├── .env.example
└── templates/
    ├── base.html, login.html, signup.html, complete_profile.html, change_password.html
    ├── member/dashboard.html
    └── admin/dashboard.html
```

## Local setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD

python app.py
```

The app creates all tables automatically on first run (`db.create_all()`), and seeds one Admin user
from `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env` if no admin with that username exists yet.

Visit `http://localhost:5000`.

## Database

Uses **SQLite** by default — nothing to configure. The database file is created automatically at
`instance/cms.db` the first time you run the app.

If you ever want to move to PostgreSQL later, just set `DATABASE_URL` in `.env`:

```
DATABASE_URL=postgresql://user:password@localhost:5432/molete_cms
```

Nothing else in the code needs to change — SQLAlchemy handles both the same way.

**Note on schema changes:** `db.create_all()` only creates tables that don't exist yet — it does
**not** add new columns to tables that already exist. If you've already deployed this app and have
a live database file, and later pull an update that adds a new column (like `Loan.receipt_path`),
you'll need to add it manually once, e.g.:
```
sqlite3 instance/cms.db "ALTER TABLE loan ADD COLUMN receipt_path VARCHAR(255);"
```
If you haven't deployed yet, or don't mind losing test data, simplest is to just delete
`instance/cms.db` and let the app recreate it fresh on next run.

## SMS (currently manual)

Real SMS delivery isn't connected — providers like Termii charge per message (no free tier). Email
is now compulsory at signup, so approval emails (with Membership Code + default password) go out
automatically via `MAIL_USERNAME`/`MAIL_PASSWORD` in most cases. If email delivery fails for any
reason, the generated Membership Code and default password are also shown directly in the admin
dashboard flash message as a fallback, for the admin to relay to the member by hand (call,
WhatsApp, etc). When you're ready to wire up real SMS as a second channel, the place to add it is
inside the `approve_member` route in `app.py`, alongside the existing `send_email` call.

## Deploying to Render

1. Push this project to a GitHub repo.
2. On Render: **New → Web Service**, connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn -k eventlet -w 1 app:app`
   (single worker is required with Flask-SocketIO's default setup unless you add a message queue
   like Redis for multi-worker pub/sub)
5. **Persistent storage (important for SQLite)**: Render's filesystem is ephemeral on redeploys, so
   both the SQLite database file and uploaded passport/signature images would be wiped on every
   deploy without this. Add a Render **Disk** (from your web service's settings), mount it at e.g.
   `/var/data`, then set these environment variables:
   - `DATABASE_URL=sqlite:////var/data/cms.db` (note: four slashes for an absolute path)
   - `UPLOAD_ROOT=/var/data/uploads`
6. Add the other environment variables from `.env.example` (`SECRET_KEY`, `ADMIN_PHONE`,
   `ADMIN_PASSWORD`, `ADMIN_NAME`, `DEFAULT_MEMBER_PASSWORD`, `MAX_UPLOAD_MB`).
7. Deploy. On first boot the app creates tables and seeds the admin account automatically.

Log in to the admin dashboard at `/login` using username `admin` (or your `ADMIN_USERNAME`) and
`ADMIN_PASSWORD`. Members log in with their full name and password.
