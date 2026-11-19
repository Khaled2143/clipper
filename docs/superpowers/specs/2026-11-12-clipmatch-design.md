# Clipmatch design — approved

Approved by the owner in conversation following design review. Milestone implementation remains gated by the agreed stop points.

## Phase 2 of the live clip workflow

The watcher posts confirmed Twitch links and stream status to `DISCORD_WEBHOOK_URL`. For the finished channel, keep the full Twitch capture and use a fixed script to choose the opening and ending from the original footage. Chat timing helps locate the reaction. Gameplay, facecam and audio changes help identify the setup and payoff. Preserve extra context when the timing is ambiguous. Apply the saved facecam and gameplay layout and export the master without generated speech captions. The renderer's `--send-finished` option sends the MP4 to `DISCORD_FINISHED_WEBHOOK_URL`. A size rejection triggers a smaller encode of the selected edit. Keep the full-quality master for publishing.

## Intent and agreed scope

Produce 3–5 unique short videos per day, manually approved and cross-posted by the owner to TikTok, YouTube Shorts, and Instagram Reels. Quality takes precedence over the count: a day may produce fewer or no passing clips. Eligible content includes genuine reactions, funny commentary, and surprising gameplay with an understandable setup and payoff.

Production should run while the owner's Windows PC is off and avoid occupying its RTX 4070. The recurring cloud budget is approximately $40/month, including compute, storage, and editorial AI. This is a feasibility target pending measurement, not a promised capacity. Development labor and the owner's existing CapCut subscription are outside that operating allocation. Treat $40 as an inclusive ceiling with operational headroom for taxes and billing lag.

The repository is currently named clipper; the Python package and CLI will be clipmatch. Keep the repository directory unchanged.

## Approaches considered

1. Thin custom CLI around existing tools: best control over audio-first processing, resumability, judging, and budget. Requires orchestration and review code.
2. Fork a complete editor such as auto-video-editor: potentially faster captions/layout work, but inherits assumptions, dependencies, and maintenance. Its advertised quality is unverified on the target footage.
3. Packaged clipping service: Eklipse is an evaluation baseline, not an approved subscription. Source limits, external-creator imports, story-reaction quality, and automation need testing.

Selected: the thin custom workflow, evaluating existing editor components before duplicating them. The owner approved this architecture and content rubric. No dependencies beyond the original stack, purchases, cloud deployment, or paid calls are authorized by this design alone. Ask before adding dependencies as requested.

## Quality model

Candidate discovery gathers evidence, not a probability of virality. Separate scores for audience response, streamer response, and gameplay/event interest. Any route can nominate a moment; loudness and chat are not mandatory gates.

- Audience: rolling chat deviations, emotional/hype markers, and independent viewer clips near the same VOD offset. Deduplicate overlapping clips; raw views and clip counts are imperfect popularity signals.
- Streamer: unusual loudness, speech changes, laughter, surprise, commentary, or quiet disbelief. Mixed game/voice audio prevents reliable speaker attribution by default.
- Event: achievement, discovery, unexpected failure, reversal, or other payoff supported by observable footage. Do not infer unseen milestones from hype alone.

Audience retention/rewatch analytics on another creator's VOD are not assumed available. Missing chat or clip offsets are recorded as missing evidence, not zero engagement. A capped sample of non-spike windows helps estimate what discovery misses; it does not guarantee coverage.

Editor review examines candidate media plus surrounding context and timed transcript. It proposes start/end, an evidence-backed hook, event type, timestamped setup/payoff, and reasons. Reject unexplained yelling, routine gameplay without payoff, misleading hooks, incomplete outcomes, and duplicates. Do not stretch clips to meet a quota or a monetization duration.

An independent judge uses a separate review call/session without the editor's persuasive rationale. Prefer a separate model if benchmark results and cost support it; model/provider choice remains open until evaluation. It must receive the actual rendered preview with audio, captions, and the rubric. Transcript-only checks cannot approve visual or audio quality. Record modality limitations explicitly.

Judge output: pass, revise, reject, or uncertain; reason codes, timestamped evidence, detected issues, and rubric/model version. Check context, actual payoff, hook accuracy, cut boundaries, caption meaning/readability, crop, sound, duplicates, and configured spoiler/language/content rules. Hard technical failures block review readiness. Subjective concerns remain visible. Cap automatic revision at one attempt initially; unresolved results remain for human review rather than retrying until approval.

AI approval means ready for owner review. It does not certify rights, music clearance, platform policy compliance, monetization, or expected performance. The owner manually approves every publication.

## Pipeline and data

1. Discover completed VODs for configured creators; retain manual URL and batch inputs. Start with 2–3 active creators, with other entries disabled or unverified. Verify handles before enabling. Optional Twitch clip metadata provides VOD offsets when available; it is a discovery input, not authorization to use footage.
2. Ingest metadata, audio and available chat. Keep original source times and UTC start time where available. Print retention warnings only when supported by metadata; no invented expiry dates.
3. Analyze chat and loudness cheaply. Generate and deduplicate candidate windows with generous context. Transcribe candidate windows first using faster-whisper, CUDA when available, word timestamps, and VAD. Preserve source offset mappings. Full transcription remains optional; cross-stream matching is deferred.
4. Fetch only shortlisted video sections with buffers. Test cloud download reliability before selecting a host. Missing/private/deleted sources produce actionable failures and do not advance to rendering.
5. Editor selection, low-cost preview rendering, independent judge, then owner review. Store selected video promptly: audio cannot recover footage after VOD deletion.
6. Render final 1080×1920 H.264/AAC output plus caption files and source/timestamp sidecar. Generate one platform-neutral master for cross-posting. Target 15–40 seconds as a preference, subordinate to a complete payoff. Longer clips require deliberate selection.

Use yt-dlp, chat-downloader, faster-whisper, NumPy, Typer or argparse, PyYAML, ffmpeg and ffprobe. No database. Keep work/<stream_id> artifacts, candidates/, renders/, review/, and config.yaml. Store editorial decisions and queue states as JSON: candidate, rejected, needs_revision, ready_for_review, approved, posted. AI cannot set owner-approved or posted states. Record manual platform posting information.

Every expensive stage caches outputs. An existing file alone is insufficient proof of success: use atomic writes and completion manifests containing source identity, time range, relevant settings, and tool/model versions. Interrupted work preserves completed chunks. Changing caption style must not retranscribe; relevant input/settings changes invalidate only dependent work. --force explicitly reruns the selected stage.

## Rendering and CapCut

Use configured facecam crop above gameplay, otherwise full source frame over a blurred background. Hook appears from frame one, labels identify the streamer, and captions stay out of bottom/right UI areas. Mixed audio captions may include game dialogue; do not claim voice separation. Manual crop configuration comes before automatic detection.

Export a captioned final and optionally a clean edit with SRT for CapCut finishing. SRT does not preserve ASS animation. No dependency on undocumented CapCut APIs, GUI automation, or project-file modification. Hard cuts first. Defer transitions and voice/game separation. Music heuristics produce warnings, not clearance.

## Cloud and budget proposal

Prefer scheduled, scale-to-zero CPU jobs for discovery/downloads and on-demand GPU transcription. Provider choice is provisional: benchmark Modal first and compare alternatives if download, region, scheduling, or cost constraints fail. Avoid paying for GPU download wait time. Persistent cloud JSON/media storage survives worker shutdown. Sequential jobs initially avoid concurrent JSON writes; a single scheduler owns queue changes.

Provisional allocations: $10 CPU/download/render orchestration, $10 transcription GPU, $10 editor/judge calls, $10 storage/contingency. These are limits to test, not quotes. Recurring free credits may reduce the invoice but must be shown separately from gross resource cost.

Start with at most 15 candidates receiving editorial AI and 8 judged previews per day; lower these when projected costs require it. No guaranteed output count. Track resource usage and conservative cost reservations before dispatch, set provider limits where available, bound retries and runtime, and stop optional work before exhausting the overall budget. Budget alerts alone are insufficient. Provider billing delay prevents an exact application-only financial guarantee.

Keep raw audio/context temporarily and selected footage through review; an initial implementation choice is seven-day unselected-cache retention, while preserving source metadata, decisions, and approved masters until owner deletion. Storage cleanup must not delete the only footage for pending reviews. Provide a private downloadable HTML review bundle first; no public bucket or hosted app is required. Remote jobs continue independently of the local CLI; review downloads occur when the owner returns.

## Evaluation before scale

Use representative real excerpts: loud reactions, quiet commentary, mixed dialogue/music, and false alarms. Owner labels examples and reasons. Evaluate packaged service and reusable editor components without assuming their marketing claims. Paid trials require a concrete proposed spend first.

Measure owner acceptance among AI passes, useful moments missed against owner-labeled examples, correction minutes per accepted clip, caption/crop failures, cost per accepted clip, and job completion/recovery. Include known bad clips and non-spike samples. Keep calibration and evaluation examples separate. Report sample size and disagreements; no success threshold has been established yet. Proposed benchmark gate: owner inspects results and approves the measured quality/cost before unattended expansion.

Offline synthetic ffmpeg fixtures verify render mechanics; they cannot validate entertainment quality. Unit tests cover signal calculations, candidate selection, boundaries, budget accounting, and resume behavior as introduced. Inspect render dimensions/duration/audio with ffprobe and extracted frames.

## Milestones — stop after each

1. Manual render MVP: render-manual spec.yaml supports one or multiple independent outputs, explicit source windows, hook, label, captions, caching, source sidecars, and offline synthetic verification. No cloud account needed to verify locally; target deployment is cloud.
2. Ingest and candidate analysis: resumable audio/chat/metadata, signals, candidate-window transcripts and progress. Verify configured handles before reliance.
3. Automatic selection and review: editor plus independent judge, technical checks, batch individual clips, basic static review bundle and owner decisions. Establish rubric and measured acceptance/cost.
4. Cloud operation: unattended discovery/scheduling, durable artifacts, budget controls, retention and private review delivery. Validate networking and costs with a small deployment before expanding.
5. Quality and usability: stronger duplicate checks, music/spoiler warnings, calibration reporting, clear errors, Windows/cloud setup and GPU notes. Core validation/security/errors needed by earlier milestones are implemented earlier.
6. Optional enhancements: cross-stream scene matching and compilations only if they justify their cost. Original shingle/clustering/bleed requirements apply if this is activated later.

After every milestone provide exact commands, expected artifacts, verification evidence, limitations, and wait for the owner's go-ahead. Implementation planning follows this approved design; implementation had not begun at approval.

## Research references — checked November 12, 2026

- https://github.com/kiy0ni/auto-video-editor (MIT; selective reuse candidate, not validated)
- https://github.com/ricardogr07/clipsmith (alternative pipeline reference)
- https://eklipse.gg/help/how-much-does-eklipse-premium-cost/
- https://eklipse.gg/help/how-much-credit-do-i-have-per-day-to-upload-clips/
- https://modal.com/pricing
- https://frontend.modal.com/docs/guide/budgets
- https://dev.twitch.tv/docs/api/clips
- https://www.capcut.com/help/how-to-import-subtitles

Prices, allowances, and provider/model availability must be rechecked before deployment. No cloud provider, AI model, or paid service is selected irrevocably.
