-- Mafutani CTA full DDL (stdlib sqlite3 compatible)
-- Tables: users, sessions, tiers, symbols, candles_1m, quotes_l1,
--   watchlists, strategies, backtests, orders, fills, positions,
--   risk_limits, risk_events, journal, audit_log, api_keys

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- tiers
CREATE TABLE IF NOT EXISTS tiers (
  name            TEXT PRIMARY KEY,
  max_symbols     INTEGER NOT NULL DEFAULT 10,
  live_enabled    INTEGER NOT NULL DEFAULT 0,
  methods_allowed TEXT NOT NULL DEFAULT '[]',
  -- Deprecated, unused. The backtest window is a single product-wide
  -- setting (engines.backtest.BACKTEST_WINDOW_DAYS), not per-tier. This
  -- column held 30/365/1825/3650 and was read by nothing: all four tiers
  -- silently got the same 20,000-bar cap. Kept so existing databases do
  -- not need a migration to drop it.
  backtest_days   INTEGER NOT NULL DEFAULT 30,
  api_rate        INTEGER NOT NULL DEFAULT 60
);

-- ------------------------------------------------------- COT (CFTC)
-- Commitments of Traders. Weekly snapshots of POSITIONS HELD, published
-- three days after the Tuesday they describe. Not a price series and not
-- joinable to candles_1m as if it were one.
--
-- `family` is in the primary key on purpose: the four report families
-- (disaggregated / TFF / legacy / CIT) use mutually incompatible trader
-- classifications, so merging them into one series silently produces a
-- confident wrong number. `report` names the specific file.
--
-- One row per (report, contract, report_date, category). Contracts appear
-- and disappear between weeks -- a market drops out of the report when it
-- falls below 20 reportable traders -- so the key carries no assumption
-- of a continuous series.
CREATE TABLE IF NOT EXISTS cot_positions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  report         TEXT    NOT NULL,   -- disagg_fut | disagg_comb | tff_fut | ...
  family         TEXT    NOT NULL,   -- disagg | tff | legacy | cit
  combined       INTEGER NOT NULL DEFAULT 0,   -- futures+options basis
  market         TEXT    NOT NULL,   -- "WHEAT-HRW - CHICAGO BOARD OF TRADE"
  market_code    TEXT    NOT NULL DEFAULT '',
  region_code    TEXT    NOT NULL DEFAULT '',
  commodity_code TEXT    NOT NULL DEFAULT '',
  contract_code  TEXT    NOT NULL,
  report_date    TEXT    NOT NULL,   -- ISO YYYY-MM-DD (Tuesday)
  as_of_yymmdd   TEXT    NOT NULL DEFAULT '',
  open_interest  INTEGER NOT NULL DEFAULT 0,
  category       TEXT    NOT NULL,   -- canonical, shared across families
  long_pos       INTEGER NOT NULL DEFAULT 0,
  short_pos      INTEGER NOT NULL DEFAULT 0,
  spread_pos     INTEGER NOT NULL DEFAULT 0,
  has_spread     INTEGER NOT NULL DEFAULT 0,
  fetched_at     INTEGER NOT NULL DEFAULT 0,
  UNIQUE (report, contract_code, report_date, category)
);
CREATE INDEX IF NOT EXISTS idx_cot_contract ON cot_positions(report, contract_code, report_date);
CREATE INDEX IF NOT EXISTS idx_cot_date     ON cot_positions(report_date);
CREATE INDEX IF NOT EXISTS idx_cot_market   ON cot_positions(report, market);

-- Provenance. Without it a stale table is indistinguishable from a fresh
-- one, which is the difference between "positioning is unchanged" and
-- "we have not fetched since March".
CREATE TABLE IF NOT EXISTS cot_fetches (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  report      TEXT NOT NULL,
  kind        TEXT NOT NULL,          -- weekly | historical
  year        INTEGER NOT NULL DEFAULT 0,
  url         TEXT NOT NULL DEFAULT '',
  rows_ok     INTEGER NOT NULL DEFAULT 0,
  report_date TEXT NOT NULL DEFAULT '',
  ok          INTEGER NOT NULL DEFAULT 0,
  error       TEXT NOT NULL DEFAULT '',
  fetched_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cot_fetch ON cot_fetches(report, fetched_at);

-- ---------------------------------------------------------------- users
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  tier          TEXT NOT NULL DEFAULT 'Free' REFERENCES tiers(name),
  created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  disabled      INTEGER NOT NULL DEFAULT 0,
  totp_secret   TEXT NOT NULL DEFAULT '',
  totp_enabled  INTEGER NOT NULL DEFAULT 0,
  recovery_codes TEXT NOT NULL DEFAULT '[]',
  last_login_ts INTEGER NOT NULL DEFAULT 0,
  failed_logins INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- ---------------------------------------------------------------- sessions
CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  expires_at INTEGER NOT NULL,
  csrf_token TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- ------------------------------------------------- login throttle (2FA-safe)
-- Sliding-window failure ledger for brute-force throttling. `key` is either
-- 'email:<addr>' or 'ip:<addr>'; rows older than the window are pruned.
CREATE TABLE IF NOT EXISTS login_attempts (
  key TEXT NOT NULL,
  ts  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_key_ts ON login_attempts(key, ts);

-- ---------------------------------------------------------------- exchanges
CREATE TABLE IF NOT EXISTS exchanges (
  code         TEXT PRIMARY KEY,
  name         TEXT NOT NULL DEFAULT '',
  type         TEXT NOT NULL DEFAULT 'exchange',
  asset_classes TEXT NOT NULL DEFAULT '',
  region       TEXT NOT NULL DEFAULT '',
  currency     TEXT NOT NULL DEFAULT 'USD',
  tz           TEXT NOT NULL DEFAULT 'UTC',
  notes        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_exchanges_asset ON exchanges(asset_classes);

-- ---------------------------------------------------------------- symbols
CREATE TABLE IF NOT EXISTS symbols (
  symbol     TEXT PRIMARY KEY,
  asset_type TEXT NOT NULL DEFAULT 'equity',
  name       TEXT NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1,
  tick_size  REAL NOT NULL DEFAULT 0.01,
  lot_size   REAL NOT NULL DEFAULT 1.0,
  exchange   TEXT NOT NULL DEFAULT '',
  currency   TEXT NOT NULL DEFAULT 'USD',
  sector     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_symbols_exchange ON symbols(exchange);
CREATE INDEX IF NOT EXISTS idx_symbols_asset ON symbols(asset_type);

-- ---------------------------------------------------------------- candles_1m
CREATE TABLE IF NOT EXISTS candles_1m (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL REFERENCES symbols(symbol) ON DELETE CASCADE,
  ts     INTEGER NOT NULL,
  open   REAL NOT NULL,
  high   REAL NOT NULL,
  low    REAL NOT NULL,
  close  REAL NOT NULL,
  volume REAL NOT NULL DEFAULT 0,
  -- Where this bar came from: a venue name ('venue') for an observed
  -- bar, or 'synthetic' for one generated by scripts/seed.py. The
  -- provenance gate (engines/dataprov.py) refuses to let a figure from a
  -- generated tape be quoted as evidence, and it reads this column.
  --
  -- This column used to live only in data.db._migrate, so a database built
  -- from schema.sql alone -- which is what the test fixtures do -- silently
  -- lacked it, every bar classified as "unknown provenance", and every
  -- backtest verdict came back blanked. schema.sql has to be the complete
  -- schema, not schema-plus-migrations.
  source TEXT NOT NULL DEFAULT 'synthetic',
  UNIQUE (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_sym_ts ON candles_1m(symbol, ts);

-- Daily bars.
--
-- These are NOT a substitute for, and must never be loaded into,
-- candles_1m. Everything downstream is resolution-aware in ways that are
-- easy to get wrong: engines/backtest.py annualises with sqrt(252*390),
-- which is a 1-minute figure; the indicators are parameterised per bar; and
-- a daily bar labelled as a minute bar would make every return, every
-- volatility figure and every significance test wrong in a direction that
-- looks entirely plausible.
--
-- They exist because 42 of this deployment's symbols -- equities,
-- commodities and FX -- have no free 1-minute source. Yahoo carries years
-- of daily OHLCV and only minutes of intraday, so daily is what real data
-- actually means for them. Storing it at its true resolution is the honest
-- option; expanding it into fabricated minutes is what this table exists to
-- make unnecessary.
--
-- `ts` is the session open, in epoch seconds UTC, matching candles_1m.
CREATE TABLE IF NOT EXISTS candles_1d (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL REFERENCES symbols(symbol) ON DELETE CASCADE,
  ts     INTEGER NOT NULL,
  open   REAL NOT NULL,
  high   REAL NOT NULL,
  low    REAL NOT NULL,
  close  REAL NOT NULL,
  volume REAL NOT NULL DEFAULT 0,
  -- Same provenance contract as candles_1m: 'yahoo-chart' for real daily
  -- bars, 'synthetic' for anything generated.
  source TEXT NOT NULL DEFAULT 'synthetic',
  UNIQUE (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_1d_sym_ts ON candles_1d(symbol, ts);

-- Covering index for the volume aggregation behind /api/screener
-- (SUM(volume) GROUP BY symbol). Without `volume` in the index SQLite
-- must read every one of the ~1.5M rows; with it the aggregation is
-- answered from the index alone (~0.3s instead of ~17s).
CREATE INDEX IF NOT EXISTS idx_candles_sym_vol ON candles_1m(symbol, volume);

-- Maintained summary of per-symbol candle volume.
--
-- The screener ranks by total volume, which means SUM(volume) GROUP BY
-- symbol over ~1.5M rows on every request. That is CPU work even with a
-- covering index, and on a network/DrvFs mount it is dominated by I/O.
-- candles_1m is only ever written by the seeder and the CSV importer, so
-- the total is stable between imports and worth materialising.
--
-- `stale` is set by refresh_symbol_volume() consumers and by the importer;
-- volume_by_symbol() recomputes from candles when it is set.
CREATE TABLE IF NOT EXISTS symbol_volume (
  symbol       TEXT PRIMARY KEY,
  total_volume REAL NOT NULL DEFAULT 0,
  candle_rows  INTEGER NOT NULL DEFAULT 0,
  built_at     INTEGER NOT NULL DEFAULT 0,
  stale        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_symbol_volume_vol
  ON symbol_volume(total_volume DESC);

-- ---------------------------------------------------------------- quotes_l1
CREATE TABLE IF NOT EXISTS quotes_l1 (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol   TEXT NOT NULL REFERENCES symbols(symbol) ON DELETE CASCADE,
  ts       INTEGER NOT NULL,
  bid      REAL NOT NULL,
  ask      REAL NOT NULL,
  bid_size REAL NOT NULL DEFAULT 0,
  ask_size REAL NOT NULL DEFAULT 0,
  last     REAL,
  -- Where this quote came from: a venue name ('venue', 'coinbase') for a
  -- real book, or 'synthetic' for a generated tick. Without this a
  -- simulated price is indistinguishable from a live one, which is how a
  -- demo feed ends up being taken for a market.
  source   TEXT NOT NULL DEFAULT 'synthetic'
);
CREATE INDEX IF NOT EXISTS idx_quotes_sym_ts ON quotes_l1(symbol, ts);

-- ----------------------------------------------------------- venue_quotes
-- Per-venue top-of-book, separate from quotes_l1 on purpose.
--
-- quotes_l1 is keyed by symbol alone: exactly one price row set per
-- instrument, which is the right shape for a tape ("what is BTCUSD
-- trading at?") and the wrong shape for arbitrage, which is defined
-- precisely by holding two venues' books for the *same* instrument at
-- once and comparing them. Squeezing a second venue into quotes_l1
-- would mean either a synthetic symbol suffix (BTCUSD@coinbase), which
-- invents instruments the symbols table has never heard of, or
-- overwriting the primary tape on every poll.
--
-- So this table is additive: quotes_l1 keeps answering "what is it
-- worth", venue_quotes answers "where can I buy it and where can I sell
-- it". Rows carry `source` for the same reason quotes_l1 does -- a
-- generated tick must never be read as a real book, and the arbitrage
-- scanner refuses to price an edge off a synthetic leg.
--
-- Venue is deliberately a plain TEXT rather than a foreign key into
-- `exchanges`: those codes are the *instrument venue* (COMEX, LME) and
-- do not correspond one-to-one with the *feed endpoints* used here.
CREATE TABLE IF NOT EXISTS venue_quotes (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol   TEXT NOT NULL REFERENCES symbols(symbol) ON DELETE CASCADE,
  venue    TEXT NOT NULL,
  ts       INTEGER NOT NULL,
  bid      REAL NOT NULL,
  ask      REAL NOT NULL,
  bid_size REAL NOT NULL DEFAULT 0,
  ask_size REAL NOT NULL DEFAULT 0,
  last     REAL,
  source   TEXT NOT NULL DEFAULT 'venue',
  UNIQUE (symbol, venue, ts)
);
CREATE INDEX IF NOT EXISTS idx_vq_sym_venue_ts ON venue_quotes(symbol, venue, ts);
CREATE INDEX IF NOT EXISTS idx_vq_ts ON venue_quotes(ts);

-- ---------------------------------------------------------------- watchlists
CREATE TABLE IF NOT EXISTS watchlists (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name         TEXT NOT NULL DEFAULT 'default',
  symbols_json TEXT NOT NULL DEFAULT '[]',
  created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_watchlists_user ON watchlists(user_id);

-- ---------------------------------------------------------------- strategies
CREATE TABLE IF NOT EXISTS strategies (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL UNIQUE,
  display_name  TEXT NOT NULL DEFAULT '',
  params_json   TEXT NOT NULL DEFAULT '{}',
  tier_required TEXT NOT NULL DEFAULT 'Free',
  mode          TEXT NOT NULL DEFAULT 'wired'
);
CREATE INDEX IF NOT EXISTS idx_strategies_name ON strategies(name);

-- ---------------------------------------------------------------- backtests
CREATE TABLE IF NOT EXISTS backtests (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  strategy_id  INTEGER REFERENCES strategies(id) ON DELETE SET NULL,
  symbol       TEXT NOT NULL DEFAULT '',
  params_json  TEXT NOT NULL DEFAULT '{}',
  from_ts      INTEGER NOT NULL DEFAULT 0,
  to_ts        INTEGER NOT NULL DEFAULT 0,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  equity_json  TEXT NOT NULL DEFAULT '[]',
  status       TEXT NOT NULL DEFAULT 'done',
  created_at   INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_backtests_user ON backtests(user_id);

-- ---------------------------------------------------------------- orders
CREATE TABLE IF NOT EXISTS orders (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol          TEXT NOT NULL DEFAULT '',
  side            TEXT NOT NULL DEFAULT 'buy',
  type            TEXT NOT NULL DEFAULT 'market',
  qty             REAL NOT NULL DEFAULT 0,
  price           REAL,
  stop_price      REAL,
  status          TEXT NOT NULL DEFAULT 'open',
  mode            TEXT NOT NULL DEFAULT 'paper',
  -- The exchange's own order id for a live order. Without it a cancel can
  -- only flip the local row, which leaves the order working at the venue
  -- and produces a fill the app has no record of requesting.
  venue_order_id  TEXT NOT NULL DEFAULT '',
  client_order_id TEXT NOT NULL DEFAULT '',
  created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- ---------------------------------------------------------------- fills
CREATE TABLE IF NOT EXISTS fills (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol   TEXT NOT NULL DEFAULT '',
  -- Which way this fill went. A fill that cannot say buy from sell is an
  -- incomplete record: the UI rendered a blank column, and any consumer
  -- reading fills without joining orders had to infer direction from a
  -- position delta that may since have flipped.
  side     TEXT NOT NULL DEFAULT '',
  qty      REAL NOT NULL DEFAULT 0,
  price    REAL NOT NULL DEFAULT 0,
  fee      REAL NOT NULL DEFAULT 0,
  ts       INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_user ON fills(user_id);

-- ---------------------------------------------------------------- accounts
CREATE TABLE IF NOT EXISTS accounts (
  user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  cash         REAL NOT NULL DEFAULT 0,
  starting_cash REAL NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------- positions
CREATE TABLE IF NOT EXISTS positions (
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol       TEXT NOT NULL DEFAULT '',
  qty          REAL NOT NULL DEFAULT 0,
  avg_price    REAL NOT NULL DEFAULT 0,
  realized_pnl REAL NOT NULL DEFAULT 0,
  updated_at   INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  PRIMARY KEY (user_id, symbol)
);

-- ---------------------------------------------------------------- risk_limits
CREATE TABLE IF NOT EXISTS risk_limits (
  user_id              INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  max_position_qty     REAL NOT NULL DEFAULT 100,
  max_notional         REAL NOT NULL DEFAULT 100000,
  max_daily_loss       REAL NOT NULL DEFAULT 1000,
  max_trades_per_hour  INTEGER NOT NULL DEFAULT 20,
  cooldown_after_losses INTEGER NOT NULL DEFAULT 2,
  allowlist_json       TEXT NOT NULL DEFAULT '[]',
  eod_flatten          INTEGER NOT NULL DEFAULT 0,
  kill_switch          INTEGER NOT NULL DEFAULT 0,
  updated_at           INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- ---------------------------------------------------------------- risk_events
CREATE TABLE IF NOT EXISTS risk_events (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ts                 INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  kind               TEXT NOT NULL DEFAULT 'reject',
  reason             TEXT NOT NULL DEFAULT '',
  order_payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_risk_events_user ON risk_events(user_id);

-- ---------------------------------------------------------------- macro
-- FRED macro series (rates, curve, risk, economy).
--
-- Provenance follows cot_fetches: every fetch records where it came from and
-- what happened, so a stored number can be traced and a truncated series is
-- visible rather than silent.
--
-- Primary key is (series_id, obs_date) because FRED restates history -- a
-- CPI revision replaces the stored value for a past date, and ON CONFLICT
-- upsert is the correct semantic. Deliberately NOT a log: storing both the
-- old and new value for a restated month would make "what did we believe on
-- date X" impossible to answer, because nothing else records when we learned
-- it. macro_fetches carries that "when".
CREATE TABLE IF NOT EXISTS macro_observations (
  series_id   TEXT    NOT NULL,
  obs_date    TEXT    NOT NULL,   -- ISO YYYY-MM-DD
  value       REAL    NOT NULL,   -- '.' from FRED is dropped upstream, never 0
  units       TEXT    NOT NULL DEFAULT '',
  category    TEXT    NOT NULL DEFAULT '',
  title       TEXT    NOT NULL DEFAULT '',
  source      TEXT    NOT NULL DEFAULT 'fred',
  fetched_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  PRIMARY KEY (series_id, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_macro_obs_date ON macro_observations(obs_date DESC);

CREATE TABLE IF NOT EXISTS macro_fetches (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  series_id   TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'series',   -- single | batch
  url         TEXT NOT NULL DEFAULT '',
  rows_ok     INTEGER NOT NULL DEFAULT 0,
  missing     INTEGER NOT NULL DEFAULT 0,       -- '.' observations dropped
  latest_date TEXT NOT NULL DEFAULT '',
  ok          INTEGER NOT NULL DEFAULT 0,
  error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_macro_fetches_ts ON macro_fetches(ts DESC);

-- ---------------------------------------------------------------- feed_health
-- Poll and venue failures, per (symbol, venue).
--
-- Deliberately NOT `risk_events`. That table is an order-risk audit trail:
-- `user_id INTEGER NOT NULL REFERENCES users(id)` with foreign keys enforced
-- (data/db.py), plus an `order_payload_json` column. A dead feed has neither
-- a user nor an order. Recording one there means inventing a user id, and
-- the only reader is an admin COUNT(*) -- which would then answer "how many
-- risk rejections have we had" with a number that also includes feed
-- outages. A figure answering two questions cannot answer either.
--
-- RFC-002's acceptance line says poller failures should "log to
-- risk_events". The intent -- a failure must be durable and visible, not
-- swallowed by the isolation handler -- is what this table serves. The
-- wording conflated feed health with order risk; that is recorded in the
-- ratified RFC rather than quietly obeyed.
--
-- Rows are transitions, not per-poll. The pollers run every 5-10s over ~50
-- symbols; logging every failure would write tens of thousands of identical
-- rows an hour and bury the one event that matters. A symbol writes one
-- `down` row when it first fails and one `recovered` when it comes back.
-- See data/feeds.py:_note_feed_health.
CREATE TABLE IF NOT EXISTS feed_health (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  symbol      TEXT NOT NULL DEFAULT '',
  venue       TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL DEFAULT 'down',
  reason      TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_feed_health_symbol ON feed_health(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_feed_health_kind ON feed_health(kind, ts DESC);

-- ---------------------------------------------------------------- journal
CREATE TABLE IF NOT EXISTS journal (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ts       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  symbol   TEXT NOT NULL DEFAULT '',
  strategy TEXT NOT NULL DEFAULT '',
  note     TEXT NOT NULL DEFAULT '',
  pnl_ref  REAL
);
CREATE INDEX IF NOT EXISTS idx_journal_user ON journal(user_id);

-- ---------------------------------------------------------------- audit_log
CREATE TABLE IF NOT EXISTS audit_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  actor_user_id INTEGER,
  action        TEXT NOT NULL DEFAULT '',
  tier          TEXT NOT NULL DEFAULT '',
  paper_live    TEXT NOT NULL DEFAULT 'paper',
  details_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

-- ---------------------------------------------------------------- api_keys
CREATE TABLE IF NOT EXISTS api_keys (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key_hash   TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL DEFAULT 'default',
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

-- ================================ seeds ================================

INSERT INTO tiers (name, max_symbols, live_enabled, methods_allowed, backtest_days, api_rate) VALUES
  ('Free',         10,  0, '["ema_trend","dual_ema"]',                                                        30,  60),
  ('Pro',          50,  1, '["ema_trend","dual_ema","vwap_pullback","vwap_bands","ema_trend_cot","manual","orb_breakout","rsi_fade","mean_reversion"]', 365, 300),
  ('Institutional',100, 1, '["ema_trend","dual_ema","vwap_pullback","vwap_bands","ema_trend_cot","manual","orb_breakout","rsi_fade","mean_reversion","book_imbalance","news_momentum"]', 1825, 1200),
  ('Admin',        100, 1, '["ema_trend","dual_ema","vwap_pullback","vwap_bands","ema_trend_cot","manual","orb_breakout","rsi_fade","mean_reversion","book_imbalance","news_momentum"]', 3650, 3600)
ON CONFLICT(name) DO UPDATE SET
  max_symbols = excluded.max_symbols,
  live_enabled = excluded.live_enabled,
  methods_allowed = excluded.methods_allowed,
  backtest_days = excluded.backtest_days,
  api_rate = excluded.api_rate;

INSERT OR IGNORE INTO strategies (name, display_name, params_json, tier_required, mode) VALUES
  ('ema_trend',      'EMA Trend',       '{"fast":9,"slow":21}',                    'Free', 'wired'),
  ('vwap_pullback',  'VWAP Pullback',   '{"lookback":20,"dev_bps":10}',            'Pro',  'wired'),
  ('manual',         'Manual',          '{}',                                     'Pro',  'wired'),
  ('dual_ema',       'Dual EMA Cross',  '{"fast":12,"slow":26}',                   'Free', 'wired'),
  ('vwap_bands',    'VWAP Band (session, sigma)', '{"source":"hlc3","session_min":1440,"anchor":"session","entry_sigma":2.0,"regime_ema":200,"stop_ema":50}', 'Pro', 'wired'),
  ('ema_trend_cot','EMA Trend + COT gate', '{"fast":9,"slow":21,"cot_category":"m_money","cot_max_percentile":90.0,"cot_min_percentile":10.0,"cot_lag_days":3,"cot_require":true}', 'Pro',  'wired'),
  ('mean_reversion', 'Mean Reversion',  '{"period":20,"entry_z":2.0}',             'Pro',  'wired'),
  ('orb_breakout',   'Opening Range Breakout', '{"range_min":15}',                'Pro',  'stub'),
  ('rsi_fade',       'RSI Fade',        '{"period":14,"overbought":70,"oversold":30}', 'Pro', 'stub'),
  ('book_imbalance', 'Book Imbalance (OFI)', '{"lookback":60,"min_transitions":20,"threshold":0.15,"max_spread_bps":25.0}', 'Institutional', 'wired'),
  ('news_momentum',  'News Momentum',   '{"window_min":30}',                      'Institutional', 'stub');

INSERT OR IGNORE INTO symbols (symbol, asset_type, name, enabled, tick_size, lot_size) VALUES
  ('BTCUSD', 'crypto', 'Bitcoin / US Dollar', 1, 0.01, 0.0001),
  ('AAPL',   'equity', 'Apple Inc.',           1, 0.01, 1.0),
  ('EURUSD', 'fx',     'Euro / US Dollar',     1, 0.0001, 1000.0);

INSERT OR IGNORE INTO exchanges
  (code, name, type, asset_classes, region, currency, tz, notes) VALUES
  ('CME',     'CME Group / Globex',            'exchange', 'commodity,fx,index', 'US',     'USD', 'America/Chicago', 'Derivatives venue: metals, energy, rates, equity index.'),
  ('COMEX',   'COMEX (Metals)',                'exchange', 'commodity',          'US',     'USD', 'America/New_York','Precious & base metals futures: gold, silver, copper.'),
  ('NYMEX',   'NYMEX (Energy)',                'exchange', 'commodity',          'US',     'USD', 'America/New_York','Energy futures: WTI crude, natural gas, heating oil.'),
  ('ICE',     'Intercontinental Exchange',     'exchange', 'commodity,fx',       'EU/US',  'USD', 'UTC',            'Brent crude, softs (coffee, cocoa), FX futures.'),
  ('CBOT',    'Chicago Board of Trade',        'exchange', 'commodity',          'US',     'USD', 'America/Chicago','Grains & ags: wheat, corn, soybeans.'),
  ('LME',     'London Metal Exchange',         'exchange', 'commodity',          'UK',     'USD', 'Europe/London',  'Industrial metals: aluminium, copper, nickel, zinc.'),
  ('NYSE',    'New York Stock Exchange',       'exchange', 'equity',             'US',     'USD', 'America/New_York','US equities.'),
  ('NASDAQ',  'Nasdaq',                        'exchange', 'equity',             'US',     'USD', 'America/New_York','US equities & tech listings.'),
  ('EBS',     'EBS (FX Venue)',                'venue',    'fx',                 'EU/US',  'USD', 'UTC',            'Interbank FX liquidity venue.'),
  ('venue', 'venue',                       'exchange', 'crypto',             'Global', 'USD', 'UTC',            'Crypto spot & perpetuals.'),
  ('COINBASE','Coinbase Exchange',             'exchange', 'crypto',             'US',     'USD', 'America/New_York','US-regulated crypto spot.');

-- ---------------------------------------------------------------- CommCoin M-Pesa off-ramp
-- User/admin off-ramp requests for CommCoin/MFT -> M-Pesa (Daraja). Minima
-- funding ledger tables were removed from the primary integration; legacy
-- `mint_*` tables may exist in older databases but are not used.
CREATE TABLE IF NOT EXISTS mft_mpesa_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  mft_address TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL,
  amount_mft TEXT NOT NULL,
  amount_kes INTEGER NOT NULL,
  rate_kes_per_mft TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  daraja_response TEXT NOT NULL DEFAULT '{}',
  conversation_id TEXT NOT NULL DEFAULT '',
  originator_conversation_id TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  admin_note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  confirmed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mft_mpesa_user ON mft_mpesa_requests(user_id, id);
CREATE INDEX IF NOT EXISTS idx_mft_mpesa_status ON mft_mpesa_requests(status, id);
