#!/bin/bash
# Wink Fakecam — Setup launcher (macOS).
#
# Run once. Installs ffmpeg via Homebrew (if not already), downloads the
# sample Virat video from the deployed demo server, and pre-converts it to
# the Y4M format Chrome's --use-file-for-fake-video-capture expects.
#
# After this runs, the "Launch Fakecam" button on the demo page will work.

set -e

INSTALL_DIR="$HOME/wink-fakecam"
DEMO_HOST="https://wink-image-demo.fly.dev"
SAMPLE_NAME="Virat-Kohli-realvideo"

mkdir -p "$INSTALL_DIR/videos"

popup() {
  osascript -e "display dialog \"$1\" with title \"Wink Fakecam Setup\" buttons {\"OK\"} default button 1" >/dev/null 2>&1 || true
}

echo "=== Wink Fakecam setup ==="
echo "Install dir: $INSTALL_DIR"
echo

# 1) ffmpeg
if ! command -v ffmpeg >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "ffmpeg not found — installing via Homebrew (this may take a minute)..."
    brew install ffmpeg
  else
    popup "ffmpeg is required.\\n\\nInstall Homebrew first (see https://brew.sh) then re-run this Setup."
    echo "ERROR: Homebrew not found. Install it from https://brew.sh and re-run." >&2
    exit 1
  fi
else
  echo "ffmpeg already installed: $(command -v ffmpeg)"
fi

# 2) sample video
MP4="$INSTALL_DIR/videos/$SAMPLE_NAME.mp4"
if [[ ! -f "$MP4" ]]; then
  echo "Downloading sample video from $DEMO_HOST/samples/$SAMPLE_NAME.mp4 ..."
  curl -fL -o "$MP4" "$DEMO_HOST/samples/$SAMPLE_NAME.mp4"
else
  echo "Sample video already present at $MP4"
fi

# 3) y4m pre-conversion (Chrome's fake-camera flag wants Y4M)
Y4M="$INSTALL_DIR/videos/$SAMPLE_NAME.y4m"
if [[ ! -f "$Y4M" || "$MP4" -nt "$Y4M" ]]; then
  echo "Converting MP4 -> Y4M (uses ~80MB disk)..."
  ffmpeg -y -i "$MP4" -pix_fmt yuv420p "$Y4M" -hide_banner -loglevel error
else
  echo "Y4M already present at $Y4M"
fi

# 4) Local copy of the launch script — fetched via curl so it has no
#    quarantine attribute, meaning future double-clicks bypass Gatekeeper.
LAUNCHER="$INSTALL_DIR/launch-fakecam.command"
echo "Installing local launcher to $LAUNCHER ..."
curl -fL -o "$LAUNCHER" "$DEMO_HOST/launcher/launch-fakecam.command"
chmod +x "$LAUNCHER"

echo
echo "=== Setup complete ==="
echo "To run an attack from now on, double-click:"
echo "    $LAUNCHER"
echo "(Or: open $INSTALL_DIR in Finder)"

popup "Setup complete!\\n\\nDouble-click ~/wink-fakecam/launch-fakecam.command to run the attack.\\n\\nNo more Gatekeeper warnings — that file was downloaded cleanly."
