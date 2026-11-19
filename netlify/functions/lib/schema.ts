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
