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

Keep the terminal running to watch live channels. You can start midway through a broadcast. Discord receives a notice for each live channel once the worker is watching its chat, including channels already streaming at startup. The worker logs a JSON line for each completed bucket and starts a fresh warmup after a gap in chat coverage. Edit the optional settings in `.env.example` in your own `.env` and restart to tune the detector. The default 20-message floor means quiet channels may rarely trigger.

Chat messages arrive over EventSub. For watchlists of up to five channels the worker also subscribes to live-status events. For longer lists it checks live status in batches every 30 seconds so those status subscriptions do not exhaust Twitch's WebSocket allowance. [Twitch subscription limits](https://dev.twitch.tv/docs/eventsub/manage-subscriptions/#subscription-limits)

### Automatic finished videos

Set `DISCORD_FINISHED_WEBHOOK_URL` to the webhook for the finished channel and restart the watcher. Install the `live` extra and put FFmpeg with libass and ffprobe on PATH. On a Mac with Homebrew's `ffmpeg-full`, start the watcher from the checkout with

```sh
export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"
caffeinate -i .venv/bin/python -m clipmatch.live watch
```

A startup log with `"finish_clips": true` confirms automatic rendering is enabled. The watcher posts the full Twitch link to the original channel and queues the edit. For new spikes it records how long before the clip request the chat activity began rising. The finisher downloads the best available landscape MP4 and uses that timing as an approximate guide. It looks for camera cuts, movement in the game and facecam, and sustained changes in sound. Nearby pauses help it choose where to start and end. Earlier clips can be edited from the footage alone.

The finished MP4 keeps the original audio and a channel label. After selecting the edit, the finisher starts with one screenshot from the middle of the original landscape footage. If the facecam cannot be confirmed, it checks nearby frames for repeated evidence of the same inset. A verified facecam fills the top 720 pixels, and a separate gameplay crop fills the lower 1200 pixels. Camera-only shots, game-only shots and uncertain crops use a centered, full-height portrait view. Sources up to 18 seconds keep their full duration. Weak cues get a wider window of up to 36 seconds.

The default `CLIP_LAYOUT_MODE=screenshot` runs the bundled YuNet face detector on the CPU during the clip job. It checks faces against the streamer's saved camera regions and plausible alternate corners. One position applies to that edit, so a camera move during the clip can be missed. The optional `CLIP_LAYOUT_MODE=vision` uses Ollama and `SOCIAL_VISION_MODEL` to check multiple frames and switch layouts within an edit. Source times, camera decisions and fallbacks appear under `layout_review` in the master’s JSON. Camera profiles in `clipmatch/layouts.json` use normalized left, top, width and height. YourRAGE has two saved lower-corner positions.

Discord receives the verified 1080×1920 master first. If Discord rejects its size, the sender encodes the selected edit to fit 9 MiB. For publishing, use the retained master in `<state-dir>/finished/<channel>/<clip>/renders/`. Its JSON sidecar records the source times, the selected cue and any chat timing. The full Twitch download is beside the `renders` directory as `source.mp4`.

To inspect an earlier source locally, run `python -m clipmatch.finish_clip --state-dir .clipbot --channel LOGIN --clip-id ID --url URL --render-only`. Use `clipmatch-live enqueue-render --channel LOGIN --clip-id ID --url URL` to queue an earlier clip for first delivery. If Twitch confirms a clip before its video is available, the queue retries the download after one minute and again two minutes after the second failure. It logs `render_review` if all three attempts fail. Check the finished channel before using `clipmatch-live retry-render --clip-id ID` after an uncertain upload.

### Shared YouTube Shorts title and TikTok description

On Windows, install Ollama and run `ollama pull qwen3-vl:8b-instruct`. Set `SOCIAL_VISION_MODEL=qwen3-vl:8b-instruct` in the watcher's `.env`. The watcher connects to loopback port 11434 by default. On Railway, set `SOCIAL_OLLAMA_URL` to your Ollama service's private address (for example, `http://ollama.railway.internal:11434`) and install the selected vision model on that service. For speech context, install the transcription extra and set `SOCIAL_SPEECH_MODEL` to a cached faster-whisper model in the worker's filesystem. Without a cached speech model the writer uses frames alone.

With visual review enabled, the writer samples the selected source window, drafts two titles and checks their claims against the footage. The Railway image also includes a small English speech model that runs during the clip job. When visual copy is unavailable, the writer tries a high-confidence phrase from the actual edit, a usable original Twitch title, then factual stream and game information. The adjacent `.social.json` records the title, description, evidence method and associated master. Discord receives this copy with the video.

### Public YouTube Shorts and TikTok

The worker uses the full 1080×1920 master for both destinations. TikTok captions use the clip title. Set `PUBLIC_POSTING_ENABLED=1` to queue new finished clips on the persistent volume, including while an account is being connected. Each platform has its own receipts and retry schedule. Posts can go out immediately when a clip arrives. A backlog uses one slot per hour per destination by default. Set `PUBLISH_INTERVAL_MINUTES` to change that spacing.

Set `DISCORD_CLIP_DELIVERY=0` for publishing-only operation. Twitch events still trigger renders and titles. The worker sends new videos directly to the two publication queues. Set `DISCORD_WATCH_ALERTS=1` to receive the watchlist and live or offline updates through `DISCORD_WEBHOOK_URL`.

1. **YouTube.** Enable the YouTube Data API v3 in your Google Cloud project, configure its OAuth consent screen and download a Desktop app client JSON. On Windows run `.\.venv\Scripts\python.exe -m clipmatch.youtube_publisher authorize --client-secrets "C:\path\to\client.json"` and choose `@llkaydll1304`. The command verifies the channel ID before saving the private grant to `.clipbot/youtube-grant.json`. Store its JSON in the worker’s private `YOUTUBE_GRANT_JSON` variable. Long-running uploads need a production OAuth grant. Google normally expires an external Testing grant after seven days. [Enable the API](https://console.cloud.google.com/apis/library/youtube.googleapis.com) · [Desktop OAuth client](https://console.cloud.google.com/auth/clients) · [Google OAuth expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
2. **TikTok.** Connect your public TikTok profile in Buffer. Create a personal key with account-read and post-read/write permissions and save it privately as `BUFFER_API_KEY` on the worker. With one connected TikTok channel, the worker binds its account ID automatically. For multiple TikTok channels, select the intended Buffer channel ID via `BUFFER_TIKTOK_CHANNEL_ID`. Add a Railway-provided HTTPS domain targeting port 8080 and set `PORT=8080`. Buffer retrieves each master through its own unlisted media URL. Free-plan API limits and TikTok’s account-specific posting cap apply. [Buffer channels](https://publish.buffer.com/settings/channels) · [Create an API key](https://publish.buffer.com/settings/api) · [TikTok posting limits](https://developers.tiktok.com/docs/en/content-sharing-guidelines)
3. Restart the worker after changing credentials. Check `publishing` in the Railway logs. Each `publish_published` event includes the verified destination link. For counts and recent receipts run `python -m clipmatch.publishing` on the mounted worker. A submission interrupted before the provider confirms a post ID can enter review. Check the destination’s sent posts before retrying. Each platform can continue while the other is capped.

Set `PUBLISH_CLEANUP_CONFIRMED=1` on Railway to release new source and master media after both public receipts are at least 24 hours old. The worker retains pending media, publication history and metadata.

### Run on Railway

Deploy this repository as a **single worker** from the directory containing `railway.toml`. Attach a volume at `/data`. In Railway Variables set the same client ID, secret, channels, and `DISCORD_WEBHOOK_URL`. To deliver finished videos, also set `DISCORD_FINISHED_WEBHOOK_URL`. Stop the local watcher before transferring its current `TWITCH_REFRESH_TOKEN` from the local `.clipbot/refresh-token` file. The worker uses Railway's volume mount path for its rotated token, pending confirmations, cooldowns, render queue, finished clips and publication receipts. Twitch and Discord use outbound connections. TikTok publishing also uses the worker's public media domain.

When moving an existing watcher, let the current render finish and stop the local process between jobs. With both watchers stopped, copy the contents of its state directory to the mounted volume before starting the hosted watcher. This preserves queue history, finished media, Discord receipts and per-channel title history. Compare the queue counts at cutover. Size the volume for the existing archive plus new recordings; the worker retains the original Twitch footage and the vertical master for each completed clip.

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

To send finished MP4s to a second Discord channel, create a webhook in that channel under **Edit Channel → Integrations → Webhooks**. Save it as `DISCORD_FINISHED_WEBHOOK_URL` in the local `.env`, install the `live` extra, and add `--send-finished` to the render command. The watcher uses `DISCORD_WEBHOOK_URL` for Twitch links and status. Finished-channel delivery requires a configured `facecam_rect` for each clip and skips generated speech captions. A manual job uses the source times in your spec. The renderer uploads the verified vertical master as a playable attachment. Repeating the command skips an identical MP4 already delivered to that webhook. If Discord rejects an upload, the master stays in the output folder. After a connection failure, check the channel before retrying.

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
