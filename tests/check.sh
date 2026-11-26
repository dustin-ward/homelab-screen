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
chart = re.search(r"\.chart\s*\{([^}]*)\}", html)
assert chart, "missing .chart rule"
assert "flex: 1 0 auto" in chart.group(1), "CORE charts must grow into the leftover panel height"
assert "overflow: hidden" in chart.group(1), "CORE charts must clip, not overflow"
row = re.search(r"\.chart-row\s*\{([^}]*)\}", html)
assert row, "missing .chart-row rule"
size = re.search(r"font-size:\s*(\d+)px", row.group(1))
assert size, "missing font-size in .chart-row"
assert int(size.group(1)) <= 13, "60-glyph charts need <= 13px to fit the panel"
grow = re.findall(r'class="chart[^"]*"\s+data-spark="([^"]+)"\s+data-rows="(\d+)"', html)
rows = dict(grow)
assert set(rows) == {"cpu", "down", "up"}, "CPU and both network charts must be multi-row"
assert int(rows["cpu"]) >= 4, "CPU chart should fill its panel with at least four rows"
PY

echo "== chart scale behaviour =="
python3 - "$ROOT/src/kiosk.html" "$TMP/chart.js" <<'PY'
import pathlib
import re
import sys

js = re.search(r"<script>(.*?)</script>",
               pathlib.Path(sys.argv[1]).read_text(), re.S).group(1)

def grab(name):
    start = js.index("function " + name + "(")
    depth = 0
    for i in range(start, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
    raise SystemExit("unterminated " + name)

blocks = re.search(r"^\s*const BLOCKS\s*=.*$", js, re.MULTILINE)
if not blocks:
    raise SystemExit("no BLOCKS table")
clamp = re.search(r"^\s*const clamp\s*=.*$", js, re.MULTILINE)
if not clamp:
    raise SystemExit("no clamp helper")

harness = """
const idle = chartRows(Array(60).fill(0), 4, 7);
const peak = chartRows(Array(8).fill(0).map((_, i) => i % 2 ? 7 : 0), 4, 7);
const ramp = chartRows([0, 1, 2, 3, 4, 5, 6, 7], 4, 7);
const sub = ch => ch === " " ? 0 : ch.codePointAt(0) - 0x2581 + 1;
for (const [label, grid] of [["idle", idle], ["peak", peak], ["ramp", ramp]]) {
  for (let c = 0; c < grid[0].length; c++) {
    const col = grid.map(r => sub(r[c]));
    let state = 0;
    for (const v of col) {
      if (state === 0) { if (v === 8) state = 2; else if (v > 0) state = 1; }
      else if (state === 1) { if (v !== 8) throw new Error(label + " col " + c + ": hole under a partial row"); state = 2; }
      else if (v !== 8) throw new Error(label + " col " + c + ": unfilled row under a filled one");
    }
  }
}
if (sub(idle[3][0]) < 2) throw new Error("idle series baseline is only " + sub(idle[3][0]) + " sub-cell(s) high");
if (!peak[0].includes("\\u2588")) throw new Error("a peak sample never reaches the top row");
if (idle.join("").length !== 240) throw new Error("chart grid is not 60x4");
console.log("chart scale OK");
"""

pathlib.Path(sys.argv[2]).write_text("\n".join([
    blocks.group(0).strip(), clamp.group(0).strip(),
    grab("chartRows"), harness]))
PY
if command -v node >/dev/null 2>&1; then
  node "$TMP/chart.js"
else
  echo "node not found; skipping chart scale test"
fi

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
