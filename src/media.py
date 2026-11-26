#!/usr/bin/env python3
"""Read-only media integrations for the homelab-screen kiosk.

The adapter reads a mode-600 env file containing service URLs and
credentials, polls each service over the LAN, and produces the ``/api/media``
snapshot consumed by the MEDIA page.  Nothing in this module writes to the
media services; the only local write is the small daily issue tracker used to
show how many new problems have appeared since midnight.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import http.cookiejar
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
DEFAULT_PORTS = {
    "jellyfin": 8096,
    "sonarr": 8989,
    "radarr": 7878,
    "prowlarr": 9696,
    "qbittorrent": 8080,
    "nzbget": 6789,
}
SOURCE_ORDER = ("prowlarr", "sonarr", "radarr", "nzbget", "qbittorrent")


# ---------------------------------------------------------------------------
# small parsing / formatting helpers
# ---------------------------------------------------------------------------

def load_env_file(path):
    """Read a KEY=VALUE file without shell expansion or logging values."""
    values = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
            value = value[1:-1]
        values[key] = value
    return values


def base_url(raw, default_port):
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    try:
        port = parts.port
    except ValueError:
        return ""
    netloc = host + (":" + str(port) if port else ":" + str(default_port))
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def text(value, limit=160):
    if value is None:
        return ""
    value = re.sub(r"\s+", " ", str(value)).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def rate_text(bps):
    bps = max(0.0, _as_float(bps))
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.1f} Gb/s"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mb/s"
    if bps >= 1_000:
        return f"{bps / 1_000:.0f} Kb/s"
    if bps > 0:
        return f"{bps:.0f} B/s"
    return "0 B/s"


def eta_text(seconds):
    seconds = _as_int(seconds)
    if seconds <= 0 or seconds >= 86_400 * 9:
        return "--"
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86_400}d {seconds % 86_400 // 3600}h"


def pct_text(value):
    return clamp(int(round(value)), 0, 100)


def iso_now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def is_today(value):
    if not value:
        return True
    try:
        if isinstance(value, (int, float)):
            stamp = float(value)
        else:
            stamp = dt.datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            ).timestamp()
    except (TypeError, ValueError):
        return True
    start = dt.datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    return stamp >= start


def error_kind(exc):
    """A short, non-sensitive label for the page."""
    raw = str(exc).lower()
    if "401" in raw or "403" in raw or "auth" in raw or "login" in raw:
        return "AUTH"
    if "timed out" in raw or "timeout" in raw:
        return "TIMEOUT"
    if "refused" in raw or "unreachable" in raw or "name or service" in raw:
        return "OFFLINE"
    return "OFFLINE"


def _read_json(request, opener=None, timeout=5.0):
    opener = opener or urllib.request.build_opener()
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(MAX_BYTES)
    if len(raw) >= MAX_BYTES:
        raise ValueError("response too large")
    return json.loads(raw.decode("utf-8"))


def get_json(url, headers=None, timeout=5.0):
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", **(headers or {})}
    )
    return _read_json(request, timeout=timeout)


def post_json(url, payload, headers=None, opener=None, timeout=5.0):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 **(headers or {})},
    )
    return _read_json(request, opener=opener, timeout=timeout)


# ---------------------------------------------------------------------------
# HTTP clients
# ---------------------------------------------------------------------------

class QBittorrentClient:
    def __init__(self, base, username, password):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def login(self):
        data = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/v2/auth/login",
            data=data,
            headers={"Referer": self.base + "/", "Origin": self.base},
        )
        with self.opener.open(request, timeout=5.0) as response:
            body = response.read(256).strip()
            status = response.status
        if status not in (200, 204):
            raise RuntimeError("login rejected")
        if status == 200 and body not in (b"", b"Ok."):
            raise RuntimeError("login rejected")

    def get(self, path):
        request = urllib.request.Request(
            self.base + path,
            headers={"Referer": self.base + "/", "Accept": "application/json"},
        )
        return _read_json(request, opener=self.opener, timeout=5.0)


class NzbgetClient:
    def __init__(self, base, username, password):
        self.url = base.rstrip("/") + "/jsonrpc"
        token = base64.b64encode(
            (username + ":" + password).encode("utf-8")
        ).decode("ascii")
        self.auth = "Basic " + token

    def rpc(self, method, params=None):
        payload = {"method": method, "params": params or []}
        result = post_json(
            self.url,
            payload,
            headers={"Authorization": self.auth},
        )
        if result.get("error"):
            raise RuntimeError("JSON-RPC error")
        return result.get("result")


# ---------------------------------------------------------------------------
# service fetchers
# ---------------------------------------------------------------------------

def fetch_jellyfin(url, token, fixture):
    if fixture is not None:
        return fixture.get("Sessions") or []
    headers = {
        "Authorization": (
            'MediaBrowser Client="homelab-kiosk", Device="Raspberry Pi", '
            'DeviceId="homelab-kiosk", Version="1.0", Token="' + token + '"'
        )
    }
    data = get_json(url + "/Sessions", headers=headers)
    return data if isinstance(data, list) else []


def fetch_arr(url, key, kind, fixture):
    if fixture is not None:
        return {
            "health": fixture.get("health") or [],
            "queue": fixture.get("queue") or {},
            "wanted": fixture.get("wanted") or {},
            "history": fixture.get("history") or {},
        }
    headers = {"X-Api-Key": key}
    history_params = {
        "page": 1,
        "pageSize": 30,
        "sortKey": "date",
        "sortDirection": "descending",
    }
    if kind == "series":
        history_params["includeSeries"] = "true"
        history_params["includeEpisode"] = "true"
    else:
        history_params["includeMovie"] = "true"
    history_query = urllib.parse.urlencode(history_params)
    return {
        "health": get_json(url + "/api/v3/health", headers=headers),
        "queue": get_json(
            url + "/api/v3/queue?failed=true&pageSize=100", headers=headers
        ),
        "wanted": get_json(
            url + "/api/v3/wanted/missing?pageSize=1", headers=headers
        ),
        "history": get_json(
            url + "/api/v3/history?" + history_query, headers=headers
        ),
    }


def fetch_prowlarr(url, key, fixture):
    if fixture is not None:
        return {
            "health": fixture.get("health") or [],
            "status": fixture.get("indexerstatus") or [],
            "indexers": fixture.get("indexer") or [],
        }
    headers = {"X-Api-Key": key}
    health = get_json(url + "/api/v1/health", headers=headers)
    status = get_json(url + "/api/v1/indexerstatus", headers=headers)
    try:
        indexers = get_json(url + "/api/v1/indexer", headers=headers)
    except Exception:
        indexers = []
    return {
        "health": health if isinstance(health, list) else [],
        "status": status if isinstance(status, list) else [],
        "indexers": indexers if isinstance(indexers, list) else [],
    }


def fetch_qbittorrent(url, username, password, fixture):
    if fixture is not None:
        return {
            "transfer": fixture.get("transfer") or {},
            "torrents": fixture.get("torrents") or [],
            "errored": fixture.get("errored") or [],
        }
    client = QBittorrentClient(url, username, password)
    client.login()
    transfer = client.get("/api/v2/transfer/info")
    torrents = client.get("/api/v2/torrents/info?filter=downloading")
    errored = client.get("/api/v2/torrents/info?filter=errored")
    return {
        "transfer": transfer if isinstance(transfer, dict) else {},
        "torrents": torrents if isinstance(torrents, list) else [],
        "errored": errored if isinstance(errored, list) else [],
    }


def _nzb_pair_mb(item, prefix):
    lo = _as_int(item.get(prefix + "Lo"))
    hi = _as_int(item.get(prefix + "Hi"))
    if lo or hi:
        return ((hi & 0xFFFFFFFF) << 32 | (lo & 0xFFFFFFFF)) / 1_000_000
    return _as_float(item.get(prefix + "MB"))


def _nzb_rate(status):
    lo = _as_int(status.get("DownloadRateLo"))
    hi = _as_int(status.get("DownloadRateHi"))
    value = ((hi & 0xFFFFFFFF) << 32) | (lo & 0xFFFFFFFF)
    if value:
        return value
    return _as_int(status.get("DownloadRate"))


def fetch_nzbget(url, username, password, fixture):
    if fixture is not None:
        return {
            "status": fixture.get("status") or {},
            "groups": fixture.get("listgroups") or [],
            "systemhealth": fixture.get("systemhealth") or {},
            "log": fixture.get("log") or [],
            "health_error": False,
        }
    client = NzbgetClient(url, username, password)
    status = client.rpc("status")
    groups = client.rpc("listgroups")
    health_error = False
    systemhealth = {}
    log = []
    try:
        systemhealth = client.rpc("systemhealth") or {}
    except Exception:
        health_error = True
    try:
        log = client.rpc("log", [0, 100]) or []
    except Exception:
        health_error = True
    return {
        "status": status if isinstance(status, dict) else {},
        "groups": groups if isinstance(groups, list) else [],
        "systemhealth": systemhealth if isinstance(systemhealth, dict) else {},
        "log": log if isinstance(log, list) else [],
        "health_error": health_error,
    }


# ---------------------------------------------------------------------------
# daily issue tracker
# ---------------------------------------------------------------------------

class IssueTracker:
    """Counts distinct problem keys seen on the current local day."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.day = ""
        self.seen = {}
        self._load()

    @staticmethod
    def _today():
        return dt.datetime.now().astimezone().date().isoformat()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if raw.get("date") != self._today():
            return
        seen = raw.get("seen")
        if not isinstance(seen, dict):
            return
        self.day = raw["date"]
        self.seen = {
            str(key): {str(item) for item in values}
            for key, values in seen.items()
            if isinstance(values, list)
        }

    def _save(self):
        payload = {
            "date": self.day,
            "seen": {key: sorted(values) for key, values in self.seen.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, separators=(",", ":")), encoding="utf-8"
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError:
            pass

    def update(self, source, keys):
        with self.lock:
            today = self._today()
            changed = False
            if today != self.day:
                self.day = today
                self.seen = {}
                changed = True
            bucket = self.seen.setdefault(source, set())
            before = len(bucket)
            bucket.update(str(key) for key in keys)
            if len(bucket) != before or changed or not self.path.exists():
                self._save()
            return len(bucket)


# ---------------------------------------------------------------------------
# normalisers
# ---------------------------------------------------------------------------

def _payload(result):
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _source_row(key, name, configured, ok, state, detail):
    return {
        "key": key,
        "name": name,
        "configured": configured,
        "ok": ok,
        "state": state,
        "new": 0,
        "detail": detail,
    }


def _health_severity(item):
    if isinstance(item, dict):
        value = (
            item.get("Severity")
            or item.get("Kind")
            or item.get("Type")
            or item.get("type")
            or ""
        )
    else:
        value = item
    raw = str(value).lower()
    if "error" in raw or "critical" in raw:
        return "ERROR"
    if "warn" in raw:
        return "WARN"
    return "INFO"


def _health_text(item):
    if isinstance(item, dict):
        for key in ("Message", "Text", "Title", "Name", "message", "text"):
            value = item.get(key)
            if value:
                return text(value, 120)
        return ""
    return text(item, 120)


def _health_items(systemhealth):
    """Flatten NZBGet's Alerts/Sections/Subsections into one list."""
    items = []
    alerts = systemhealth.get("Alerts") if isinstance(systemhealth, dict) else None
    if isinstance(alerts, list):
        items.extend(alerts)

    def walk(sections):
        if not isinstance(sections, list):
            return
        for section in sections:
            if not isinstance(section, dict):
                continue
            issues = section.get("Issues")
            if isinstance(issues, list):
                items.extend(issues)
            # Options carry per-section configuration warnings.  Info options
            # are normal settings and must not fill the error panel.
            options = section.get("Options")
            if isinstance(options, list):
                for option in options:
                    if not isinstance(option, dict):
                        continue
                    severity = _health_severity(option)
                    if severity in ("WARN", "ERROR"):
                        items.append(option)
            walk(section.get("Subsections"))

    if isinstance(systemhealth, dict):
        walk(systemhealth.get("Sections"))
    return items


def _event_type(record):
    value = record.get("eventType")
    if value is None:
        return ""
    return str(value).lower().replace("_", "")


def _history_title(record, kind):
    if kind == "series":
        series = record.get("series") or {}
        episode = record.get("episode") or {}
        base = series.get("title") or record.get("seriesTitle") or ""
        season = episode.get("seasonNumber")
        number = episode.get("episodeNumber")
        code = ""
        if isinstance(season, int) and isinstance(number, int):
            code = f"S{season:02d}E{number:02d}"
        title = episode.get("title") or record.get("sourceTitle") or ""
        parts = [part for part in (base, code, title) if part]
        return text(" · ".join(parts), 100)
    movie = record.get("movie") or {}
    title = movie.get("title") or record.get("movieTitle") or record.get("sourceTitle") or ""
    year = movie.get("year")
    if year and title:
        return text(f"{title} ({year})", 100)
    return text(title, 100)


def _history_at(record):
    value = record.get("date") or record.get("downloadDate") or ""
    return str(value) if value else ""


class MediaService:
    def __init__(self, config_home, env_file=None, fixture_file=None, state_file=None):
        self.config_home = Path(config_home)
        self.env_file = Path(
            env_file or os.environ.get("MEDIA_ENV_FILE") or self.config_home / "media.env"
        )
        self.fixture_file = Path(
            fixture_file
            or os.environ.get("MEDIA_FIXTURE_FILE")
            or self.config_home / "media-fixture.json"
        )
        self.state_file = Path(
            state_file
            or os.environ.get("MEDIA_STATE_FILE")
            or self.config_home / "media-state.json"
        )
        self._lock = threading.RLock()
        self._cache = {}
        self._snapshot = self._stub_snapshot()
        self._tracker = IssueTracker(self.state_file)

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._snapshot)

    def tick(self):
        cfg = load_env_file(self.env_file)
        if not cfg or not any(value.strip() for value in cfg.values()):
            snapshot = self._stub_snapshot()
            with self._lock:
                self._snapshot = snapshot
            return
        fixture = self._load_fixture()
        results = self._fetch_all(cfg, fixture)
        snapshot = self._build_snapshot(results)
        with self._lock:
            self._snapshot = snapshot

    # -- fixture / cache -------------------------------------------------

    def _load_fixture(self):
        try:
            raw = json.loads(self.fixture_file.read_text())
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _fixture_source(self, fixture, name):
        if name not in fixture:
            return None
        value = fixture[name]
        if isinstance(value, dict) and "__error__" in value:
            raise RuntimeError(str(value["__error__"]))
        return value if isinstance(value, dict) else {}

    def _fetch_all(self, cfg, fixture):
        jobs = {
            "jellyfin": (5.0, self._fetch_jellyfin),
            "sonarr": (30.0, self._fetch_sonarr),
            "radarr": (30.0, self._fetch_radarr),
            "prowlarr": (60.0, self._fetch_prowlarr),
            "qbittorrent": (5.0, self._fetch_qbittorrent),
            "nzbget": (5.0, self._fetch_nzbget),
        }
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {
                name: pool.submit(self._cached, name, ttl, fn, cfg, fixture)
                for name, (ttl, fn) in jobs.items()
            }
            return {name: future.result() for name, future in futures.items()}

    def _cached(self, name, ttl, fn, cfg, fixture):
        now = time.time()
        with self._lock:
            previous = self._cache.get(name)
        if previous is not None and now - previous["at"] < ttl:
            return dict(previous)
        try:
            data = fn(cfg, fixture)
            entry = {"data": data, "error": None, "at": now}
        except Exception as exc:  # keep the last good payload for context
            entry = {
                "data": previous["data"] if previous else None,
                "error": error_kind(exc),
                "at": now,
            }
        with self._lock:
            self._cache[name] = entry
        return dict(entry)

    # -- fetchers --------------------------------------------------------

    def _fetch_jellyfin(self, cfg, fixture):
        url = base_url(cfg.get("JELLYFIN_URL"), DEFAULT_PORTS["jellyfin"])
        token = (cfg.get("JELLYFIN_TOKEN") or "").strip()
        if not url or not token:
            return {"configured": False}
        sessions = fetch_jellyfin(
            url, token, self._fixture_source(fixture, "jellyfin")
        )
        return {"configured": True, "sessions": sessions}

    def _fetch_sonarr(self, cfg, fixture):
        return self._fetch_arr(cfg, fixture, "sonarr", "series")

    def _fetch_radarr(self, cfg, fixture):
        return self._fetch_arr(cfg, fixture, "radarr", "movie")

    def _fetch_arr(self, cfg, fixture, key, kind):
        url = base_url(cfg.get(key.upper() + "_URL"), DEFAULT_PORTS[key])
        api_key = (cfg.get(key.upper() + "_API_KEY") or "").strip()
        if not url or not api_key:
            return {"configured": False}
        data = fetch_arr(
            url, api_key, kind, self._fixture_source(fixture, key)
        )
        return {"configured": True, **data}

    def _fetch_prowlarr(self, cfg, fixture):
        url = base_url(cfg.get("PROWLARR_URL"), DEFAULT_PORTS["prowlarr"])
        api_key = (cfg.get("PROWLARR_API_KEY") or "").strip()
        if not url or not api_key:
            return {"configured": False}
        data = fetch_prowlarr(
            url, api_key, self._fixture_source(fixture, "prowlarr")
        )
        return {"configured": True, **data}

    def _fetch_qbittorrent(self, cfg, fixture):
        url = base_url(cfg.get("QBITTORRENT_URL"), DEFAULT_PORTS["qbittorrent"])
        username = (cfg.get("QBITTORRENT_USERNAME") or "").strip()
        password = (cfg.get("QBITTORRENT_PASSWORD") or "").strip()
        if not url or not username or not password:
            return {"configured": False}
        data = fetch_qbittorrent(
            url, username, password, self._fixture_source(fixture, "qbittorrent")
        )
        return {"configured": True, **data}

    def _fetch_nzbget(self, cfg, fixture):
        url = base_url(cfg.get("NZBGET_URL"), DEFAULT_PORTS["nzbget"])
        username = (cfg.get("NZBGET_USERNAME") or "").strip()
        password = (cfg.get("NZBGET_PASSWORD") or "").strip()
        if not url or not username or not password:
            return {"configured": False}
        data = fetch_nzbget(
            url, username, password, self._fixture_source(fixture, "nzbget")
        )
        return {"configured": True, **data}

    # -- snapshot builders ----------------------------------------------

    def _build_snapshot(self, results):
        streaming = self._streaming(results["jellyfin"])
        activity = self._activity(results)
        rows = [
            self._prowlarr_row(results["prowlarr"]),
            self._arr_row(results["sonarr"], "sonarr", "SONARR"),
            self._arr_row(results["radarr"], "radarr", "RADARR"),
            self._nzbget_row(results["nzbget"]),
            self._qbittorrent_row(results["qbittorrent"]),
        ]
        for row in rows:
            issues = row.pop("_issues", [])
            if row["configured"]:
                row["new"] = self._tracker.update(row["key"], issues)
            else:
                row["new"] = 0
        latest = self._latest(results)
        configured = [row for row in rows if row["configured"]]
        if not configured:
            mode = "stub"
        elif any(not row["ok"] for row in configured):
            mode = "partial"
        else:
            mode = "live"
        return {
            "mode": mode,
            "generated_at": int(time.time()),
            "streaming": streaming,
            "activity": activity,
            "errors": {
                "ok": all(
                    row["state"] in ("OK", "SETUP", "STUB") for row in rows
                ),
                "sources": rows,
            },
            "latest": latest,
        }

    def _streaming(self, result):
        payload = _payload(result)
        if result.get("error"):
            return {
                "configured": True,
                "ok": False,
                "state": result["error"],
                "count": 0,
                "sessions": [],
                "more": 0,
            }
        if not payload.get("configured"):
            return {
                "configured": False,
                "ok": True,
                "state": "SETUP",
                "count": 0,
                "sessions": [],
                "more": 0,
            }
        sessions = payload.get("sessions") or []
        active = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            playing = session.get("NowPlayingItem")
            if not isinstance(playing, dict):
                continue
            play_state = session.get("PlayState") or {}
            if play_state.get("IsPaused"):
                continue
            active.append((session, playing, play_state))
        rows = []
        for session, playing, play_state in active[:3]:
            method = str(play_state.get("PlayMethod") or "")
            if method == "DirectPlay":
                method = "DIRECT"
            elif method == "DirectStream":
                method = "DIRECT"
            elif method == "Transcode":
                method = "TRANSCODE"
            else:
                method = "PLAY"
            transcoding = session.get("TranscodingInfo") or {}
            rate = _as_int(transcoding.get("Bitrate")) or _as_int(
                playing.get("Bitrate")
            )
            code = ""
            season = playing.get("ParentIndexNumber")
            episode = playing.get("IndexNumber")
            if isinstance(season, int) and isinstance(episode, int):
                code = f"S{season:02d}E{episode:02d}"
            name = playing.get("Name") or playing.get("SeriesName") or "Untitled"
            series = playing.get("SeriesName") or ""
            if series:
                title = " · ".join(part for part in (series, code) if part)
            else:
                title = name
            title = text(title, 90)
            client = " · ".join(
                part
                for part in (
                    text(session.get("Client"), 24),
                    text(session.get("DeviceName"), 24),
                )
                if part
            )
            rows.append(
                {
                    "user": text(session.get("UserName") or "UNKNOWN", 28),
                    "method": method,
                    "rate_bps": rate,
                    "rate_text": rate_text(rate),
                    "title": title,
                    "series": text(series, 60),
                    "client": text(client or "PLAYER", 44),
                }
            )
        return {
            "configured": True,
            "ok": True,
            "state": "LIVE" if active else "IDLE",
            "count": len(active),
            "sessions": rows,
            "more": max(0, len(active) - len(rows)),
        }

    def _qb_source(self, result):
        payload = _payload(result)
        if result.get("error"):
            return {
                "configured": True,
                "ok": False,
                "state": result["error"],
                "rate_bps": 0,
                "rate_text": "0 B/s",
                "count": 0,
                "item": None,
                "detail": result["error"],
            }
        if not payload.get("configured"):
            return {
                "configured": False,
                "ok": True,
                "state": "SETUP",
                "rate_bps": 0,
                "rate_text": "0 B/s",
                "count": 0,
                "item": None,
                "detail": "NOT CONFIGURED",
            }
        data = payload
        transfer = data.get("transfer") or {}
        torrents = data.get("torrents") or []
        rate = _as_int(transfer.get("dl_info_speed"))
        connection = text(transfer.get("connection_status") or "unknown", 18).upper()
        top = None
        for torrent in torrents:
            if not isinstance(torrent, dict):
                continue
            if top is None or _as_float(torrent.get("dlspeed")) > _as_float(
                top.get("dlspeed")
            ):
                top = torrent
        item = None
        if top:
            progress = _as_float(top.get("progress")) * 100
            state = str(top.get("state") or "").lower()
            state_map = {
                "download": "DL",
                "downloading": "DL",
                "forceddl": "DL",
                "metadl": "META",
                "stalleddl": "STALLED",
                "queueddl": "QUEUED",
                "checkingdl": "CHECK",
                "pauseddl": "PAUSED",
            }
            item = {
                "name": text(top.get("name") or "Untitled", 90),
                "pct": pct_text(progress),
                "eta_s": _as_int(top.get("eta")),
                "eta_text": eta_text(top.get("eta")),
                "state": state_map.get(state, state.upper() or "DL"),
            }
        state = "DL" if rate > 0 else ("QUEUED" if torrents else "IDLE")
        detail = f"{len(torrents)} ACTIVE · {connection}"
        return {
            "configured": True,
            "ok": True,
            "state": state,
            "rate_bps": rate,
            "rate_text": rate_text(rate),
            "count": len(torrents),
            "item": item,
            "detail": detail,
        }

    def _nzb_source(self, result):
        payload = _payload(result)
        if result.get("error"):
            return {
                "configured": True,
                "ok": False,
                "state": result["error"],
                "rate_bps": 0,
                "rate_text": "0 B/s",
                "count": 0,
                "item": None,
                "detail": result["error"],
            }
        if not payload.get("configured"):
            return {
                "configured": False,
                "ok": True,
                "state": "SETUP",
                "rate_bps": 0,
                "rate_text": "0 B/s",
                "count": 0,
                "item": None,
                "detail": "NOT CONFIGURED",
            }
        data = payload
        status = data.get("status") or {}
        groups = [g for g in (data.get("groups") or []) if isinstance(g, dict)]
        rate = _nzb_rate(status)
        post_count = _as_int(status.get("PostJobCount"))
        paused = bool(status.get("DownloadPaused"))
        top = None
        for group in groups:
            if str(group.get("Status") or "").upper() in ("DOWNLOADING", "PAUSED"):
                top = group
                break
        if top is None and groups:
            top = groups[0]
        item = None
        if top:
            size = _nzb_pair_mb(top, "FileSize")
            remaining = _nzb_pair_mb(top, "RemainingSize")
            progress = 0 if size <= 0 else (size - remaining) / size * 100
            group_state = str(top.get("Status") or "").upper()
            state_map = {
                "DOWNLOADING": "DL",
                "PAUSED": "PAUSED",
                "QUEUED": "QUEUED",
                "VERIFYING": "VERIFY",
                "REPAIRING": "REPAIR",
                "UNPACKING": "UNPACK",
                "MOVING": "MOVE",
                "POSTPROCESSING": "PP",
            }
            item = {
                "name": text(top.get("NZBName") or top.get("Name") or "Untitled", 90),
                "pct": pct_text(progress),
                "eta_s": int(remaining * 1_000_000 / rate) if rate > 0 and remaining > 0 else 0,
                "eta_text": eta_text(
                    int(remaining * 1_000_000 / rate) if rate > 0 and remaining > 0 else 0
                ),
                "state": state_map.get(group_state, group_state or "DL"),
            }
        if rate > 0 and groups:
            state = "DL"
        elif post_count > 0:
            state = "PP"
        elif paused:
            state = "PAUSED"
        elif groups:
            state = "QUEUED"
        else:
            state = "IDLE"
        free = _as_float(status.get("FreeDiskSpaceMB"))
        detail_parts = [f"{len(groups)} ACTIVE"]
        if post_count:
            detail_parts.append(f"{post_count} PP")
        if free > 0:
            detail_parts.append(f"{free / 1024:.0f}G FREE")
        if paused:
            detail_parts.append("PAUSED")
        return {
            "configured": True,
            "ok": True,
            "state": state,
            "rate_bps": rate,
            "rate_text": rate_text(rate),
            "count": len(groups),
            "post_count": post_count,
            "item": item,
            "detail": " · ".join(detail_parts),
        }

    def _activity(self, results):
        qb = self._qb_source(results["qbittorrent"])
        nzb = self._nzb_source(results["nzbget"])
        candidates = []
        for key, source in (("qbittorrent", qb), ("nzbget", nzb)):
            if not source.get("ok"):
                continue
            active = (
                source["rate_bps"] > 0
                or source["count"] > 0
                or source.get("post_count", 0) > 0
                or source["state"] == "PP"
            )
            if active:
                candidates.append(
                    (
                        source["rate_bps"],
                        1 if source["state"] == "PP" else 0,
                        source["count"],
                        key,
                    )
                )
        primary = max(candidates)[3] if candidates else "idle"
        return {
            "primary": primary,
            "sources": {"qbittorrent": qb, "nzbget": nzb},
        }

    def _prowlarr_row(self, result):
        payload = _payload(result)
        if result.get("error"):
            return _source_row(
                "prowlarr", "PROWLARR", True, False, result["error"],
                result["error"] + " · CHECK SERVICE",
            )
        if not payload.get("configured"):
            return _source_row(
                "prowlarr", "PROWLARR", False, True, "SETUP", "NO API KEY OR URL"
            )
        data = payload
        health = data.get("health") or []
        status = data.get("status") or []
        indexers = data.get("indexers") or []
        names = {
            str(item.get("id")): text(item.get("name"), 28)
            for item in indexers
            if isinstance(item, dict)
        }
        issues = []
        warnings = 0
        errors = 0
        first_error = ""
        first_warning = ""
        for item in health:
            if not isinstance(item, dict):
                continue
            severity = _health_severity(item)
            message = _health_text(item)
            if severity == "ERROR":
                errors += 1
                if message and not first_error:
                    first_error = message
            elif severity == "WARN":
                warnings += 1
                if message and not first_warning:
                    first_warning = message
            else:
                continue
            issues.append(f"health:{severity}:{message}")
        failing = []
        for item in status:
            if not isinstance(item, dict):
                continue
            disabled = str(item.get("disabledTill") or "")
            if (
                item.get("mostRecentFailure")
                or item.get("initialFailure")
                or (disabled and not disabled.startswith("0001"))
            ):
                indexer_id = str(item.get("indexerId") or "")
                name = names.get(indexer_id, "INDEXER " + indexer_id)
                failing.append(name)
                issues.append(f"indexer:{indexer_id}")
        if errors or failing:
            state = "ERROR"
        elif warnings:
            state = "WARN"
        else:
            state = "OK"
        if failing:
            detail = "FAILING · " + text(", ".join(failing), 80)
        elif first_error:
            detail = text(first_error, 90)
        elif first_warning:
            detail = text(first_warning, 90)
        elif warnings or errors:
            detail = f"{warnings} WARNING · {errors} ERROR"
        else:
            detail = "NO INDEXER ERRORS"
        row = _source_row("prowlarr", "PROWLARR", True, True, state, detail)
        row["_issues"] = issues
        return row

    def _arr_row(self, result, key, name):
        payload = _payload(result)
        if result.get("error"):
            return _source_row(
                key, name, True, False, result["error"],
                result["error"] + " · CHECK SERVICE",
            )
        if not payload.get("configured"):
            return _source_row(
                key, name, False, True, "SETUP", "NO API KEY OR URL"
            )
        data = payload
        health = data.get("health") or []
        queue = data.get("queue") or {}
        wanted = data.get("wanted") or {}
        issues = []
        warnings = 0
        errors = 0
        first = ""
        for item in health:
            if not isinstance(item, dict):
                continue
            severity = _health_severity(item)
            message = _health_text(item)
            if severity == "ERROR":
                errors += 1
            elif severity == "WARN":
                warnings += 1
            else:
                continue
            issues.append(f"health:{severity}:{message}")
            if message and not first:
                first = message
        records = queue.get("records") or []
        failed = _as_int(queue.get("totalRecords")) or len(records)
        for item in records:
            if isinstance(item, dict):
                issues.append(f"failed:{item.get('id') or item.get('title')}")
        missing = _as_int(wanted.get("totalRecords"))
        if errors or failed:
            state = "ERROR"
        elif warnings:
            state = "WARN"
        else:
            state = "OK"
        detail = f"MISSING {missing}"
        if failed:
            detail += f" · FAILED {failed}"
        if first:
            detail += " · " + first
        row = _source_row(key, name, True, True, state, text(detail, 120))
        row["_issues"] = issues
        return row

    def _nzbget_row(self, result):
        payload = _payload(result)
        if result.get("error"):
            return _source_row(
                "nzbget", "NZBGET", True, False, result["error"],
                result["error"] + " · CHECK SERVICE",
            )
        if not payload.get("configured"):
            return _source_row(
                "nzbget", "NZBGET", False, True, "SETUP", "NO USER OR PASSWORD"
            )
        data = payload
        health = _health_items(data.get("systemhealth") or {})
        log = data.get("log") or []
        issues = []
        warnings = 0
        errors = 0
        first = ""
        for item in health:
            severity = _health_severity(item)
            message = _health_text(item)
            if severity == "ERROR":
                errors += 1
            elif severity == "WARN":
                warnings += 1
            else:
                continue
            issues.append(f"health:{severity}:{message}")
            if message and not first:
                first = message
        for entry in log:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("Kind") or "").upper()
            if kind not in ("WARNING", "ERROR"):
                continue
            if not is_today(entry.get("Time")):
                continue
            issues.append(f"log:{entry.get('ID')}")
            if kind == "ERROR":
                errors += 1
            else:
                warnings += 1
            message = text(entry.get("Text"), 120)
            if message and not first:
                first = message
        if errors:
            state = "ERROR"
        elif warnings or data.get("health_error"):
            state = "WARN"
        else:
            state = "OK"
        detail = f"{warnings} WARN · {errors} ERR"
        if data.get("health_error"):
            detail += " · HEALTH API UNAVAILABLE"
        elif first:
            detail += " · " + first
        row = _source_row("nzbget", "NZBGET", True, True, state, text(detail, 120))
        row["_issues"] = issues
        return row

    def _qbittorrent_row(self, result):
        payload = _payload(result)
        if result.get("error"):
            return _source_row(
                "qbittorrent", "QBITTORRENT", True, False, result["error"],
                result["error"] + " · CHECK SERVICE",
            )
        if not payload.get("configured"):
            return _source_row(
                "qbittorrent", "QBITTORRENT", False, True, "SETUP",
                "NO USER OR PASSWORD",
            )
        data = payload
        transfer = data.get("transfer") or {}
        errored = [item for item in (data.get("errored") or []) if isinstance(item, dict)]
        connection = text(transfer.get("connection_status") or "unknown", 18).lower()
        issues = []
        if connection and connection != "connected":
            issues.append(f"connection:{connection}")
        for torrent in errored:
            issues.append(f"torrent:{torrent.get('hash') or torrent.get('name')}")
        state = "WARN" if errored or connection != "connected" else "OK"
        detail = connection.upper()
        if errored:
            detail += f" · {len(errored)} ERRORED"
        row = _source_row(
            "qbittorrent", "QBITTORRENT", True, True, state, text(detail, 80)
        )
        row["_issues"] = issues
        return row

    def _latest(self, results):
        items = []
        ok = True
        for key, name, kind in (
            ("sonarr", "SONARR", "series"),
            ("radarr", "RADARR", "movie"),
        ):
            result = results[key]
            payload = _payload(result)
            if result.get("error"):
                ok = False
                continue
            if not payload.get("configured"):
                continue
            records = (payload.get("history") or {}).get("records") or []
            for record in records:
                if not isinstance(record, dict):
                    continue
                if _event_type(record) != "downloadfolderimported":
                    continue
                items.append(
                    {
                        "source": name,
                        "title": _history_title(record, kind),
                        "at": _history_at(record),
                    }
                )
        items.sort(key=lambda item: item.get("at") or "", reverse=True)
        return {"ok": ok, "items": items[:3]}

    # -- stub ------------------------------------------------------------

    def _stub_snapshot(self):
        now = time.time()
        rate = int(18_400_000 + 2_100_000 * math.sin(now / 9))
        pct = pct_text(42 + 3 * math.sin(now / 7))
        sessions = [
            {
                "user": "VIEWER ONE",
                "method": "DIRECT",
                "rate_bps": 8_200_000,
                "rate_text": "8.2 Mb/s",
                "title": "Example Series · S02E05",
                "series": "Example Series",
                "client": "Web · Chrome",
            },
            {
                "user": "VIEWER TWO",
                "method": "TRANSCODE",
                "rate_bps": 3_100_000,
                "rate_text": "3.1 Mb/s",
                "title": "Example Movie",
                "series": "",
                "client": "Phone · Jellyfin",
            },
            {
                "user": "VIEWER THREE",
                "method": "DIRECT",
                "rate_bps": 1_700_000,
                "rate_text": "1.7 Mb/s",
                "title": "Second Series · S01E02",
                "series": "Second Series",
                "client": "TV · Android",
            },
        ]
        sources = []
        for key, name in (
            ("prowlarr", "PROWLARR"),
            ("sonarr", "SONARR"),
            ("radarr", "RADARR"),
            ("nzbget", "NZBGET"),
            ("qbittorrent", "QBITTORRENT"),
        ):
            sources.append(
                _source_row(
                    key, name, False, True, "STUB", "AWAITING MEDIA CREDENTIALS"
                )
            )
        stamps = [
            (dt.datetime.now().astimezone() - dt.timedelta(minutes=42)).isoformat(
                timespec="seconds"
            ),
            (dt.datetime.now().astimezone() - dt.timedelta(hours=3)).isoformat(
                timespec="seconds"
            ),
            (dt.datetime.now().astimezone() - dt.timedelta(hours=8)).isoformat(
                timespec="seconds"
            ),
        ]
        return {
            "mode": "stub",
            "generated_at": int(now),
            "streaming": {
                "configured": False,
                "ok": True,
                "state": "STUB",
                "count": 3,
                "sessions": sessions,
                "more": 0,
            },
            "activity": {
                "primary": "qbittorrent",
                "sources": {
                    "qbittorrent": {
                        "configured": False,
                        "ok": True,
                        "state": "DL",
                        "rate_bps": rate,
                        "rate_text": rate_text(rate),
                        "count": 4,
                        "item": {
                            "name": "SAMPLE.RELEASE.1080P",
                            "pct": pct,
                            "eta_s": 720,
                            "eta_text": "12m",
                            "state": "DL",
                        },
                        "detail": "4 ACTIVE · FIREWALLED",
                    },
                    "nzbget": {
                        "configured": False,
                        "ok": True,
                        "state": "IDLE",
                        "rate_bps": 0,
                        "rate_text": "0 B/s",
                        "count": 0,
                        "item": None,
                        "detail": "0 ACTIVE · 0 PP",
                    },
                },
            },
            "errors": {"ok": True, "sources": sources},
            "latest": {
                "ok": True,
                "items": [
                    {
                        "source": "SONARR",
                        "title": "Example Series · S02E05 · The Long Example",
                        "at": stamps[0],
                    },
                    {
                        "source": "RADARR",
                        "title": "Example Movie (2024)",
                        "at": stamps[1],
                    },
                    {
                        "source": "SONARR",
                        "title": "Second Series · S01E02 · Another Example",
                        "at": stamps[2],
                    },
                ],
            },
        }


def main():
    """Tiny local debug entry point: print one snapshot and exit."""
    home = Path(os.environ.get("HOMELAB_CONFIG_HOME", Path.home() / ".config/homelab-screen"))
    service = MediaService(home)
    service.tick()
    print(json.dumps(service.snapshot(), indent=2))


if __name__ == "__main__":
    main()
