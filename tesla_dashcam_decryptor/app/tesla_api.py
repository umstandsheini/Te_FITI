"""
Direct client for the Tesla key service (optional hybrid path).

POST https://dashcam.tesla.com/api/1/decrypt/batch  (Bearer)
  {items:[{id,vin,key_id,timestamp,wrapped_key,public_key}]} -> {results:[{id,key}]}

dashcam.tesla.com is behind Akamai. If the server call goes through -> great.
If it is blocked (403/challenge) -> caller falls back to the browser bookmarklet.
"""
import json, base64, urllib.request, urllib.error

BATCH_URL = "https://dashcam.tesla.com/api/1/decrypt/batch"
CHUNK = 30  # API-Maximum (envoy: "batch size exceeds maximum of 30")


class DecryptApiError(Exception):
    pass


def fetch_keys(items: list, access_token: str) -> dict:
    """Returns {id: base64-FEK} for successful entries. Raises on HTTP/network error."""
    out = {}
    for i in range(0, len(items), CHUNK):
        body = json.dumps({"items": items[i:i + CHUNK]}).encode()
        req = urllib.request.Request(
            BATCH_URL, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {access_token}",
                     "Accept": "application/json", "User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            _body = b""
            try:
                _body = e.read()[:500]
            except Exception:
                pass
            _srv = e.headers.get("Server", "?") if getattr(e, "headers", None) else "?"
            raise DecryptApiError(f"HTTP {e.code} server={_srv} body={_body!r}")
        except urllib.error.URLError as e:
            raise DecryptApiError(f"network: {e.reason}")
        for res in payload.get("results", []):
            if res.get("key"):
                out[res["id"]] = res["key"]   # keep as base64 (as stored)
    return out
