#!/usr/bin/env python3
"""Fixture tests for the read-only MEDIA snapshot."""
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from media import MediaService  # noqa: E402

ENV = """JELLYFIN_URL=http://jellyfin.invalid
JELLYFIN_TOKEN=fixture-token
SONARR_URL=http://sonarr.invalid
SONARR_API_KEY=fixture-key
RADARR_URL=http://radarr.invalid
RADARR_API_KEY=fixture-key
PROWLARR_URL=http://prowlarr.invalid
PROWLARR_API_KEY=fixture-key
QBITTORRENT_URL=http://qb.invalid
QBITTORRENT_USERNAME=fixture-user
QBITTORRENT_PASSWORD=fixture-pass
NZBGET_URL=http://nzb.invalid
NZBGET_USERNAME=fixture-user
NZBGET_PASSWORD=fixture-pass
"""


def service(tmp, fixture):
    env = pathlib.Path(tmp) / "media.env"
    env.write_text(ENV)
    return MediaService(
        tmp,
        env_file=env,
        fixture_file=ROOT / "tests" / "fixtures" / fixture,
    )


def main():
    with tempfile.TemporaryDirectory() as tmp:
        svc = service(tmp, "media.json")
        svc.tick()
        snap = svc.snapshot()
        blob = json.dumps(snap)

        assert snap["mode"] == "live"
        assert snap["streaming"]["count"] == 5
        assert len(snap["streaming"]["sessions"]) == 3
        assert snap["streaming"]["more"] == 2
        assert snap["streaming"]["state"] == "LIVE"
        sessions = snap["streaming"]["sessions"]
        assert all(len(row["user"]) <= 32 for row in sessions)
        assert all(len(row["title"]) <= 96 for row in sessions)
        assert [row["title"] for row in sessions] == [
            "Example Series · S02E05",
            "Example Movie",
            "Second Series · S01E02",
        ]

        assert snap["activity"]["primary"] == "nzbget"
        nzb = snap["activity"]["sources"]["nzbget"]
        assert nzb["rate_bps"] == 9500000
        assert nzb["rate_text"] == "9.5 Mb/s"
        assert nzb["item"]["pct"] == 48
        assert nzb["item"]["eta_text"] == "7m"
        assert nzb["item"]["state"] == "DL"
        assert "117G FREE" in nzb["detail"]

        rows = {row["key"]: row for row in snap["errors"]["sources"]}
        assert list(rows) == ["prowlarr", "sonarr", "radarr", "nzbget", "qbittorrent"]
        assert (rows["prowlarr"]["state"], rows["prowlarr"]["new"]) == ("ERROR", 2)
        assert (rows["sonarr"]["state"], rows["sonarr"]["new"]) == ("ERROR", 3)
        assert (rows["radarr"]["state"], rows["radarr"]["new"]) == ("OK", 0)
        assert (rows["nzbget"]["state"], rows["nzbget"]["new"]) == ("ERROR", 5)
        assert (rows["qbittorrent"]["state"], rows["qbittorrent"]["new"]) == ("WARN", 2)

        latest = snap["latest"]["items"]
        assert [item["source"] for item in latest] == ["SONARR", "SONARR", "RADARR"]
        assert latest[0]["title"] == "Example Series · S02E05 · The Long Example"

        for secret in ("fixture-token", "fixture-key", "fixture-pass"):
            assert secret not in blob
        for host in ("jellyfin.invalid", "sonarr.invalid", "radarr.invalid",
                     "prowlarr.invalid", "qb.invalid", "nzb.invalid"):
            assert host not in blob

        before = snap["errors"]["sources"][0]["new"]
        svc.tick()
        assert svc.snapshot()["errors"]["sources"][0]["new"] == before

    with tempfile.TemporaryDirectory() as tmp:
        svc = service(tmp, "media.json")
        svc.tick()
        snap = svc.snapshot()
        assert snap["mode"] == "live"

    with tempfile.TemporaryDirectory() as tmp:
        svc = service(tmp, "media-prowlarr.json")
        svc.tick()
        row = {r["key"]: r for r in svc.snapshot()["errors"]["sources"]}["prowlarr"]
        assert row["state"] == "ERROR"
        assert row["detail"] == "All indexer proxies are unavailable due to failures"

    with tempfile.TemporaryDirectory() as tmp:
        svc = service(tmp, "media-qb.json")
        svc.tick()
        snap = svc.snapshot()
        assert snap["activity"]["primary"] == "qbittorrent"
        qb = snap["activity"]["sources"]["qbittorrent"]
        assert qb["rate_bps"] == 20000000
        assert qb["item"]["pct"] == 31
        assert qb["item"]["eta_text"] == "10m"
        assert snap["activity"]["sources"]["nzbget"]["state"] == "IDLE"

    with tempfile.TemporaryDirectory() as tmp:
        env = pathlib.Path(tmp) / "media.env"
        env.write_text("")
        svc = MediaService(tmp, env_file=env,
                           fixture_file=ROOT / "tests" / "fixtures" / "media-empty.json")
        svc.tick()
        snap = svc.snapshot()
        assert snap["mode"] == "stub"
        assert all(row["state"] == "STUB" for row in snap["errors"]["sources"])
        assert snap["streaming"]["count"] == 3
        assert snap["activity"]["primary"] == "qbittorrent"

    print("media fixture checks OK")


if __name__ == "__main__":
    main()
