#!/bin/bash
# Wink Fakecam — Launch launcher (macOS).
#
# Opens a fresh Chrome instance with --use-file-for-fake-video-capture
# pointed at one of the .mp4 files in this folder's videos/ subdirectory.
# Chrome's getUserMedia will then return that video as if it were the
# camera.
#
# Behavior:
#   - Zero .mp4 files in videos/        → error popup, drop a video in
#   - Exactly one .mp4 file             → uses it directly (no picker)
#   - Two or more .mp4 files            → native macOS list picker
#   - .y4m exists and is up-to-date     → reused (instant launch)
#   - .y4m missing or older than .mp4   → ffmpeg re-converts before launch

set -e

# Resolve the script's own directory so this launcher works wherever the
# wink-fakecam folder lives — home dir, Desktop, project subdirectory,
# anywhere. Looking up videos relative to "$PWD" or "$HOME" breaks the
# moment the user moves the folder or double-clicks from Finder (where
# the working dir is "/").
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$SCRIPT_DIR"
VIDEOS_DIR="$INSTALL_DIR/videos"
DEMO_URL="https://wink-image-demo.fly.dev"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

popup() {
  osascript -e "display dialog \"$1\" with title \"Wink Fakecam\" buttons {\"OK\"} default button 1" >/dev/null 2>&1 || true
}

# --- Sanity checks -----------------------------------------------------
if [[ ! -d "$VIDEOS_DIR" ]]; then
  popup "videos/ folder not found at $VIDEOS_DIR.\\n\\nCreate it and drop .mp4 files in."
  exit 1
fi

if [[ ! -x "$CHROME" ]]; then
  popup "Google Chrome not found at /Applications/Google Chrome.app.\\n\\nInstall Chrome from https://google.com/chrome and try again."
  exit 1
fi

# --- Discover .mp4 files ----------------------------------------------
shopt -s nullglob
MP4S=("$VIDEOS_DIR"/*.mp4)
shopt -u nullglob

if [[ ${#MP4S[@]} -eq 0 ]]; then
  popup "No .mp4 files found in $VIDEOS_DIR.\\n\\nDrop one or more .mp4 files into the videos folder and try again."
  exit 1
fi

# --- Pick the video ----------------------------------------------------
if [[ ${#MP4S[@]} -eq 1 ]]; then
  # Single video: use it directly, no picker.
  CHOSEN_MP4="${MP4S[0]}"
  echo "Single video found: $(basename "$CHOSEN_MP4")"
else
  # Multiple videos: show macOS native "choose from list" dialog.
  # Build a comma-separated AppleScript list of "filename" entries from
  # the basenames. Filenames with embedded double-quotes will break this
  # — practical assumption is that no one names video files with quotes.
  AS_LIST=""
  for f in "${MP4S[@]}"; do
    NAME="$(basename "$f")"
    [[ -n "$AS_LIST" ]] && AS_LIST+=","
    AS_LIST+="\"$NAME\""
  done
  DEFAULT_NAME="$(basename "${MP4S[0]}")"

  CHOSEN=$(osascript -e "choose from list {$AS_LIST} with title \"Wink Fakecam\" with prompt \"Pick a video to inject as the fake camera:\" default items {\"$DEFAULT_NAME\"} OK button name \"Launch\" cancel button name \"Cancel\"" 2>/dev/null || echo "false")

  if [[ "$CHOSEN" == "false" || -z "$CHOSEN" ]]; then
    echo "Cancelled."
    exit 0
  fi

  CHOSEN_MP4="$VIDEOS_DIR/$CHOSEN"
fi

if [[ ! -f "$CHOSEN_MP4" ]]; then
  popup "Selected video not found: $CHOSEN_MP4"
  exit 1
fi

# --- Convert to .y4m if needed -----------------------------------------
# Chrome's --use-file-for-fake-video-capture wants Y4M. We cache the
# conversion alongside the .mp4 so each video is only converted once.
# Re-converts if the .mp4 is newer than the cached .y4m (e.g., user
# replaced the .mp4 with a different file at the same name).
Y4M="${CHOSEN_MP4%.*}.y4m"
if [[ ! -f "$Y4M" || "$CHOSEN_MP4" -nt "$Y4M" ]]; then
  if ! command -v ffmpeg >/dev/null 2>&1; then
    popup "ffmpeg not found. Install it first:\\n\\nbrew install ffmpeg"
    exit 1
  fi
  echo "Converting $(basename "$CHOSEN_MP4") -> Y4M (one-time, ~5s)..."
  ffmpeg -y -i "$CHOSEN_MP4" -pix_fmt yuv420p "$Y4M" -hide_banner -loglevel error
fi

# --- Launch Chrome with fake camera flags ------------------------------
PROFILE_DIR="$(mktemp -d -t chrome-fakecam)"
echo "Launching Chrome (profile: $PROFILE_DIR)"
echo "Video: $(basename "$CHOSEN_MP4")"
echo "Y4M:   $Y4M"
echo "URL:   $DEMO_URL"

"$CHROME" \
  --user-data-dir="$PROFILE_DIR" \
  --use-fake-ui-for-media-stream \
  --use-fake-device-for-media-stream \
  --use-file-for-fake-video-capture="$Y4M" \
  "$DEMO_URL"
