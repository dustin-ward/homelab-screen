#!/usr/bin/env python3
"""Loopback-only snapshot server for the homelab-screen kiosk.

Without a token file it serves the built-in 1 Hz stub. With
~/.config/homelab-screen/pve-token present it polls PVE read-only and serves
live values; the page switches over on its next fetch.
"""
import hashlib
import http.client
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Deployment-specific values come from the environment / config file.
# No hostname, address, username or credential is stored in the source tree.
def env_path(name, default):
    value = os.environ.get(name, "")
    return Path(value) if value else Path(default)


CONFIG_HOME = env_path("HOMELAB_CONFIG_HOME", Path.home() / ".config/homelab-screen")
TOKEN_FILE = env_path("HOMELAB_TOKEN_FILE", CONFIG_HOME / "pve-token")
CERT_FILE = env_path("HOMELAB_CERT_FILE", CONFIG_HOME / "pve-cert.pem")
APP_HOME = env_path("HOMELAB_APP_HOME", Path.home() / "homelab-screen")
PAGE_FILE = env_path("HOMELAB_PAGE_FILE", APP_HOME / "kiosk.html")
VERSION_FILE = env_path("HOMELAB_VERSION_FILE", APP_HOME / "VERSION")
BASE = os.environ.get("PVE_API_BASE", "").rstrip("/")
NODE = os.environ.get("PVE_NODE", "")
CERT_PIN = os.environ.get("PVE_CERT_PIN", "").lower().replace(":", "")
LABEL = os.environ.get("HOMELAB_LABEL", "") or "HOMELAB"
PORT = int(os.environ.get("HOMELAB_PORT", "8080"))
BLOCKS = "▁▂▃▄▅▆▇█"
SPARK_LEN = {"cpu": 60, "ram": 40, "down": 60, "up": 60,
             "read": 40, "write": 40}


def read_version():
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "dev"

state_lock = threading.Lock()
state = {}
sparks = {}
live = False
last_status = None
last_rrd = None
last_rrd_ts = 0.0


def clamp(n, lo, hi):
    return max(lo, min(hi, n))


def walk(v, step, lo, hi):
    return clamp(v + random.uniform(-step, step), lo, hi)


def render(levels):
    return "".join(BLOCKS[clamp(int(i), 0, 7)] for i in levels)


def shift(key, value):
    series = sparks[key]
    n = SPARK_LEN[key]
    series.append(clamp(int(value), 0, 7))
    if len(series) > n:
        del series[:len(series) - n]


def init_state():
    global state, sparks
    state = dict(cpu=31.2, load=[0.42, 0.51, 0.60], ram=2.1, ram_total=7.8,
                 swap=118, swap_total=512, arc=1.2, down=84.2, up=12.6,
                 rootfs=42, rootfs_total=128, cores=8,
                 uptime=42 * 86400 + 6 * 3600 + 12 * 60)
    seeds = {
        "cpu": [2, 3, 4, 5, 4, 6, 7, 5, 4, 3, 2, 3, 5, 6, 7, 6, 5, 4, 3, 2] * 3,
        "ram": ([2, 2, 3, 3, 4, 4, 5, 5, 4, 3, 3, 2, 3, 4, 5, 5, 4, 3, 2, 2] * 2),
        "down": [2, 3, 4, 5, 4, 6, 7, 5, 4, 3, 2, 3, 5, 6, 7, 6, 5, 4, 3, 2] * 3,
        "up": [1, 1, 2, 2, 1, 2, 3, 2, 1, 1, 2, 3, 4, 3, 2, 1, 1, 2, 2, 1] * 3,
        "read": [0, 1, 2, 4, 6, 7, 5, 3, 2, 1, 0, 1, 3, 5, 7, 6, 4, 2, 1, 0] * 2,
        "write": [1, 2, 3, 4, 3, 2, 1, 0, 1, 2, 3, 4, 5, 4, 3, 2, 1, 0, 1, 2] * 2,
    }
    sparks = {k: v[:60] for k, v in seeds.items()}


def stub_tick():
    if live:  # do not let the stub thread mutate live state
        return
    with state_lock:
        s = state
        s["cpu"] = walk(s["cpu"], 6, 12, 68)
        s["load"] = [walk(v, 0.06, 0.12, 1.4) for v in s["load"]]
        s["ram"] = walk(s["ram"], 0.08, 1.5, 3.4)
        s["swap"] = walk(s["swap"], 4, 80, 220)
        s["arc"] = walk(s["arc"], 0.02, 0.9, 1.6)
        s["down"] = walk(s["down"], 12, 30, 145)
        s["up"] = walk(s["up"], 3, 4, 28)
        s["rootfs"] = walk(s["rootfs"], 0.02, 40, 46)
        s["uptime"] += 1
        shift("cpu", s["cpu"] / 10)
        shift("ram", s["ram"] * 2)
        shift("down", s["down"] / 20)
        shift("up", s["up"] / 4)
        shift("read", random.randint(0, 7))
        shift("write", random.randint(0, 7))


def ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # identity is checked by CERT_PIN above
    return ctx


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        fp = hashlib.sha256(der).hexdigest()
        if fp != CERT_PIN:
            self.sock.close()
            raise ssl.SSLError("PVE certificate pin mismatch")


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=ssl_context())

    def https_open(self, req):
        return self.do_open(PinnedHTTPSConnection, req, context=self._context)


OPENER = urllib.request.build_opener(PinnedHTTPSHandler())


def pve_get(path):
    token = TOKEN_FILE.read_text().strip()
    req = urllib.request.Request(BASE + path, headers={"Authorization": "PVEAPIToken=" + token})
    with OPENER.open(req, timeout=10) as r:
        return json.loads(r.read().decode())["data"]


def live_tick():
    global live, last_status, last_rrd, last_rrd_ts
    if not (BASE and NODE and CERT_PIN
            and TOKEN_FILE.exists() and CERT_FILE.exists()):
        live = False
        return
    try:
        status = pve_get("/nodes/" + NODE + "/status")
        if last_status is not None:  # discard cold cpu=0 sample
            with state_lock:
                s = state
                s["cpu"] = status.get("cpu", 0) * 100
                s["load"] = [float(x) for x in status.get("loadavg", [0, 0, 0])[:3]]
                mem, swap, root = status.get("memory", {}), status.get("swap", {}), status.get("rootfs", {})
                s["ram"] = mem.get("used", 0) / 1e9
                s["ram_total"] = mem.get("total", 1) / 1e9
                s["swap"] = swap.get("used", 0) / 1e6
                s["swap_total"] = swap.get("total", 1) / 1e6
                s["rootfs"] = root.get("used", 0) / 1e9
                s["rootfs_total"] = root.get("total", 1) / 1e9
                s["uptime"] = status.get("uptime", s["uptime"])
                s["arc"] = s["arc"]  # not exposed by PVE API; keep last value
                live = True
        last_status = status
    except Exception as e:
        live = False
        print("collector: status fetch failed:", e, file=sys.stderr, flush=True)

    if time.time() - last_rrd_ts < 60:
        return
    try:
        rrd = pve_get("/nodes/" + NODE + "/rrddata?timeframe=hour&cf=AVERAGE")
        if rrd:
            last_rrd_ts = time.time()
            last_rrd = rrd
            with state_lock:
                cpu_pts = [p.get("cpu", 0) * 100 for p in rrd][-60:]
                ram_pts = [p.get("memused", 0) / 1e9 for p in rrd][-40:]
                down_pts = [p.get("netin", 0) * 8 / 1e6 for p in rrd][-60:]
                up_pts = [p.get("netout", 0) * 8 / 1e6 for p in rrd][-60:]
                # PVE host RRD has no diskread/diskwrite; use I/O wait and PSI I/O.
                read_pts = [p.get("iowait", 0) * 100 for p in rrd][-40:]
                write_pts = [p.get("pressureiofull", 0) for p in rrd][-40:]
                sparks["cpu"] = [int(clamp(v / 10, 0, 7)) for v in cpu_pts]
                sparks["ram"] = [int(clamp(v * 2, 0, 7)) for v in ram_pts]
                sparks["down"] = [int(clamp(v / 20, 0, 7)) for v in down_pts]
                sparks["up"] = [int(clamp(v / 4, 0, 7)) for v in up_pts]
                sparks["read"] = [int(clamp(v / 12, 0, 7)) for v in read_pts]
                sparks["write"] = [int(clamp(v * 3, 0, 7)) for v in write_pts]
                # instantaneous values from the latest RRD sample
                last = rrd[-1]
                if "netin" in last:
                    state["down"] = last["netin"] * 8 / 1e6
                    state["up"] = last["netout"] * 8 / 1e6
                if "arcsize" in last:
                    state["arc"] = last["arcsize"] / 1e9
                if "maxcpu" in last:
                    state["cores"] = int(last["maxcpu"])
    except Exception as e:
        print("collector: rrd fetch failed:", e, file=sys.stderr, flush=True)


def snapshot():
    with state_lock:
        return {
            "mode": "live" if live else "stub",
            "host": LABEL,
            "version": read_version(),
            "cpu": state["cpu"],
            "load": state["load"],
            "ram": state["ram"], "ram_total": state["ram_total"],
            "swap": state["swap"], "swap_total": state["swap_total"],
            "arc": state["arc"],
            "down": state["down"], "up": state["up"], "cores": state["cores"],
            "rootfs": state["rootfs"], "rootfs_total": state["rootfs_total"],
            "uptime": state["uptime"],
            "sparks": {k: render(v) for k, v in sparks.items()},
        }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/snapshot"):
            body = json.dumps(snapshot()).encode()
            ctype = "application/json"
        elif self.path in ("/", "/kiosk.html"):
            body = PAGE_FILE.read_bytes()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # no token or query data in logs


def loop(fn, delay):
    while True:
        try:
            fn()
        except Exception as exc:
            print(f"collector: {fn.__name__} failed: {exc}", file=sys.stderr)
        time.sleep(delay)


def main():
    init_state()
    threading.Thread(target=loop, args=(stub_tick, 1), daemon=True).start()
    threading.Thread(target=loop, args=(live_tick, 5), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
