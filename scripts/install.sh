#!/bin/sh
# Idempotent install / upgrade / rollback for homelab-screen.
# All identifying values stay on the host; this repo contains only code.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PREFIX=/usr/local
CONFIG_DIR=/etc/homelab-screen
UNIT_DIR=/etc/systemd/system
APP_DIR=
USER_NAME=
VERSION_OVERRIDE=
ALLOW_STUB=0
ACTION=install
ROLLBACK_TAG=

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [options]
  --allow-stub          install without live PVE token/config (development only)
  --user NAME           service user (default: current user)
  --prefix DIR          bin prefix (default: /usr/local)
  --config-dir DIR      config dir (default: /etc/homelab-screen)
  --unit-dir DIR        systemd unit dir (default: /etc/systemd/system)
  --app-dir DIR         installed source dir (default: USER_HOME/homelab-screen)
  --rollback [TAG]      restore a timestamped backup, or the latest one
  --status              print installed version and service health
  --version TEXT         version stamp to write (default: git commit)
  -h, --help            show this help
EOF
}

fail() { echo "ERROR: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --allow-stub) ALLOW_STUB=1 ;;
    --user) USER_NAME=${2:?--user needs a value}; shift ;;
    --prefix) PREFIX=$2; shift ;;
    --config-dir) CONFIG_DIR=$2; shift ;;
    --unit-dir) UNIT_DIR=$2; shift ;;
    --app-dir) APP_DIR=$2; shift ;;
    --rollback)
      ACTION=rollback
      if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then ROLLBACK_TAG=$2; shift
      else ROLLBACK_TAG=latest; fi
      ;;
    --status) ACTION=status ;;
    --version) VERSION_OVERRIDE=$2; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
  shift
done

[ -n "$USER_NAME" ] || USER_NAME=$(id -un)
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
[ -n "$USER_HOME" ] || USER_HOME=$HOME
USER_UID=$(id -u "$USER_NAME")
[ -n "$APP_DIR" ] || APP_DIR=$USER_HOME/homelab-screen

SUDO=
if [ "$(id -u)" -ne 0 ]; then SUDO=sudo; fi
run_root() { $SUDO "$@"; }
as_user() {
  if [ "$(id -u)" -eq 0 ]; then
    runuser -u "$USER_NAME" -- "$@"
  elif [ "$USER_NAME" != "$(id -un)" ]; then
    sudo -u "$USER_NAME" "$@"
  else
    "$@"
  fi
}

render() {
  sed -e "s|__KIOSK_USER__|$USER_NAME|g" \
      -e "s|__PREFIX__|$PREFIX|g" \
      -e "s|__CONFIG_DIR__|$CONFIG_DIR|g" \
      -e "s|__APP_DIR__|$APP_DIR|g" \
      -e "s|__USER_HOME__|$USER_HOME|g" \
      -e "s|__USER_UID__|$USER_UID|g" "$1" > "$2"
}

config_value() {
  sed -n "s/^$1=//p" "$CONFIG_DIR/config.env" 2>/dev/null | tail -n 1
}

clean_kiosk_restart() {
  run_root systemctl stop kiosk.service
  sleep 5
  run_root systemctl start kiosk.service
}

do_status() {
  echo "user:    $USER_NAME"
  echo "app:     $APP_DIR"
  echo "version: $(cat "$APP_DIR/VERSION" 2>/dev/null || echo unknown)"
  echo "services:"
  run_root systemctl is-active collector.service kiosk.service || true
  if command -v vcgencmd >/dev/null 2>&1; then
    vcgencmd measure_temp
    vcgencmd get_throttled
  fi
}

do_backup() {
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup="$APP_DIR/backups/$stamp"
  run_root install -d -m 0755 "$backup"
  for f in kiosk.html collector.py media.py VERSION; do
    [ -f "$APP_DIR/$f" ] && run_root cp -p "$APP_DIR/$f" "$backup/$f"
  done
  for u in kiosk.service collector.service; do
    [ -f "$UNIT_DIR/$u" ] && run_root cp -p "$UNIT_DIR/$u" "$backup/$u"
  done
  echo "$backup"
}

do_install() {
  command -v systemctl >/dev/null 2>&1 || fail "systemctl not found"

  run_root install -d -m 0755 "$APP_DIR"
  echo "backup: $(do_backup)"

  run_root install -m 0644 "$ROOT/src/kiosk.html" "$APP_DIR/kiosk.html"
  run_root install -m 0644 "$ROOT/src/collector.py" "$APP_DIR/collector.py"
  run_root install -m 0644 "$ROOT/src/media.py" "$APP_DIR/media.py"
  run_root install -d -m 0755 "$PREFIX/bin"
  run_root install -m 0755 "$ROOT/src/kiosk.sh" "$PREFIX/bin/kiosk.sh"
  run_root install -d -m 0755 "$CONFIG_DIR"

  if [ ! -f "$CONFIG_DIR/config.env" ]; then
    run_root install -m 0600 "$ROOT/config.env.example" "$CONFIG_DIR/config.env"
    run_root chown "$USER_NAME:$USER_NAME" "$CONFIG_DIR/config.env"
    echo "created $CONFIG_DIR/config.env - fill it in locally"
  fi
  as_user install -d -m 0700 "$USER_HOME/.config/homelab-screen"

  if [ "$ALLOW_STUB" -eq 0 ]; then
    missing=
    for key in PVE_API_BASE PVE_NODE PVE_CERT_PIN; do
      [ -n "$(config_value "$key")" ] || missing="$missing $key"
    done
    token_file=$(config_value HOMELAB_TOKEN_FILE)
    cert_file=$(config_value HOMELAB_CERT_FILE)
    [ -n "$token_file" ] || token_file=$USER_HOME/.config/homelab-screen/pve-token
    [ -n "$cert_file" ] || cert_file=$USER_HOME/.config/homelab-screen/pve-cert.pem
    [ -f "$token_file" ] || missing="$missing token"
    [ -f "$cert_file" ] || missing="$missing cert"
    if [ -n "$missing" ]; then
      fail "missing live setup:$missing. Fill $CONFIG_DIR/config.env and install token/cert, or rerun with --allow-stub."
    fi
    as_user chmod 600 "$token_file" "$cert_file" 2>/dev/null || true
  fi

  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  render "$ROOT/src/kiosk.service" "$tmp/kiosk.service"
  render "$ROOT/src/collector.service" "$tmp/collector.service"
  run_root install -m 0644 "$tmp/kiosk.service" "$UNIT_DIR/kiosk.service"
  run_root install -m 0644 "$tmp/collector.service" "$UNIT_DIR/collector.service"

  commit=${VERSION_OVERRIDE:-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)}
  printf '%s %s\n' "$commit" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp/VERSION"
  run_root install -m 0644 "$tmp/VERSION" "$APP_DIR/VERSION"

  run_root systemctl daemon-reload
  run_root systemctl enable collector.service kiosk.service
  run_root systemctl restart collector.service
  clean_kiosk_restart
  echo "installed $commit; use --status to verify"
}

do_rollback() {
  root="$APP_DIR/backups"
  if [ "$ROLLBACK_TAG" = latest ]; then
    target=
    for d in "$root"/*; do [ -d "$d" ] && target=$d; done
    [ -n "$target" ] || fail "no backups found under $root"
  else
    target="$root/$ROLLBACK_TAG"
  fi
  [ -d "$target" ] || fail "backup not found: $target"

  for f in kiosk.html collector.py media.py VERSION; do
    [ -f "$target/$f" ] && run_root install -m 0644 "$target/$f" "$APP_DIR/$f"
  done
  for u in kiosk.service collector.service; do
    [ -f "$target/$u" ] && run_root install -m 0644 "$target/$u" "$UNIT_DIR/$u"
  done
  run_root systemctl daemon-reload
  run_root systemctl restart collector.service
  clean_kiosk_restart
  echo "rolled back to $target"
}

case "$ACTION" in
  install) do_install ;;
  rollback) do_rollback ;;
  status) do_status ;;
esac
