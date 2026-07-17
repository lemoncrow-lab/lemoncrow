# LemonCrow runtime-loop video brief

## Decision

Produce one primary landing-page video showing the complete runtime loop:

> Find the right code → make the edit → catch missing verification → run the test → finish with evidence.

Do not combine installation into this video. Installation is already one command and does not show the product's distinctive value. If time remains, make the separate six-second installation clip described below for docs and install surfaces.

Use video, not GIF. Ship WebM first, MP4 as fallback, and a static WebP poster.

## Goal

Make a developer understand, without audio, why LemonCrow is a runtime rather than another search MCP.

The memorable moment is the verification hook:

```text
FIXME (verify): edited charge.ts, run test/verification.
```

That line is real product output. It should appear after the agent edits code and attempts to finish without a successful behavioral test after the last edit.

## Audience and takeaway

Primary viewer: a developer already using Claude Code, Codex, or another terminal coding agent.

After one loop, they should understand:

1. LemonCrow starts the agent at the relevant symbol and its relationships.
2. It keeps tool output narrow enough to remain useful.
3. It controls the working loop, not only retrieval.
4. It notices when verifiable work is about to finish without verification.

Do not sell token savings in this clip. Do not show benchmark numbers. This asset demonstrates the mechanism; the benchmark block elsewhere on the page supplies proof.

## Deliverables

Primary asset:

- `landing/public/video/runtime-loop.webm`
- `landing/public/video/runtime-loop.mp4`
- `landing/public/video/runtime-loop-poster.webp`
- 1440 × 900 pixels, 16:10
- 30 fps
- 15–18 seconds
- No audio
- WebM target: at most 1.5 MB
- MP4 target: at most 2.5 MB

Optional install asset:

- `landing/public/video/install.webm`
- `landing/public/video/install.mp4`
- `landing/public/video/install-poster.webp`
- 1200 × 675 pixels, 16:9
- 5–7 seconds
- No audio

Keep the lossless or high-quality master outside `public/`; only web-ready outputs belong there.

## Primary storyboard

### 0.0–1.2 seconds — Establish the task

Show a clean terminal with the agent prompt already entered:

```text
Prevent duplicate gateway charges in chargeCard().
```

No intro card and no logo animation. The product should begin doing useful work within the first second.

Small corner label:

```text
01 · MAP
```

### 1.2–4.2 seconds — Ranked code context

Show the real agent invoking:

```text
code_search("chargeCard idempotency")
```

Hold long enough to read the top result. The visible response should contain the real result shape from the demo repository, ideally:

```text
src/payments/charge.ts:42-78  chargeCard()
ranked #1 · 3 callers · 2 callees
```

Then briefly expose the relevant definition and exact range. Avoid scrolling through a long response. The result should occupy fewer than twelve terminal lines.

Visual emphasis:

- Purple accent on `chargeCard()` and the ranked result.
- Callers and callees visible, but secondary.
- Exact path and line range readable at normal landing-page width.

Do not fabricate output. Prepare a demo repository whose real index naturally produces this result, then record LemonCrow's actual response.

### 4.2–7.5 seconds — Grounded edit

Corner label changes to:

```text
02 · EDIT
```

Show the agent reading only the relevant range, then applying one compact edit. The change should be visually understandable without reading a full diff: add an idempotency guard or reuse an existing idempotency key before calling the payment gateway.

Ideal visible tool sequence:

```text
read src/payments/charge.ts:42-78
edit src/payments/charge.ts
✓ 1 grounded change
```

Keep the diff to four or five lines. Do not show an artificial wall of green text.

### 7.5–10.0 seconds — Runtime catches the omission

The agent attempts to finish before running a behavioral test after the edit. Show the real stop-hook response:

```text
FIXME (verify): edited charge.ts, run test/verification.
```

Corner label changes to:

```text
03 · VERIFY
```

Pause for 1.2–1.5 seconds so the line can be read. Give the nudge a restrained amber highlight. This is the video's key frame and the preferred poster frame.

The hook is bounded and fires once for the unresolved edit. Do not imply an endless blocking loop or that every command triggers a warning.

### 10.0–14.5 seconds — Verification runs

Show the agent immediately running the repository's real focused test command, for example:

```text
npm test -- charge
```

Use the command that actually belongs to the demo repo. The result should be real and concise:

```text
8 tests passed
```

If the project's native output is noisy, LemonCrow's bounded command result should remain visible rather than replacing it with a designed fake.

### 14.5–17.0 seconds — Evidence-backed finish

Show the agent's final line:

```text
done: duplicate-charge guard added
verified: 8 payment tests passed
```

Add a small final badge:

```text
finished with evidence
```

Hold for at least 1.2 seconds. End with a 200–300 ms crossfade back to the opening terminal state or allow a clean hard restart after the hold.

## Demo repository preparation

Use a small TypeScript payment fixture or a real public repository with:

- `src/payments/charge.ts`
- `tests/payments/charge.test.ts`
- a `chargeCard()` symbol
- at least two callers
- at least one gateway callee
- a focused test command that completes in under two seconds

Before recording:

1. Create a clean branch and reset it before every take.
2. Install dependencies and warm the package-manager cache.
3. Install and enable LemonCrow for the chosen host.
4. Pre-index the repository; indexing progress is not part of this clip.
5. Confirm `code_search("chargeCard idempotency")` returns a compact, credible result.
6. Confirm the verification hook emits the exact real nudge after a code edit with no subsequent test.
7. Confirm the test passes after the edit.
8. Remove API keys, usernames, home-directory paths, hostnames, and private remotes from the visible terminal.

Record the real interaction. Do not type designed output into a terminal to imitate a product run. If the agent automatically verifies before stopping, use a fresh session without a separate global instruction that independently enforces testing; keep LemonCrow's verify hook active.

## Capture setup

Recommended capture:

- Canvas: 1440 × 900
- Terminal content area: roughly 1320 × 760
- Terminal font: JetBrains Mono, Berkeley Mono, or Geist Mono
- Font size: 18 px at capture resolution
- Line height: 1.35–1.45
- Terminal background: `#0d0d10`
- Main text: `#d4d4d8`
- LemonCrow accent: `#a78bfa`
- Verification accent: `#fbbf24`
- Success accent: `#6ee7b7`
- Cursor: hidden or steady; never blinking over important text
- Shell prompt: minimal, with no user, machine, branch noise, or timestamps
- Browser and OS chrome: cropped out
- Notifications: disabled
- Mouse pointer: hidden unless it demonstrates a real hover state

Capture at 30 or 60 fps; export at 30 fps. Record at 2× the intended display dimensions when possible, then downscale for sharper terminal text.

Screen Studio, ScreenFlow, OBS, or a native recorder all work. Avoid simulated camera movement. One restrained 3–5% crop push during the verification nudge is enough.

## Editing rules

- Speed up dead time, not readable output.
- Keep tool-call transitions between 150 and 300 ms.
- Use direct cuts for agent actions.
- Use only three corner labels: `MAP`, `EDIT`, `VERIFY`.
- No voice-over, music, typing sound, gradient title cards, or floating feature list.
- No more than one zoom.
- Never shrink terminal text below legibility to fit more output.
- Hold every important result for at least one second.
- Keep the verification nudge on screen longest.
- Preserve authentic command and tool output.
- If a step requires more than twelve visible lines, shorten the repository output rather than scrolling quickly.

## Optional installation clip

Use this only on docs or an install page. Do not put both clips next to each other on the homepage.

Storyboard:

### 0.0–1.5 seconds

Show the actual install command:

```bash
curl -fsSL https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download/install.sh | bash
```

### 1.5–5.0 seconds

Show real host detection and installation output. Keep only successful, useful lines visible. The exact text must match the installer; expected content may include detected hosts, installed MCP server, hooks, agents, and skills.

### 5.0–6.5 seconds

End on the real ready state and the next command the installer recommends. A restrained checkmark is enough; no confetti.

If installation takes longer during capture, pre-warm downloads and remove waiting time in the edit. Do not imply a false elapsed time with an on-screen timer.

## Encoding

From a high-quality master such as `runtime-loop-master.mov`:

```bash
ffmpeg -i runtime-loop-master.mov -vf "scale=1440:900:flags=lanczos,fps=30" -an -c:v libvpx-vp9 -crf 34 -b:v 0 -row-mt 1 -deadline good -cpu-used 2 runtime-loop.webm
```

```bash
ffmpeg -i runtime-loop-master.mov -vf "scale=1440:900:flags=lanczos,fps=30" -an -c:v libx264 -preset slow -crf 24 -pix_fmt yuv420p -movflags +faststart runtime-loop.mp4
```

Create the poster from the verification-nudge frame:

```bash
ffmpeg -ss 00:00:08.500 -i runtime-loop-master.mov -frames:v 1 -vf "scale=1440:900:flags=lanczos" -c:v libwebp -q:v 78 runtime-loop-poster.webp
```

Adjust the poster timestamp to the final edit. Inspect the image manually; it must show the edited filename and full verification message without a cursor covering either.

## Landing-page placement

Place the primary video in the “Why a runtime” section after the four pillars and before the surface table. The surrounding section explains Map, Bound, Carry, and Verify; the clip then shows the full loop before the table names the implementation surfaces.

Do not add a placeholder before the final assets exist.

Recommended markup:

```html
<video
  autoplay
  muted
  loop
  playsinline
  preload="metadata"
  poster="/video/runtime-loop-poster.webp"
  aria-label="LemonCrow finds chargeCard, grounds an edit, catches missing verification, and runs the payment tests."
>
  <source src="/video/runtime-loop.webm" type="video/webm" />
  <source src="/video/runtime-loop.mp4" type="video/mp4" />
</video>
```

The poster must carry the full story for users with reduced motion, data-saving settings, autoplay restrictions, or unsupported codecs. Under `prefers-reduced-motion: reduce`, show the poster instead of autoplaying the loop.

Do not show native video controls on the silent loop. Make the entire video optional evidence; no important copy should exist only inside it.

## Acceptance checklist

- Understandable with sound off.
- Useful action visible within the first second.
- Verification nudge readable at 720 px rendered width.
- All terminal output generated by the real product and demo repo.
- No customer code, secrets, usernames, machine names, or private paths.
- No unsupported benchmark or savings claim.
- WebM no larger than 1.5 MB; MP4 no larger than 2.5 MB.
- Poster selected from the verification frame.
- Loop does not flash or jump aggressively.
- Reduced-motion experience shows a complete poster.
- Mobile layout uses the poster unless the video remains legible and lightweight.
- Runtime clip appears only once on the homepage.
