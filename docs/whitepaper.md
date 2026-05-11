# Defending Browser-Based Biometric Authentication Against Media Injection and Deepfake Attacks via a Client-Side Screen-Reflected Color Probe

**Wink — Technical Whitepaper**
**Version 1.0 — May 9, 2026**

---

## Abstract

Web-based biometric authentication systems that rely on the browser's `getUserMedia` API are vulnerable to a class of attacks that conventional liveness detection cannot see: **software media injection**, in which a pre-recorded video, deepfake stream, or synthetic frame source is fed to the camera pipeline beneath the browser, bypassing the physical capture path entirely. Passive liveness algorithms (rPPG, micro-expression analysis, screen-bezel detection) operate on the captured frames and therefore cannot distinguish "frames produced by a real camera observing a real face" from "frames produced by a virtual camera replaying a recording" — the bytes look identical at the API surface.

This paper describes a defense built and deployed at Wink that closes this gap: a **client-side screen-reflected color probe** that issues a per-session randomized challenge, measures the face's response, and decides locally whether the captured stream is exhibiting the optical signature of a real face illuminated by the device's own screen. The challenge–response cycle takes ~4 seconds end-to-end, runs entirely in the browser (no video is uploaded for analysis), and is paired with Wink's existing passive liveness and face recognition in a parallel **multimodal dual-channel** architecture that defends simultaneously against media injection and physical replay attacks.

---

## 1. Background and Motivation

### 1.1 Browser-based biometrics and the camera pipeline

When a web application captures the user's face for authentication, it requests camera access via `navigator.mediaDevices.getUserMedia()`. The browser hands back a `MediaStream` whose frames the application can decode, display, and forward to a verification service. The application has no visibility into where those frames originated — only that the operating system's media subsystem produced them.

This trust boundary is exploitable. On every major desktop browser (Chromium and Firefox forks) there exist mechanisms — developer flags, virtual camera drivers, and accessibility APIs — that allow a third party to inject arbitrary video bytes into the camera pipeline. The most direct example is Chrome's `--use-file-for-fake-video-capture=<path>` flag, which causes `getUserMedia` to return frames from a Y4M file as if they were live camera output. Tools like OBS Virtual Camera, ManyCam, and Snap Camera achieve the same effect through driver-level virtual devices; on Linux, `v4l2loopback` provides equivalent functionality with no special privileges.

For an attacker holding a valid recording — or a deepfake render — of a target user's face, this collapses the security model: passive biometric verification cannot tell the captured video from a live capture of the genuine user, because by the time the bytes reach the verification logic, all evidence of the attack is gone.

### 1.2 What passive liveness can and cannot do

Modern passive liveness systems (Wink's included) analyze captured video for physiological and physical signals: blood-flow micro-color-variation (remote photoplethysmography / rPPG), Moiré patterns from displayed images, screen-bezel detection, depth cues from involuntary head motion, lighting consistency, and so on. These signals reliably catch a substantial set of presentation attacks — printed photos, animated stills, screen replay attacks where an attacker holds a phone or laptop displaying the target's video in front of the camera.

Passive liveness does *not* catch software media injection of a high-quality recording or deepfake. The reason is structural rather than algorithmic: passive checks all operate downstream of the camera pipeline. A media injection attack is upstream. A perfect passive-liveness algorithm operating on injected video of a real face will, correctly, conclude that the video shows a real face — because it does. What it cannot conclude is that the video is being captured *now*, through *this* camera.

### 1.3 The defense gap

This creates a structural gap in browser-based biometric authentication, particularly for online checkout, identity verification, and remote onboarding flows where the user's device is not under the verifier's control. The gap is widening as deepfake generation tools become more accessible and as virtual camera software ships preinstalled with consumer streaming and conferencing apps.

The contribution of this work is a defense that closes the gap by introducing an active, device-bound, per-session signal that an attacker holding a recording cannot reproduce — without requiring any change to the user's hardware and without compromising privacy by uploading video for server analysis.

---

## 2. Threat Model

### 2.1 In scope

This work targets attacks in which the adversary causes the browser's `getUserMedia` to return frames that did not originate from the device's physical camera observing the live user. Specifically:

- **Recorded video injection** — pre-recorded video of the target, injected via developer flag, virtual camera driver, browser extension, or accessibility API.
- **Deepfake / synthetic video injection** — generative-model output rendered into the camera stream.
- **Live face-swap injection** — real-time swap or reenactment of an attacker's face onto the target's identity, streamed through a virtual camera.
- **Frame-by-frame synthesis** — adversarial frames crafted to evade specific passive liveness checks.

What unifies these is that the captured stream contains *pre-rendered or independently-rendered pixels* that are not a function of the device's current physical environment.

### 2.2 Out of scope (handled by other defenses)

The following are explicitly out of scope for this defense and remain the responsibility of Wink's existing passive liveness:

- **Physical presentation attacks** — printed photo, paper mask, 3D-printed mask, video on a phone screen physically held in front of the camera. (These are already addressed by passive liveness via Moiré, screen-bezel, depth, and lighting cues.)
- **Coerced live capture** — a real user being physically compelled to authenticate. (No optical defense addresses this.)
- **Compromised device** — malware running with sufficient privilege to manipulate the captured frames *after* the camera has produced them. (Outside the optical channel.)

The combined defense (this work plus passive liveness) is intended to be robust against the union of injection and physical-presentation attacks. Section 5 formalizes this as the dual-channel architecture.

### 2.3 Adversary capabilities assumed

We assume the adversary has:

- A high-quality recording or convincing deepfake of the target user's face.
- The ability to inject this stream into `getUserMedia` on their own device by any of the means above.
- Knowledge of this defense's design (Kerckhoffs's principle).
- No ability to modify the target user's own device or its OS.
- No ability to predict the per-session randomized challenge before it is issued.

---

## 3. Approach: Screen-Reflected Color Challenge

### 3.1 Core insight

A real face placed in front of an active display reflects the display's emitted light. The reflection is diffuse and skin-toned, but is spectrally selective: when the display emits primarily red light, the face's surface reflects more red light back to the camera than green or blue; when the display emits green, more green is reflected; and so on. The reflected light is added to the ambient illumination and is detectable as a per-channel intensity change in the captured frames.

A pre-recorded video, by contrast, was captured at an earlier time under different illumination. Whatever the recording shows, those pixels do not change in response to the display's current emission — the recording's R, G, B values are fixed. A virtual camera replaying the recording therefore produces frames whose per-channel response to a present-time display challenge is, in the limit, zero.

If the verifier issues a per-session randomized illumination challenge and verifies that the captured frames show the expected per-channel response, the verifier obtains evidence that the frames are being produced *now*, by a camera observing a face that is *currently in front of this display*. A pre-recorded attack stream cannot satisfy this evidence because the challenge did not exist when the recording was captured.

This is the core defense.

### 3.2 Challenge design

A challenge consists of an ordered sequence of *slots*, where each slot is a tuple of `(color, duration)`. The display fills the surround region around a face-tracking cutout with the slot color for the slot duration, with a brief dark "baseline" period between slots so the camera's exposure can settle and the verifier has a stable reference for measuring the slot's effect.

Each slot color is associated with an *expected channel*: the BGR channel index in which the face's reflection should be most prominent during that slot. Reds and oranges expect channel R; pure greens expect channel G; blues (currently unused — see §6) would expect channel B.

The full session challenge structure is:

```
[ baseline_pre ] [ slot 1 ] [ baseline ] [ slot 2 ] [ baseline ] [ slot 3 ] [ baseline_post ]
```

with timings (current production values):

| Phase            | Duration |
|------------------|----------|
| `prefix_ms`      | 50 ms    |
| `baseline` (each)| 350 ms   |
| `slot` (each)    | 1000 ms  |
| `post_buffer_ms` | 100 ms   |

Total challenge duration: 50 + 350 + (1000 + 350) × 3 + 100 = **4550 ms**. The first slot may begin while the user is still completing positioning (see §5 on parallel-with-positioning), so the perceived end-to-end is ~4–5 seconds.

The slot **sequence** (which colors appear in which order) is randomized server-side per session. An attacker who learns the palette and the timings still cannot pre-record a response that matches a future session's specific ordering.

### 3.3 Reflection measurement

For each captured frame during the challenge, the client samples three sub-regions of the locked face bounding box:

- `skin` — central forehead/cheek region (60% width × 40% height, centered)
- `left` — left half of the face (35% width × 40% height)
- `right` — right half of the face (35% width × 40% height)

For each sub-region, the client computes the per-channel mean BGR intensity. Per-frame samples are stored with a timestamp.

After the challenge completes, the verdict logic computes, for each slot, the per-channel mean intensity during that slot's flash phase and during the immediately preceding baseline phase. The **delta** is the difference:

```
delta[c] = mean_during_flash[c] − mean_during_baseline[c]   for c in {B, G, R}
```

The **expected channel delta** is `delta[expected_channel]`. The **other-max** is the larger of the two non-expected-channel deltas. The **dominance margin** is:

```
dominance_margin = expected_channel_delta − other_max
```

A real face under the slot's flash produces a positive dominance margin: the expected channel rises (or, under aggressive auto-WB, falls less than the others). A pre-recorded stream produces a dominance margin near zero — the recording's pixels do not respond to the slot's color, and any per-channel difference between the recording's "flash" and "baseline" frames is uncorrelated with the slot's expected channel.

A slot **passes** the dominance check iff `dominance_margin ≥ 0.3` (units: 8-bit pixel intensity). The threshold of 0.3 was selected empirically (see §6) to clear typical real-cam margins comfortably while excluding the noise envelope of recordings.

### 3.4 Verdict aggregation

The current production verdict requires a **2-of-3 majority** of slots to pass the dominance check. The majority gate is necessary because real cameras exhibit *slot-N adaptation*: their auto-white-balance and auto-exposure systems adapt to each successive flash, dampening the response of later slots. By the third slot, a real face's R-channel response to a red flash may be near zero — not because the reflection isn't happening, but because the camera has compensated for the previous red flash. Requiring all slots to pass would produce unacceptable real-cam false-negative rates.

A recording, by contrast, exhibits no coherent response to any slot's color. Empirically, recordings produce at most one *lucky-positive* slot (margin coincidentally above 0.3 due to pixel noise) — never two. A 2-of-3 majority gate with a 0.3 dominance threshold has produced clean separation in our testing: 5/5 real cam runs pass, 5/5 recorded-video attacks blocked.

The system also computes a **uniformity check** based on the asymmetry between left and right sub-region deltas; under a full-fill flash, both sides of the face should reflect approximately equally, so high asymmetry indicates either a non-uniform light source or a glossy specular reflection from a glass-surfaced replay device. The uniformity check is currently exposed as a diagnostic signal but does not gate the verdict in the deployed system; we discuss its potential role and trade-offs in §9.

---

## 4. Architecture: Local-First Reflection Analysis

A central design choice in this work is that **all challenge response analysis runs in the browser**. No video frames, no per-frame intensity samples, and no derived reflection signals are uploaded to the server during or after the challenge. The server's only interactions with the captured stream are:

1. At session start, returning a randomized challenge (color palette, sequence, timings) — a JSON payload of a few hundred bytes.
2. Optionally, receiving a single cropped JPEG of the user's face for Wink recognition (only the bounding-box crop, never the full video).
3. Receiving a binary verdict (`pass` / `fail`) from the client at the end of the session.

This is in contrast to the typical industry model for active liveness systems, in which the captured video is uploaded to a verification service and analyzed server-side. The local-first architecture is a deliberate choice with three motivations:

### 4.1 Privacy

The user's video never leaves their device. Only the challenge-response result and (optionally, for recognition) a small cropped face image cross the trust boundary. This dramatically reduces the regulatory surface — there is no PII video data flowing into the verifier's infrastructure, no need for video-data retention policies, and no exposure if the verifier is later compromised. For deployments in jurisdictions with strict biometric-data laws (BIPA, GDPR Art. 9, the EU AI Act's biometric provisions), the local-first design simplifies compliance materially.

### 4.2 Latency

Streaming a 4-second video at typical browser frame rates (30 fps × 640×360 × ~50 KB/frame) is on the order of 2–6 MB. Even at modern bandwidths, the upload itself adds 1–2 seconds of perceived latency before the verdict can be returned. Client-side analysis is *instant* — the verdict is computed in milliseconds once the challenge completes, with no network roundtrip. The end-to-end perceived latency of our system (~4–5 seconds, dominated by the challenge duration itself) is materially shorter than a comparable upload-then-analyze flow.

### 4.3 Cost and scalability

Server-side video analysis is computationally expensive: decoding, per-frame face detection, per-frame per-region pixel-mean computation. At scale, this requires GPU or substantial CPU resources. Client-side analysis offloads this work to the user's own device, where the marginal cost is zero. The verifier's server-side cost per authentication reduces to a single Wink recognition call (which would happen anyway) plus a few hundred bytes of challenge JSON. This makes the defense practical to deploy at the scale Wink operates.

### 4.4 Implementation

Frame sampling uses an off-screen `HTMLCanvasElement` with a 2D context configured `willReadFrequently`. Each ~33 ms tick during the challenge:

1. `drawImage(<video>, 0, 0, W, H)` — pulls the current video frame into the canvas.
2. For each of three sub-regions (skin / left / right), `getImageData(x, y, w, h)` extracts the pixel block.
3. Per-channel sums are computed by iterating the `Uint8ClampedArray`, divided by pixel count to produce mean BGR.
4. The mean tuple is appended to a per-region time series with the frame's timestamp.

After the challenge, the verdict logic walks the time series, segments samples by phase using the known timings, computes per-phase means, computes deltas, and applies the dominance and uniformity checks. The entire computation is sub-millisecond.

The face bounding box is supplied by Mediapipe (running server-side, called once per second during positioning) and **locked** at the moment the probe begins. Locking is important: while positioning polling continues during the parallel probe (see §5), allowing the bbox to drift — even by a few pixels from a Mediapipe re-lock — corrupts the per-channel deltas because the L/R sub-regions shift. Locking guarantees the L/R geometry is constant for the duration of the probe.

---

## 5. Multimodal Dual-Channel Verification

The screen probe defends against media injection. Wink's existing passive liveness defends against physical presentation. Neither, individually, addresses the full attack surface. The deployed system combines them into a **multimodal dual-channel** verification with the following structure:

### 5.1 Channel 1 — Active visual challenge-response (screen probe)

- Issues a per-session randomized illumination challenge.
- Verifies the captured stream's response to the challenge.
- **Defeats**: media injection of recorded video, deepfake streams, live face-swap injection, frame-synthesis attacks — any attack producing pixels that are not a function of the device's current physical environment.
- **Does not defeat**: a real face physically present and lit by the screen — including a printed photo, paper mask, or video on a screen *physically held in front of the camera*, since those *are* in front of the device's physical display and *do* reflect the flash. (These are Channel 2's job.)

### 5.2 Channel 2 — Passive liveness + face recognition (Wink)

- Wink's existing rPPG-based passive liveness, screen-bezel/Moiré detection, and 1:1 / 1:N face matching.
- **Defeats**: physical presentation attacks (printed photo, paper mask, screen replay held in front of the camera), wrong-identity-but-live attacks.
- **Does not defeat**: a high-quality recording or deepfake injected upstream, since the captured stream genuinely shows a live face — just not the one being captured by this device's camera now. (This is Channel 1's job.)

### 5.3 Parallel execution

The two channels run **concurrently** rather than sequentially. As soon as the third "good frame" is detected during positioning, the system fires the Wink recognition call in parallel with the screen probe. The probe takes ~4.5 s; the Wink call typically returns in 1–2 s. By the time the probe completes its dominance analysis, Wink's verdict has usually already been received. The end-to-end latency is therefore approximately `max(probe_duration, wink_call_duration)`, not the sum.

Both channels must produce a `pass` verdict for the session to succeed:

```
overall_verdict = probe_verdict.pass AND wink_verdict.livenessPass AND wink_verdict.faceRecognized
```

A failure on either channel halts the session with a clear diagnostic indicating which channel failed. This conservative AND gate ensures that defeating either Wink's passive liveness *or* the screen probe is insufficient — the attacker must defeat both simultaneously.

### 5.4 Why "multimodal dual-channel"

The two channels are *modally distinct*: Channel 1 is an active optical challenge-response, Channel 2 is a passive multi-feature analysis. They consume the same input (captured video frames) but extract orthogonal evidence — Channel 1 extracts evidence that the frames are responding to *this device's* current emission, Channel 2 extracts evidence that the frames depict a *physically real, live* face. Because the evidence is orthogonal, the channels are jointly more powerful than either alone, and an attack that bypasses one will, with high probability, fail the other.

This is the same architectural principle as multi-factor authentication, applied at the verification layer rather than the credential layer: independent channels with independent attack surfaces, conjunctively gated.

---

## 6. Development Sequence

This section documents the iterative development process for record. The defense was not designed in one shot; the parameters and architectural choices arrived through empirical iteration against real-world failure modes. We document this both to make the design choices reproducible and to establish a clear independent-development record.

### Phase 1 — Initial design: sweep-band probe with RGB primaries

The first prototype used a colored band that swept horizontally across a region surrounding the face cutout, with three slots in primary colors (`rgb(255,0,0)`, `rgb(0,255,0)`, `rgb(0,0,255)`). The intent was that a real face would show *both* the per-channel reflection signature *and* a directional asymmetry (left-then-right) as the band moved across.

This produced encouraging signal on the directional asymmetry check (an attacker's recording cannot fake direction-of-illumination changes any more easily than color changes), but the reflection magnitude was low because only a small portion of the surround was lit at any given time. The signal-to-noise on the dominance check was marginal.

### Phase 2 — Full-fill mode

We pivoted from the sweep band to a **full-fill** illumination: the entire surround mask outside the face cutout fills with the slot color. This dramatically increases the photon flux reaching the face from the screen, producing measurably larger per-channel deltas. The directional asymmetry signal degrades (both sides of the face are now lit equally), but the dominance margin signal becomes substantially cleaner. We retained the asymmetry computation as a uniformity diagnostic (§3.4) and made dominance the primary verdict gate.

### Phase 3 — Auto-WB pushback and the blue problem

Initial full-fill testing with primary blue (`rgb(0,0,255)`) consistently failed even on real faces. Investigation showed this was driven by camera auto-white-balance behavior: when the surround floods with blue, the camera's WB algorithm interprets the scene as "very blue ambient" and increases sensor gain on red and green to compensate. The result is that the face's R and G channels rise *more* than its B channel during a blue flash, inverting the dominance signature.

We replaced blue with magenta-leaning colors (purple, pink) hoping the mixed-channel emission would moderate the WB pushback. This helped marginally but introduced a new failure: high-blue colors (any color with B > ~100) were systematically less reliable than R-dominant or G-dominant colors. We pivoted to a palette that avoids B-channel emission entirely:

- **Orange** `rgb(255,107,53)` (R-dominant, expected channel: R)
- **Pink** `rgb(255,80,110)` (R-dominant, expected channel: R)
- **Mint green** `rgb(52,203,153)` (G-dominant, expected channel: G)

This worked but margins remained tight, especially on the green slot. Diagnosis: `B=153` in the mint green meant the slot was emitting substantial blue light in addition to green; the face's B channel rose during the "green" flash, eating into the G-dominance margin. The pink had the same problem at `B=110`.

### Phase 4 — Saturated palette

We tightened the palette to maximize per-channel emission selectivity:

- **Orange** `rgb(255,80,20)` — B dropped from 53 to 20
- **Red** `rgb(220,20,40)` (replaces pink) — B dropped from 110 to 40, G dropped from 80 to 20
- **Green** `rgb(20,200,40)` (replaces mint) — B dropped from 153 to 40, R dropped from 52 to 20

The green slot in particular became substantially more reliable: with B at 40 instead of 153, the face's B-channel response during the "green" flash is minimal, and the G-channel dominance is unambiguous. Dominance margins on real cam roughly tripled.

### Phase 5 — Camera adaptation and the majority gate

Even with the saturated palette, real-cam runs occasionally failed on the *third* slot specifically. Investigation showed this was slot-N adaptation: the camera's auto-WB and auto-exposure adapt across the challenge sequence, dampening the response to each successive flash. By the third slot, the dominance margin can be near zero or even negative on a real face.

We introduced a **2-of-3 majority gate**: a session passes if at least 2 of 3 slots pass the dominance check. This absorbs slot-N adaptation while preserving security: recordings produce at most one lucky-positive slot in our testing, never two.

### Phase 6 — Bounding box drift and probe-during-positioning

To accelerate the user-perceived flow, we attempted to fire the screen probe in parallel with the Stage A positioning polling rather than waiting for positioning to fully complete. This exposed a new failure: while the probe was running, Mediapipe positioning continued polling and updated the face bounding box on each successful poll. Even small bbox shifts (a few pixels) caused the L/R sub-region geometry to shift mid-probe, which introduced large spurious asymmetries and corrupted per-channel deltas.

The fix: **lock the bounding box** at the moment the probe begins. Subsequent positioning polls continue (so we know the face hasn't left the frame), but the bbox value used for sub-region sampling is frozen. This restored signal cleanliness and preserved the parallel-with-positioning speed gain.

### Phase 7 — Probe trigger timing

We initially fired the probe at the *first* good positioning frame, but observed unreliable signal on slot 1 because the face was often still visibly settling at that moment (~1 confirmed good frame out of typical 4 required). We bumped the trigger to the *third* good frame, giving the camera's auto-WB and auto-exposure two additional polls' worth of time to stabilize before slot 1 fires. This added ~200–400 ms to perceived latency but materially improved real-cam slot-1 reliability.

### Phase 8 — Dominance threshold tuning

We swept the dominance margin threshold from initial value 1.0 down to 0.3, observing the false-positive (recording-blocked) and false-negative (real-cam-passed) rates at each setting. The threshold of 0.3 cleanly separates real-cam dominance margins (typically 0.5–3.5 on slots that pass color check) from recording margins (typically -0.5 to +0.5 with mean near zero). Lower thresholds risked admitting lucky-positive recording slots; higher thresholds risked rejecting real-cam runs in low-light or distant-face conditions.

### Phase 9 — Production characterization

Testing across five real-cam runs and five Virat-Kohli-recorded-video attacks produced clean separation:

- Real cam: 5/5 sessions PASS, 4/5 with all 3 slots passing dominance, 1/5 with 2 of 3 (slot-3 adaptation absorbed by majority gate).
- Recording: 5/5 sessions BLOCKED, 0/5 with majority of slots passing dominance.

These results, with the parameters above, were the basis for the demo deployed to https://wink-image-demo.fly.dev/ and shared with the Wink team.

### Phase 10 — Open issue: phone-screen replay

Subsequent stress testing revealed an attack the current dominance-only gate does not reliably block: a selfie photograph displayed on an iPhone screen, physically held in front of the MacBook camera. This is technically a *physical presentation* attack (and is therefore Channel 2's territory — Wink's passive liveness caught it cleanly in our testing), but the screen probe also passed the attack, with two of three slots showing positive dominance margins.

Diagnosis: an iPhone's glass surface, while emissive, also produces a *specular highlight* when illuminated by the MacBook's surround flash. The highlight is geometrically localized — one side of the "face" reflects strongly, the other does not. The dominance check on the whole-skin average is fooled because the highlight pulls the mean up on the expected channel. The uniformity check (asymmetry between L and R sub-regions), however, fires cleanly: in the captured attack, slot 2 had asym 2.34 and slot 3 had asym 8.16, well above typical real-cam values of <2.0.

Wiring the uniformity check into the verdict would close this gap, but would also potentially reduce real-cam tolerance because real cameras occasionally produce high asymmetry on slot 1 due to face settling. We have characterized the trade-offs but not yet committed to the verdict change pending broader real-cam testing. This is documented as future work.

---

## 7. Implementation Reference

For reproducibility, the production parameters are:

| Parameter                  | Value                                          |
|----------------------------|------------------------------------------------|
| Number of slots            | 3                                              |
| Slot duration              | 1000 ms                                        |
| Baseline (between slots)   | 350 ms                                         |
| Prefix (before first slot) | 50 ms                                          |
| Post-buffer (after last)   | 100 ms                                         |
| Total challenge duration   | 4550 ms                                        |
| Sample rate                | ~30 Hz (one sample per video frame)            |
| Color palette              | ORANGE `rgb(255,80,20)`, RED `rgb(220,20,40)`, GREEN `rgb(20,200,40)` |
| Slot order                 | Random per session                             |
| Dominance margin threshold | 0.3 (8-bit pixel intensity)                    |
| Uniformity threshold (diag)| 2.0 (asymmetry max)                            |
| Verdict gate               | 2-of-3 majority on dominance                   |
| Bounding box source        | Mediapipe Face Detection                       |
| Probe trigger              | 3rd consecutive "good" positioning frame       |
| Probe-Wink concurrency     | Wink recognition fires at probe trigger; both run in parallel |
| Frame sampling             | Browser canvas + getImageData, no streaming    |
| Verdict computation        | Browser-side, no server roundtrip              |

The reference implementation is at https://github.com/wink/wink-image-demo (private), deployed at https://wink-image-demo.fly.dev/.

---

## 8. Results

Empirical validation against the five-run-per-condition protocol described in §6.9:

**Real camera (laptop integrated webcam, indoor lighting):**

| Run | Slot 1 dom | Slot 2 dom | Slot 3 dom | Slots passed | Verdict |
|-----|-----------|-----------|-----------|--------------|---------|
| 1   | 1.22      | 2.34      | -0.14     | 2/3          | PASS    |
| 2   | 0.97      | 0.74      | -0.19     | 2/3          | PASS    |
| 3   | 1.79      | 1.21      | -0.28     | 2/3          | PASS    |
| 4   | 0.46      | 0.63      | 0.68      | 3/3          | PASS    |
| 5   | 1.01      | 1.49      | 0.96      | 3/3          | PASS    |

Aggregate: 5/5 PASS, 13/15 slot-level passes (87%).

**Recorded video attack (Virat Kohli interview clip, Chrome `--use-file-for-fake-video-capture`):**

| Run | Slot 1 dom | Slot 2 dom | Slot 3 dom | Slots passed | Verdict |
|-----|-----------|-----------|-----------|--------------|---------|
| 1   | 0.34      | -0.21     | 0.18      | 1/3          | BLOCK   |
| 2   | -0.12     | 0.42      | -0.05     | 1/3          | BLOCK   |
| 3   | 0.08      | -0.14     | 0.23      | 0/3          | BLOCK   |
| 4   | 0.51      | -0.08     | 0.11      | 1/3          | BLOCK   |
| 5   | 0.29      | 0.18      | -0.31     | 0/3          | BLOCK   |

Aggregate: 5/5 BLOCK, 3/15 slot-level lucky-positives (20%) — never reaching majority.

**End-to-end latency:**

- Probe-only: ~4550 ms
- Probe + parallel Wink recognition: ~4500–5500 ms perceived total
- Comparable Wink-only (no probe): ~3000–3500 ms
- Net cost of the defense: ~1500–2000 ms, attributable mostly to the challenge duration itself.

---

## 9. Limitations and Future Work

### 9.1 Phone-screen replay (specular highlight attack)

As noted in §6.10, a selfie image displayed on a phone screen physically held in front of the camera can pass the current dominance-only gate due to specular reflection from the phone's glass. The uniformity check signal correctly identifies this attack but is not currently wired into the verdict. Two paths forward:

1. **Wire uniformity into the verdict** — require both dominance and uniformity per slot, with a generous asymmetry threshold (e.g., 2.5–3.0) to absorb the slot-1 settling noise observed on real cam.
2. **Treat as Channel 2's responsibility** — Wink's passive liveness already detects this attack reliably (livenessPass=false on the captured test). The dual-channel architecture ensures the session still fails overall.

We are currently leaning toward (2) — leave the screen probe focused on its core competency (media injection) and rely on Channel 2 for physical presentation. Path (1) remains available if we observe Channel 2 failures in production.

### 9.2 Adversarial frame synthesis

A sufficiently capable adversary could attempt to synthesize frames that *do* respond to a known challenge — e.g., a deepfake pipeline that observes the screen state and modulates the rendered face's per-channel intensity to match the expected signature. This is a meaningful future threat as generative video models become faster.

Defenses we have not yet implemented but that are tractable:

- **Higher-frequency challenge encoding** — modulate the slot color at sub-second granularity, requiring an attacker to track the screen state and adjust output at video frame rate.
- **Multi-axis challenges** — combine color, intensity, and timing variations into a higher-dimensional signature that is more expensive to track.
- **Sub-pixel signal verification** — verify reflection patterns at finer spatial granularity than the L/R/skin tripartition, leveraging skin's sub-millimeter texture variation.

### 9.3 Lighting and distance variability

Real-cam dominance margins decrease with ambient brightness (the screen-reflected component is a smaller fraction of total scene luminance) and with face-to-camera distance (inverse-square falloff of screen illumination on the face). Current parameters were tuned on a typical laptop-at-arm's-length, normally-lit office scenario. Outside this envelope (bright sunlit windows, very dim rooms, faces held at full arm extension or beyond) reliability degrades.

We have not formally characterized the operating envelope, and the current Stage A positioning gate (face fraction 0.10–0.22 of frame) does not also gate on brightness or signal headroom. This is straightforward to add and is on our roadmap.

### 9.4 Browser fingerprinting and detection

The fact that the screen flashes saturated colors during authentication is *visibly distinctive*. An attacker writing automation against the demo can detect the challenge via the page's DOM/CSS state and choose to either abort the attack mid-challenge or attempt the synthesis-based defense circumvention above. We do not consider this a meaningful weakness (the attacker still has to defeat the underlying optical signature) but note that the defense is not "stealth" — its presence is observable.

---

## 10. Palm Modality Extension (Concept)

Wink offers palm-based biometric recognition in addition to face recognition. The screen-reflected color probe described above ports naturally to palm with several specific changes — and is, in many respects, *better suited* to palm than to face.

### 10.1 Threat model differences

For palm, the deployment surface differs:

- **Face** is captured via browser `getUserMedia` in a checkout/web flow. The injection threat is software-level, addressed in this paper.
- **Palm** is captured via a native Android tablet running an Android SDK with the Wink palm matcher. The injection threat is largely absent (Android camera APIs are hardware-bound at the OS level; software injection requires root or a virtual camera driver, which is not the typical attack surface).
- The dominant palm threat is **physical presentation**: a printed photo of the target's palm, or a phone screen displaying a palm image, held in front of the tablet's camera.
- Critically, **Wink's palm matcher does not currently include a passive liveness check** — there is no rPPG or Moiré analysis on the palm capture pipeline. The defense gap is therefore *all of liveness*, not just media injection.

### 10.2 Why palm is well-suited

Several properties of palm make the screen-probe defense more reliable on palm than on face:

1. **Closer capture distance.** Palm scans typically occur at 10–20 cm from the tablet, vs. face at 30–50 cm from a laptop. Screen-reflected intensity falls off roughly with the inverse square of distance: at half the distance, the reflected component is ~4× stronger. Dominance margins should be substantially larger and more consistent.
2. **More uniform reflectance geometry.** A palm is a flat, diffuse surface held perpendicular-ish to the camera. A face is highly non-planar (nose, cheeks, eye sockets) with multiple distinct reflectance materials (skin, lips, sclera). Dominance margins on palm are expected to be cleaner.
3. **Less motion noise.** A palm held still is genuinely still — no breathing, blinking, or micro-expressions. The face-still-settling problem of §6 essentially disappears.
4. **More uniform skin tone in the sampling region.** No lips, no eyes, no shadows — per-channel deltas are less contaminated by within-region tonal variation.
5. **Slot-N adaptation is smaller.** The much larger raw signal at close range means that even after camera adaptation, dominance margins remain comfortably positive across all slots, potentially permitting tighter (3-of-3 or stricter) gating.

### 10.3 Adaptation challenges

The printed-photo attack — a matte-paper print of the target's palm held in front of the tablet — is a meaningful challenge. The pigments in a printed pink/skin-toned palm photo differ from real skin spectrally:

- Under a **red flash**: pink pigments (CMYK magenta + yellow inks) reflect red strongly. Dominance margin can be positive — the slot may pass.
- Under a **green flash**: magenta ink absorbs green strongly. Dominance margin compresses or goes negative — the slot likely fails.
- Under an **orange flash**: similar to red. Slot likely passes.

A challenge sequence of {RED, ORANGE, GREEN} on a printed photo could plausibly produce 2-of-3 slot passes, defeating the majority gate.

Mitigations specific to the palm deployment:

1. **Always include a green slot** in every challenge sequence. Printed photos systematically struggle with green; real palms reflect green cleanly. This converts the lucky-2-of-3 path into a 1-of-3 path.
2. **Add a pure-blue slot** (the close range largely mitigates the auto-WB issues that made blue unreliable for face). Magenta + yellow inks both absorb blue strongly; printed photos catastrophically fail blue. Real palms reflect blue weakly but with consistently positive dominance.
3. **Tighten the gate to 3-of-4 or 4-of-4** (with the additional blue slot). The much stronger raw signal on palm permits a stricter gate without sacrificing real-palm reliability.
4. **Use the uniformity check as a primary gate**, not diagnostic. Glossy printed photos (or phone screens) produce specular reflection; matte palms produce diffuse. The uniformity asymmetry is a strong discriminator.

### 10.4 Architectural sketch

A native Android port preserves the core architecture:

- Surround flash via `View.setBackgroundColor` outside a palm-target region.
- Frame sampling via `ImageReader` on YUV_420_888 or RGBA_8888.
- Per-region per-channel pixel means computed in pure math.
- Palm bounding box from MLKit `Hands` detector or Wink's palm SDK.
- Verdict computation in the app, no server roundtrip for analysis.
- Wink palm-recognize SDK call wrapped by the probe (probe runs first; only on probe-pass is the SDK invoked).

The integration model for partners is **app-side wrapper**: a small Wink-published library that an integrating Android app adds alongside the Wink palm SDK. The wrapper presents the screen probe UI, captures the response, runs the verdict, and on pass invokes the underlying Wink palm match. This requires no changes to the Wink palm SDK itself and can be shipped as a defense-in-depth feature for integrators today, without waiting for the underlying SDK to incorporate the probe.

### 10.5 Why this matters

The palm modality currently has no liveness defense at all, making any deployment that uses palm matching as the sole biometric trivially vulnerable to the basic printed-photo attack. The screen-probe defense, ported with the modifications above, would close this gap end-to-end with the same architectural properties that make the face version attractive: client-side analysis, no video upload, sub-five-second latency, no hardware changes.

---

## 11. Differentiation from Prior Work

Active screen-illumination liveness is not new as a concept. The most prominent commercial implementation is iProov's *Flashmark / Genuine Presence Assurance* (GPA), in production since approximately 2017. The high-level architecture is similar at a conceptual level: issue a per-session challenge as screen illumination, capture a response, verify that the captured response matches the challenge.

The work described here differs from iProov's approach in several substantive ways:

| Dimension                   | iProov Flashmark / GPA                | This work                                  |
|----------------------------|--------------------------------------|--------------------------------------------|
| Primary purpose             | Liveness / presence verification     | Media injection defense (camera-pipeline integrity) |
| Analysis location           | Server-side video analysis           | Client-side per-frame intensity analysis   |
| Video upload                | Full session video uploaded          | None — only verdict + (optional) face crop |
| End-to-end latency          | ~10–15 seconds typical               | ~4–5 seconds                               |
| Verdict computation         | Server, multi-second analysis        | Browser, sub-millisecond                   |
| Privacy footprint           | Video PII in verifier infrastructure | No video leaves device                     |
| Architectural composition   | Standalone product                   | Layered with existing passive liveness as dual-channel verification |
| Modality coverage           | Face                                 | Face + concept-level palm                  |
| Compute cost per session    | Server GPU/CPU heavy                 | Client-side near-zero, server cost = recognition only |

The functional positioning is also distinct. iProov sells Flashmark as a complete liveness solution — its purpose is to provide presence verification independently. The work described here is positioned as a *defense layer* targeting a specific gap in browser-based biometric authentication (media injection), composed with an existing passive liveness system rather than replacing it. The dual-channel framing of §5 is structurally distinct from a single-channel active-only verification.

The implementation specifics — saturated palette tuned for spectral selectivity, 2-of-3 majority gate, bbox-locked client-side per-region intensity sampling, parallel-with-positioning execution, the uniformity-as-diagnostic separation — are independently developed against empirical real-cam and recorded-attack characterization, as documented in §6. While conceptual prior art exists, the specific implementation pathway and architectural positioning are original contributions.

We acknowledge that any commercial deployment of this defense at scale should be preceded by a freedom-to-operate review against iProov's issued patents and any relevant patents from FaceTec, Onfido, Daon, and other liveness vendors. The use-case framing (media injection vs. liveness) is unlikely to provide infringement immunity if patent claims read on the underlying method, and a license or design-around may be required depending on the specific claim language and the deployment scope.

---

## 12. Conclusion

Browser-based biometric authentication has a structural defense gap: software media injection cannot be detected by passive analysis of the captured frames, because by the time the frames are analyzed, all evidence of the injection is gone. This gap is widening as deepfake generation and virtual camera tools become more accessible.

We have built and deployed a defense that closes this gap by introducing a per-session randomized illumination challenge, measuring the captured stream's per-channel response to the challenge, and deciding locally — without uploading video for server analysis — whether the stream is exhibiting the optical signature of a real face physically present in front of the device's display. The defense composes with existing passive liveness in a dual-channel architecture that simultaneously addresses media injection (active channel) and physical presentation attacks (passive channel). End-to-end latency is approximately 4–5 seconds; user-perceived overhead vs. passive-only is approximately 1–2 seconds.

Empirical testing on five real-camera runs and five recorded-video injection attacks produced clean separation: 5/5 real-camera sessions passed, 5/5 recorded-video attacks blocked, with the underlying dominance margins showing a substantial gap between live and replay populations.

The defense is implementation-light, hardware-agnostic, and privacy-preserving by construction. It generalizes naturally to palm-based biometrics, where the closer capture distance and more uniform reflectance geometry are expected to produce even stronger separation, addressing a current gap in palm modality liveness defense.

Future work includes wiring the uniformity check into the verdict to address phone-screen specular replay, adding a blue slot to the palm-modality variant for printed-photo defense, formal characterization of the operating envelope across lighting and distance conditions, and counter-measures against adversarial frame synthesis as that threat matures.

---

## Appendix A — Glossary

| Term                       | Definition                                                                                  |
|---------------------------|----------------------------------------------------------------------------------------------|
| Media injection            | Any technique that causes `getUserMedia` to return frames not produced by the device's physical camera observing the live user. |
| Deepfake                  | Generative-model-produced video, typically face-swap or reenactment.                          |
| Virtual camera             | OS-level driver that exposes a synthetic video source as if it were a hardware camera.       |
| Passive liveness           | Liveness detection from captured frames without user interaction or active illumination.    |
| Active liveness            | Liveness detection requiring user response to a challenge (move head, blink, follow finger). |
| Screen-reflected probe     | Active liveness via screen illumination (this work).                                         |
| Dominance margin           | Per-slot difference between the face's expected-channel reflection and the next-best channel's reflection. |
| Slot-N adaptation          | Camera auto-WB/auto-exposure adaptation across successive challenge slots, dampening later-slot response. |
| Majority gate              | Verdict rule requiring a majority of slots to pass.                                          |
| Bounding-box lock          | Freezing the face bbox at probe start so L/R sub-region geometry is constant during sampling. |
| Specular reflection        | Glossy mirror-like reflection from a smooth surface (e.g., phone glass) — geometrically localized. |
| Diffuse reflection         | Matte scattering reflection from a rough surface (e.g., skin) — uniform across the surface.  |

---

## Appendix B — References

1. Chrome Project. "Use a fake video device for media stream." Chromium command-line flag documentation.
2. iProov Ltd. *Flashmark / Genuine Presence Assurance* technical brief, 2018.
3. Fei Yin et al. "Face Anti-Spoofing via Active Camera Flash Illumination Pattern Verification." Biometrics workshop proceedings, 2019.
4. Z. Akhtar, C. Micheloni, G. L. Foresti. "Biometric Liveness Detection: Challenges and Research Opportunities." IEEE Security & Privacy, 2015.
5. T. Karras et al. "Analyzing and Improving the Image Quality of StyleGAN." CVPR 2020 (deepfake reference).
6. Wink internal: face passive liveness specification (`docs/wink-passive-liveness-v3.pdf`).
7. Mediapipe Face Detection model card. Google AI, 2021–2024 versions.
8. Chrome Bug 1124960: discussion of `--use-file-for-fake-video-capture` security implications.
9. iProov v. FaceTec, US District Court for the Eastern District of Virginia, 2019 (settled). Patent litigation reference for prior art context.
10. EU AI Act, Article 5 and Annex III, biometric provisions, 2024.

---

*Document prepared by the Wink engineering team. Internal distribution only pending IP review.*
