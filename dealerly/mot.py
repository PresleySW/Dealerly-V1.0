"""
dealerly/mot.py
===============
MOT history providers.

Three implementations behind a common interface:
  - MOTProvider      base class / protocol
  - MockMOTProvider  reads local JSON sample files (development / testing)
  - DVSAMOTProvider  calls the real DVSA MOT History API (production)

The provider is selected by Config.mot_mode:
  "0" → None (MOT disabled)
  "1" → MockMOTProvider
  "2" → DVSAMOTProvider

Depends on:
  - dealerly.config (MOT_SAMPLES_DIR)

I/O: DVSAMOTProvider makes HTTP requests. MockMOTProvider reads files.
No DB access — caching is handled by scoring.py via db.py.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

from dealerly.config import MOT_SAMPLES_DIR


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class MOTProvider:
    """Abstract MOT history provider. Subclasses must implement fetch()."""

    provider_name: str = "base"

    def fetch(self, vrm: str) -> Optional[dict]:
        """
        Fetch MOT history for a VRM.

        Returns the DVSA payload dict on success, None if not found.
        Raises RuntimeError on auth failures or unexpected API errors.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Mock provider (dev / testing)
# ---------------------------------------------------------------------------

class MockMOTProvider(MOTProvider):
    """
    Reads MOT history from local JSON files in mot_samples/.
    File naming convention: <VRM>.json  (e.g. AB12CDE.json)
    Returns None silently if the file does not exist.
    """

    provider_name = "mock-json"

    def __init__(self, samples_dir: Path = MOT_SAMPLES_DIR) -> None:
        self.samples_dir = samples_dir

    def fetch(self, vrm: str) -> Optional[dict]:
        path = self.samples_dir / f"{vrm}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# DVSA production provider
# ---------------------------------------------------------------------------

class DVSAMOTProvider(MOTProvider):
    """
    Calls the DVSA MOT History Trade API.

    Required environment variables (loaded from .env by cli.py before use):
        DVSA_MOT_TOKEN_URL      OAuth2 token endpoint
        DVSA_MOT_CLIENT_ID
        DVSA_MOT_CLIENT_SECRET
        DVSA_MOT_SCOPE_URL      OAuth2 scope
        DVSA_MOT_API_KEY        X-API-Key header value
        DVSA_MOT_BASE_URL       e.g. https://history.mot.api.gov.uk
        DVSA_MOT_ENDPOINT_PATH  e.g. /v1/trade/vehicles/registration/

    Raises KeyError on construction if any required variable is missing.
    Token is cached in-process and refreshed 30 s before expiry.
    """

    provider_name = "dvsa"

    def __init__(self) -> None:
        self._token_url     = os.environ["DVSA_MOT_TOKEN_URL"].strip()
        self._client_id     = os.environ["DVSA_MOT_CLIENT_ID"].strip()
        self._client_secret = os.environ["DVSA_MOT_CLIENT_SECRET"].strip()
        self._scope_url     = os.environ["DVSA_MOT_SCOPE_URL"].strip()
        self._api_key       = os.environ["DVSA_MOT_API_KEY"].strip()
        self._base_url      = os.environ["DVSA_MOT_BASE_URL"].strip().rstrip("/")
        ep                  = os.environ["DVSA_MOT_ENDPOINT_PATH"].strip()
        # Normalise endpoint path: must start and end with "/"
        if not ep.startswith("/"):
            ep = "/" + ep
        if not ep.endswith("/"):
            ep = ep + "/"
        self._endpoint_path = ep

        self._token: Optional[str] = None
        self._token_expiry: float = 0.0

    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Return a valid OAuth2 bearer token, refreshing if needed."""
        if self._token and time.time() < self._token_expiry - 30:
            return self._token

        r = requests.post(
            self._token_url,
            data={
                "grant_type":    "client_credentials",
                "client_id":     self._client_id,
                "client_secret": self._client_secret,
                "scope":         self._scope_url,
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"DVSA token request failed ({r.status_code}): {r.text[:400]}"
            )
        payload = r.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    def fetch(self, vrm: str) -> Optional[dict]:
        """
        Look up MOT history for vrm.

        Returns the parsed JSON payload, or None for 404 (vehicle not found).
        Raises RuntimeError on 401/403 (auth failure).
        Raises requests.HTTPError on other 4xx/5xx responses.
        """
        vrm = (vrm or "").upper().replace(" ", "")
        if not vrm:
            return None

        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "X-API-Key": self._api_key,
            "Accept": "application/json",
        }
        url = f"{self._base_url}{self._endpoint_path}{vrm}"
        r = requests.get(url, headers=headers, timeout=30)

        if r.status_code == 404:
            return None
        if r.status_code in (401, 403):
            raise RuntimeError(
                f"DVSA auth error {r.status_code}: {r.text[:200]}"
            )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_mot_provider(mot_mode: str) -> Optional[MOTProvider]:
    """
    Construct the appropriate MOTProvider from a mot_mode config string.

    Args:
        mot_mode: "0" = disabled, "1" = mock-json, "2" = DVSA

    Returns:
        A MOTProvider instance, or None if mot_mode is "0".
        Prints a warning and returns None if DVSA env vars are missing.
    """
    if mot_mode == "1":
        return MockMOTProvider()
    if mot_mode == "2":
        try:
            return DVSAMOTProvider()
        except KeyError as exc:
            print(f"\n[MOT] DVSA env var missing: {exc}. MOT disabled.")
            return None
    return None
