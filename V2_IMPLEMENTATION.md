# P2C Alert Center V2 — implementation notes

## Design decisions

- Clean light Streamlit theme and sidebar navigation.
- Existing navigation pages and operational functions are retained.
- New Alert Center provides universal search and a split layout with a right-side conversation-history panel.
- Conversation membership uses explicit `thread_id` relationships. It intentionally does not merge separate alerts merely because they reference the same DID.
- Outgoing email content remains in `alerts.body` / `alerts.body_html`.
- New inbound messages are stored in `email_archive` with original text/HTML, sender/recipient headers, Message-ID and reply headers. The legacy `inbound_emails` table and shortened `alert_notes` reply records remain intact.
- `audit_events` is append-only for newly tracked V2 actions. Historical audit records are not fabricated.
- Existing Follow-ups and DIDs pages remain the source of truth for scheduling and DID lifecycle rules; new dashboard links and analytics sit on top of those records.

## Additive database migration

`v2_features.ensure_schema()` creates `email_archive` and `audit_events` and their indexes using `IF NOT EXISTS`. It runs from the Streamlit app and the background engine before IMAP reply processing. It does not drop, rename, truncate, or rewrite existing operational tables.

## Important history limitation

The previous IMAP reader saved a shortened reply as an `alert_notes` entry and kept limited metadata in `inbound_emails`. The complete original body was not stored. V2 archives full content for newly processed replies; it cannot reconstruct old full messages from already-truncated notes. Existing notes and outgoing alert bodies remain viewable.

## New navigation

- Alert Center
- Operations Analytics
- Publisher Profiles
- Buyer Profiles
- Email Center
- Audit Log

Existing Dashboard, Follow-ups, Schedule, Reports, Publishers, Buyers, DIDs, Caps, My Tasks, Emails, Email Templates, Alert Log, Sync & Import and Settings remain available.

## Validation performed

- Python AST parsing / `py_compile` for project Python files.
- SQL schema statement parsing through the project's setup SQL splitter.
- Unit tests for strict conversation membership (same-DID unrelated alerts are excluded).
- Full live integration tests require the user's Postgres, Ringba, SMTP and IMAP credentials and were not run in this environment.

## Deployment

1. Back up the production Postgres database before deploying.
2. Deploy the complete project folder, including `.streamlit/config.toml`, `v2_features.py` and `v2_ui.py`.
3. Keep existing Streamlit Secrets / environment variables unchanged.
4. Open the app once; additive V2 tables are created automatically.
5. Ensure the GitHub Actions engine uses this same project version so new inbound replies are archived.
6. Verify Ringba sync, one test alert in DRY RUN, a manager reply, and a retired-DID follow-up check before switching to live sending.
