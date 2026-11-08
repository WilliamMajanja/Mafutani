"""M-Pesa Daraja API integration (stdlib only).

Supports operator-configured withdrawals/payouts to M-Pesa via Daraja.
This module makes HTTPS requests using urllib; it has no third-party deps.

IMPORTANT (policy/compliance):
- Daraja/M-Pesa is a third-party service with its own terms, fees, limits,
  availability, and compliance requirements.
- The OPERATOR must configure credentials securely (env/secret store), enable
  only in lawful jurisdictions, and satisfy KYC/AML/sanctions/tax obligations.
- This code provides technical integration hooks; it does not make a deployment
  compliant.
- B2C "SecurityCredential" is normally the initiator password encrypted with
  Safaricom's public certificate. Pure-Python RSA/cert handling is non-trivial
  under a zero-dep constraint; therefore this implementation accepts
  `security_credential` from configuration (pre-encrypted by the operator using
  Safaricom's tooling/cert). Do NOT pass plaintext passwords.

Environment/config (examples):
  DARAJA_ENV=sandbox|production
  DARAJA_CONSUMER_KEY=...
  DARAJA_CONSUMER_SECRET=...
  DARAJA_SHORTCODE=...
  DARAJA_INITIATOR_NAME=...
  DARAJA_SECURITY_CREDENTIAL=<pre-encrypted>
  DARAJA_B2C_TIMEOUT_URL=https://...
  DARAJA_B2C_RESULT_URL=https://...
  DARAJA_COMMAND_ID=BusinessPayment  (or SalaryPayment/PromotionPayment)
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class DarajaError(Exception):
    pass


def _env(name, default=""):
    v = os.environ.get(name)
    return default if v is None else str(v)


class DarajaClient(object):
    def __init__(self, consumer_key=None, consumer_secret=None, shortcode=None,
                 initiator=None, security_credential=None, env=None,
                 timeout_url=None, result_url=None, command_id=None,
                 timeout=30):
        self.env = (env or _env("DARAJA_ENV", "sandbox")).strip().lower()
        self.consumer_key = consumer_key or _env("DARAJA_CONSUMER_KEY")
        self.consumer_secret = consumer_secret or _env("DARAJA_CONSUMER_SECRET")
        self.shortcode = shortcode or _env("DARAJA_SHORTCODE")
        self.initiator = initiator or _env("DARAJA_INITIATOR_NAME")
        self.security_credential = security_credential or _env("DARAJA_SECURITY_CREDENTIAL")
        self.timeout_url = timeout_url or _env("DARAJA_B2C_TIMEOUT_URL")
        self.result_url = result_url or _env("DARAJA_B2C_RESULT_URL")
        self.command_id = command_id or _env("DARAJA_COMMAND_ID", "BusinessPayment")
        self.timeout = int(timeout)
        self._token = None
        self._token_exp = 0

    @property
    def base_url(self):
        if self.env == "production":
            return "https://api.safaricom.co.ke"
        return "https://sandbox.safaricom.co.ke"

    def configured(self):
        return bool(self.consumer_key and self.consumer_secret and self.shortcode)

    def _request(self, method, path, body=None, headers=None, auth_token=None):
        url = self.base_url + path
        data = None
        hdrs = {"Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        if auth_token:
            hdrs["Authorization"] = "Bearer " + auth_token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace") if hasattr(e, "read") else str(e)
            raise DarajaError("Daraja HTTP %s: %s" % (getattr(e, "code", "?"), raw[:500]))
        except Exception as e:
            raise DarajaError("Daraja request failed: %s" % e)
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {"raw": raw}

    def get_access_token(self, force=False):
        now = time.time()
        if not force and self._token and now < self._token_exp - 30:
            return self._token
        if not (self.consumer_key and self.consumer_secret):
            raise DarajaError("Daraja consumer key/secret not configured")
        creds = base64.b64encode(
            ("%s:%s" % (self.consumer_key, self.consumer_secret)).encode("utf-8")
        ).decode("ascii")
        url = self.base_url + "/oauth/v1/generate?grant_type=client_credentials"
        req = urllib.request.Request(url, headers={"Authorization": "Basic " + creds})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            raise DarajaError("Daraja OAuth failed: %s" % e)
        tok = obj.get("access_token")
        if not tok:
            raise DarajaError("Daraja OAuth returned no access_token: %s" % obj)
        self._token = tok
        try:
            self._token_exp = now + int(obj.get("expires_in", 3599))
        except Exception:
            self._token_exp = now + 3599
        return tok

    def normalize_msisdn(self, phone):
        """Normalize to 2547XXXXXXXX / 2541XXXXXXXX format where possible."""
        p = "".join(ch for ch in str(phone or "") if ch.isdigit() or ch == "+")
        p = p.lstrip("+")
        if p.startswith("0") and len(p) == 10:
            p = "254" + p[1:]
        if p.startswith("7") and len(p) == 9:
            p = "254" + p
        if p.startswith("1") and len(p) == 9:
            p = "254" + p
        if not (p.startswith("254") and len(p) == 12):
            raise DarajaError("invalid M-Pesa phone number; expected Kenya MSISDN (e.g. 2547XXXXXXXX)")
        return p

    def b2c_payment(self, phone, amount, remarks="Mafutani withdrawal", occasion="",
                    originator_conversation_id=None):
        """Initiate B2C payout. Returns Daraja response; caller must persist IDs
        and reconcile via result callbacks - API acceptance != money delivered."""
        if not self.configured():
            raise DarajaError("Daraja is not configured")
        for name, val in (("initiator", self.initiator),
                          ("security_credential", self.security_credential),
                          ("timeout_url", self.timeout_url),
                          ("result_url", self.result_url)):
            if not val:
                raise DarajaError("Daraja B2C missing %s" % name)
        msisdn = self.normalize_msisdn(phone)
        try:
            amt = int(float(str(amount)))
        except Exception:
            raise DarajaError("invalid amount")
        if amt <= 0:
            raise DarajaError("amount must be positive")
        token = self.get_access_token()
        payload = {
            "InitiatorName": self.initiator,
            "SecurityCredential": self.security_credential,
            "CommandID": self.command_id or "BusinessPayment",
            "Amount": amt,
            "PartyA": self.shortcode,
            "PartyB": msisdn,
            "Remarks": (remarks or "Mafutani withdrawal")[:100],
            "QueueTimeOutURL": self.timeout_url,
            "ResultURL": self.result_url,
            "Occasion": (occasion or remarks or "Mafutani")[:100],
        }
        if originator_conversation_id:
            payload["OriginatorConversationID"] = str(originator_conversation_id)[:64]
        res = self._request("POST", "/mpesa/b2c/v1/paymentrequest", body=payload, auth_token=token)
        # Normalize key fields for callers
        return {
            "ok": str(res.get("ResponseCode", "")) == "0",
            "provider": "daraja_b2c",
            "env": self.env,
            "phone": msisdn,
            "amount": amt,
            "conversation_id": res.get("ConversationID"),
            "originator_conversation_id": res.get("OriginatorConversationID") or originator_conversation_id,
            "response_code": res.get("ResponseCode"),
            "response_description": res.get("ResponseDescription"),
            "raw": res,
            "policy": {
                "accepted_not_settled": True,
                "reconcile_via_callback": True,
                "operator_compliance_required": True,
            },
        }

    def status(self):
        return {
            "provider": "daraja",
            "env": self.env,
            "configured": self.configured(),
            "b2c_ready": bool(self.configured() and self.initiator and self.security_credential
                              and self.timeout_url and self.result_url),
            "shortcode_set": bool(self.shortcode),
            "security_credential_set": bool(self.security_credential),
            "note": "SecurityCredential must be pre-encrypted per Safaricom requirements (zero-dep build).",
        }
