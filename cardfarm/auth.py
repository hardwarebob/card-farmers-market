"""Steam sign-in by QR code (approved in the Steam mobile app), so Card Farm never sees a password.

The refresh token lasts months; access tokens last about a day and are renewed from it.
"""
import base64
import json
import os
import secrets
import time

from .http import post_json

API = "https://api.steampowered.com/IAuthenticationService"


def _claims(jwt):
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class Session:
    def __init__(self, path):
        self.path = path
        self.steamid = self.account = self.refresh_token = self.access_token = None
        self.sessionid = secrets.token_hex(12)
        self.qr = None  # pending sign-in: {client_id, request_id, url, interval, expires}
        if os.path.exists(path):
            saved = json.load(open(path))
            for k in ("steamid", "account", "refresh_token", "access_token", "sessionid"):
                setattr(self, k, saved.get(k) or getattr(self, k))

    @property
    def signed_in(self):
        return bool(self.refresh_token)

    def save(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({k: getattr(self, k) for k in
                       ("steamid", "account", "refresh_token", "access_token", "sessionid")}, f)

    def sign_out(self):
        self.steamid = self.account = self.refresh_token = self.access_token = None
        self.qr = None
        if os.path.exists(self.path):
            os.remove(self.path)

    # --- QR sign-in -------------------------------------------------------------------
    def start_qr(self):
        r = post_json(f"{API}/BeginAuthSessionViaQR/v1/",
                      {"device_friendly_name": "Card Farm", "platform_type": 2, "website_id": "Community"})["response"]
        self.qr = {"client_id": r["client_id"], "request_id": r["request_id"], "url": r["challenge_url"],
                   "interval": float(r.get("interval", 5)), "expires": time.time() + 600}
        return self.qr

    def poll_qr(self):
        """True once approved on the phone. Steam rotates the code now and then; follow it."""
        if not self.qr:
            return False
        if time.time() > self.qr["expires"]:
            self.qr = None
            return False
        r = post_json(f"{API}/PollAuthSessionStatus/v1/",
                      {"client_id": self.qr["client_id"], "request_id": self.qr["request_id"]}).get("response", {})
        if r.get("new_challenge_url"):
            self.qr["url"] = r["new_challenge_url"]
            self.qr["client_id"] = r.get("new_client_id", self.qr["client_id"])
        if not r.get("refresh_token"):
            return False
        self.refresh_token, self.access_token = r["refresh_token"], r.get("access_token")
        self.account = r.get("account_name")
        self.steamid = _claims(self.refresh_token)["sub"]
        self.qr = None
        if not self.access_token:
            self.renew()
        self.save()
        return True

    # --- tokens -----------------------------------------------------------------------
    def renew(self):
        r = post_json(f"{API}/GenerateAccessTokenForApp/v1/",
                      {"refresh_token": self.refresh_token, "steamid": self.steamid}).get("response", {})
        if not r.get("access_token"):
            raise RuntimeError("Steam wouldn't renew the sign-in; sign in again")
        self.access_token = r["access_token"]
        self.refresh_token = r.get("refresh_token") or self.refresh_token
        self.save()

    def cookies(self):
        if not self.access_token or _claims(self.access_token).get("exp", 0) < time.time() + 3600:
            self.renew()
        return {"steamLoginSecure": f"{self.steamid}%7C%7C{self.access_token}", "sessionid": self.sessionid,
                "Steam_Language": "english"}
