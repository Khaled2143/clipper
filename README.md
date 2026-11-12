# clipmatch

A local CLI for turning explicitly selected VOD windows into independent vertical clips. One source moment can become several separate videos. This is the **manual-render milestone**; discovery, chat analysis, AI selection/judging, cloud jobs and cross-stream matching are not implemented yet.

Every result is `manual_render_unreviewed`. Watch it before posting.

## Requirements

- Python **3.11+** (3.12 recommended).
- Full FFmpeg and ffprobe on PATH, built with **libass**, **libx264**, and **AAC**. A browser's stripped-down FFmpeg binary is insufficient.
- PyYAML and yt-dlp (installed by the package).
- faster-whisper for captions (optional installation extra). Its first use downloads model weights.
- Internet for remote VOD downloads/model downloads. Local media and synthetic render tests can run offline without speech models.

No paid accounts, cloud subscription, or API key is required for this milestone.

## Windows PowerShell setup

Install Python and a full FFmpeg build. If using Windows Package Manager:

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Gyan.FFmpeg -e
```

Open a new terminal after installation, then from this repository's implementation checkout:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[transcription]"
ffmpeg -version
ffprobe -version
.\.venv\Scripts\python.exe -m clipmatch --help
```

Using the venv's Python directly avoids PowerShell activation policy changes.

### RTX 4070 transcription

Set `device: auto` in config for CUDA detection and CPU fallback on initialization failure. Set `device: cuda` to require CUDA and report initialization failures instead of falling back. Auto compute type is `float16` on CUDA and `int8` on CPU. A model loaded for one clip is reused within the batch. The default model is `small`; benchmark accuracy before changing it.

NVIDIA drivers alone may not supply all required inference libraries. The upstream faster-whisper documentation currently describes CUDA 12 cuBLAS and cuDNN 9 for its current CTranslate2 versions. Follow [its current GPU requirements](https://github.com/SYSTRAN/faster-whisper#gpu) for your installed version and make the DLL directories visible on PATH. This project does not install or modify NVIDIA drivers. GPU inference failure during processing preserves completed stages but does not silently change models.

Rendering currently uses CPU libx264 even on a GPU computer. GPU encoding and cloud execution are later decisions.

## macOS/Linux development

With Python 3.11+ and full FFmpeg installed:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[transcription]'
.venv/bin/python -m clipmatch --help
```

For example, macOS users with Homebrew can install system tools using `brew install python@3.12 ffmpeg`. CUDA is unavailable on Mac; use CPU. The production target remains cloud workers, with Windows for owner testing.

## Describe the selected moments

Edit `examples/manual.yaml`. Replace the sample path and times; the supplied example requires at least 20 seconds of source footage.

```yaml
version: 1
clips:
  - id: reaction_a
    source: ./sample.mp4
    start: "00:00:02"
    end: "00:00:08"
    label: STREAMER
    hook: "That confidence lasted one second"
  - id: reaction_b
    source: "https://www.twitch.tv/videos/REPLACE_WITH_VOD_ID"
    start: "01:20:10"
    end: "01:20:30"
    label: OTHER
    hook: "Wait for the reaction"
    # facecam_rect: [1500, 0, 420, 300]
```

- Each entry produces **its own video**, in spec order. No automatic concatenation.
- Times refer to the original VOD, not the downloaded section. Numeric seconds also work; quote HH:MM:SS timestamps.
- Local sources resolve relative to the spec file. Use an absolute Windows path in single quotes if preferred, e.g. `'D:\Recordings\stream.mp4'`.
- `facecam_rect` is `[x, y, width, height]` in source pixels, not normalized coordinates. Bounds are checked against the source.
- Without a crop: full frame over a blurred background. With a crop: facecam above fitted gameplay.
- Keep hooks short (roughly 75 characters maximum, depending on wrapping). Keep labels short. Use truthful wording supported by the footage.
- Unknown fields, duplicate IDs (including case-only duplicates), invalid times and invalid crops fail clearly.
- Streamer handles are deliberately not populated: no handles have been verified, and this milestone uses explicit sources/labels.

## Render

```powershell
.\.venv\Scripts\python.exe -m clipmatch render-manual examples\manual.yaml --config config.yaml --clean
```

With `--config config.yaml`, default output/cache directories are beside that config. Without `--config`, defaults are beside the spec; for the example this means `examples/renders/` and `examples/work/`. Inline `settings` in the spec override explicit config values. The root config is not auto-loaded.

Expect stage messages followed by `ready for manual review: ...`. Each selected clip produces:

```text
renders/reaction_a.mp4          # 1080×1920 H.264/AAC, hook/label/captions
renders/reaction_a.ass          # timed word-highlight captions
renders/reaction_a.srt          # editable subtitles
renders/reaction_a.json         # source window, settings/provenance, warnings
renders/reaction_a.manifest.json
renders/reaction_a.clean.mp4    # optional --clean; all burned-in text removed
work/reaction_a/audio.wav
work/reaction_a/transcript.json
```

Remote sources additionally cache their buffered downloaded section. Selected local recordings are read directly. Source audio is required; a silent audio track is supported, but a video with no audio track is rejected.

### Resume and force

Repeat the same command to reuse completed downloads, exact-window audio, transcripts and video. Artifact hashes and completion manifests detect missing, partial or changed files. Changing only hook/font/crop reuses audio and transcript; changing source content/times invalidates the affected downstream work. Changes to relevant tool/model versions also invalidate stages.

```powershell
.\.venv\Scripts\python.exe -m clipmatch render-manual examples\manual.yaml --config config.yaml --force
```

`--force` reruns all expensive stages selected by that command. Previously completed outputs are retained if encoding fails. Resume is per selected clip/stage; long clip transcription is not chunk-resumable in this manual MVP. Choose short windows.

For an explicitly caption-free render (no model required):

```powershell
.\.venv\Scripts\python.exe -m clipmatch render-manual examples\manual.yaml --config config.yaml --no-captions
```

Hook/label remain. Sidecar records captions disabled. This does not verify caption/transcription quality.

### CapCut

Import the clean MP4 and matching SRT into CapCut Desktop to correct text and choose your own styling. SRT does not carry ASS highlighting or fonts. The captioned master is also available if no finishing is needed. There is no CapCut API or desktop automation dependency.

## Offline verification

Install only the base package if speech recognition isn't needed for development:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The render tests generate FFmpeg `testsrc` and tones, inject known word timings at the speech-model boundary, and run real rendering and ffprobe verification. They cover both layouts, independent clips, nonzero source offsets, silent audio, cache/force behavior, interrupted transcription, and clean exports. No real downloads or model weights are used.

**Skipped media tests mean render verification is incomplete.** A test run without full FFmpeg/ffprobe cannot establish working video output. Synthetic tests also cannot establish transcription accuracy or editorial quality; review real footage separately.

To inspect your own output:

```powershell
ffprobe -v error -show_streams -show_format -of json renders\reaction_a.mp4
ffmpeg -ss 0.2 -i renders\reaction_a.mp4 -frames:v 1 renders\reaction_a-frame.png
```

Expect 1080×1920, H.264 video, AAC audio, and duration within 0.15 seconds of the selected window. Watch audio sync, text placement, first-frame hook, crop, and ending. Font rendering can vary by machine; requested fonts may be substituted by libass. Sidecars currently flag visual font review.

## Limits and troubleshooting

- Expired/private/subscriber-only sources may be inaccessible. The CLI reports failure and preserves completed other clips; it never intentionally falls back to a full remote-video download.
- Restricted networks can prevent VOD or model downloads. A cloud host must be tested before deployment.
- Remote section timing and platform compatibility require testing against representative real VODs. They are not certified by offline fixtures.
- Whisper hears the mixed audio. Captions may include game dialogue; speaker separation and game-audio ducking are not implemented.
- Review captions for slang/names, hallucinations and overlapping voices. No AI judge, spoiler check, music-risk detection or publication approval is active yet.
- Keep renders/source sidecars private if source URLs contain access tokens; they preserve the source you supplied. Media errors avoid echoing raw downloader stderr.
- Do not run overlapping jobs with the same output directory/clip IDs. There is no concurrent writer coordination in this milestone.
- Full-VOD ingestion, candidate discovery, $40 cloud spending controls, queue/review page and automatic retention are future milestones, not included here.
