#!/bin/bash
# Launch Chrome on macOS with a fake camera fed from a video or still image.
#
# Usage:
#   ./fakecam.sh <video-or-image-file> [url]
#
# Stills (jpg/jpeg/png/heic/webp) are converted into a 10s 30fps Y4M loop.
# Videos (anything else) are transcoded to Y4M.
# Existing .y4m files are passed through unchanged.

set -e
shopt -s nocasematch

INPUT="${1:?Usage: $0 <video-or-image-file> [url]}"
URL="${2:-http://localhost:5050}"

if [[ ! -f "$INPUT" ]]; then
  echo "Input not found: $INPUT" >&2
  exit 1
fi

case "$INPUT" in
  *.y4m)
    Y4M="$INPUT"
    ;;
  *.jpg|*.jpeg|*.png|*.heic|*.webp)
    Y4M="${INPUT%.*}.y4m"
    if [[ ! -f "$Y4M" || "$INPUT" -nt "$Y4M" ]]; then
      echo "Building still->Y4M: $INPUT -> $Y4M (10s, 30fps loop)"
      ffmpeg -y -loop 1 -t 10 -i "$INPUT" -r 30 -pix_fmt yuv420p \
        -f yuv4mpegpipe "$Y4M" -hide_banner -loglevel error
    fi
    ;;
  *)
    Y4M="${INPUT%.*}.y4m"
    if [[ ! -f "$Y4M" || "$INPUT" -nt "$Y4M" ]]; then
      echo "Transcoding video->Y4M: $INPUT -> $Y4M"
      ffmpeg -y -i "$INPUT" -pix_fmt yuv420p "$Y4M" \
        -hide_banner -loglevel error
    fi
    ;;
esac

PROFILE_DIR="$(mktemp -d -t chrome-fakecam)"

echo "Launching Chrome (profile: $PROFILE_DIR)"
echo "Y4M source: $Y4M"
echo "URL:        $URL"

"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$PROFILE_DIR" \
  --use-fake-ui-for-media-stream \
  --use-fake-device-for-media-stream \
  --use-file-for-fake-video-capture="$Y4M" \
  "$URL"
