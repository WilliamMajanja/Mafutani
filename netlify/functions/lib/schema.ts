// Minimal Postgres schema for the Netlify port. Applied on first boot via /api/admin/init.
export const SCHEMA = `
CREATE TABLE IF NOT EXISTS tiers (
  name TEXT PRIMARY KEY,
  max_symbols INTEGER NOT NULL DEFAULT 10,
  live_enabled INTEGER NOT NULL DEFAULT 0,
  methods_allowed TEXT NOT NULL DEFAULT '[]',
  backtest_days INTEGER NOT NULL DEFAULT 30,
  api_rate INTEGER NOT NULL DEFAULT 60
);
INSERT INTO tiers (name, max_symbols, live_enabled, methods_allowed, backtest_days, api_rate) VALUES
  ('Free', 10, 0, '["ema_trend","dual_ema"]', 30, 60),
  ('Pro', 50, 1, '["ema_trend","dual_ema","vwap_pullback","vwap_bands","ema_trend_cot","manual","orb_breakout","rsi_fade","mean_reversion"]', 365, 300),
  ('Institutional', 100, 1, '["ema_trend","dual_ema","vwap_pullback","vwap_bands","ema_trend_cot","manual","orb_breakout","rsi_fade","mean_reversion","book_imbalance","news_momentum"]', 1825, 1200),
  ('Admin', 100, 1, '["ema_trend","dual_ema","vwap_pullback","vwap_bands","ema_trend_cot","manual","orb_breakout","rsi_fade","mean_reversion","book_imbalance","news_momentum"]', 3650, 3600)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  tier TEXT NOT NULL DEFAULT 'Free' REFERENCES tiers(name),
  created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
  disabled INTEGER NOT NULL DEFAULT 0,
  totp_secret TEXT NOT NULL DEFAULT '',
  totp_enabled INTEGER NOT NULL DEFAULT 0,
  recovery_codes TEXT NOT NULL DEFAULT '[]',
  last_login_ts BIGINT NOT NULL DEFAULT 0,
  failed_logins INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
  expires_at BIGINT NOT NULL,
  csrf_token TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS news (
  id SERIAL PRIMARY KEY,
  ts BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
  symbol TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_news_symbol ON news(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS accounts (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  cash DOUBLE PRECISION NOT NULL DEFAULT 0,
  starting_cash DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL DEFAULT '',
  qty DOUBLE PRECISION NOT NULL DEFAULT 0,
  avg_price DOUBLE PRECISION NOT NULL DEFAULT 0,
  realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
  updated_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
  PRIMARY KEY (user_id, symbol)
);

CREATE TABLE IF NOT EXISTS risk_limits (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  max_position_qty DOUBLE PRECISION NOT NULL DEFAULT 100,
  max_notional DOUBLE PRECISION NOT NULL DEFAULT 100000,
  max_daily_loss DOUBLE PRECISION NOT NULL DEFAULT 1000,
  max_trades_per_hour INTEGER NOT NULL DEFAULT 20,
  cooldown_after_losses INTEGER NOT NULL DEFAULT 2,
  allowlist_json TEXT NOT NULL DEFAULT '[]',
  eod_flatten INTEGER NOT NULL DEFAULT 0,
  kill_switch INTEGER NOT NULL DEFAULT 0,
  updated_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);

CREATE TABLE IF NOT EXISTS orders (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL DEFAULT '',
  side TEXT NOT NULL DEFAULT 'buy',
  type TEXT NOT NULL DEFAULT 'market',
  qty DOUBLE PRECISION NOT NULL DEFAULT 0,
  price REAL,
  stop_price REAL,
  status TEXT NOT NULL DEFAULT 'open',
  mode TEXT NOT NULL DEFAULT 'paper',
  venue_order_id TEXT NOT NULL DEFAULT '',
  client_order_id TEXT NOT NULL DEFAULT '',
  required_approvals INTEGER NOT NULL DEFAULT 0,
  approvals_count INTEGER NOT NULL DEFAULT 0,
  approval_note TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS exchanges (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT 'exchange',
  asset_classes TEXT NOT NULL DEFAULT '',
  region TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT 'USD',
  tz TEXT NOT NULL DEFAULT 'UTC',
  notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_exchanges_asset ON exchanges(asset_classes);

CREATE TABLE IF NOT EXISTS quotes_l1 (
  id SERIAL PRIMARY KEY,
  symbol TEXT NOT NULL,
  ts BIGINT NOT NULL,
  bid DOUBLE PRECISION NOT NULL,
  ask DOUBLE PRECISION NOT NULL,
  bid_size DOUBLE PRECISION NOT NULL DEFAULT 0,
  ask_size DOUBLE PRECISION NOT NULL DEFAULT 0,
  last REAL,
  source TEXT NOT NULL DEFAULT 'synthetic'
);
CREATE INDEX IF NOT EXISTS idx_quotes_sym_ts ON quotes_l1(symbol, ts);

CREATE TABLE IF NOT EXISTS candles_1m (
  id SERIAL PRIMARY KEY,
  symbol TEXT NOT NULL,
  ts BIGINT NOT NULL,
  open DOUBLE PRECISION NOT NULL,
  high DOUBLE PRECISION NOT NULL,
  low DOUBLE PRECISION NOT NULL,
  close DOUBLE PRECISION NOT NULL,
  volume DOUBLE PRECISION NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'synthetic',
  UNIQUE (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_sym_ts ON candles_1m(symbol, ts);

CREATE TABLE IF NOT EXISTS journal (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ts BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
  symbol TEXT NOT NULL DEFAULT '',
  strategy TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  pnl_ref DOUBLE PRECISION,
  kind TEXT NOT NULL DEFAULT 'note',
  details_json TEXT NOT NULL DEFAULT '{}',
  ref_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_journal_user ON journal(user_id);
CREATE INDEX IF NOT EXISTS idx_journal_user_kind ON journal(user_id, kind, ts DESC);

CREATE TABLE IF NOT EXISTS strategies (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL DEFAULT '',
  params_json TEXT NOT NULL DEFAULT '{}',
  tier_required TEXT NOT NULL DEFAULT 'Free',
  mode TEXT NOT NULL DEFAULT 'wired'
);
CREATE INDEX IF NOT EXISTS idx_strategies_name ON strategies(name);

INSERT INTO strategies (name, display_name, params_json, tier_required, mode) VALUES
  ('ema_trend', 'EMA Trend', '{"fast":9,"slow":21}', 'Free', 'wired'),
  ('dual_ema', 'Dual EMA Cross', '{"fast":12,"slow":26}', 'Free', 'wired'),
  ('vwap_pullback', 'VWAP Pullback', '{"lookback":20,"dev_bps":10}', 'Pro', 'wired'),
  ('manual', 'Manual', '{}', 'Pro', 'wired'),
  ('vwap_bands', 'VWAP Band (session, sigma)', '{"source":"hlc3","session_min":1440,"anchor":"session","entry_sigma":2.0,"regime_ema":200,"stop_ema":50}', 'Pro', 'wired'),
  ('ema_trend_cot', 'EMA Trend + COT gate', '{"fast":9,"slow":21,"cot_category":"m_money","cot_max_percentile":90.0,"cot_min_percentile":10.0,"cot_lag_days":3,"cot_require":true}', 'Pro', 'wired'),
  ('mean_reversion', 'Mean Reversion', '{"period":20,"entry_z":2.0}', 'Pro', 'wired'),
  ('orb_breakout', 'Opening Range Breakout', '{"range_min":15}', 'Pro', 'stub'),
  ('rsi_fade', 'RSI Fade', '{"period":14,"overbought":70,"oversold":30}', 'Pro', 'stub'),
  ('book_imbalance', 'Book Imbalance (OFI)', '{"lookback":60,"min_transitions":20,"threshold":0.15,"max_spread_bps":25.0}', 'Institutional', 'wired'),
  ('news_momentum', 'News Momentum', '{"window_min":30}', 'Institutional', 'stub')
ON CONFLICT (name) DO NOTHING;

INSERT INTO exchanges (code, name, type, asset_classes, region, currency, tz, notes) VALUES
  ('CME', 'CME Group / Globex', 'exchange', 'commodity,fx,index', 'US', 'USD', 'America/Chicago', 'Derivatives venue'),
  ('NYSE', 'New York Stock Exchange', 'exchange', 'equity', 'US', 'USD', 'America/New_York', 'US equities'),
  ('NASDAQ', 'Nasdaq', 'exchange', 'equity', 'US', 'USD', 'America/New_York', 'US equities'),
  ('venue', 'venue', 'exchange', 'crypto', 'Global', 'USD', 'UTC', 'Crypto spot'),
  ('COINBASE', 'Coinbase Exchange', 'exchange', 'crypto', 'US', 'USD', 'America/New_York', 'US-regulated crypto spot')
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS symbols (
  symbol TEXT PRIMARY KEY,
  asset_type TEXT NOT NULL DEFAULT 'equity',
  name TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  tick_size DOUBLE PRECISION NOT NULL DEFAULT 0.01,
  lot_size DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  exchange TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT 'USD',
  sector TEXT NOT NULL DEFAULT ''
);

INSERT INTO symbols (symbol, asset_type, name, enabled, tick_size, lot_size) VALUES
  ('BTCUSD', 'crypto', 'Bitcoin / US Dollar', 1, 0.01, 0.0001),
  ('AAPL', 'equity', 'Apple Inc.', 1, 0.01, 1.0),
  ('EURUSD', 'fx', 'Euro / US Dollar', 1, 0.0001, 1000.0)
ON CONFLICT (symbol) DO NOTHING;

CREATE TABLE IF NOT EXISTS watchlists (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL DEFAULT 'default',
  symbols_json TEXT NOT NULL DEFAULT '[]',
  created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_watchlists_user ON watchlists(user_id);

CREATE TABLE IF NOT EXISTS backtests (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  strategy_id INTEGER,
  symbol TEXT NOT NULL DEFAULT '',
  params_json TEXT NOT NULL DEFAULT '{}',
  from_ts BIGINT NOT NULL DEFAULT 0,
  to_ts BIGINT NOT NULL DEFAULT 0,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  equity_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'done',
  created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_backtests_user ON backtests(user_id);

CREATE TABLE IF NOT EXISTS candles_1d (
  id SERIAL PRIMARY KEY,
  symbol TEXT NOT NULL,
  ts BIGINT NOT NULL,
  open DOUBLE PRECISION NOT NULL,
  high DOUBLE PRECISION NOT NULL,
  low DOUBLE PRECISION NOT NULL,
  close DOUBLE PRECISION NOT NULL,
  volume DOUBLE PRECISION NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'yahoo-chart',
  UNIQUE (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_1d_sym_ts ON candles_1d(symbol, ts);

CREATE TABLE IF NOT EXISTS fills (
  id SERIAL PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL DEFAULT '',
  side TEXT NOT NULL DEFAULT '',
  qty DOUBLE PRECISION NOT NULL DEFAULT 0,
  price DOUBLE PRECISION NOT NULL DEFAULT 0,
  fee DOUBLE PRECISION NOT NULL DEFAULT 0,
  cash_after REAL,
  ts BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_user ON fills(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY,
  ts BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
  actor_user_id INTEGER,
  action TEXT NOT NULL DEFAULT '',
  tier TEXT NOT NULL DEFAULT '',
  paper_live TEXT NOT NULL DEFAULT 'paper',
  details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_user_id);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
  updated_by INTEGER
);

CREATE TABLE IF NOT EXISTS cash_movements (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  movement_type TEXT NOT NULL DEFAULT 'deposit',
  rail TEXT NOT NULL DEFAULT 'mpesa',
  amount DOUBLE PRECISION NOT NULL,
  currency TEXT NOT NULL DEFAULT 'KES',
  external_reference TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  idempotency_key TEXT NOT NULL UNIQUE,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
  confirmed_at BIGINT,
  confirmed_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cash_user ON cash_movements(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_cash_status ON cash_movements(status, id DESC);

CREATE TABLE IF NOT EXISTS consolidation_bases (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  region TEXT NOT NULL,
  country TEXT NOT NULL,
  city TEXT NOT NULL,
  lat DOUBLE PRECISION NOT NULL,
  lon DOUBLE PRECISION NOT NULL,
  related_symbols TEXT NOT NULL DEFAULT '[]',
  operator TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  updated_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_bases_region ON consolidation_bases(region, type);
`;
