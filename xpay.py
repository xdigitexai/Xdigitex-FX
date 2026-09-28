"""Server-side Xdigitex Pay API client based on pay.xdigitex.space/docs.

The API key is read only from the process environment and never returned to a
browser or included in application logs.
"""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class XPayError(Exception):
    pass


class XDigitexPay:
    def __init__(self):
        self.base = os.environ.get("XDIGITEX_PAY_BASE_URL", "https://pay.xdigitex.space/api").rstrip("/")
        self.key = os.environ.get("XDIGITEX_PAY_API_KEY", "").strip()

    @property
    def ready(self):
        origin = urlsplit(os.environ.get("PUBLIC_BASE_URL", "").strip())
        public_url_ok = origin.scheme == "https" and bool(origin.hostname)
        public_url_ok |= origin.scheme == "http" and origin.hostname in ("localhost", "127.0.0.1")
        return bool(self.key and public_url_ok)

    def call(self, method, path, payload=None):
        if not self.key:
            raise XPayError("Xdigitex Pay API key is not configured on the server.")
        url = self.base + "/" + path.lstrip("/")
        raw = json.dumps(payload).encode() if payload is not None else None
        headers = {"X-API-Key": self.key, "Accept": "application/json"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        req = Request(url, data=raw, headers=headers, method=method)
        try:
            with urlopen(req, timeout=20) as response:
                body = response.read(1_000_001)
                if len(body) > 1_000_000:
                    raise XPayError("Xdigitex Pay returned an unexpectedly large response.")
                data = json.loads(body or b"{}")
        except HTTPError as e:
            try:
                data = json.loads(e.read(16_384) or b"{}")
                message = data.get("message") or data.get("error") or data.get("status", {}).get("message")
            except Exception:
                message = None
            raise XPayError(f"Xdigitex Pay rejected the request ({e.code}). {message or 'Check merchant API settings.'}")
        except (URLError, TimeoutError, OSError) as e:
            raise XPayError(f"Could not reach Xdigitex Pay: {type(e).__name__}.")
        except json.JSONDecodeError:
            raise XPayError("Xdigitex Pay returned an invalid response.")
        if not isinstance(data, dict):
            raise XPayError("Xdigitex Pay returned an unexpected response format.")
        if data.get("success") is False or (isinstance(data.get("status"), dict) and data["status"].get("code", 200) not in (0, 200, 201)):
            detail = data.get("message") or data.get("status", {}).get("message") or "Provider request failed."
            raise XPayError(str(detail)[:240])
        return data

    def initiate_payment(self, payload):
        return self.call("POST", "payments/initiate", payload)

    def payment_status(self, reference):
        return self.call("GET", "payments/" + quote(reference, safe="") + "/status")

    def withdrawals(self):
        return self.call("GET", "withdrawals")

    def balance(self):
        return self.call("GET", "balance")

    def initiate_withdrawal(self, payload):
        return self.call("POST", "withdrawals", payload)
