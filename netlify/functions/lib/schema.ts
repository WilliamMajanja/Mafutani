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
  cash REAL NOT NULL DEFAULT 0,
  starting_cash REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL DEFAULT '',
  qty REAL NOT NULL DEFAULT 0,
  avg_price REAL NOT NULL DEFAULT 0,
  realized_pnl REAL NOT NULL DEFAULT 0,
  updated_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
  PRIMARY KEY (user_id, symbol)
);

CREATE TABLE IF NOT EXISTS risk_limits (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  max_position_qty REAL NOT NULL DEFAULT 100,
  max_notional REAL NOT NULL DEFAULT 100000,
  max_daily_loss REAL NOT NULL DEFAULT 1000,
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
  qty REAL NOT NULL DEFAULT 0,
  price REAL,
  stop_price REAL,
  status TEXT NOT NULL DEFAULT 'open',
  mode TEXT NOT NULL DEFAULT 'paper',
  venue_order_id TEXT NOT NULL DEFAULT '',
  client_order_id TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);

CREATE TABLE IF NOT EXISTS symbols (
  symbol TEXT PRIMARY KEY,
  asset_type TEXT NOT NULL DEFAULT 'equity',
  name TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  tick_size REAL NOT NULL DEFAULT 0.01,
  lot_size REAL NOT NULL DEFAULT 1.0,
  exchange TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT 'USD',
  sector TEXT NOT NULL DEFAULT ''
);
`;
