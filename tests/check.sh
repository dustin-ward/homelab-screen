#!/bin/sh
# Fast pre-deploy checks for homelab-screen.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d)
PID=
trap 'rm -rf "$TMP"; [ -n "$PID" ] && kill "$PID" 2>/dev/null || true' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== python syntax =="
python3 -m py_compile "$ROOT/src/collector.py" "$ROOT/src/media.py"

echo "== media fixture snapshot =="
python3 "$ROOT/tests/media_check.py"

echo "== inline JS syntax =="
python3 - "$ROOT/src/kiosk.html" "$TMP/kiosk.js" <<'PY'
import pathlib
import re
import sys

html = pathlib.Path(sys.argv[1]).read_text()
match = re.search(r"<script>(.*?)</script>", html, re.S)
if not match:
    raise SystemExit("no inline <script> found")
pathlib.Path(sys.argv[2]).write_text(match.group(1))
PY
if command -v node >/dev/null 2>&1; then
  node --check "$TMP/kiosk.js"
else
  echo "node not found; skipping JS syntax check"
fi

echo "== no identifying info in tracked files =="
if git -C "$ROOT" grep -I -n -iE 'black''forest|192\.168\.|/home/dwa''rd|dwa''rd' -- .; then
  fail "identifying host/user/address found in tracked files"
fi

echo "== unit specifiers =="
if grep -nE '%[hU]' "$ROOT"/src/*.service; then
  fail "systemd %h/%U resolve against the manager user, not User="
fi

echo "== chart width budget =="
python3 - "$ROOT/src/kiosk.html" <<'PY'
import re
import sys

html = open(sys.argv[1], encoding="utf-8").read()
base = re.search(r"\.spark\s*\{([^}]*)\}", html)
assert base and "overflow: hidden" in base.group(1), "sparklines must clip, not overflow"
tall = re.search(r"\.spark\.tall\s*\{([^}]*)\}", html)
assert tall, "missing .spark.tall rule"
size = re.search(r"font-size:\s*(\d+)px", tall.group(1))
assert size, "missing font-size in .spark.tall"
assert int(size.group(1)) <= 13, "60-glyph sparklines need <= 13px to fit the panel"
PY

echo "== stub snapshot smoke test =="
export HOMELAB_CONFIG_HOME="$TMP/config"
export HOMELAB_APP_HOME="$TMP/app"
export HOMELAB_PORT=18080
mkdir -p "$HOMELAB_CONFIG_HOME" "$HOMELAB_APP_HOME"
python3 "$ROOT/src/collector.py" >"$TMP/collector.log" 2>&1 &
PID=$!

python3 - "$HOMELAB_PORT" <<'PY'
import json
import sys
import time
import urllib.request

port = int(sys.argv[1])
url = f"http://127.0.0.1:{port}/api/snapshot"
for _ in range(50):
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            data = json.load(response)
        break
    except Exception:
        time.sleep(0.1)
else:
    raise SystemExit("collector did not start")

assert data["mode"] == "stub", data
assert data["host"] == "HOMELAB", data
first = data
time.sleep(1.2)
with urllib.request.urlopen(url, timeout=0.5) as response:
    data = json.load(response)
assert data["uptime"] > first["uptime"], (first["uptime"], data["uptime"])
expected = {"cpu", "ram", "down", "up", "read", "write"}
assert expected <= set(data["sparks"]), data["sparks"].keys()
assert "load" not in data["sparks"], data["sparks"].keys()
assert "total" not in data["sparks"], data["sparks"].keys()
print("stub snapshot OK; ticks advancing")
PY

echo "== media endpoint smoke test =="
python3 - "$HOMELAB_PORT" <<'PY'
import json
import sys
import time
import urllib.request

port = int(sys.argv[1])
url = f"http://127.0.0.1:{port}/api/media"
for _ in range(50):
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            data = json.load(response)
        break
    except Exception:
        time.sleep(0.1)
else:
    raise SystemExit("collector media endpoint did not start")

assert data["mode"] == "stub", data
assert [row["key"] for row in data["errors"]["sources"]] == [
    "prowlarr", "sonarr", "radarr", "nzbget", "qbittorrent"
], data["errors"]["sources"]
assert data["streaming"]["count"] == 3, data["streaming"]
assert data["activity"]["primary"] == "qbittorrent", data["activity"]
print("media endpoint OK")
PY

echo "ALL CHECKS PASSED"
