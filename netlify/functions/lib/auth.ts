export const AUTH_BASE = process.env.NEON_AUTH_BASE_URL || "";
export const JWKS_URL = process.env.NEON_AUTH_JWKS_URL || "";

export function authConfig() {
  return { base: AUTH_BASE, jwks: JWKS_URL };
}