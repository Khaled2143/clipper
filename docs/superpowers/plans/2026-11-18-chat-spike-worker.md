# Twitch chat-spike worker implementation plan

Implement the November 18 Twitch Chat-Spike Clip Bot Spec as a separate Python 3.11 worker. Twitch hosts the video. Discord receives confirmed public links. Use an aiohttp session for HTTP and WebSockets. Keep credentials and per-channel cooldowns on the Railway volume.

1. Implement and exercise the detector in `clipmatch/spikes.py`. Count independent non-overlapping 10-second windows including silent windows. Compare against up to 10 minutes of earlier windows after 5 minutes of warmup. Require at least 20 messages and a 3× increase. Log counts and top reactions. Reserve a 90-second cooldown before issuing a clip request.
2. Implement and test Twitch OAuth refresh, validation, Helix, and Discord in `clipmatch/live_api.py`. Rotate refresh tokens atomically. Serialize refreshes, handle throttling, redact credentials, and confirm a clip for up to 60 seconds. Limit Discord retries and disable mentions.
3. Implement and test live EventSub intake in `clipmatch/live_events.py`. Subscribe, reconcile currently live channels, keep reading during setup, deduplicate notifications, handle server-directed handoffs and lost connections, and respect per-channel subscription rejections.
4. Wire these components into the worker, one-time OAuth setup, local launch instructions and Railway configuration. Verify an offline chat-to-webhook flow, authenticated API edge cases, and the existing renderer suite.

Live acceptance requires a broadcaster where the authorized account may clip, a Discord webhook, and a 24-hour Railway run. Measure link latency and review the clip timing against the observed spike.
