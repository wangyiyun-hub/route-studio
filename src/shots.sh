#!/usr/bin/env bash
# Batch screenshots through Windows Edge, one server for the whole set.
#
# Edge's SwiftShader renderer intermittently produces nothing, so each shot is
# retried; a run that reports a size for every tag is a real pass, and a tag
# that stays at 0 after three tries is a genuine failure rather than flakiness.
#
# Usage: shots.sh <route> <tag>[:<fragment>] ...
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EDGE="/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
[ -x "$EDGE" ] || EDGE="/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe"

ROUTE="${1:?usage: shots.sh <route> <tag>[:<frag>] ...}"; shift
SITE="$HERE/routes/$ROUTE/site"
[ -f "$SITE/scene.bin" ] || { echo "no scene.bin in $SITE -- run build first" >&2; exit 1; }

SIZE="${SIZE:-1400,900}"
WAIT="${WAIT:-22000}"
WINTMP="$(cmd.exe /c 'echo %TEMP%' 2>/dev/null | tr -d '\r\n')"
WSLTMP="$(printf '%s' "$WINTMP" | sed -E 's|^([A-Za-z]):|/mnt/\L\1|; s|\\|/|g')"

# Pick a free port. 8787 is claude-web and 8799 may be a preview server; a
# screenshot taken against the wrong service comes back as someone else's page
# ("bad token" was one), which is worse than a failure because it looks real.
PORT=0
for p in $(seq 8740 8760); do
  ss -ltn 2>/dev/null | grep -q ":$p " || { PORT=$p; break; }
done
[ "$PORT" = 0 ] && { echo "no free port in 8740-8760" >&2; exit 1; }

python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$SITE" \
  >/dev/null 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
for _ in $(seq 1 40); do
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/terrain.html" && break
  sleep 0.25
done

mkdir -p "$HERE/shots"
fails=0
for spec in "$@"; do
  tag="${spec%%:*}"
  frag="${spec#*:}"
  [ "$frag" = "$spec" ] && frag=""
  out_win="$WINTMP\\rs-$tag.png"
  out_wsl="$WSLTMP/rs-$tag.png"
  sz=0
  for try in 1 2 3; do
    rm -f "$out_wsl"
    timeout 90 "$EDGE" --headless=new --use-angle=swiftshader \
      --enable-unsafe-swiftshader --hide-scrollbars \
      --force-device-scale-factor=1 --window-size="$SIZE" \
      --screenshot="$out_win" --virtual-time-budget="$WAIT" \
      "http://127.0.0.1:$PORT/terrain.html$frag" >/dev/null 2>&1
    sz=$(stat -c%s "$out_wsl" 2>/dev/null || echo 0)
    # Under 60 kB means the page rendered an error screen or nothing at all.
    [ "$sz" -gt 60000 ] && break
    sleep 2
  done
  if [ "$sz" -gt 60000 ]; then
    cp "$out_wsl" "$HERE/shots/$tag.png"
    printf '  ok    %-16s %8s bytes%s\n' "$tag" "$sz" \
      "$([ "$try" -gt 1 ] && echo "  (重试 $try 次)")"
  else
    printf '  FAIL  %-16s %8s bytes\n' "$tag" "$sz"
    fails=$((fails+1))
  fi
done
[ "$fails" = 0 ] && echo "全部成功" || echo "$fails 个失败"
exit $((fails > 0))
