"""Set delivery address on the build account (and optionally verify checkout total).

Usage:
    python tests/set_address.py --address "44 norman st 06704"
    python tests/set_address.py --address "44 norman st 06704" --cart-id <uuid>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curl_cffi.requests.exceptions import RequestException

from doordash.address import set_delivery_address
from doordash.web_client import DoorDashWebSession, cart_referer, load_cookies

DEFAULT_COOKIES = ROOT / "config" / "doordash_build_cookies.txt"


def main() -> None:
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Set DoorDash delivery address")
    parser.add_argument("--address", required=True, help="Street address text")
    parser.add_argument("--cart-id", default="", help="Optional cart id for warm referer")
    parser.add_argument(
        "--cookies-file",
        type=Path,
        default=DEFAULT_COOKIES,
        help="Cookie file path",
    )
    args = parser.parse_args()

    cookies = load_cookies(args.cookies_file)
    referer = cart_referer(args.cart_id) if args.cart_id else "https://www.doordash.com/"
    client = DoorDashWebSession(cookies)
    client.warm(referer)

    try:
        result = set_delivery_address(client, args.address, referer=referer)
    except (RequestException, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    default_address = result.get("default_address") or {}
    print("=" * 60)
    print("ADDRESS SET")
    print("=" * 60)
    print(f"Mode:              {result.get('mode')}")
    print(f"Requested:         {result.get('requested')}")
    print(f"Printable:         {default_address.get('printableAddress')}")
    print(f"Default address id: {default_address.get('id')}")
    print(f"Cart id (if any):  {result.get('order_cart_id')}")
    print("=" * 60)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
