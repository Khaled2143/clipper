# Automatic YouTube Shorts on llKaydll

The owner chose automatic public YouTube uploads for `@llkaydll1304`. Its public channel ID is `UCqd66XANzi22AxRvjeoOz8A`. The posting cadence and the owner's Google authorization are pending. This document proposes how the first release should work.

## Publishing choices

The existing Discord delivery gives the owner an MP4 and copy for manual posting. A private YouTube upload would put each video into Studio for review. The owner selected automatic public posting. Discord continues to receive every finished clip. The publisher sends a selection of new clips directly to YouTube using Google's resumable upload API.

## Eligible clips and timing

Explicit activation records the newest queue row already present. Only subsequent jobs qualify. A candidate needs a completed render, a verified full-quality MP4, and a ready title and description for that master. The proposed source list is the five channels already monitored on Windows. The owner can narrow that list to the creators whose clips they want to republish when enabling YouTube. A later change to the Twitch watch list does not add a YouTube source automatically. Use the UTC activation time and retain it across process restarts.

The cadence options offered are three Shorts per day spaced at least eight hours apart, five per day spaced at least four hours and 48 minutes apart, or one Short followed by a persistent pause. Publishing starts only after the owner picks one. For the first release, select the newest eligible candidate from the last 24 hours when a slot opens. Skip empty slots and carry no backlog into a burst. These selection and freshness rules are proposed implementation choices. A pause stops new uploads and retains the publication history.

## Google connection

Run the Google desktop OAuth flow on Windows with the owner's downloaded client file. The owner signs in directly with Google. Request upload permission and read access to identify the authorized channel. Compare the channel returned by `channels.list(mine=true)` with the channel ID above before enabling publication. A different channel or revoked grant leaves public posting paused. Keep credentials in ignored local state or private Railway variables. The host refreshes its access without a running Windows PC.

## Posting and recovery

A separate publisher task uses the existing durable SQLite state. Twitch monitoring and Discord delivery continue when YouTube is unavailable. Claim the posting slot and clip once, with a unique key on YouTube channel and Twitch clip ID. Use the vertical master, generated title and one-line description with the original Twitch link. Set the requested visibility to public. Persist the resumable session URI before sending video bytes, keep it out of logs, and resume that session after a timeout or restart. An ambiguous final response that cannot be reconciled puts that publication in review and keeps its slot reserved. Store the remote video ID and returned visibility on confirmation. Private or rejected results surface for review.

## Acceptance

Use a local YouTube simulator to verify wrong-channel sign-in, exclusion of old clips, rate limits, a restart mid-upload and an ambiguous final acknowledgement. Verify Discord still delivers during a YouTube failure. The live check requires the owner's grant and selected cadence. Confirm that the first eligible Short has one video ID, the intended channel, and public visibility, then provide its watch link.
