# Automatic finished clips

Implement the fixed Phase 2 workflow in the [project design](../specs/2026-11-12-clipmatch-design.md). A configured finished webhook enables rendering. The watcher's original webhook continues to receive Twitch links and stream status.

- Queue each confirmed clip ID once in SQLite. Run one finisher at a time in a subprocess. Recover pending work across restarts, and hold uncertain sends for review.
- Validate and download the public clip. Apply a saved streamer-specific facecam crop, transcribe locally, burn captions into the vertical MP4, and verify the output with ffprobe.
- Upload the MP4 to the finished webhook. Preserve the full master if Discord requires a smaller attachment.
- Exercise the queue, renderer and delivery in local tests. Inspect a real Agent00 export and confirm delivery from the active watcher.
