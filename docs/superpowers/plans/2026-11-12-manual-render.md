# Manual Render MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Turn explicitly selected local/video-URL windows into independent captioned vertical clips, with repeatable offline verification and safe resume behavior.

**Architecture:** A small Python CLI validates YAML, acquires buffered video windows, transcribes only selected audio, and runs FFmpeg to create independent outputs. Stage manifests preserve successful work and invalidate only affected artifacts. Milestone 1 runs locally for verification; cloud scheduling, discovery, AI editorial selection, and judging remain later milestones.

**Tech Stack:** Python 3.11+, argparse, PyYAML, yt-dlp, faster-whisper, system ffmpeg/ffprobe, standard-library unittest. Use setuptools for packaging only; no additional runtime libraries. Do not add pytest or a font download dependency.

**Spec:** `docs/superpowers/specs/2026-11-12-clipmatch-design.md` (approved).

## Global Constraints

- “Quality takes precedence over the count: a day may produce fewer or no passing clips.”
- “The repository is currently named clipper; the Python package and CLI will be clipmatch. Keep the repository directory unchanged.”
- “No database.”
- “No dependencies beyond the original stack, purchases, cloud deployment, or paid calls are authorized by this design alone.”
- “Use configured facecam crop above gameplay, otherwise full source frame over a blurred background.”
- “SRT does not preserve ASS animation.”
- “Hard cuts first.”
- “After every milestone provide exact commands, expected artifacts, verification evidence, limitations, and wait for the owner's go-ahead.”
- Run on Windows and macOS/Linux; use subprocess argument lists with shell=False. No cloud accounts, API keys, or real VODs are required for offline tests.

## Review Focus

1. Windows paths, spaces, apostrophes, Unicode, and filter punctuation must not break media commands or text (Tasks 2/4).
2. Buffered downloads and nonzero source offsets must not shift captions or source sidecars (Tasks 2/3/5).
3. Interrupted writes and changed hooks must never reuse partial videos or retranscribe valid audio (Tasks 1/5).
4. Silent or missing-audio footage must have intentional behavior rather than fabricated speech (Tasks 2/3/5).
5. Invalid/out-of-frame crops and portrait sources must produce clear errors or preserve aspect ratio (Tasks 1/4/5).

## Files and contracts

- `pyproject.toml`, `.gitignore`, `clipmatch/__init__.py`, `clipmatch/__main__.py`: package, CLI entry points, generated-file exclusions.
- `clipmatch/spec.py`: dataclasses and strict YAML parsing; config and spec merge.
- `clipmatch/cache.py`: fingerprints, completion manifests, atomic JSON writes.
- `clipmatch/media.py`: preflight, ffprobe, buffered section acquisition, audio extraction.
- `clipmatch/transcribe.py`: lazy faster-whisper backend and clip-relative words.
- `clipmatch/captions.py`: ASS/SRT generation with escaped text.
- `clipmatch/render.py`: aspect-preserving layouts, text overlays, encoding and output verification.
- `clipmatch/cli.py`: argparse and sequential independent clip orchestration.
- `config.yaml`, `examples/manual.yaml`, `README.md`: editable defaults, sample, instructions.
- `tests/test_spec_cache.py`, `tests/test_media.py`, `tests/test_captions.py`, `tests/test_render.py`, `tests/test_cli_integration.py`: unittest tests; `tests/__init__.py` enables discovery/imports.

Builder may refine internal helpers but must keep the following shared contracts consistent:

`ClipSpec(id: str, source: str, start: float, end: float, label: str, hook: str, facecam_rect: tuple[int,int,int,int] | None)`; times are original-source seconds, end exclusive.

`Settings`: model="small", device="auto", compute_type="auto", buffer_seconds=2.0, crf=20, preset="veryfast", font="Arial", fps=30; output fixed at 1080×1920. CLI config values can override defaults. Style positions defined in Task 4. These defaults are builder proposals, not measured quality claims.

`MediaInfo(width: int, height: int, duration: float, has_audio: bool)`.

`SourceWindow(path: Path, source_start: float, trim_start: float, duration: float, identity: str)`; its file starts at source_start, trim_start=start-source_start, duration=end-start. Local sources retain their original file with source_start=0.

`Word(start: float, end: float, text: str)`; transcript times are relative to the exact selected clip, never the buffered window.

### Task 1: Validated job specification and completion cache

**Files:** package metadata/entry shells, spec.py, cache.py, config.yaml, examples/manual.yaml, tests/test_spec_cache.py, .gitignore.

**Interfaces:** `load_job(path: Path, config_path: Path | None) -> tuple[Settings, list[ClipSpec]]`; `fingerprint(payload: dict) -> str`; `cache_valid(output: Path, manifest: Path, key: str) -> bool`; `atomic_json(path: Path, data: dict) -> None`.

- [x] Write tests asserting HH:MM:SS and numeric times produce identical seconds, end<=start fails, duplicate/unsafe IDs fail, nonpositive crop dimensions fail, paths resolve relative to spec, and unknown fields fail with their names. Assert `fingerprint` ignores dictionary key order, a temporary output is not valid, changed keys miss cache, and missing/zero-byte output cannot pass a manifest.
- [x] Run `python -m unittest tests.test_spec_cache -v`; expect failure because the new modules do not exist.
- [x] Implement schema version 1 with top-level `clips` and each clip's `source`, `start`, `end`, `label`, `hook`, optional `facecam_rect`, required safe `id`. Config fields only cover this milestone. Do not populate unverified creator handles; inline labels suffice for manual jobs.
- [x] Implement completion manifests with stage name/version, key, tool versions, artifact path/size and completion status; atomic writes use same-directory temporary files plus os.replace. Key identity includes content hash for local inputs. Avoid checking all media hashes repeatedly within one invocation.
- [x] Add minimal setuptools packaging with console script `clipmatch=clipmatch.cli:main`, Python>=3.11, PyYAML/yt-dlp runtime requirements and a `transcription` extra for faster-whisper. Preserve install-without-model capability for offline render tests.
- [x] Run the tests again; expect all assertions to pass. Commit task changes.

Example spec (independent outputs, never implicit concatenation):

```yaml
version: 1
clips:
  - id: reaction_a
    source: ./sample.mp4
    start: "00:00:02"
    end: "00:00:08"
    label: STREAMER
    hook: "That confidence lasted one second"
```

### Task 2: Probe and acquire exact source windows

**Files:** media.py, tests/test_media.py.

**Interfaces:** `probe(path: Path) -> MediaInfo`; `acquire(clip: ClipSpec, settings: Settings, work_dir: Path, force: bool=False) -> SourceWindow`; `extract_audio(window: SourceWindow, output: Path) -> Path`.

- [x] Write tests for source start=100/end=110/buffer=2 producing a requested 98–112 remote window and trim_start=2; local start=2/end=8 produces trim_start=2/duration=6. Assert argv construction uses no shell for paths with spaces/apostrophes, failed downloads do not publish manifests, end beyond local duration fails, no-audio input fails with a clear message, and remote metadata duration bounds are validated when known.
- [x] Run `python -m unittest tests.test_media -v`; expect failures on missing implementation.
- [x] Implement system-tool preflight, machine-readable ffprobe parsing and yt-dlp metadata lookup/section requests using the original URL. Capture actual output path. Use FFmpeg-backed accurate section boundaries (including forced keyframes when required); normalize buffered results to zero-based media timestamps before final trimming. Never assume arbitrary keyframe cuts have exact boundaries. Version this acquisition behavior in manifests.
- [x] Extract only the exact selected window to mono 16-kHz PCM WAV. Preserve audio/video offset handling. Pass metadata/errors through actionable CLI messages; classify unsupported/private/expired sources without printing secrets or entire authenticated URLs. Do not silently fall back to whole-VOD video downloads.
- [x] Run tests again; all pass. Real network behavior is an explicitly unverified follow-up until a sample URL is available. Commit task changes.

### Task 3: Reusable timed transcription and caption files

**Files:** transcribe.py, captions.py, tests/test_captions.py.

**Interfaces:** `transcribe(audio: Path, settings: Settings, output: Path, force: bool=False) -> list[Word]`; `write_captions(words: list[Word], ass_path: Path, srt_path: Path, duration: float, settings: Settings) -> None`.

- [x] Write tests with injected fake backend words: selected-clip word 1.0–1.4 remains 1.0–1.4 despite original source offset 100; out-of-range words are clipped/dropped; empty transcript produces no fabricated words; ASS braces/backslashes/newlines are escaped; SRT is chronological with valid millisecond timestamps. Assert changing render font does not invalidate transcription, while changing model/audio identity does.
- [x] Run `python -m unittest tests.test_captions -v`; expect failures on absent implementation.
- [x] Implement lazy faster-whisper loading, word timestamps and vad_filter=True. CUDA availability selects float16; CPU selects int8 by default. In auto mode, a CUDA initialization failure may fall back once with a visible warning; explicit cuda fails clearly. Save backend/model/version provenance and atomic completion after iteration fully finishes. Reuse a model within a batch. No speech model downloads in tests.
- [x] Generate editable SRT and ASS with word-level highlighting, wrapping into short readable phrases. Use fixture word JSON in offline tests; production defaults to transcription. Provide `--no-captions` for deliberately caption-free output, clearly marked in sidecars. Never label a no-caption render as caption verification.
- [x] Run tests again; all pass. Commit task changes.

### Task 4: Vertical rendering and safe typography

**Files:** render.py, tests/test_render.py.

**Interfaces:** `render_clip(clip: ClipSpec, window: SourceWindow, words: list[Word], settings: Settings, output: Path, *, clean: bool=False) -> MediaInfo`.

- [x] Write tests for bounded crops, no-stretch fit/pad geometry for landscape and portrait inputs, user text containing colons/quotes/Unicode, and render cache invalidation by changed hook/layout but not source/transcript. Exercise filters through FFmpeg integration in Task 5, not only string comparison.
- [x] Run `python -m unittest tests.test_render -v`; expect missing implementation failures.
- [x] Implement 1080×1920, 30fps H.264 libx264 CRF20/preset veryfast, AAC 48kHz audio, yuv420p and faststart. Normalize aspect ratio and input timestamps. Use a blurred fill plus a fit full-frame foreground, or facecam top (y=200, height=600) and fit gameplay below (y=800, height=800). Avoid stretching. Reject crop outside probed bounds.
- [x] Put hook in x=80..900/y=60..180, label near y=820, captions centered near y=1360 with right bound<=900/bottom<=1500. Hook/label appear from frame one. Use ASS text events for overlays to avoid drawtext command interpolation. Validate libass availability and font resolution; surface fallback font choice. Keep font family configurable without downloading fonts.
- [x] Make `--clean` export a second video without any burned-in text for CapCut; retain SRT and captioned master. Publish output/manifests only after ffprobe verifies geometry, codecs, audio, and duration tolerance <=0.15s. Failed output must not replace the last valid master.
- [x] Run tests again; all pass. Commit task changes.

### Task 5: CLI integration, offline evidence, and owner handoff

**Files:** cli.py, tests/test_cli_integration.py, README.md; finalize __main__.py.

**Interfaces:** `main(argv: list[str] | None=None) -> int`; command `clipmatch render-manual SPEC [--config PATH] [--force] [--clean] [--no-captions]`. Work/renders roots relative to the selected config directory, defaulting to the spec directory. Each clip produces `renders/<id>.mp4`, `.ass`, `.srt`, `.json`; clean variant `<id>.clean.mp4` when requested. Caption-free mode omits caption files.

- [ ] Write end-to-end tests generating short testsrc+sine media via ffmpeg; use unittest.mock to inject known timed words into the real orchestrator while rendering real ASS/MP4. Generate two independent clips (blur and facecam) from nonzero source offsets. Assert two outputs, 1080×1920, H.264/AAC, exact source windows in sidecars, duration tolerance <=0.15s, no network/model calls. For layout evidence use a distinctive colored facecam region in one synthetic source.
- [ ] Add failure/resume tests: missing ffmpeg preflight, source without audio, out-of-bounds crop, interrupted stage, identical rerun skips acquisition/transcription/render, changed hook rerenders only, --force reruns all stages in that command, changed local source invalidates dependents. Verify silent sine-free audio plus an empty fake transcript still renders successfully without invented captions. Mock transcription failures and assert no finished transcript or final-output manifest.
- [ ] Run `python -m unittest tests.test_cli_integration -v`; expect integration failures until the CLI is wired.
- [ ] Implement sequential orchestration and per-stage progress (acquire/transcribe/caption/render/verify/cache hit). Prevalidate whole job before expensive work. Batch failures return nonzero with per-clip results and preserve completed clips. Sidecar includes source identity/URL, source timestamps, requested hook/label/crop, output duration, caption/model provenance, warnings and status `manual_render_unreviewed`. Do not implement AI approval states yet.
- [ ] Document Windows PowerShell and macOS/Linux setup, system FFmpeg with libass, Python>=3.11, optional faster-whisper install, CUDA runtime prerequisites verified against installed version, CPU fallback, local/remote examples, cache/--force semantics, clean CapCut handoff, privacy of sidecars, and known limits. No cloud deployment instructions implying that a scheduler exists.
- [ ] Run `python -m unittest discover -s tests -v`, `python -m clipmatch --help`, `python -m clipmatch render-manual --help`, and `git diff --check`. Require FFmpeg integration tests to run, not skip, before milestone completion. If prerequisites are unavailable, report the blocked checks without claiming full verification.
- [ ] Retain an offline sample render, ffprobe JSON, and extracted frames at 0.2s and a fixture spoken-word time in an ignored work directory. Inspect both layouts visually; verify hook from frame one and captions at the intended timestamp. Synthetic footage does not verify speech-recognition accuracy or real VOD downloads.
- [ ] Commit final changes and stop. Provide exact installation/run commands, paths to outputs, tests/visual evidence, and remaining real-VOD/transcription checks. Wait for owner go-ahead before milestone 2.

## Expected owner commands after implementation

Windows PowerShell, from repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[transcription]"
.\.venv\Scripts\python.exe -m clipmatch render-manual examples\manual.yaml
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The user must replace `sample.mp4` in the example with their own local source or a supported VOD URL and sensible timestamps. Expect captioned video plus subtitle/source files in `examples/renders/` with the default example-relative paths. A rerun reports cache hits; changing only the hook reuses acquisition/transcription. First transcription use downloads model weights. Offline integration tests generate their own media and never need a model.

## Scope exclusions and self-review

Milestone 1 does not implement ingest/analyze commands, creator discovery, chat signals, AI judging, hosting, spending controls, or cross-stream matching. They belong to later approved milestones and must not be represented as working.

Before adopting third-party editor code, inspect the relevant functions, license and dependencies; compare reuse with the small FFmpeg implementation above. No whole-editor fork or extra packages without approval. Preserve attribution for any copied code. This inspection is part of Tasks 3/4, not a paid service trial.

Self-review completed: every milestone 1 design requirement maps to Tasks 1–5; shared time bases and signatures agree; each Review Focus condition has a named test owner. Host has python3 but ffmpeg/ffprobe were not found on PATH during planning. Python version and permission to obtain prerequisites must be checked at execution; do not infer render success from planning.

## Execution status

Tasks 1–4 implemented and verified by the native Windows suite. Task 5 CLI/docs and integration tests are implemented. On November 13, 2026, all 39 tests passed with zero skips using Python 3.11.5 and FFmpeg/ffprobe 9.0.2. Retained both synthetic layouts, clean exports, ffprobe JSON, and extracted frames in ignored work/windows-verification; first-frame hooks and timed captions were visually inspected. Test-only fixes address UTF-8 reading on Windows and modern FFmpeg rotation fixture creation. Real-source transcription/download checks and owner acceptance remain pending; the owner has no source footage prepared yet. Do not equate synthetic verification with milestone acceptance or speech-recognition quality. See docs/HANDOFF.md for current evidence and commands.
