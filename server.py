#!/usr/bin/env python3
"""Wink liveness gap demo server.

Flow:
  1. Browser POSTs a captured selfie JPEG to /api/recognize
  2. Server mints a Wink session: Basic clientId:clientSecret
  3. Server calls Wink recognize-face: Basic clientId:sessionId
  4. Wink response is forwarded back to the browser

Credentials are read from a .env file in this directory (or from real env vars,
which take precedence). See .env in this folder for the expected keys.
"""

from __future__ import annotations

import base64
import json
import os
import random
import sys
import tempfile

import cv2
import mediapipe as mp
import numpy as np
import requests
from flask import Flask, jsonify, request, send_from_directory

WINK_BASE = "https://stagelogin-api.winkapis.com"
SESSION_URL = f"{WINK_BASE}/wink/v1/session"
RECOGNIZE_URL = f"{WINK_BASE}/wink/v1/enroll-login/face"
HERE = os.path.dirname(os.path.abspath(__file__))

# Wink's Azure Application Gateway WAF rejects the default python-requests
# User-Agent with a 403. Any non-default UA passes.
USER_AGENT = "wink-liveness-demo/1.0"


def _load_dotenv() -> None:
    """Tiny .env loader; no python-dotenv dependency. Real env vars win."""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_dotenv()

app = Flask(__name__)


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def _json_or_text(resp: requests.Response) -> dict:
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        try:
            return resp.json()
        except ValueError:
            pass
    return {"raw": resp.text}


# ─── Face quality gate + rPPG ─────────────────────────────────────────
# Mirrors Wink's production pipeline: face capture → quality gate → liveness.
# A capture that fails quality (too dim, off-axis, too small) gets a
# specific "improve capture" message rather than a misleading liveness fail.
# Only quality-cleared captures reach rPPG. This keeps the comparison fair
# between live faces and Y4M-injected stills.

_MP_FACE = mp.solutions.face_detection.FaceDetection(
    min_detection_confidence=0.5,
    model_selection=1,  # 1 = "full range" model (handles closer faces too)
)

# Quality gate thresholds — tunable from observed numbers.
QUALITY_DETECTION_RATE_MIN = 0.80      # face must be detected in 80%+ of frames
QUALITY_BRIGHTNESS_MIN = 80.0          # mean V (HSV, 0-255) over face bbox
QUALITY_FACE_FRAC_MIN = 0.05           # face bbox must cover ≥5% of frame area
QUALITY_DETECTION_SCORE_MIN = 0.70     # mean MediaPipe confidence

# rPPG thresholds — discriminate by post-bandpass signal magnitude
# (resilient to broadband motion noise in real captures).
RPPG_GREEN_DETREND_STD_MIN = 0.30      # 8-bit intensity units
RPPG_GREEN_BANDPASS_RMS_MIN = 0.15
RPPG_BPM_LOW = 42.0
RPPG_BPM_HIGH = 180.0

# Screen probe thresholds — minimum delta (in 8-bit units) for the
# screen-flashed channel to count as "reflected". Tunable from observed
# real-cam values; depends on screen brightness, ambient, distance.
PROBE_DELTA_MIN = -1000.0       # effectively disabled — camera auto-exposure can drop ALL channels; what matters is the relative dominance, not the absolute sign
PROBE_DOMINANCE_MARGIN = 0.3    # lowered from 0.4 — when the probe runs in parallel with Stage A positioning the camera is still settling, so deltas are smaller; 0.3 still sits well above the systematic ~0 margins recordings produce, so attackers can't manufacture a 2/3 majority above 0.3 either
PROBE_TIME_LEAD_MS = 50.0       # legacy: kept for back-compat with older clients (peak-time check)
PROBE_ASYMMETRY_MIN = 1.0       # left vs right sub-region delta in expected channel (8-bit units) — real reflection produces ≥1; recordings are near-zero
PROBE_FACE_PAD = 0.20           # bbox padding (face + 20% on each side); used by face-quality response and as the crop sent to Wink
PROBE_MEDIAPIPE_EVERY_N_FRAMES = 3  # Run MediaPipe on every Nth frame, carry the bbox forward for the rest. Face moves negligibly in <100ms, so this gives ~3× speedup on probe analysis with no measurable signal loss.

# Wink-branded probe colors. Each entry has the CSS color the client renders
# and the BGR channel index the analyzer tracks. We avoid blue-dominant
# colors because webcam blue-channel sensitivity is poor.
WINK_COLORS = {
    # Saturated palette — earlier pastels (orange #ff6b35, pink #ff506e, mint
    # #34cb99) had significant off-channel emission (e.g. mint had B=153,
    # almost teal) which leaked into the face's non-expected channels and
    # collapsed the dominance margin. Pure-channel emission gives the face
    # a much cleaner spectral signal to reflect, so the expected channel
    # rises far above the others. Camera auto-WB pushes back harder against
    # saturated colors but the optical reflection is selective enough that
    # the net dominance still grows.
    "ORANGE": {"render": "rgb(255,80,20)",   "channel": 2},  # saturated orange — strong R, minimal B
    "RED":    {"render": "rgb(220,20,40)",   "channel": 2},  # deep red — near-pure R (was PINK rgb(255,80,110); B=110 leaked into R-dominance)
    "GREEN":  {"render": "rgb(20,200,40)",   "channel": 1},  # pure green — near-zero R/B (was rgb(52,203,153) which was actually teal — B=153 destroyed G-dominance)
}
# Mapping from screen-sweep direction → expected ordering of which IMAGE
# sub-region peaks first. Camera mirrors horizontally (user's left = image
# right) but not vertically.
PROBE_DIRECTION_EXPECTED_LEAD = {
    "ltr": ("right", "left"),    # screen L→R, user-left lights first, image-right peaks first
    "rtl": ("left", "right"),
    "ttb": ("top", "bottom"),    # screen top→bottom, image top peaks first
    "btt": ("bottom", "top"),
}


def _compute_rppg_from_series(rgb_means: list, fps: float) -> dict:
    """Run rPPG analysis on an in-memory list of [B, G, R] means."""
    n = len(rgb_means)
    if n < 30:
        return {"pass": False, "reason": f"too few skin samples ({n})", "frames_analyzed": n}

    arr = np.array(rgb_means, dtype=float)
    G = arr[:, 1]

    win = max(3, int(round(fps * 1.0)))
    kernel = np.ones(win) / win
    G_d = G - np.convolve(G, kernel, mode="same")
    edge = win
    if n > 2 * edge:
        G_d = G_d[edge:-edge]

    g_pre_std = float(np.std(G))
    g_detrend_std = float(np.std(G_d))

    N = len(G_d)
    spec = np.fft.rfft(G_d - G_d.mean())
    freqs = np.fft.rfftfreq(N, d=1.0 / fps)
    band_mask = (freqs >= 0.7) & (freqs <= 3.0)
    if not np.any(band_mask):
        return {"pass": False, "reason": "prefix too short for pulse band"}

    spec_bp = np.where(band_mask, spec, 0.0)
    g_bp = np.fft.irfft(spec_bp, n=N)
    g_bp_rms = float(np.sqrt(np.mean(g_bp ** 2)))

    spec_mag = np.abs(spec)
    spec_mag_band = np.where(band_mask, spec_mag, 0.0)
    peak_idx = int(np.argmax(spec_mag_band))
    bpm = float(freqs[peak_idx]) * 60.0

    is_pass = (
        g_bp_rms >= RPPG_GREEN_BANDPASS_RMS_MIN
        and g_detrend_std >= RPPG_GREEN_DETREND_STD_MIN
        and RPPG_BPM_LOW <= bpm <= RPPG_BPM_HIGH
    )
    return {
        "pass": is_pass,
        "bpm": round(bpm, 1),
        "green_pre_std": round(g_pre_std, 3),
        "green_detrend_std": round(g_detrend_std, 3),
        "green_bandpass_rms": round(g_bp_rms, 3),
        "frames_analyzed": n,
        "fps": round(float(fps), 1),
        "thresholds": {
            "green_bandpass_rms_min": RPPG_GREEN_BANDPASS_RMS_MIN,
            "green_detrend_std_min": RPPG_GREEN_DETREND_STD_MIN,
            "bpm_range": [RPPG_BPM_LOW, RPPG_BPM_HIGH],
        },
        "reason": (
            None
            if is_pass
            else (
                f"bandpass_rms={g_bp_rms:.3f} (need≥{RPPG_GREEN_BANDPASS_RMS_MIN}), "
                f"detrend_std={g_detrend_std:.3f} (need≥{RPPG_GREEN_DETREND_STD_MIN}), "
                f"bpm={bpm:.0f}"
            )
        ),
    }


def _compute_screen_probe(per_frame: list, phases: list, sequence: list) -> dict:
    """Verify each flash slot showed:
      (a) expected color channel rising vs the preceding baseline (whole skin), AND
      (b) the expected sub-region peaking earlier than its mirror sub-region
          (directional asymmetry from the sweep band's position over time).

    per_frame: [{time_ms, rgb_mean, sub_regions}].
    phases:    [{kind, color, direction, start, end}] from the client.
    sequence:  [{color, direction}, …] from the challenge.
    """
    # color → BGR channel index where reflection should peak
    # Combines Wink-branded palette + back-compat for the older R/G/B labels.
    color_channel = {name: cfg["channel"] for name, cfg in WINK_COLORS.items()}
    color_channel.update({"R": 2, "G": 1, "B": 0})

    # --- Whole-skin per-phase mean for the color check ---
    phase_means: dict[int, np.ndarray] = {}
    phase_frames: dict[int, int] = {}
    for i, p in enumerate(phases):
        in_phase = [f["rgb_mean"] for f in per_frame
                    if p["start"] <= f["time_ms"] < p["end"] and f["rgb_mean"] is not None]
        if in_phase:
            phase_means[i] = np.mean(np.array(in_phase, dtype=float), axis=0)
            phase_frames[i] = len(in_phase)

    flash_results = []

    for i, p in enumerate(phases):
        if p.get("kind") != "flash":
            continue
        # nearest preceding baseline
        baseline_idx = None
        for j in range(i - 1, -1, -1):
            if phases[j].get("kind") in ("baseline_pre", "baseline"):
                baseline_idx = j
                break

        color = p.get("color")
        position = p.get("position") or p.get("direction")  # back-compat for older clients

        if (baseline_idx is None
                or i not in phase_means
                or baseline_idx not in phase_means):
            flash_results.append({
                "color": color, "position": position,
                "color_dominant": False, "direction_correct": False,
                "is_pass": False,
                "reason": "missing frame data for flash or baseline",
            })
            continue

        # --- (a) Color check (whole skin) ---
        delta = phase_means[i] - phase_means[baseline_idx]  # [dB, dG, dR]
        ch = color_channel.get(color)
        if ch is None:
            flash_results.append({
                "color": color, "position": position,
                "color_dominant": False, "direction_correct": False,
                "is_pass": False, "reason": "unknown color",
            })
            continue
        expected_delta = float(delta[ch])
        other_max = float(max(delta[j] for j in range(3) if j != ch))
        color_dominant = (
            expected_delta >= PROBE_DELTA_MIN
            and expected_delta - other_max >= PROBE_DOMINANCE_MARGIN
        )

        # --- (b) Spatial asymmetry: left vs right skin sub-regions ---
        # The static colored fill on one side of the stage illuminates that
        # side of the face more than the other. We compare the expected-color
        # channel delta of left vs right skin sub-regions (each measured
        # against its own baseline). A real reflection produces an asymmetry
        # of ≥1 unit; a recording has near-zero asymmetry because the video
        # plays uniformly across both regions.
        def _sub_region_mean_in_phase(region: str, phase):
            samples = [
                f["sub_regions"][region]
                for f in per_frame
                if phase["start"] <= f["time_ms"] < phase["end"]
                and f.get("sub_regions") and region in f["sub_regions"]
            ]
            if not samples:
                return None
            return np.mean(np.array(samples, dtype=float), axis=0)

        left_slot = _sub_region_mean_in_phase("left", p)
        right_slot = _sub_region_mean_in_phase("right", p)
        left_base = _sub_region_mean_in_phase("left", phases[baseline_idx])
        right_base = _sub_region_mean_in_phase("right", phases[baseline_idx])

        left_delta_in_chan = right_delta_in_chan = None
        asymmetry = None
        direction_correct = False
        if (left_slot is not None and right_slot is not None
                and left_base is not None and right_base is not None):
            left_delta_in_chan = float((left_slot - left_base)[ch])
            right_delta_in_chan = float((right_slot - right_base)[ch])
            asymmetry = abs(left_delta_in_chan - right_delta_in_chan)
            direction_correct = asymmetry >= PROBE_ASYMMETRY_MIN

        # Per-flash is_pass mirrors the gate (color-only in full-fill mode).
        # Asymmetry stays in the response as a diagnostic but doesn't gate.
        is_pass = color_dominant
        flash_results.append({
            "color": color,
            "position": position,
            "delta_BGR": [round(float(x), 2) for x in delta.tolist()],
            "expected_channel_delta": round(expected_delta, 2),
            "max_other_channel_delta": round(other_max, 2),
            "color_dominant": bool(color_dominant),
            "left_delta_in_channel": (round(left_delta_in_chan, 2) if left_delta_in_chan is not None else None),
            "right_delta_in_channel": (round(right_delta_in_chan, 2) if right_delta_in_chan is not None else None),
            "asymmetry": (round(asymmetry, 2) if asymmetry is not None else None),
            "direction_correct": bool(direction_correct),
            "frames_in_flash": phase_frames.get(i, 0),
            "frames_in_baseline": phase_frames.get(baseline_idx, 0),
            "is_pass": bool(is_pass),
        })

    expected_count = len([p for p in phases if p.get("kind") == "flash"])
    # Full-fill mode: gate solely on color dominance. Position/asymmetry is
    # not enforced because uniform surround illumination produces no
    # left/right gradient on the face. Recording attacks still fail because
    # their expected channel doesn't rise vs the gray baseline.
    color_pass_count = sum(1 for r in flash_results if r.get("color_dominant"))
    direction_pass_count = sum(1 for r in flash_results if r.get("direction_correct"))
    # Gate: with 2 slots, both must pass (majority of 2 = 2). With 3+ slots,
    # tolerate one failure to absorb camera-adaptation drift on later slots.
    max_color_failures = 1 if expected_count >= 3 else 0
    all_pass = (
        len(flash_results) == expected_count
        and expected_count == len(sequence)
        and color_pass_count >= max(1, expected_count - max_color_failures)
    )
    return {
        "pass": all_pass,
        "sequence": sequence,
        "flashes": flash_results,
        "color_pass_count": color_pass_count,
        "direction_pass_count": direction_pass_count,
        "thresholds": {
            "delta_min": PROBE_DELTA_MIN,
            "dominance_margin": PROBE_DOMINANCE_MARGIN,
        },
    }


def _analyze_video_with_probe(video_path: str, phases: list, sequence: list) -> dict:
    """Single pass over video → quality + rPPG-on-prefix + screen-probe.

    rPPG runs only on frames before the first flash (prefix), to avoid
    contaminating the pulse-band spectrum with screen-induced color shifts.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {
            "quality": {"pass": False, "reason": "could not open video"},
            "rppg": None,
            "probe": None,
        }

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    decoded = 0
    detections: list[dict] = []
    per_frame: list[dict] = []  # {time_ms, rgb_mean}
    last_box: tuple[int, int, int, int] | None = None
    mp_attempts = 0  # how many frames we actually ran MediaPipe on

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        decoded += 1
        time_ms = float(cap.get(cv2.CAP_PROP_POS_MSEC))
        H, W = frame.shape[:2]

        # Skip MediaPipe on most frames — face hardly moves in <100ms, so we
        # can carry the bbox forward and still get accurate skin samples.
        # MediaPipe always runs on the first frame (no prior box) and on every
        # Nth frame after that.
        should_run_mp = (last_box is None) or (decoded % PROBE_MEDIAPIPE_EVERY_N_FRAMES == 0)

        if should_run_mp:
            mp_attempts += 1
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = _MP_FACE.process(rgb_frame)
            if results.detections:
                d = max(
                    results.detections,
                    key=lambda x: (
                        x.location_data.relative_bounding_box.width
                        * x.location_data.relative_bounding_box.height
                    ),
                )
                rb = d.location_data.relative_bounding_box
                x = max(0, int(rb.xmin * W))
                y = max(0, int(rb.ymin * H))
                w = min(int(rb.width * W), W - x)
                h = min(int(rb.height * H), H - y)
                if w > 0 and h > 0:
                    last_box = (x, y, w, h)
                    face_roi = frame[y : y + h, x : x + w]
                    hsv = cv2.cvtColor(face_roi, cv2.COLOR_BGR2HSV)
                    detections.append({
                        "score": float(d.score[0]),
                        "brightness": float(np.mean(hsv[:, :, 2])),
                        "face_frac": (w * h) / (W * H),
                    })

        rgb_mean = None
        sub_regions = None  # {top, bottom, left, right} → [B, G, R] mean
        if last_box is not None:
            x, y, w, h = last_box
            # Whole-skin sample (forehead+upper face) — used by rPPG and the
            # color-reflection check.
            y0 = max(0, y + int(0.05 * h))
            y1 = min(H, y + int(0.45 * h))
            x0 = max(0, x + int(0.20 * w))
            x1 = min(W, x + int(0.80 * w))
            skin = frame[y0:y1, x0:x1]
            if skin.size > 0:
                rgb_mean = skin.reshape(-1, 3).mean(axis=0).tolist()  # [B, G, R]
            # Sub-regions for directional check: forehead (top) vs lower
            # cheek/chin (bottom), and left half vs right half of face skin.
            top = frame[
                max(0, y + int(0.05 * h)) : min(H, y + int(0.25 * h)),
                max(0, x + int(0.20 * w)) : min(W, x + int(0.80 * w)),
            ]
            bottom = frame[
                max(0, y + int(0.55 * h)) : min(H, y + int(0.80 * h)),
                max(0, x + int(0.20 * w)) : min(W, x + int(0.80 * w)),
            ]
            left = frame[
                max(0, y + int(0.10 * h)) : min(H, y + int(0.50 * h)),
                max(0, x + int(0.10 * w)) : min(W, x + int(0.45 * w)),
            ]
            right = frame[
                max(0, y + int(0.10 * h)) : min(H, y + int(0.50 * h)),
                max(0, x + int(0.55 * w)) : min(W, x + int(0.90 * w)),
            ]
            sub_regions = {}
            for name, patch in (("top", top), ("bottom", bottom),
                                ("left", left), ("right", right)):
                if patch.size > 0:
                    sub_regions[name] = patch.reshape(-1, 3).mean(axis=0).tolist()
        per_frame.append({
            "time_ms": time_ms,
            "rgb_mean": rgb_mean,
            "sub_regions": sub_regions,
        })
    cap.release()

    n_det = len(detections)
    # Detection rate is now relative to MediaPipe attempts, not total frames,
    # so the threshold semantics still hold ("of frames we tried, what % had a
    # face") even though we only run MediaPipe on every Nth frame.
    detection_rate = n_det / max(1, mp_attempts)
    quality: dict = {
        "frames_decoded": decoded,
        "frames_with_face": n_det,
        "mediapipe_attempts": mp_attempts,
        "detection_rate": round(detection_rate, 3),
        "thresholds": {
            "detection_rate_min": QUALITY_DETECTION_RATE_MIN,
            "brightness_min": QUALITY_BRIGHTNESS_MIN,
            "face_frac_min": QUALITY_FACE_FRAC_MIN,
            "detection_score_min": QUALITY_DETECTION_SCORE_MIN,
        },
    }
    if n_det == 0:
        quality.update({"pass": False, "reason": "no face detected"})
        return {"quality": quality, "rppg": None, "probe": None}

    mean_brightness = float(np.mean([d["brightness"] for d in detections]))
    mean_face_frac = float(np.mean([d["face_frac"] for d in detections]))
    mean_score = float(np.mean([d["score"] for d in detections]))
    quality.update({
        "mean_brightness": round(mean_brightness, 1),
        "mean_face_frac": round(mean_face_frac, 3),
        "mean_detection_score": round(mean_score, 2),
    })
    failures: list[str] = []
    if detection_rate < QUALITY_DETECTION_RATE_MIN:
        failures.append(f"face detected in only {detection_rate:.0%} of frames")
    if mean_brightness < QUALITY_BRIGHTNESS_MIN:
        failures.append(f"face too dim (brightness {mean_brightness:.0f}/255)")
    if mean_face_frac < QUALITY_FACE_FRAC_MIN:
        failures.append(f"face too small ({mean_face_frac:.1%} of frame)")
    if mean_score < QUALITY_DETECTION_SCORE_MIN:
        failures.append(f"low detection confidence ({mean_score:.2f})")
    if failures:
        quality.update({"pass": False, "reason": "; ".join(failures)})
        return {"quality": quality, "rppg": None, "probe": None}
    quality["pass"] = True

    # rPPG was historically run on the prefix, but the screen-probe stage
    # already covers every attack class rPPG catches plus more (animated stills,
    # AI-synthesized motion, real-video injection). Dropped from this pipeline
    # to save ~4s of capture time. Function _compute_rppg_from_series is still
    # used by the rPPG-only endpoint for comparison.
    rppg = None

    # Screen probe: per-flash-window channel deltas
    probe = _compute_screen_probe(per_frame, phases, sequence)

    return {"quality": quality, "rppg": rppg, "probe": probe}


def _analyze_video(video_path: str) -> dict:
    """One pass over the video: collect quality stats AND skin RGB time series.

    Returns:
      { "quality": {pass, ..., reason}, "rppg": {pass, ..., reason} | None }
    rppg is None when quality gate failed (we skip running it).
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {
            "quality": {"pass": False, "reason": "could not open video"},
            "rppg": None,
        }

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    decoded = 0
    detections: list[dict] = []
    rgb_means: list[np.ndarray] = []
    last_box: tuple[int, int, int, int] | None = None

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        decoded += 1
        H, W = frame.shape[:2]
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = _MP_FACE.process(rgb_frame)

        if results.detections:
            d = max(
                results.detections,
                key=lambda x: (
                    x.location_data.relative_bounding_box.width
                    * x.location_data.relative_bounding_box.height
                ),
            )
            rb = d.location_data.relative_bounding_box
            x = max(0, int(rb.xmin * W))
            y = max(0, int(rb.ymin * H))
            w = min(int(rb.width * W), W - x)
            h = min(int(rb.height * H), H - y)
            if w > 0 and h > 0:
                last_box = (x, y, w, h)
                face_roi = frame[y : y + h, x : x + w]
                hsv = cv2.cvtColor(face_roi, cv2.COLOR_BGR2HSV)
                detections.append({
                    "score": float(d.score[0]),
                    "brightness": float(np.mean(hsv[:, :, 2])),
                    "face_frac": (w * h) / (W * H),
                })

        # Skin sampling for rPPG (carry forward last-known box if MP missed)
        if last_box is not None:
            x, y, w, h = last_box
            y0 = max(0, y + int(0.05 * h))
            y1 = min(H, y + int(0.45 * h))
            x0 = max(0, x + int(0.20 * w))
            x1 = min(W, x + int(0.80 * w))
            skin = frame[y0:y1, x0:x1]
            if skin.size > 0:
                rgb_means.append(skin.reshape(-1, 3).mean(axis=0))  # [B, G, R]
    cap.release()

    n_det = len(detections)
    detection_rate = n_det / max(1, decoded)

    quality: dict = {
        "frames_decoded": decoded,
        "frames_with_face": n_det,
        "detection_rate": round(detection_rate, 3),
        "thresholds": {
            "detection_rate_min": QUALITY_DETECTION_RATE_MIN,
            "brightness_min": QUALITY_BRIGHTNESS_MIN,
            "face_frac_min": QUALITY_FACE_FRAC_MIN,
            "detection_score_min": QUALITY_DETECTION_SCORE_MIN,
        },
    }

    if n_det == 0:
        quality.update({"pass": False, "reason": "no face detected in any frame"})
        return {"quality": quality, "rppg": None}

    mean_brightness = float(np.mean([d["brightness"] for d in detections]))
    mean_face_frac = float(np.mean([d["face_frac"] for d in detections]))
    mean_score = float(np.mean([d["score"] for d in detections]))
    quality.update({
        "mean_brightness": round(mean_brightness, 1),
        "mean_face_frac": round(mean_face_frac, 3),
        "mean_detection_score": round(mean_score, 2),
    })

    failures: list[str] = []
    if detection_rate < QUALITY_DETECTION_RATE_MIN:
        failures.append(f"face detected in only {detection_rate:.0%} of frames")
    if mean_brightness < QUALITY_BRIGHTNESS_MIN:
        failures.append(f"face too dim (brightness {mean_brightness:.0f}/255)")
    if mean_face_frac < QUALITY_FACE_FRAC_MIN:
        failures.append(f"face too small ({mean_face_frac:.1%} of frame)")
    if mean_score < QUALITY_DETECTION_SCORE_MIN:
        failures.append(f"low detection confidence ({mean_score:.2f})")

    if failures:
        quality.update({"pass": False, "reason": "; ".join(failures)})
        return {"quality": quality, "rppg": None}

    quality["pass"] = True

    # ─── rPPG (only if quality gate cleared) ──────────────────────────
    n = len(rgb_means)
    if n < 30:
        return {
            "quality": quality,
            "rppg": {"pass": False, "reason": f"too few skin samples ({n})", "frames_analyzed": n},
        }

    arr = np.array(rgb_means, dtype=float)
    G = arr[:, 1]  # green channel — primary rPPG carrier

    # Detrend by subtracting a 1s moving average
    win = max(3, int(round(fps * 1.0)))
    kernel = np.ones(win) / win
    G_detrended = G - np.convolve(G, kernel, mode="same")
    edge = win
    if n > 2 * edge:
        G_detrended = G_detrended[edge:-edge]

    g_pre_std = float(np.std(G))
    g_detrend_std = float(np.std(G_detrended))

    # FFT-domain bandpass [0.7, 3.0 Hz]
    N = len(G_detrended)
    spec = np.fft.rfft(G_detrended - G_detrended.mean())
    freqs = np.fft.rfftfreq(N, d=1.0 / fps)
    band_mask = (freqs >= 0.7) & (freqs <= 3.0)
    if not np.any(band_mask):
        return {
            "quality": quality,
            "rppg": {"pass": False, "reason": "video too short for pulse band"},
        }

    spec_bp = np.where(band_mask, spec, 0.0)
    g_bandpassed = np.fft.irfft(spec_bp, n=N)
    g_bp_rms = float(np.sqrt(np.mean(g_bandpassed ** 2)))

    spec_mag = np.abs(spec)
    spec_mag_band = np.where(band_mask, spec_mag, 0.0)
    peak_idx = int(np.argmax(spec_mag_band))
    bpm = float(freqs[peak_idx]) * 60.0

    is_pass = (
        g_bp_rms >= RPPG_GREEN_BANDPASS_RMS_MIN
        and g_detrend_std >= RPPG_GREEN_DETREND_STD_MIN
        and RPPG_BPM_LOW <= bpm <= RPPG_BPM_HIGH
    )

    rppg = {
        "pass": is_pass,
        "bpm": round(bpm, 1),
        "green_pre_std": round(g_pre_std, 3),
        "green_detrend_std": round(g_detrend_std, 3),
        "green_bandpass_rms": round(g_bp_rms, 3),
        "frames_analyzed": n,
        "fps": round(float(fps), 1),
        "thresholds": {
            "green_bandpass_rms_min": RPPG_GREEN_BANDPASS_RMS_MIN,
            "green_detrend_std_min": RPPG_GREEN_DETREND_STD_MIN,
            "bpm_range": [RPPG_BPM_LOW, RPPG_BPM_HIGH],
        },
        "reason": (
            None
            if is_pass
            else (
                f"bandpass_rms={g_bp_rms:.3f} (need≥{RPPG_GREEN_BANDPASS_RMS_MIN}), "
                f"detrend_std={g_detrend_std:.3f} (need≥{RPPG_GREEN_DETREND_STD_MIN}), "
                f"bpm={bpm:.0f}"
            )
        ),
    }
    return {"quality": quality, "rppg": rppg}


def _mint_session(client_id: str, client_secret: str):
    resp = requests.post(
        SESSION_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": _basic(client_id, client_secret),
            "User-Agent": USER_AGENT,
        },
        json={
            "returnUrl": "http://localhost:5050/callback",
            "cancelUrl": "http://localhost:5050/cancel",
        },
        timeout=15,
    )
    body = _json_or_text(resp)
    sid = body.get("sessionId") or body.get("session_id") or body.get("id") if isinstance(body, dict) else None
    return sid, {"status": resp.status_code, "body": body}


def _crop_to_face(frame: np.ndarray, padding: float = 0.20) -> np.ndarray | None:
    """Detect the largest face with MediaPipe and return a tight crop with
    `padding` (fraction of bbox) on each side. Returns None if no face."""
    if frame is None:
        return None
    H, W = frame.shape[:2]
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = _MP_FACE.process(rgb)
    if not results.detections:
        return None
    d = max(
        results.detections,
        key=lambda x: (
            x.location_data.relative_bounding_box.width
            * x.location_data.relative_bounding_box.height
        ),
    )
    rb = d.location_data.relative_bounding_box
    x = int(rb.xmin * W)
    y = int(rb.ymin * H)
    w = int(rb.width * W)
    h = int(rb.height * H)
    pad_x = int(w * padding)
    pad_y = int(h * padding)
    x0 = max(0, x - pad_x)
    y0 = max(0, y - pad_y)
    x1 = min(W, x + w + pad_x)
    y1 = min(H, y + h + pad_y)
    if x1 <= x0 or y1 <= y0:
        return None
    return frame[y0:y1, x0:x1]


def _call_wink_recognize(img_bytes: bytes) -> dict:
    """Mint a session and POST the JPEG to Wink's recognize-face endpoint."""
    cid = os.environ.get("WINK_CLIENT_ID")
    csec = os.environ.get("WINK_CLIENT_SECRET")
    if not cid or not csec:
        return {"error": "WINK_CLIENT_ID / WINK_CLIENT_SECRET not set"}

    sid, mint_meta = _mint_session(cid, csec)
    if not sid:
        return {
            "error": "session mint failed or sessionId not found in response",
            "mint": mint_meta,
        }

    resp = requests.post(
        RECOGNIZE_URL,
        headers={
            "Authorization": _basic(cid, sid),
            "User-Agent": USER_AGENT,
        },
        files={"InputFile": ("selfie.jpg", img_bytes, "image/jpeg")},
        data={
            "RequestType": "1",
            "LivenessRequestMode": "1",
            "ExternalDeviceId": "demo-mac-01",
            "PaymentIntent": "true",
            "threshold": "0.7",
        },
        timeout=30,
    )
    return {
        "http_status": resp.status_code,
        "session_id": sid,
        "wink": _json_or_text(resp),
    }


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/samples/<path:filename>")
def serve_sample(filename):
    """Serve the sample fakecam media files. Used by the Setup Fakecam
    launcher to download the Virat sample video to the tester's machine.
    Restricted to the samples/ subdirectory to prevent path traversal."""
    return send_from_directory(os.path.join(HERE, "samples"), filename)


@app.route("/launcher/<path:filename>")
def serve_launcher(filename):
    """Serve the Setup/Launch fakecam launcher scripts. The "Setup Fakecam"
    and "Launch Fakecam" buttons on the main page link directly to these
    endpoints with `download` attribute, so the browser saves the file
    instead of trying to render it. Sent with attachment Content-Disposition
    so even non-Chromium browsers force a download."""
    # Hint the browser to download by name, not display inline.
    return send_from_directory(
        os.path.join(HERE, "launchers"),
        filename,
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/recognize", methods=["POST"])
def recognize():
    """Phase 2: forward a still frame to Wink with no rPPG gate (the gap demo)."""
    if "image" not in request.files:
        return jsonify({"error": "missing 'image' file"}), 400
    img_bytes = request.files["image"].read()
    return jsonify(_call_wink_recognize(img_bytes)), 200


@app.route("/api/recognize-with-rppg", methods=["POST"])
def recognize_with_rppg():
    """Phase 3: 3-stage pipeline mirroring Wink's production flow.

    Body: multipart with 'video' (webm) — typically a 10s recording.

    Stage A — face quality gate (MediaPipe-based, mirrors Wink's pre-check):
      brightness, face size, detection rate, confidence. Fail → "improve capture".
    Stage B — rPPG: pulse-band signal in green channel skin region. Fail → block.
    Stage C — Wink recognize-face. Only reached when A and B both pass.
    """
    if "video" not in request.files:
        return jsonify({"error": "missing 'video' file"}), 400

    video_bytes = request.files["video"].read()
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(video_bytes)
        video_path = tmp.name

    try:
        analysis = _analyze_video(video_path)
        quality = analysis["quality"]
        rppg = analysis["rppg"]

        if not quality.get("pass"):
            return jsonify({
                "stage": "quality",
                "verdict": "QUALITY_GATE_FAILED",
                "quality": quality,
                "rppg": None,
                "wink_called": False,
            }), 200

        if not rppg or not rppg.get("pass"):
            return jsonify({
                "stage": "rppg",
                "verdict": "BLOCKED_BY_RPPG",
                "quality": quality,
                "rppg": rppg,
                "wink_called": False,
            }), 200

        # Both gates passed — extract a representative frame and call Wink
        cap = cv2.VideoCapture(video_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total // 2))
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            last = None
            while True:
                ok2, f2 = cap.read()
                if not ok2:
                    break
                last = f2
            frame = last
        cap.release()

        if frame is None:
            return jsonify({
                "stage": "frame_extract",
                "verdict": "PASSED_GATES_BUT_NO_FRAME",
                "quality": quality,
                "rppg": rppg,
                "wink_called": False,
            }), 200

        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            return jsonify({
                "stage": "frame_extract",
                "verdict": "JPEG_ENCODE_FAILED",
                "quality": quality,
                "rppg": rppg,
                "wink_called": False,
            }), 200

        wink_result = _call_wink_recognize(jpg.tobytes())
        return jsonify({
            "stage": "wink",
            "verdict": "FORWARDED_TO_WINK",
            "quality": quality,
            "rppg": rppg,
            "wink_called": True,
            **wink_result,
        }), 200
    finally:
        try:
            os.unlink(video_path)
        except OSError:
            pass


@app.route("/api/face-quality", methods=["POST"])
def face_quality():
    """Single-image face-quality check — used by the polished flow's first
    stage. Returns pass/fail with the face bbox in relative coords so the
    client can draw an overlay before the probe phase starts."""
    if "image" not in request.files:
        return jsonify({"error": "missing 'image'"}), 400
    img_bytes = request.files["image"].read()
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify({"error": "could not decode image"}), 400
    H, W = frame.shape[:2]
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = _MP_FACE.process(rgb)
    if not results.detections:
        return jsonify({
            "pass": False,
            "reason": "no face detected — center your face in the frame",
        }), 200
    d = max(
        results.detections,
        key=lambda x: (
            x.location_data.relative_bounding_box.width
            * x.location_data.relative_bounding_box.height
        ),
    )
    rb = d.location_data.relative_bounding_box
    x = max(0, int(rb.xmin * W))
    y = max(0, int(rb.ymin * H))
    w = min(int(rb.width * W), W - x)
    h = min(int(rb.height * H), H - y)
    face_roi = frame[y : y + h, x : x + w]
    hsv = cv2.cvtColor(face_roi, cv2.COLOR_BGR2HSV)
    brightness = float(np.mean(hsv[:, :, 2]))
    score = float(d.score[0])
    face_frac = (w * h) / (W * H)

    failures = []
    if brightness < QUALITY_BRIGHTNESS_MIN:
        failures.append(f"face too dim ({brightness:.0f}/255)")
    if face_frac < QUALITY_FACE_FRAC_MIN:
        failures.append(f"face too small ({face_frac:.1%} of frame)")
    if score < QUALITY_DETECTION_SCORE_MIN:
        failures.append(f"low detection confidence ({score:.2f})")

    # Padded bbox = exact crop the client will send to Wink later, so we
    # don't need to re-detect the face after the probe runs.
    pad_w = rb.width * PROBE_FACE_PAD
    pad_h = rb.height * PROBE_FACE_PAD
    pad_xmin = max(0.0, rb.xmin - pad_w)
    pad_ymin = max(0.0, rb.ymin - pad_h)
    pad_xmax = min(1.0, rb.xmin + rb.width + pad_w)
    pad_ymax = min(1.0, rb.ymin + rb.height + pad_h)

    return jsonify({
        "pass": not failures,
        "reason": "; ".join(failures) if failures else None,
        "bbox": {  # PADDED relative 0..1 coords (face + 20%) — what client displays AND sends to Wink
            "x": float(pad_xmin),
            "y": float(pad_ymin),
            "w": float(pad_xmax - pad_xmin),
            "h": float(pad_ymax - pad_ymin),
        },
        "tight_bbox": {  # tight face bbox, retained for diagnostics
            "x": float(rb.xmin),
            "y": float(rb.ymin),
            "w": float(rb.width),
            "h": float(rb.height),
        },
        "padding_fraction": PROBE_FACE_PAD,
        "brightness": round(brightness, 1),
        "face_frac": round(face_frac, 3),
        "detection_score": round(score, 2),
    }), 200


@app.route("/api/probe-challenge", methods=["POST"])
def probe_challenge():
    """Server issues a randomized 3-slot sweep-probe challenge.

    Each slot is {color, direction}. The client renders a sweeping bright
    band across the capture stage; the server later verifies (a) the color
    reflected in the matching skin channel and (b) the band's sweep direction
    is observable as a peak-time gradient between mirror sub-regions of skin.
    Both color and direction are random per call — a pre-recorded clip can't
    have known today's secret.
    """
    # Wink-branded palette, full-fill mode. Each slot bathes the entire
    # surround in the slot's color — strongest possible reflection per slot
    # and a stable camera scene. Position-based asymmetry is dropped because
    # uniform illumination produces no left/right gradient on the face;
    # security comes from the color-dominance check (recording attacks fail
    # because their expected channel doesn't rise vs the gray baseline).
    # 3 slots for the 2-of-3 majority gate (more forgiving than strict 2-of-2,
    # since slot-N camera adaptation can dampen one slot's signal). Timings
    # are tightened for speed: at 30fps client-side sampling, an 800ms slot
    # gives ~24 stable samples per color and a 300ms baseline gives ~9 frames
    # of neutral baseline mean — both comfortably above the noise floor.
    color_names = list(WINK_COLORS.keys())
    random.shuffle(color_names)
    sequence = [
        {"color": name, "render": WINK_COLORS[name]["render"]}
        for name in color_names
    ]
    return jsonify({
        "sequence": sequence,
        "slot_ms": 1000,        # 800 was too tight — camera needs ~250ms to settle exposure on color change, leaving only ~550ms of stable signal; 1000 gives ~750ms stable
        "baseline_ms": 350,     # short enough to be fast, long enough for camera to reset between slots
        "prefix_ms": 50,
        "post_buffer_ms": 100,
        "fill_mode": "full",
    }), 200


@app.route("/api/wink-recognize", methods=["POST"])
def wink_recognize_only():
    """Standalone Wink recognize call. Used by the polished verify flow to
    run Wink's recognize+passive-liveness in parallel with the screen probe,
    saving ~1-2s of perceived UX time."""
    if "face_jpeg" not in request.files:
        return jsonify({"error": "missing 'face_jpeg' file"}), 400
    face_bytes = request.files["face_jpeg"].read()
    return jsonify(_call_wink_recognize(face_bytes)), 200


@app.route("/api/recognize-with-probe", methods=["POST"])
def recognize_with_probe():
    """Stacked pipeline: quality → rPPG (prefix only) → screen probe → Wink.

    Body: multipart with
      video    — webm capture covering rPPG prefix + flash sequence
      phases   — JSON array of {kind, color, start, end} (ms relative to
                 recording start) describing what the client played and when
      sequence — JSON array of color tokens from /api/probe-challenge
    """
    if "video" not in request.files:
        return jsonify({"error": "missing 'video' file"}), 400
    try:
        phases = json.loads(request.form.get("phases", "[]"))
        sequence = json.loads(request.form.get("sequence", "[]"))
    except (json.JSONDecodeError, TypeError):
        return jsonify({"error": "invalid phases/sequence JSON"}), 400
    skip_wink = request.form.get("skip_wink", "").lower() in ("1", "true", "yes")
    if not isinstance(phases, list) or not isinstance(sequence, list):
        return jsonify({"error": "phases and sequence must be JSON arrays"}), 400

    video_bytes = request.files["video"].read()
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(video_bytes)
        video_path = tmp.name

    try:
        analysis = _analyze_video_with_probe(video_path, phases, sequence)
        quality = analysis["quality"]
        rppg = analysis["rppg"]
        probe = analysis["probe"]

        if not quality.get("pass"):
            return jsonify({
                "stage": "quality",
                "verdict": "QUALITY_GATE_FAILED",
                "quality": quality, "rppg": None, "probe": None,
                "wink_called": False,
            }), 200
        if not probe or not probe.get("pass"):
            return jsonify({
                "stage": "probe",
                "verdict": "BLOCKED_BY_SCREEN_PROBE",
                "quality": quality, "rppg": rppg, "probe": probe,
                "wink_called": False,
            }), 200

        # If the client is running Wink in parallel via /api/wink-recognize,
        # short-circuit and just return the probe verdict — caller will merge.
        if skip_wink:
            return jsonify({
                "stage": "probe_only",
                "verdict": "PROBE_PASSED",
                "quality": quality, "rppg": rppg, "probe": probe,
                "wink_called": False,
            }), 200

        # All gates passed — prefer the client-supplied pre-cropped face JPEG
        # (already cropped to the padded bbox we returned from /api/face-quality).
        # Falls back to extracting a frame and re-cropping with MediaPipe if the
        # client didn't send one (back-compat for the dev buttons).
        face_crop_source = None
        face_jpeg_bytes = None
        if "face_jpeg" in request.files:
            face_jpeg_bytes = request.files["face_jpeg"].read()
            face_crop_source = "client"

        if face_jpeg_bytes is None:
            cap = cv2.VideoCapture(video_path)
            cap.set(cv2.CAP_PROP_POS_MSEC, 3000)
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            cap.release()
            if frame is None:
                return jsonify({
                    "stage": "frame_extract",
                    "verdict": "PASSED_GATES_BUT_NO_FRAME",
                    "quality": quality, "rppg": rppg, "probe": probe,
                    "wink_called": False,
                }), 200
            face_crop = _crop_to_face(frame, padding=0.20)
            out_frame = face_crop if face_crop is not None else frame
            ok, jpg = cv2.imencode(".jpg", out_frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                return jsonify({
                    "stage": "frame_extract",
                    "verdict": "JPEG_ENCODE_FAILED",
                    "quality": quality, "rppg": rppg, "probe": probe,
                    "wink_called": False,
                }), 200
            face_jpeg_bytes = jpg.tobytes()
            face_crop_source = "server-mediapipe-fallback"

        wink_result = _call_wink_recognize(face_jpeg_bytes)
        return jsonify({
            "stage": "wink",
            "verdict": "FORWARDED_TO_WINK",
            "quality": quality, "rppg": rppg, "probe": probe,
            "face_crop_source": face_crop_source,
            "wink_called": True,
            **wink_result,
        }), 200
    finally:
        try:
            os.unlink(video_path)
        except OSError:
            pass


if __name__ == "__main__":
    if not (os.environ.get("WINK_CLIENT_ID") and os.environ.get("WINK_CLIENT_SECRET")):
        print("ERROR: export WINK_CLIENT_ID and WINK_CLIENT_SECRET first", file=sys.stderr)
        sys.exit(1)
    port = int(os.environ.get("PORT", "5050"))
    # When PORT is supplied by the environment we're almost certainly in a
    # cloud runtime (Render, Fly, etc.) and need to accept connections on
    # all interfaces. When PORT is unset we're running locally and binding
    # to loopback only is the right default.
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print(f"\n  Wink liveness gap demo")
    print(f"  http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    print(f"  Open in regular Chrome for control, or via fakecam.sh for the attack\n")
    app.run(host=host, port=port, debug=False)
