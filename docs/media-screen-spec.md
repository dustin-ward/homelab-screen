# Spec: Jellyfin / arr media screen

Status: draft for review. Read-only probes have confirmed the services and response shapes; the integratee LXCs (201 for the arr stack and downloaders, 202 for Jellyfin) were left untouched.

## Problem statement

The kiosk currently shows only Proxmox host telemetry and a screensaver. It says nothing about the media stack: who is watching, what is being fetched, or whether the automation queue is healthy. Those questions currently require opening several web UIs.

## Solution

Add one new rotation page, `MEDIA`, to the existing 1280×400 kiosk. It reads the Jellyfin, Sonarr, Radarr, qBittorrent and NZBGet APIs through the existing loopback collector on the Pi. No agent or change is required on LXC 201/202. Data stays read-only; credentials stay in mode-600 files outside the repo. Like CORE, the page ships with a stub and switches to live data automatically when credentials appear.

## Confirmed service inventory (read-only probe)

| Host | Service | Port | Unauthenticated probe result |
| --- | --- | --- | --- |
| LXC 202 | Jellyfin | 8096 | Version 12.2.0. Legacy headers and `api_key` return 401; the `Authorization: MediaBrowser Token=<key>, Client=..., Device=..., DeviceId=..., Version=...` header works. |
| LXC 201 | Sonarr | 8989 | Version 4.0.20.3014; API v3 confirmed with a key; `/ping` works unauthenticated. |
| LXC 201 | Radarr | 7878 | Version 6.4.4.10685; API v3 confirmed with a key; `/ping` works unauthenticated. |
| LXC 201 | Lidarr | 8686 | `/ping` works; API requires a key (not in the first iteration). |
| LXC 201 | Prowlarr | 5055 | Root redirects to login; `/ping` reachable. `/api/v1/health` and `/api/v1/indexerstatus` are available but need an API key to verify live. |
| LXC 201 | Bazarr | 6767 | Root reachable; `/api/system/status` returns 401 (not in the first iteration). |
| LXC 201 | qBittorrent | 8080 | Version v5.3.0. WebUI API v2 works with a session; login returns 204 with an empty body. |
| LXC 201 | NZBGet | 6789 | Version 26.3. JSON-RPC works with a restricted user over Basic auth. |

The `/ping` endpoints give a cheap liveness signal for the arr services; the richer queue/history data needs keys. Lidarr and Bazarr are present but are not in the first iteration. Prowlarr moves into the first iteration because the user explicitly asked for indexer errors.

### What the credentialed probe confirmed

- Jellyfin `/Sessions` returns 200 with the modern header. Session keys include `UserName`, `Client`, `DeviceName`, `IsActive`, `PlayState` and `NowPlayingQueue`. Only one idle session existed at probe time (`PlayState` keys: `CanSeek`, `IsPaused`, `IsMuted`, `RepeatMode`, `PlaybackOrder`; no `NowPlayingItem`), so the active-stream item shape is still doc-based.
- Sonarr and Radarr `/api/v3/system/status`, `/api/v3/queue` and `/api/v3/history` all return 200. Both paged responses use `{page, pageSize, sortKey, sortDirection, totalRecords, records}`. History record keys include `sourceTitle`, `date`, `eventType`, `quality`, `seriesId` / `movieId`, and a `data` object with `downloadClientName`, `releaseGroup`, `size` and import paths. Observed event types: `downloadFolderImported`, `grabbed`, `episodeFileDeleted` / `movieFileDeleted`.
- qBittorrent v5 login succeeds with HTTP 204 and an empty body, not the older `200 Ok.` — adapters must accept both. `/api/v2/transfer/info` keys include `dl_info_speed`, `dl_info_data`, `up_info_speed`, `dl_rate_limit`, `connection_status`. No torrents existed at probe time, so per-torrent field shapes remain doc-based.
- NZBGet 26.3 JSON-RPC `status` has 55 keys; useful ones are `DownloadRateLo`/`DownloadRateHi` (64-bit rate in bytes/s; the scalar `DownloadRate` is deprecated), `AverageDownloadRateLo`/`AverageDownloadRateHi`, `RemainingSizeMB`, `DaySizeMB`, `MonthSizeMB`, `FreeDiskSpaceMB`, `TotalDiskSpaceMB`, `QuotaReached`, `DownloadPaused`, `ServerStandBy`, `PostPaused`. There is no `Health` field in this version. `listgroups` and `history` both returned empty lists at probe time.
- Error sources confirmed with the existing credentials: Sonarr `/api/v3/health` and Radarr `/api/v3/health` return 200 with `warning` entries; `/api/v3/wanted/missing?pageSize=1` returns `totalRecords` (311 Sonarr, 9 Radarr at probe time); `/api/v3/queue?failed=true` returns 200. NZBGet `systemhealth` returns `Alerts` and `Sections` (no alerts at probe time) and `log` returns `ID`, `Kind`, `Time`, `Text` with `INFO`/`WARNING`/`ERROR` kinds. qBittorrent `torrents/info?filter=errored` returns 200 (empty at probe time) and `transfer/info.connection_status` was `firewalled`.
- Prowlarr error sources are documented but not yet verified live: `GET /api/v1/health` returns `HealthResource` objects with `type` in `ok|notice|warning|error` plus `source` and `message`; `GET /api/v1/indexerstatus` returns per-indexer `disabledTill`, `mostRecentFailure` and `initialFailure`. Both need `X-Api-Key`; add `PROWLARR_URL` and `PROWLARR_API_KEY` to the probe template.

## Suggested screen (1280×400, one page)

The user chose option 2 but wants it on one screen, so the two-page stream/download rotation is compressed into three columns plus a thin bottom strip. Order in rotation stays `CORE → MEDIA → SCREENSAVER`, 15 s each, with the static pager fallback at `1/3 CORE`.

| Region | Content |
| --- | --- |
| `STREAMING` (left) | `N STREAMING`; up to three active Jellyfin sessions as `USER · DIRECT/TRANSCODE · bitrate` then `SERIES · SxxExx` (or the movie title), then client. `+N MORE` when there are more. |
| `ACTIVITY` (middle) | One adaptive panel: compare qBittorrent `dl_info_speed` with the NZBGet 64-bit `DownloadRate`; the higher one is primary and shows rate, top item, progress bar and ETA/status (`PP` for post-processing). The lower one is a dim secondary line with its own rate and queue/disk summary. If both are zero, show `IDLE`. |
| `ERRORS / STATUS` (right) | One row per source — Prowlarr, Sonarr, Radarr, NZBGet, qBittorrent — with a current-state badge and a `NEW n` counter. Prowlarr: health warnings/errors and failing indexers. Sonarr/Radarr: `wanted/missing` total, failed queue items and `health` warnings. NZBGet: `systemhealth` alerts plus new `WARNING`/`ERROR` log entries seen since the last tick. qBittorrent: errored torrent count and non-nominal `connection_status`. A `new since 00:00` line makes the accumulation explicit. |
| `LATEST` (bottom strip) | Three most recent Sonarr/Radarr import events: `S/M · TITLE · relative time`. Falls back to downloader completion history when arr data is unavailable. |

Titles are truncated by the collector to a fixed character budget so 1 Hz updates never shift the layout. No source caveats, API names or cross-page eyebrows appear on the screen; those belong in notes. Existing UI rules apply: black background, thin grey borders, dark-red panel titles, green nominal dot, 12-hour clock, 64×44px nav targets, hidden pointer, one shared background canvas.

## User stories

1. As a household member, I want to see who is currently streaming from Jellyfin, so that I know whether the server is in use.
2. As a household member, I want to see the title being streamed, so that I know what is playing without opening the Jellyfin UI.
3. As a household member, I want to see whether a stream is direct play or transcoding, so that I can tell when the server is doing heavy work.
4. As a household member, I want to see stream bitrate and client, so that I can spot a remote or high-bitrate stream.
5. As a household member, I want idle Jellyfin sessions filtered out, so that the panel shows only active playback.
6. As a household member, I want a `+N MORE` summary when more than two people are streaming, so that the panel stays fixed-size.
7. As a household member, I want to see what qBittorrent is downloading, so that I know what is consuming bandwidth.
8. As a household member, I want to see what NZBGet is downloading, so that Usenet activity is visible next to torrent activity.
9. As a household member, I want download progress and ETA, so that I can tell when a fetch will finish.
10. As a household member, I want NZBGet post-processing states shown as `PP`, so that a completed download still being repaired or unpacked is not mistaken for idle.
11. As a household member, I want aggregate download speed, so that I can correlate network spikes on CORE with downloader activity.
12. As a household member, I want the three most recent completed imports, so that I know what landed in the library.
13. As a household member, I want imports attributed to Sonarr or Radarr, so that I can tell a series from a film at a glance.
14. As a household member, I want relative times (`2h ago`) rather than absolute timestamps, so that the list is readable at a distance.
15. As a household member, I want a fallback to downloader history when arr history is unavailable, so that LATEST is not blank during an arr outage.
16. As a household member, I want queued, downloading, failed and stalled arr queue counts, so that a stuck automation job is obvious.
17. As a household member, I want NZBGet health and qBittorrent error counts, so that bad Usenet articles or errored torrents are visible.
18. As a household member, I want free space on the download destination, so that a full disk is caught before downloads fail.
19. As a household member, I want a per-page live/partial/stub marker, so that I can tell whether MEDIA is showing real data without affecting the CORE status indicator.
20. As an operator, I want the page to render a full stub before any credentials exist, so that layout and rotation can be validated without touching the media hosts.
21. As an operator, I want credential files read on every collector tick, so that adding a token needs no service restart.
22. As an operator, I want each upstream source to fail independently, so that one unavailable service does not blank the whole page.
23. As an operator, I want upstream polling cached at different intervals, so that the Pi does not hammer five APIs every second.
24. As an operator, I want all configuration supplied by environment values and secret files, so that no hostname, address, username or credential is stored in git.
25. As an operator, I want read-only identities for every API, so that a leaked kiosk credential cannot change the media stack.
26. As an operator, I want certificate pinning or a CA file for HTTPS endpoints, so that credentials are not sent to an unverified peer.
27. As an operator, I want the existing install, status and rollback flow unchanged, so that deployment risk stays low.
28. As an operator, I want a fixture-backed smoke test of the new API contract, so that parsing changes are caught without live credentials.
29. As an operator, I want a native-resolution screenshot in the docs after privacy review, so that clipping and overflow are verified on the real layout.
30. As a viewer, I want no per-source caveat text on the screen, so that the panel stays legible; details belong in these notes.

## Implementation decisions

- **One page, not two.** Add a single `MEDIA` page to the rotation. It reuses the existing 2×2 grid, shared background canvas, panel styling, header and footer. The dormant CPU/MEM/NET/DISK emphasis rules stay dormant.
- **No new host-side component.** The collector on the Pi calls the five APIs directly over the LAN. LXC 201/202 need no new service, script or agent. If LAN reachability from the Pi turns out to be blocked, revisit with a bridge on 201 rather than exposing an API off-LAN.
- **New loopback route.** The collector serves `/api/media` alongside `/api/snapshot`. The page polls it on the existing 1 s fetch cadence; the collector caches upstream results separately (5 s for sessions and active downloads, 60 s for history, queue and free space). Keeping media status out of `/api/snapshot` avoids changing the CORE top-bar semantics.
- **Proposed `/api/media` contract** (decision-rich shape, to be treated as the stable seam):

  ```json
  {
    "mode": "live|partial|stub",
    "generated_at": 0,
    "jellyfin": {"ok": true, "sessions": [{"user": "", "title": "", "subtitle": "", "method": "", "rate": "", "client": ""}]},
    "downloading": {"ok": true, "total_down": 0, "sources": [{"name": "", "count": 0, "rate": "", "items": [{"name": "", "pct": 0, "eta": "", "status": ""}]}]},
    "latest": {"ok": true, "items": [{"source": "", "title": "", "at": 0}]},
    "health": {"ok": true, "queue": {"downloading": 0, "queued": 0, "failed": 0, "stalled": 0}, "disk_free_pct": 0, "downloader_health": ""}
  }
  ```

  `ok` is per source. A missing source yields an empty list and `ok: false`; the page renders a dim dash rather than an error stack. `mode` is `live` only when all configured sources answer, `partial` when some do, `stub` when none are configured.
- **Upstream calls** (versions and paths confirmed by the credentialed probe):
  - Jellyfin: `GET /Sessions` with `Authorization: MediaBrowser Token=<key>, Client=homelab-kiosk, Device=pi, DeviceId=homelab-kiosk, Version=1`. Version 12.2.0; legacy headers and `api_key` are removed. Filter to sessions with `NowPlayingItem` and `PlayState.IsPaused == false`; map `UserName`, `Client`, `DeviceName`, `NowPlayingItem.Name`, series/season/episode, `PlayState.PlayMethod`, bitrate. The probe saw one idle session only, so build the active-item fixture from the documented shape.
  - qBittorrent v5.3.0: `POST /api/v2/auth/login` for a session cookie — accept both `204` (observed, empty body) and `200 Ok.` — then `GET /api/v2/torrents/info?filter=downloading` and `GET /api/v2/transfer/info`. Confirmed transfer keys: `dl_info_speed`, `dl_info_data`, `up_info_speed`, `dl_rate_limit`, `connection_status`. Torrent keys from docs: `name`, `progress`, `eta`, `state`, `dlspeed`, `completion_on`. No torrents existed at probe time.
  - NZBGet 26.3: JSON-RPC `POST /jsonrpc` with `status`, `listgroups`, `history`, `systemhealth` and `log`, restricted user via Basic auth. Combine `DownloadRateLo`/`DownloadRateHi` and `AverageDownloadRateLo`/`AverageDownloadRateHi` into 64-bit bytes/s; the scalar fields are deprecated. Useful `status` fields: `RemainingSizeMB`, `DaySizeMB`, `MonthSizeMB`, `FreeDiskSpaceMB`, `TotalDiskSpaceMB`, `QuotaReached`, `DownloadPaused`, `ServerStandBy`, `PostPaused`. There is no `Health` field. `systemhealth` returns `Alerts`/`Sections` with severity; `log` returns `ID`, `Kind`, `Time`, `Text` — poll by `IDFrom` and count new `WARNING`/`ERROR` entries. `listgroups` and `history` were empty, so item keys come from docs (`NZBName`, `Status`, `HistoryTime`, `Name`).
  - Sonarr 4.0.20.3014 and Radarr 6.4.4.10685: API v3 confirmed; `GET /api/v3/queue`, `GET /api/v3/history`, `GET /api/v3/health` and `GET /api/v3/wanted/missing?pageSize=1` with `X-Api-Key`. Both paged responses use `{page, pageSize, sortKey, sortDirection, totalRecords, records}`. Filter history to `downloadFolderImported` (optionally `grabbed`) and keep the newest three; map `sourceTitle`, `date`, `seriesId`/`movieId`, `data.downloadClientName`. Use `wanted/missing.totalRecords` for the "media not found" count, `queue?failed=true` for failed items, and `health[].type/message` for warnings. Prefer read-only keys.
  - Prowlarr (live verification pending a key): `GET /api/v1/health` with `X-Api-Key`; map `type` (`ok|notice|warning|error`), `source` and `message`. `GET /api/v1/indexerstatus` supplies per-indexer `disabledTill`, `mostRecentFailure` and `initialFailure`; count indexers with a recent failure or disabled-until in the future.
  - qBittorrent errors: `GET /api/v2/torrents/info?filter=errored` for the errored-torrent count and `connection_status` from `transfer/info` for a non-nominal connection state (`firewalled` at probe time).
- **Configuration additions** to the environment template: base URL for each of the six services (Jellyfin, Sonarr, Radarr, Prowlarr, qBittorrent, NZBGet), plus a path to each secret file (Jellyfin API key, Sonarr key, Radarr key, Prowlarr key, qBittorrent credential file, NZBGet credential file). All values empty in git; real files under the service user's config home, mode 600. Empty values are treated as unset, never as literal empty strings. Existing host label, port and PVE settings are unchanged.
- **Credential handling**: qBittorrent WebUI has no granular read-only role, so use a dedicated account and keep the API on the trusted LAN. NZBGet uses its restricted user, not the control user. No token, password or URL query secret is logged or rendered. HTTPS endpoints use a pinned leaf or CA file; no blanket certificate-verification bypass.
- **Truncation and stability**: collector truncates names/titles to fixed widths; renderer uses textContent, not innerHTML, for upstream strings. Fixed-length bars/sparklines and the existing `.spark { overflow: hidden }` safety clip remain in force.
- **Failure isolation**: each upstream adapter is wrapped so one timeout cannot stop the others or the PVE tick loops. A single slow source uses its own timeout shorter than its cache interval.
- **Backlog ideas** (not in the first iteration): recently-added library strip, total series/movie counts, day/month download volume and quota, per-stream transcode detail, and a weekly activity sparkline. Missing/wanted counts, failed imports and health warnings moved into `ERRORS / STATUS` per the user's explicit request.

## Testing decisions

- **Primary seam:** the `/api/media` JSON contract. Test it at the HTTP boundary, from recorded upstream payloads, not by asserting on adapter internals. A fixture mode in the collector (selected by an environment value) feeds saved JSON from each API through the real parsing and snapshot code; the smoke test asserts the exact normalized fields. This is the existing pattern in the check script's stub snapshot test, extended rather than replaced.
- **Secondary verification:** the rendered page at exactly 1280×400. Assert no horizontal/vertical overflow, PAGER reads `2/3 MEDIA`, nav buttons are 64×44px, panel titles fit, and `scrollWidth <= clientWidth` for every fixed-width row. Save a native-resolution screenshot after a privacy review of names and titles.
- **Stub tests:** with no credential files present, assert `mode == "stub"`, all four regions (`streaming`, `activity`, `errors`, `latest`) exist with empty lists/counters, and the stub random-walk advances over successive ticks — the prior-art assertion checks values advance, not merely that the endpoint answers.
- **Fixture tests:** assert per-source mapping from saved payloads, `+N MORE` threshold inputs, `ok: false` behaviour, `partial` mode, title truncation, and that an invalid payload in one source leaves the other sections intact.
- **Privacy check:** extend the existing tracked-file grep for host/user/address identifiers; keep fixture titles generic. Do not include real usernames in fixtures or tests.
- **Syntax checks:** keep the Python compile and inline-JavaScript `node --check` steps.
- **Manual / deferred:** touch-target testing on real hardware stays deferred until a touchscreen is attached; say so rather than claiming it passed.

## Out of scope

- Any control action: pausing, deleting or prioritizing downloads; remote-controlling playback; triggering library scans or repairs.
- Changes to the services on LXC 201/202, their credentials, ports or reverse-proxy configuration.
- Exposing any of the six APIs outside trusted LAN/VPN access.
- New display hardware, resolutions, or replacing the shared background canvas.
- Long-term history storage, watch analytics, per-user statistics, or a mobile/remote client for the kiosk.
- Moving the collector off the Pi or splitting it into additional systemd units.
- Publishing the MEDIA page to a public URL; the kiosk remains local and unauthenticated on loopback.

## Further notes

- Credentialed read-only probes confirmed versions, ports, API prefixes and most response field shapes: Jellyfin 12.2.0, Sonarr 4.0.20.3014, Radarr 6.4.4.10685, qBittorrent v5.3.0, NZBGet 26.3. Follow-up probes confirmed error endpoints live: Sonarr/Radarr `/api/v3/health` and `/api/v3/wanted/missing` (311 and 9 missing), NZBGet `systemhealth`/`log`, qBittorrent errored filter and `connection_status` (`firewalled`). Active Jellyfin streams, qBittorrent torrent items and NZBGet list/history items were empty at probe time, so those three item shapes remain doc-based. No credential was guessed and no browser-vault entry existed; the credentials were read from a mode-600 file outside the repo and never echoed.
- Open decisions (defaults applied in this draft): show full Jellyfin usernames since the requirement is "who is streaming what"; use one combined page; use Sonarr/Radarr import history for "latest downloads"; keep the spec as a repo document under `docs/` unless an issue is explicitly requested. New open decisions: add `PROWLARR_URL`/`PROWLARR_API_KEY` to the probe template to verify indexer errors; confirm whether "media unable to be found" means `wanted/missing` counts or failed imports; choose the `NEW n` accumulation window (default: since 00:00 until acknowledged).
- Repo touchpoints: the collector module, the kiosk HTML page, the environment template, the check script, the installer and the README. No new top-level component is proposed.
- Public API references consulted: Jellyfin `GET /Sessions` (skmtc.dev Jellyfin API docs), qBittorrent WebUI API v2 torrent info (qBittorrent docs), NZBGet API reference for `status`, `listgroups`, `history`, `systemhealth` and `log` (nzbget.com), Sonarr/Radarr v3 API docs, and Prowlarr `HealthResource`/`IndexerStatusResource` docs.
- Jellyfin auth is settled by the probe: the installed 12.2.0 server requires `Authorization: MediaBrowser Token=<key>, Client=..., Device=..., DeviceId=..., Version=...`. Legacy headers and the `api_key` query parameter were removed in 12.0; do not add them as fallbacks. Sources: `https://gist.github.com/nielsvanvelzen/ea047d9028f676185832e51ffaf12a6f` (Jellyfin API authorization, maintained by a Jellyfin developer) and `https://github.com/gethomepage/homepage/discussions/7132` (12.0 compatibility report).
