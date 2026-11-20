"""Exits 0 when the web UI answers. Run with: docker exec narrator python3 healthcheck.py"""

import os
import sys
import urllib.request

try:
    port = os.environ.get("WEB_PORT", "9895")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception as e:  # noqa: BLE001
    print(f"UNHEALTHY: {e}")
    sys.exit(1)
