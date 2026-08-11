"""
Tesla OAuth (PKCE) for the dashcam client – for the optional Direct API.

clientId=dashcam, auth.tesla.com/oauth2/v3, redirect dashcam.tesla.com/callback (fixed),
scope incl. offline_access -> refresh token (live verified). Token cache in /data.
First login requires a browser (fixed redirect URL); afterwards the add-on refreshes
the token automatically.
"""
import json, time, base64, hashlib, secrets, urllib.parse, os, urllib.request

AUTH = "https://auth.tesla.com/oauth2/v3"
CLIENT_ID = "dashcam"
REDIRECT = "https://dashcam.tesla.com/callback"
SCOPE = "openid profile email employee offline_access"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


class TeslaAuth:
    def __init__(self, store_path: str):
        self.store_path = store_path
        self.pkce_path = store_path + ".pkce"
        self._pkce = None

    def make_login_url(self) -> str:
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(secrets.token_bytes(16))
        self._pkce = (verifier, state)
        json.dump({"v": verifier, "s": state}, open(self.pkce_path, "w"))
        q = {"client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
             "scope": SCOPE, "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{AUTH}/authorize?" + urllib.parse.urlencode(q)

    def exchange_code(self, callback_url: str) -> dict:
        if not self._pkce and os.path.exists(self.pkce_path):
            p = json.load(open(self.pkce_path))
            self._pkce = (p["v"], p["s"])
        if not self._pkce:
            raise RuntimeError("call make_login_url() first")
        verifier, state = self._pkce
        q = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
        code = q.get("code", [None])[0]
        if q.get("state", [None])[0] != state:
            raise RuntimeError("state mismatch (expired?)")
        if not code:
            raise RuntimeError("no code in the URL")
        tok = _post_form(f"{AUTH}/token", {
            "grant_type": "authorization_code", "client_id": CLIENT_ID,
            "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier})
        self._save(tok)
        return tok

    def get_access_token(self):
        """Returns a valid access token, or None if no login/refresh is possible."""
        tok = self._load()
        if not tok:
            return None
        if tok.get("_expires_at", 0) - 60 > time.time():
            return tok["access_token"]
        rt = tok.get("refresh_token")
        if not rt:
            return None
        try:
            new = _post_form(f"{AUTH}/token", {
                "grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": rt})
        except Exception:
            return None
        new.setdefault("refresh_token", rt)
        self._save(new)
        return new["access_token"]

    def status(self) -> dict:
        tok = self._load() or {}
        return {"logged_in": bool(tok), "has_refresh": bool(tok.get("refresh_token"))}

    def _save(self, tok: dict):
        tok = dict(tok)
        tok["_expires_at"] = time.time() + int(tok.get("expires_in", 28800))
        json.dump(tok, open(self.store_path, "w"))

    def _load(self):
        if os.path.exists(self.store_path):
            try:
                return json.load(open(self.store_path))
            except Exception:
                return None
        return None
