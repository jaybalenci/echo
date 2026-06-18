"""Thread-safe account pool — one account per concurrent price check."""

from __future__ import annotations

import base64
import json
import random
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from core.logger import log
from doordash.web_client import load_account_pool as _load


def _email_from_cookies(cookies: dict[str, str]) -> str:
    token = cookies.get("ddweb_token", "")
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        data = json.loads(base64.b64decode(payload_b64))
        return (data.get("user") or {}).get("email") or "unknown"
    except Exception:
        return "unknown"

_lock = threading.Lock()
_accounts: list[dict[str, str]] = []
_in_use: set[int] = set()

_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_DIR = _ROOT / "config" / "accounts"
_CF_PATH = _ROOT / "config" / "cf_clearance.txt"


def _ensure_loaded() -> None:
    global _accounts
    if not _accounts:
        _accounts = _load(_ACCOUNTS_DIR, _CF_PATH)


@contextmanager
def acquire(*, exclude: frozenset[int] = frozenset()) -> Generator[tuple[int, dict[str, str]], None, None]:
    """Claim one free account for the duration of a price check, then release it.

    Yields (index, cookies). Pass already-tried indices via *exclude* to skip them.
    """
    with _lock:
        _ensure_loaded()
        free = [i for i in range(len(_accounts)) if i not in _in_use and i not in exclude]
        if not free:
            if exclude and len(exclude) >= len(_accounts):
                raise RuntimeError(
                    "All accounts are Cloudflare-blocked (HTTP 403). "
                    "Paste a fresh cf_clearance value into config/cf_clearance.txt."
                )
            raise RuntimeError(
                f"All {len(_accounts)} accounts are currently busy — try again in a moment."
            )
        idx = random.choice(free)
        _in_use.add(idx)

    account = _accounts[idx]
    email = _email_from_cookies(account)
    log("pool", f"account {idx + 1}/{len(_accounts)}: {email}")
    try:
        yield idx, account
    finally:
        with _lock:
            _in_use.discard(idx)
        log("pool", f"released {idx + 1}: {email}")
