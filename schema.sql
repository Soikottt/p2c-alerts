CREATE TABLE IF NOT EXISTS publishers (
  id SERIAL PRIMARY KEY,
  ringba_publisher_id TEXT UNIQUE NOT NULL,
  name TEXT,
  alert_email TEXT,
  cc_on_alerts BOOLEAN DEFAULT FALSE,
  status TEXT DEFAULT 'active',
  notes TEXT,
  first_call_at TIMESTAMPTZ,
  last_call_at TIMESTAMPTZ,
  last_connected_call_at TIMESTAMPTZ,
  total_calls INTEGER DEFAULT 0,
  calls_since_first INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS buyers (
  id SERIAL PRIMARY KEY,
  ringba_buyer_id TEXT UNIQUE NOT NULL,
  name TEXT,
  alert_email TEXT,
  cc_on_alerts BOOLEAN DEFAULT FALSE,
  watchlist BOOLEAN DEFAULT FALSE,
  status TEXT DEFAULT 'active',
  notes TEXT
);

CREATE TABLE IF NOT EXISTS dids (
  id SERIAL PRIMARY KEY,
  number TEXT UNIQUE NOT NULL,
  ringba_number_id TEXT,
  publisher_id INTEGER REFERENCES publishers(id),
  buyer_id INTEGER REFERENCES buyers(id),
  monthly_cost NUMERIC(10,2),
  status TEXT DEFAULT 'active',
  last_call_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS calls (
  id SERIAL PRIMARY KEY,
  ringba_call_id TEXT UNIQUE NOT NULL,
  publisher_id INTEGER REFERENCES publishers(id),
  buyer_id INTEGER REFERENCES buyers(id),
  did_id INTEGER REFERENCES dids(id),
  started_at TIMESTAMPTZ NOT NULL,
  connected BOOLEAN DEFAULT FALSE,
  duration_sec INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at);
CREATE INDEX IF NOT EXISTS idx_calls_pub ON calls(publisher_id, started_at);
CREATE INDEX IF NOT EXISTS idx_calls_buy ON calls(buyer_id, started_at);

CREATE TABLE IF NOT EXISTS cap_rules (
  id SERIAL PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('publisher','buyer')),
  entity_id INTEGER NOT NULL,
  period TEXT NOT NULL CHECK (period IN ('daily','monthly')),
  metric TEXT DEFAULT 'incoming_calls',
  cap_value INTEGER NOT NULL,
  alert_threshold_pct INTEGER DEFAULT 80,
  active BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS cc_routing (
  alert_type TEXT PRIMARY KEY,
  cc_buyer BOOLEAN DEFAULT FALSE,
  cc_publisher BOOLEAN DEFAULT FALSE
);

INSERT INTO cc_routing (alert_type, cc_buyer, cc_publisher) VALUES
  ('new_publisher',     FALSE, TRUE),
  ('publisher_lapsed',  FALSE, TRUE),
  ('publisher_cap',     FALSE, TRUE),
  ('buyer_cap',         TRUE,  FALSE),
  ('watchlist_connect', TRUE,  FALSE),
  ('did_inactive',      FALSE, FALSE)
ON CONFLICT (alert_type) DO NOTHING;

CREATE TABLE IF NOT EXISTS alerts_log (
  id SERIAL PRIMARY KEY,
  alert_type TEXT NOT NULL,
  dedupe_key TEXT UNIQUE NOT NULL,
  sent_at TIMESTAMPTZ DEFAULT NOW(),
  recipients TEXT,
  details TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);