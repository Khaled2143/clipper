# Automated social titles implementation plan

Implement the [shared title design](../specs/2026-11-18-shared-social-title-design.md) on the Windows watcher. Use local Ollama vision inference, local Whisper when available, FFmpeg and the existing Discord webhook.

1. Test Discord multipart content on both attachment sizes, receipt reuse, unavailable inference and repeated candidate headlines.
2. Generate a chronological frame sheet and optional transcript from the verified master. Ask the model for grounded alternatives, choose a distinct title within 100 characters and retain the copy for retries and review.
3. Pass the chosen text with the MP4, with a plain review notice and source link if generation fails. Read the game category when supplied by the downloader.
4. Compare titles for saved clips from one stream against their footage, verify the complete test suite, enable the Windows configuration and check the live delivery path.
