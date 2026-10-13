#!/usr/bin/env bash
# Screenshot the terrain page through Windows Edge.
#
# Not the WSL chromium: it cannot see this user's fontconfig, so every Chinese
# label renders as tofu boxes (see memory headless-chromium-ignores-fontconfig).
# And not --disable-gpu either -- that switch turns WebGL2 off, which is the only
# thing this page draws with. SwiftShader gives software WebGL2 that actually
# produces pixels in a headless session.
#
# Usage: shot.sh <name> [url-fragment]
#   shot.sh overview            -> site/terrain.html
#   shot.sh climb '#view=climb' -> with a fragment appended
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EDGE="/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
[ -x "$EDGE" ] || EDGE="/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe"
[ -x "$EDGE" ] || { echo "Edge not found" >&2; exit 1; }

NAME="${1:?usage: shot.sh <name> [fragment]}"
FRAG="${2:-}"
PORT="${PORT:-8799}"
SIZE="${SIZE:-1600,1100}"
WAIT="${WAIT:-30000}"

# Serve over http rather than file://: the page fetches scene.json/scene.bin, and
# file:// origins are opaque, so those fetches fail CORS.
# ROUTE_SITE lets the CLI point this at routes/<name>/site; the bare site/
# directory is the page templates, which have no scene.bin beside them.
SITE="${ROUTE_SITE:-$HERE/site}"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$SITE" \
  >/tmp/route-studio-http.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 1 40); do
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/terrain.html" && break
  sleep 0.25
done

# Edge writes to a Windows path, so the output has to live somewhere both sides
# can name. Ask Windows for its own temp directory rather than hardcoding a
# user profile; this used to be a literal C:\Users\<name>\... and worked on
# exactly one machine.
WINTMP="$(cmd.exe /c 'echo %TEMP%' 2>/dev/null | tr -d '\r\n')"
case "$WINTMP" in
  [A-Za-z]:*) : ;;
  *) echo "could not read Windows %TEMP% (is this WSL with interop?)" >&2; exit 1 ;;
esac
OUT_WIN="$WINTMP\\rs-$NAME.png"
# Same directory from the Linux side: C:\foo\bar -> /mnt/c/foo/bar
OUT_WSL="$(printf '%s' "$WINTMP" \
  | sed -E 's|^([A-Za-z]):|/mnt/\L\1|; s|\\|/|g')/rs-$NAME.png"
rm -f "$OUT_WSL"

"$EDGE" --headless=new --use-angle=swiftshader --enable-unsafe-swiftshader \
  --hide-scrollbars --force-device-scale-factor=1 \
  --window-size="$SIZE" --screenshot="$OUT_WIN" \
  --virtual-time-budget="$WAIT" \
  "http://127.0.0.1:$PORT/terrain.html$FRAG" 2>&1 \
  | grep -viE 'fallback_task_provider|crbug|Every renderer|GroupMarkerNotSet' || true

sleep 2
if [ ! -f "$OUT_WSL" ]; then
  echo "no screenshot produced" >&2
  exit 1
fi
mkdir -p "$HERE/shots"
cp "$OUT_WSL" "$HERE/shots/$NAME.png"
echo "$HERE/shots/$NAME.png  $(stat -c%s "$HERE/shots/$NAME.png") bytes"
