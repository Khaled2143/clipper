# Clipmatch handoff

## Start here — shorter finished edits, November 18, 2026

The Windows checkout on `milestone1-manual-render` now cuts finished uploads from the best landscape Twitch source. For new triggers the watcher saves when chat started rising relative to the successful clip request. The finisher considers that timing alongside changes in the game, facecam and audio. It keeps the channel label and skips generated speech captions. A source with little evidence gets a wider cut.

Three archived handoff clips were rendered locally from 1080p sources. These clips predate saved chat timing. The numbers below are source times in seconds. The smaller attachments were encoded locally with the same size fallback as the watcher.

| Clip | Selected window | Result |
| --- | --- | --- |
| Agent00 car flip | 43.50–57.75 | The warning opens the edit, the flip starts about four seconds in, and the landing and “I scratched it!” line fit. The upload-sized copy measured 4.53 Mbps video. |
| Fanum repair menu | 35.00–53.75 | The edit followed a rise in sound during the menu. The 1.84 MB vertical master already fit the attachment budget. |
| Kai Cenat cutscene | 32.25–53.25 | Facecam movement guided the cut. The upload-sized copy measured 3.09 Mbps video. |

The Agent00 opening matched the hand-picked reference. Fanum and Kai still need an editorial listen to confirm which moment chat cared about. Windows previews and masters are under the ignored `work/timing-review/finished-edits/` directory. A previously delivered 60-second Agent00 attachment measured 0.97 Mbps video and carried the bot's size-limited encoder settings. All 149 Windows tests passed with FFmpeg and ffprobe enabled, including retries, legacy queue data, early payoffs and preserving the master after an uncertain Discord upload.

**Mac rollout is pending.** Once this revision is available on the Mac, stop the existing watcher between renders, update its `.worktrees/manual-render` checkout, and restart it there with its existing `.env` and `.clipbot/` state. Completed uploads keep their queue status. Inspect the first fresh MP4 and its adjacent JSON for the selected source times, cue and chat timing. The local acceptance runs used `--render-only`. [Watcher restart command](../README.md#automatic-finished-videos)

The earlier Shorts title ideas below still need an editorial choice. New finished jobs retain the Twitch title and source URL. Timed transcripts are available on older captioned jobs.

## Earlier Mac handoff — November 18, 2026

Pull `milestone1-manual-render` and use this branch to continue. The Mac is already watching all five configured channels. At 7:36 p.m. Eastern, its queue showed 23 completed uploads across four streamers (8 Agent00, 6 Fanum, 4 Jynxzi, and 5 Kai Cenat). It had no jobs waiting for review. The Mac has the active Twitch session and both Discord webhooks. Keep a single watcher active if you move it to another machine. [Local setup and restart command](../README.md#automatic-finished-videos)

**YouTube Shorts titles are next.** The owner wants copy to go alongside each MP4 in the finished Discord channel. The current post contains the playable video alone. The last discussion proposed a title, a one-line description, and the original Twitch link. The editorial style is still open. Two different Fanum clips carried the same Twitch stream title, so copying that field alone would give them identical headlines. Each clip already has its Twitch title and URL in `source.json` and timed words in `work/<clip>/transcript.json`.

For the Fanum sink-repair clip, the two examples discussed were **“Can I Use This Wrench?” | Fanum Plays GTA 6** (a line from the transcript) and **Fanum Has One Concern About This GTA 6 Sink Repair** (a headline written for the moment). Use these to settle the style with the owner. The fixed-script constraint still applies. `clipmatch/finished.py` builds the Discord attachment post.

## Automatic finished videos — November 18, 2026

The Mac watcher now sends confirmed Twitch links and live notices to the original Discord channel and uploads playable MP4s to the finished channel. An earlier Agent00 clip was queued for the first delivery. Discord confirmed the attachment at 6:32 p.m. Eastern. The master is 60 seconds long with 1080×1920 H.264 video and AAC audio. A frame check showed Agent00's facecam above gameplay with locally transcribed words burned into the picture. [Source clip](https://www.twitch.tv/agent00/clip/CovertDoubtfulBillKAPOW--bw1uFkCIa6BJc-d)

The first new live trigger came from Fanum at 6:36:44 p.m. Eastern. The original channel received the Twitch link seven seconds later. At 6:38:28 p.m., Discord had confirmed the finished MP4, one minute and 44 seconds after the spike. The saved upload receipt matched the local 60-second master. A frame check showed Fanum above the gameplay with a timed caption. [Fanum source clip](https://clips.twitch.tv/TsundereAcceptablePotatoChefFrank-E61NUL4Uw1zfSYLc)

A Kai Cenat chat spike at 6:37:55 p.m. also produced a Twitch clip. The original link arrived at 6:38:14 p.m. and the finished video at 6:39:40 p.m. The queue and Discord receipt confirmed delivery on the first render attempt. [Kai Cenat source clip](https://clips.twitch.tv/ToughVenomousWitchPunchTrees-ttVnELyxOnAzX6U8)

One watcher runs under `caffeinate -i`. It covers Agent00, Fanum, Jynxzi, Kai Cenat, and YourRAGEGaming. Each has a fixed facecam crop in `clipmatch/layouts.json` sampled from four public clips. Check the crop when a streamer changes scenes. Homebrew's `ffmpeg-full` is on the watcher's PATH, and the local Whisper `small` model is cached. The transcription extra pins PyAV below 19 for compatibility with the installed faster-whisper decoder.

The queue, source files, full-quality masters, and Discord receipts persist under `.clipbot/` in the `.worktrees/manual-render` checkout. Check any job marked `render_review` before requeuing it. All 118 tests passed on the Mac with the media dependencies installed. Hosted deployment and the 24-hour disconnect test remain pending. [Start and operate the watcher](../README.md#automatic-finished-videos)

## Mac live test — November 18, 2026

The first real Twitch-to-Discord delivery succeeded on the Mac. Agent00's chat reached 41 messages in 10 seconds. The worker detected the spike and requested a clip at 2:53:15 p.m. Eastern. Discord confirmed delivery at 2:53:18 p.m., three seconds after detection (target 30 seconds). The 60-second clip shows a drive from a tunnel onto a highway, a car flipping near the end, and driving afterward. This review used sampled video frames. The audio and the cause of the chat reaction still need playback review. [First Agent00 clip](https://www.twitch.tv/agent00/clip/CovertDoubtfulBillKAPOW--bw1uFkCIa6BJc-d)

The local worker now posts a Discord notice when it begins watching a live channel, including channels already live at startup. A reconnect during the same live session suppresses a duplicate notice. The Mac test suite ran 89 tests with 83 passing and six media tests skipped. Twitch returned HTTP 403 for a later Kai Cenat clip request. Chat monitoring continued.

The watcher was left running on the Mac under `caffeinate -i` for the local test. Before a move to Railway, review the clip with audio, stop the local worker, then follow the single-worker volume instructions below. The 24-hour hosted run and disconnect check remain pending.

## Previous Windows handoff — November 18, 2026

**Next session starts with the first live Twitch-to-Discord clip.** The worker is installed in the Windows checkout on branch `milestone1-manual-render`. This branch contains both the new live worker and the earlier manual vertical renderer.

GitHub has the live worker at commit `9ededb0` on `milestone1-manual-render`. The Twitch app `Kayd21ChatClipper` is registered with callback `http://localhost:17563/callback`. All four settings are populated in the home checkout. Twitch sign-in was started, but no local refresh token was present at handoff.

On the work PC, clone this branch, open PowerShell in the clone, and run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[live]"
Copy-Item .env.example .env
notepad .env
```

Restore the existing Twitch client ID, client secret, Discord webhook URL and channel list privately. Save the file. Run the following commands in order, completing browser approval on the work PC before starting the watcher:

```powershell
.\.venv\Scripts\python.exe -m clipmatch.live authorize
.\.venv\Scripts\python.exe -m clipmatch.live watch
```

Allow five minutes of observed live chat for warmup. Look for `spike`, `clip_requested`, then `clip_result` with `delivered: true` in the terminal. Open the link in Discord, check whether the clip caught the setup and payoff, and record the time from spike to delivery. The initial target is 30 seconds. The default trigger is 20 messages in 10 seconds and at least three times the channel's recent average, with a 90-second cooldown.

### Verified before handoff

The full Windows suite passed **88 tests with zero skips** with FFmpeg and ffprobe on PATH. The end-to-end test uses local Twitch and Discord simulators to exercise a stream already live at startup, a chat spike, a clip request, delayed confirmation, and delivery of the public URL. Other tests cover dropped connections, duplicate events, token rotation, and cooldown persistence.

### Remaining live checks

Account authorization, real clip timing, and delivery to the target Discord channel are pending. The Docker daemon was unavailable during implementation. After checking one real clip locally, stop the local watcher and deploy a single Railway worker with a volume at `/data`. Verify the container build, then run for 24 hours including a disconnect and check for duplicate clips across recovery. [Railway setup](../README.md#run-on-railway)

The sections below record the **earlier manual-render milestone**. Its real-footage review is a separate outstanding task. [Live worker implementation plan](superpowers/plans/2026-11-18-chat-spike-worker.md)

## Windows continuation — November 13, 2026

Checkout: `C:\Users\lordk\dev\personal\clipper\clipper`, branch `milestone1-manual-render`.

- Created `.venv` with Python 3.11.5 and installed `.[transcription]` normally, using a workspace-local uv cache. Installed faster-whisper 1.2.1 and CTranslate2 4.9.0; one CUDA device is detected, but model initialization/inference is not yet verified.
- The native Windows test run exposed a test-reader bug: `atomic_json` correctly writes UTF-8, but its test implicitly decoded with CP1252. Explicit UTF-8 decoding preserves the original Unicode assertion and fixes the failure; no application serialization change was needed.
- Current suite: **39 passed, zero skips**, including all six media integration tests. CLI help and compilation pass. Test log: ignored `work/windows-tests.log`.
- The owner installed Gyan.FFmpeg and copied its binaries to ignored `work/tools/ffmpeg-bin`, resolving the session access restriction. FFmpeg/ffprobe 9.0.2 pass the project's codec/filter preflight.
- The rotation fixture's old metadata command produced no display matrix on FFmpeg 9. Updated the test to use `-display_rotation` when supported, retaining the old command for older binaries. The original display-dimension assertion remains intact. No production renderer change was needed.
- Ran ignored `work/verify_samples.py`. Retained `renders/blur.mp4`, `renders/face.mp4`, clean exports, subtitle/source sidecars, ffprobe reports, and frames at 0, 0.2, 0.8, and 2.9 seconds under `work/windows-verification`. Both masters are 1080x1920 H.264/AAC with video and audio durations of exactly 3 seconds. Inspected first-frame hooks/labels and 0.8-second highlighted captions in both layouts; the red camera region is above fitted gameplay as intended. Arial-BoldMT resolves successfully.
- Samples use generated tones and injected words, not actual speech recognition. The owner has no real footage prepared yet. Real transcription, remote section timing, subjective playback review, and milestone acceptance remain pending.

To reproduce the synthetic checks, run from this checkout:

```powershell
$env:PATH = "$PWD\work\tools\ffmpeg-bin;" + $env:PATH
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe work\verify_samples.py
```

Do not count skipped media tests as successful render verification. Continue the ordered steps below; milestone 2 remains gated on milestone 1 review.

## Product decisions

- Goal: **3–5 unique videos per day**, each cross-posted to TikTok, Shorts and Reels; not a separate set per platform.
- Eligible clips: reactions, funny commentary and surprising gameplay with understandable setup/payoff. No quota padding; fewer or zero clips is acceptable.
- Discovery should combine audience engagement, viewer-created clips, streamer delivery and gameplay events. Loudness alone is insufficient. Third-party VOD replay/retention analytics are not assumed accessible.
- Cross-stream matching/compilations are optional future enhancements, not required for initial usefulness.
- Production should run in the cloud while the owner's PC is off and leave its **Windows RTX 4070** available for other use. Target total recurring budget is **$40/month**, pending measured feasibility.
- A future editor AI proposes clips; an independent judge inspects finished previews with audio and a written rubric. Neither is implemented yet. The owner manually approves every post.
- Reuse existing tools; ask before adding dependencies beyond the originally approved stack. CapCut is an optional finishing tool, not a required automation/API dependency.
- Build milestone by milestone and stop for owner go-ahead after each. The owner approved the design and native execution of milestone 1.

## Documents and code

- Approved design: `docs/superpowers/specs/2026-11-12-clipmatch-design.md`.
- Implementation plan/status: `docs/superpowers/plans/2026-11-12-manual-render.md`.
- Setup, YAML schema, limitations: `README.md`.
- Editable defaults: `config.yaml`; example independent clips: `examples/manual.yaml`.
- `clipmatch/spec.py`: strict job/config validation and source-relative times.
- `clipmatch/cache.py`: content hashes, atomic JSON and completed-artifact manifests.
- `clipmatch/media.py`: probing, section downloads, audio extraction, sanitized subprocess errors.
- `clipmatch/transcribe.py`: optional faster-whisper, CUDA/CPU selection, lazy model loading, cached timed words.
- `clipmatch/captions.py`: editable SRT and ASS word highlights, hooks/labels and typography limits.
- `clipmatch/render.py`: blur/split layouts, libass font checks, H.264/AAC export and duration checks.
- `clipmatch/cli.py`: sequential manual-job orchestration and independent clip outputs.
- `tests/`: standard-library unittest suite, including generated-media integration tests.

## Implemented behavior

`clipmatch render-manual spec.yaml [--config config.yaml] [--force] [--clean] [--no-captions]`:

- Each spec entry produces its own video; no implicit compilation.
- Local media or supported remote VOD URL with explicit original-source start/end.
- Remote requests target buffered sections via yt-dlp; no intentional full-video fallback.
- Exact selected-window audio transcription with word timings and VAD.
- 1080×1920 H.264/AAC output, first-frame hook, streamer label, word-highlight captions.
- Configured facecam above fitted gameplay, or full-frame foreground over blur.
- Optional clean export plus SRT for CapCut.
- Independent stage caching; changed hooks/layout do not intentionally retranscribe audio.
- Source/provenance JSON; output status is `manual_render_unreviewed`.
- Validation protects local inputs from output/cache path collisions.

With explicit `--config config.yaml`, paths are relative to that config directory. Without it, work/output defaults are relative to the spec directory. The root config is not automatically loaded.

## Original laptop verification evidence and blockers (historical)

Original laptop suite: **39 tests discovered: 33 passed, 6 skipped**. The Windows continuation above supersedes this result. The six formerly skipped tests require full FFmpeg and ffprobe:

1. Both layouts, captions, clean exports, resume and changed hook.
2. Force/source-change invalidation and interrupted transcription.
3. Invalid crop and missing audio rejection through the media workflow.
4. Silent audio without invented captions.
5. Delayed audio preserving leading silence and caption time.
6. Rotated and anamorphic source rendering.

These tests inject known words at the speech-model boundary and use real FFmpeg-generated video/tones. They do not require a model or network when dependencies and system binaries are installed.

**Not verified:** any actual rendered MP4, visual safe areas/font behavior, playback sync, real section-download accuracy, real transcription/CUDA, installation on a clean machine, or Windows-native execution. Do not conflate mocked boundary tests with actual media proof.

Environment findings:

- Development laptop has Python 3.12; default `/usr/bin/python3` is 3.9 and is unsuitable.
- Full FFmpeg/ffprobe are absent from PATH. A Playwright FFmpeg exists but lacks required codecs/filters and is not suitable.
- Network pip installation failed in the restricted session. A preinstalled PyYAML copy was used only inside an ignored local venv for checks; it is not vendored into the repo. Install normal declared dependencies on the next machine.
- `brew install ffmpeg` was attempted after owner authorization but failed with `Operation not permitted` writing `~/Library/Caches/Homebrew/api/formula_names.txt`. This was a session filesystem restriction, not evidence that Homebrew cannot work in the owner's Terminal.

## Independent review and fixes

One independent whole-branch review inspected the implementation and reproduced important issues. A single regression-test fix pass addressed:

- Source footage overwritten by master/clean destinations, including another clip's source.
- Audio timeline gaps/leading offsets omitted when converting to WAV; explicit normalization added to extraction/render paths.
- A failed optional clean export leaving old source metadata beside a new master; master metadata now saves before clean export.
- Wide text overflow and silent label truncation; conservative sizing/wrapping and explicit overlong-label rejection added.
- Missing font resolution/provenance; libass preflight, selected font identities and render-key inputs added.
- Rotation/crop coordinate and non-square-pixel camera mismatch.
- Container duration masking a truncated video track; independent video/audio duration checks added.

Regression tests were observed failing before fixes and passing afterward. Audio/font/geometry behavior still needs the real tests above. No second independent review was performed after the fix pass.

Deferred minor: identical local VODs are rehashed for each clip; per-invocation memoization can reduce I/O later. Concurrent writers are explicitly unsupported; use one job at a time per output directory/clip IDs.

## Resume on the Windows PC

In PowerShell, with Git available:

```powershell
git clone --branch milestone1-manual-render https://github.com/Khaled2143/clipper.git
cd clipper
winget install --id Python.Python.3.12 -e
winget install --id Gyan.FFmpeg -e
```

Open a new terminal if installation changed PATH. Then:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[transcription]"
ffmpeg -version
ffprobe -version
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Alternatively install only `-e .` to run the synthetic tests without faster-whisper; the tests replace the model boundary. **All six media tests must execute, not skip.** If they fail, investigate the real failure rather than weakening assertions. The libass font-preflight path was never exercised here and particularly needs validation.

For a real render, edit `examples/manual.yaml` with accessible sources and sensible times, then:

```powershell
.\.venv\Scripts\python.exe -m clipmatch render-manual examples\manual.yaml --config config.yaml --clean
```

Expected: independent MP4s and `.ass`, `.srt`, `.json` sidecars under `renders/`, plus `.clean.mp4` with `--clean`. `work/` holds reusable artifacts. First real transcription downloads model weights.

Inspect output:

```powershell
ffprobe -v error -show_streams -show_format -of json renders\reaction_a.mp4
ffmpeg -ss 0.2 -i renders\reaction_a.mp4 -frames:v 1 renders\reaction_a-frame.png
```

Also extract a frame at a known caption time and inspect both blur/facecam layouts. Confirm first-frame hook, readable text, safe areas, aspect ratio, audio sync, and complete ending. Keep representative generated test outputs/ffprobe JSON/frames in ignored `work/` for owner review.

For CUDA, follow the current faster-whisper/CTranslate2 GPU documentation linked in README. Current upstream guidance described CUDA 12 cuBLAS/cuDNN 9; recheck against the actual installed versions. Auto uses CPU int8 when CUDA is unavailable/initialization fails; explicit `device: cuda` requires working CUDA. Rendering uses CPU libx264 currently.

## What to do next, in order

Windows continuation has completed steps 1–3 below. Resume at step 4 when a real source is available; sample review can happen now using the retained files above.

1. Install full tools/dependencies outside this restricted laptop session or on Windows.
2. Run all 39 tests with **no media skips**; fix genuine failures with regression evidence.
3. Render and retain synthetic samples; inspect ffprobe and frames for both layouts and captions.
4. Test one real short source window, including actual transcription and remote section timing where relevant. Confirm cache reuse and changed-hook behavior on real media.
5. Update this handoff/README/plan with actual evidence and any remaining limitations. Obtain owner review of the look and milestone 1 go-ahead.
6. Only then start milestone 2: resumable ingest, chat/audio signals, candidate-window analysis and verified config-driven streamer handles.

Subsequent approved design milestones add editor/judge review, cloud scheduling/durable artifacts/budget controls, quality checks and optional matching. No cloud provider, paid service, AI model, or subscription has been selected or purchased. Recheck pricing before any deployment and present concrete spend before paid trials.

## Local development state

On the original laptop the implementation checkout is:

`/Users/khaled.ahmed/dev/personal/clipping/clipper/.worktrees/manual-render`

The parent checkout is still on main. No implementation merge has occurred. The ignored `.superpowers/sdd/2026-11-12-manual-render/` folder has the local execution ledger, test logs and review diff; it is not pushed. This handoff and Git history preserve the useful state for a fresh machine. Ignored venvs, downloaded media, credentials and generated renders must stay out of Git.
