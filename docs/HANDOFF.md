# Clipmatch handoff

## Read this first

Continue on branch **`milestone1-manual-render`**. The repository is named `clipper`; the package/CLI is `clipmatch`.

**Milestone 1 is implemented but is not yet verified complete.** Full FFmpeg/ffprobe rendering tests and frame inspection could not run on the development laptop. Do not start milestone 2 or claim the renderer works until those checks pass and the owner gives the next go-ahead.

The owner explicitly requested pushing this branch and handoff to GitHub. No merge to main is requested. Local commits use **Khaled Ahmed** and the existing GitHub noreply email, configured for this repository only. The laptop's global work identity was left unchanged.

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

## Verification evidence and blockers

Latest available suite: **39 tests discovered: 33 passed, 6 skipped**. Compilation and CLI help also pass. The six skipped tests require full FFmpeg and ffprobe:

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
