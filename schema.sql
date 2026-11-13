-- New database layout. Safe to run many times (CREATE ... IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS affiliate_managers (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS publishers (
  id SERIAL PRIMARY KEY,
  ringba_publisher_id TEXT UNIQUE,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  manager_id INTEGER REFERENCES affiliate_managers(id) ON DELETE SET NULL,
  publisher_email TEXT,
  cc_publisher BOOLEAN NOT NULL DEFAULT FALSE,
  extra_cc TEXT,
  notes TEXT,
  first_seen_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS buyers (
  id SERIAL PRIMARY KEY,
  ringba_buyer_id TEXT UNIQUE,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  watchlist BOOLEAN NOT NULL DEFAULT FALSE,
  buyer_email TEXT,
  cc_buyer BOOLEAN NOT NULL DEFAULT FALSE,
  extra_cc TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dids (
  id SERIAL PRIMARY KEY,
  number TEXT UNIQUE NOT NULL,
  ringba_number_id TEXT,
  label TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  monthly_cost NUMERIC(10,2),
  notes TEXT,
  added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_call_at TIMESTAMPTZ,
  publisher_id INTEGER,
  affiliate_name TEXT,
  next_charge_at TIMESTAMPTZ,
  campaign_id TEXT,
  never_alert BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS calls (
  call_id TEXT PRIMARY KEY,
  call_at TIMESTAMPTZ NOT NULL,
  publisher_id INTEGER REFERENCES publishers(id),
  buyer_id INTEGER REFERENCES buyers(id),
  did_id INTEGER REFERENCES dids(id),
  campaign_name TEXT,
  campaign_rid TEXT,
  target_name TEXT,
  caller_number TEXT,
  connected BOOLEAN NOT NULL DEFAULT FALSE,
  converted BOOLEAN NOT NULL DEFAULT FALSE,
  paid BOOLEAN NOT NULL DEFAULT FALSE,
  is_live BOOLEAN NOT NULL DEFAULT FALSE,
  is_duplicate BOOLEAN NOT NULL DEFAULT FALSE,
  blocked BOOLEAN NOT NULL DEFAULT FALSE,
  incomplete BOOLEAN NOT NULL DEFAULT FALSE,
  duration_sec INTEGER NOT NULL DEFAULT 0,
  connected_sec INTEGER NOT NULL DEFAULT 0,
  revenue NUMERIC(12,2),
  payout NUMERIC(12,2),
  profit NUMERIC(12,2),
  telco_cost NUMERIC(12,4),
  source TEXT NOT NULL DEFAULT 'live',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_calls_call_at ON calls (call_at);
CREATE INDEX IF NOT EXISTS ix_calls_pub_at ON calls (publisher_id, call_at);
CREATE INDEX IF NOT EXISTS ix_calls_buyer_at ON calls (buyer_id, call_at);
CREATE INDEX IF NOT EXISTS ix_calls_did_at ON calls (did_id, call_at);
CREATE INDEX IF NOT EXISTS ix_calls_source_created ON calls (source, created_at);

CREATE TABLE IF NOT EXISTS caps (
  id SERIAL PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('publisher','buyer')),
  entity_id INTEGER NOT NULL,
  period TEXT NOT NULL CHECK (period IN ('daily','monthly')),
  metric TEXT NOT NULL DEFAULT 'paid' CHECK (metric IN ('all','connected','converted','paid')),
  cap_value INTEGER NOT NULL CHECK (cap_value > 0),
  thresholds TEXT NOT NULL DEFAULT '80,100',
  active BOOLEAN NOT NULL DEFAULT TRUE,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS alert_rules (
  alert_type TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  param_days INTEGER,
  param_count INTEGER,
  cc_manager BOOLEAN NOT NULL DEFAULT FALSE,
  cc_entity_email BOOLEAN NOT NULL DEFAULT FALSE,
  cc_entity_extra BOOLEAN NOT NULL DEFAULT TRUE,
  extra_cc TEXT,
  description TEXT,
  template_id INTEGER
);

CREATE TABLE IF NOT EXISTS alerts (
  id SERIAL PRIMARY KEY,
  alert_type TEXT NOT NULL,
  dedupe_key TEXT UNIQUE NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  to_emails TEXT,
  cc_emails TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  sent_at TIMESTAMPTZ,
  body_html TEXT,
  snooze_key TEXT,
  response_label TEXT,
  responded_at TIMESTAMPTZ,
  responded_by TEXT,
  response_note TEXT,
  case_status TEXT DEFAULT 'open',
  case_updated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_alerts_status ON alerts (status);
CREATE INDEX IF NOT EXISTS ix_alerts_created ON alerts (created_at);

CREATE TABLE IF NOT EXISTS sync_runs (
  id SERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ,
  window_start TIMESTAMPTZ,
  window_end TIMESTAMPTZ,
  rows_fetched INTEGER DEFAULT 0,
  rows_inserted INTEGER DEFAULT 0,
  rows_updated INTEGER DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running',
  error TEXT
);
CREATE INDEX IF NOT EXISTS ix_sync_runs_started ON sync_runs (started_at);

CREATE TABLE IF NOT EXISTS publisher_managers (
  publisher_id INTEGER NOT NULL REFERENCES publishers(id) ON DELETE CASCADE,
  manager_id INTEGER NOT NULL REFERENCES affiliate_managers(id) ON DELETE CASCADE,
  PRIMARY KEY (publisher_id, manager_id)
);

CREATE TABLE IF NOT EXISTS email_templates (
  id SERIAL PRIMARY KEY,
  alert_type TEXT NOT NULL,
  name TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alert_action_defs (
  id SERIAL PRIMARY KEY,
  alert_type TEXT NOT NULL,
  label TEXT NOT NULL,
  effect TEXT NOT NULL DEFAULT 'record',
  effect_days INTEGER,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alert_actions (
  token TEXT PRIMARY KEY,
  alert_id INTEGER NOT NULL,
  label TEXT NOT NULL,
  effect TEXT NOT NULL DEFAULT 'record',
  effect_days INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_alert_actions_alert ON alert_actions (alert_id);

CREATE TABLE IF NOT EXISTS snoozes (
  entity_key TEXT PRIMARY KEY,
  snooze_until TIMESTAMPTZ NOT NULL,
  reason TEXT
);

CREATE TABLE IF NOT EXISTS alert_notes (
  id SERIAL PRIMARY KEY,
  alert_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  author TEXT,
  text TEXT NOT NULL,
  status_after TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_alert_notes_alert ON alert_notes (alert_id);

CREATE TABLE IF NOT EXISTS inbound_emails (
  message_id TEXT PRIMARY KEY,
  alert_id INTEGER,
  from_email TEXT,
  outcome TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS targets (
  id SERIAL PRIMARY KEY,
  ringba_target_id TEXT UNIQUE NOT NULL,
  name TEXT,
  buyer_id INTEGER,
  buyer_name TEXT,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  daily_cap INTEGER,
  monthly_cap INTEGER,
  alerts_on BOOLEAN NOT NULL DEFAULT TRUE,
  thresholds TEXT NOT NULL DEFAULT '80,100',
  count_day INTEGER,
  count_month INTEGER,
  counts_at TIMESTAMPTZ,
  synced_at TIMESTAMPTZ
);
