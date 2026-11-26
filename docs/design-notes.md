# CORE layout notes (1280×400)

- Structure: 44px header, 48px footer, 8px outer padding, 6px gaps. Main area is a 2×2 grid: left panels ~843px wide, right panels ~421px, each ~137px tall. Footer buttons are now 64×44px (widened from 44×44) for easier touch hits.
- Character grid: 13px DejaVu Sans Mono at 16px line-height ≈ 160×25 cells. Left-panel sparklines are 60 blocks over 60s; right-panel sparklines are 40 blocks over 40s (shorter window keeps them legible in the narrow column). Utilization bars are 32 cells and fill the panel width.
- Palette: black background, `#e8e8e8` primary, `#8a8a8a` secondary, `#444444` borders, `#b22222` dark-red accent for brand/titles. No gradients, shadows, or rounded corners.
- Status colours: `NOMINAL` uses green `#00aa00`; `--warn` (`#b36000`) and `--crit` (`#a31621`) are defined for later states. Dark red remains the server identity colour.
- Fake data is labelled only by window and series: CPU 1/5/15 load + one 60s CPU sparkline; RAM/SWAP/ARC + 40s RAM sparkline; network down/up rates + two 60s sparklines; rootfs capacity + uptime + 40s read/write sparklines. The extra CPU-load and network-total sparklines were removed from the live screen (total was just down + up). Source caveats and per-core/per-interface notes were removed from the UI at the user's request.
- Wording confirmed clear by the user: `1/2 CORE`, `14 CORES`, and all remaining captions stay as written. Removed eyebrows/taglines: Aggregate, no per-core data in PVE API, 1-minute average, no interface breakdown, host view, pool capacity → page 4, READ ONLY, PVE API, 5 pages.
- Verified in a browser at exactly 1280×400: screen and body are 1280×400, no horizontal or vertical overflow, all four panels fit, titles readable, nav buttons 64×44px, status dot green.

Deferred / open:

1. Touchscreen hit-target verification — no touchscreen is available at the moment; the 64×44px buttons are specified but not yet tested on hardware.
2. Real-data window/precision — 60s left, 40s right, decimal values, and 32-cell bars are display choices; adjust if the RRD windows change.
3. Physical-panel brightness check for the dark-red titles and dark-gray borders.
