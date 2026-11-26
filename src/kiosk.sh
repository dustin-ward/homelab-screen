#!/bin/sh
# Kiosk launcher for the homelab-screen dashboard on the Pi.
set -eu

export XDG_RUNTIME_DIR=/run/user/$(id -u)
export XDG_SESSION_TYPE=wayland

exec cage -d -- chromium \
  --noerrdialogs \
  --disable-session-crashed-bubble \
  --disable-features=TranslateUI \
  --disable-background-networking \
  --disable-component-update \
  --disable-dev-shm-usage \
  --password-store=basic \
  --no-first-run \
  --no-default-browser-check \
  --check-default-browser=no \
  --user-data-dir="$HOME/.config/homelab-screen/kiosk" \
  --ozone-platform=wayland \
  --window-mode=kiosk \
  --kiosk \
  --hide-scrollbars \
  --mute-audio \
  --disable-pinch \
  --disable-renderer-backgrounding \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  http://127.0.0.1:8080/kiosk.html
