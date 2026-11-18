# clipmatch

Clipmatch can watch live Twitch chat and send a clip to Discord when a channel's message rate spikes. The manual renderer creates vertical videos from selected VOD windows.

## Live chat-spike worker

One background process monitors the channels in `CHANNELS` using your Twitch account. It asks Twitch for a 60-second clip when a 10-second window reaches both 20 messages and three times the channel's recent average. After five minutes of observed chat it can trigger, using up to ten minutes of history. Each channel has its own 90-second cooldown. Discord gets the confirmed public link, message count, baseline, and top words or emotes. Twitch hosts the video.

### Connect your accounts locally

1. Register a **Confidential** Twitch application. Enter **`http://localhost:17563/callback`** as an OAuth redirect URL. Copy the client ID and generate a client secret. [Twitch developer console](https://dev.twitch.tv/console/apps)
2. In the target Discord channel, open **Edit Channel → Integrations → Webhooks** and create or select a webhook. Copy its URL.
3. From the folder containing `pyproject.toml`, install the live extra into a Python 3.11+ virtual environment. The commands below use PowerShell. Skip the first line if the project already has a virtual environment.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[live]"
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Fill in `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `DISCORD_WEBHOOK_URL`, and the comma-separated `CHANNELS` list. Run the following on the computer where you will open the browser.

```powershell
.\.venv\Scripts\python.exe -m clipmatch.live authorize
.\.venv\Scripts\python.exe -m clipmatch.live watch
```

Sign in once with an account allowed to clip the channels you chose. The default grant includes `clips:edit` and `user:read:chat`. For authenticated IRC fallback during a chat-service outage, run `authorize --irc-fallback` to also grant `chat:read`. The worker reads and refreshes its saved token automatically. Local credentials and state live in the ignored `.env` and `.clipbot/` paths.

Keep the terminal running to watch live channels. You can start midway through a broadcast. The worker checks current live status, logs a JSON line for each completed bucket, and starts a fresh warmup after a gap in chat coverage. Edit the optional settings in `.env.example` in your own `.env` and restart to tune the detector. The default 20-message floor means quiet channels may rarely trigger.

Chat messages arrive over EventSub. For watchlists of up to five channels the worker also subscribes to live-status events. For longer lists it checks live status in batches every 30 seconds so those status subscriptions do not exhaust Twitch's WebSocket allowance. [Twitch subscription limits](https://dev.twitch.tv/docs/eventsub/manage-subscriptions/#subscription-limits)

### Run on Railway

Deploy this repository as a **single worker** from the directory containing `railway.toml`. Attach a volume at `/data`. In Railway Variables set the same client ID, secret, channels, and Discord webhook, plus `TWITCH_REFRESH_TOKEN` seeded privately from the local `.clipbot/refresh-token` file. Stop the local watcher before transferring the current token. The worker uses Railway's volume mount path for its rotated token, pending confirmations, and cooldowns. The repository supplies the container, start command, and restart policy. The worker uses outbound connections and requires no public domain.

### Check the first live run

Watch for the `watching`, `online`, `bucket`, `spike`, `clip_requested`, and `clip_result` log events. An `offline` line pauses counting for that channel. A confirmed Discord post includes a `clips.twitch.tv` link. If Twitch rejects clipping, the worker reports the channel and reason. If Discord delivery itself fails, the worker logs the result and the confirmed URL. A disconnect during a POST can leave delivery uncertain.

For live acceptance, run the worker for 24 hours including a disconnect, measure time from spike to Discord (target 30 seconds), and watch the clips to check that the setup and payoff made it in. Twitch confirmation can take up to 60 seconds. [Twitch Clips API](https://dev.twitch.tv/docs/api/reference/clips)

## Manual vertical renderer

Every manual render is `manual_render_unreviewed`. Watch it before posting.

For current progress, verification blockers, and Windows continuation steps, read [the handoff](docs/HANDOFF.md).

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
- `facecam_rect` is `[x, y, width, height]` in displayed source pixels after autorotation, before correction for non-square pixels. Bounds are checked in that same coordinate system.
- Without a crop: full frame over a blurred background. With a crop: facecam above fitted gameplay.
- Keep hooks short (roughly 75 characters maximum, depending on wrapping). Labels must be at most 30 characters and are never silently truncated. Use truthful wording supported by the footage.
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

Install the live extra to run the full suite. The live integration test uses local Twitch and Discord simulators and runs without credentials.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[live]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The render tests generate FFmpeg `testsrc` and tones, inject known word timings at the speech-model boundary, and run real rendering and ffprobe verification. They cover both layouts, independent clips, nonzero source offsets, silent audio, cache/force behavior, interrupted transcription, and clean exports. No real downloads or model weights are used.

**Skipped media tests mean render verification is incomplete.** A test run without full FFmpeg/ffprobe cannot establish working video output. Synthetic tests also cannot establish transcription accuracy or editorial quality; review real footage separately.

To inspect your own output:

```powershell
ffprobe -v error -show_streams -show_format -of json renders\reaction_a.mp4
ffmpeg -ss 0.2 -i renders\reaction_a.mp4 -frames:v 1 renders\reaction_a-frame.png
```

Expect 1080×1920, H.264 video, AAC audio, and duration within 0.15 seconds of the selected window. Watch audio sync, text placement, first-frame hook, crop, and ending. Font rendering can vary by machine. A small libass preflight checks required glyphs, prints selected fonts, and records their identity in sidecars/cache keys. Missing fallback glyphs fail clearly. Visual layout review is still necessary.

## Limits and troubleshooting

- Expired/private/subscriber-only sources may be inaccessible. The CLI reports failure and preserves completed other clips; it never intentionally falls back to a full remote-video download.
- Restricted networks can prevent VOD or model downloads. A cloud host must be tested before deployment.
- Remote section timing and platform compatibility require testing against representative real VODs. They are not certified by offline fixtures.
- Whisper hears the mixed audio. Captions may include game dialogue; speaker separation and game-audio ducking are not implemented.
- Review captions for slang/names, hallucinations and overlapping voices. No AI judge, spoiler check, music-risk detection or publication approval is active yet.
- Keep renders/source sidecars private if source URLs contain access tokens; they preserve the source you supplied. Media errors avoid echoing raw downloader stderr.
- Do not run overlapping jobs with the same output directory/clip IDs. There is no concurrent writer coordination in this milestone.
- Full-VOD ingestion, offline candidate discovery, cloud spending controls, a review page and automatic retention are future milestones.

## Manual renderer milestone verification

Implementation is on branch `milestone1-manual-render`; on the original laptop its checkout is `.worktrees/manual-render` of the parent checkout. It has not been merged into main.

On Windows with Python 3.11.5 and FFmpeg/ffprobe 9.0.2, **all 39 tests pass with zero skips**. Both synthetic layouts and clean exports render successfully; retained masters have 1080x1920 H.264/AAC video with matching 3-second video/audio durations. First-frame hooks and highlighted-caption frames have been visually inspected. See the handoff for sample paths and reproduction commands. **Real-footage transcription, real VOD section timing, and owner acceptance remain pending.** CUDA device detection alone does not verify inference.

An independent code review identified seven substantive issues. A single regression-test fix pass addressed source/output collisions, source provenance after optional clean-export failure, audio timeline normalization, bounded typography, font resolution/provenance, rotation/sample-aspect handling, and independent video/audio duration checks. Windows integration tests now exercise the media behavior. Windows verification also corrected two test portability issues: implicit CP1252 decoding of UTF-8 JSON and a rotation fixture command that no longer creates rotation metadata on FFmpeg 9.

One minor optimization remains deferred: the same local VOD can be hashed more than once in a batch. This can slow repeated clips from a large recording but does not invalidate cache correctness.

Implementation decisions: Git worktree fallback after the managed tool failed; locally available PyYAML used only inside the ignored verification venv; small original FFmpeg modules retained after reviewing an existing editor's modules. Production installation uses the declared normal dependencies. The tradeoffs are an extra checkout, environment differences still to test, and responsibility for maintaining the renderer.
