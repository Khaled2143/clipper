"""Send a verified MP4 to the configured finished-clips channel."""
import asyncio
import json
import os
from pathlib import Path
import tempfile

from aiohttp import ClientSession, FormData
from dotenv import dotenv_values

from .cache import atomic_json, file_hash
from .live_api import ApiError, _request, discord_webhook_url
from .media import probe, run


_ATTACHMENT_LIMIT_BYTES = 9 * 1024 * 1024


class FinishedDeliveryUncertain(ValueError):
    """Discord may have posted the attachment. An automatic retry can duplicate it."""


class _AttachmentTooLarge(ValueError):
    pass


def finished_webhook_url() -> str:
    name = "DISCORD_FINISHED_WEBHOOK_URL"
    value = os.environ.get(name, dotenv_values(".env", interpolate=False).get(name))
    if not value:
        raise ValueError(f"Set {name} to the webhook for your finished-clips channel.")
    return discord_webhook_url(value, name)


def _make_attachment(master: Path, destination: Path) -> None:
    source = probe(master)
    byte_limit = min(_ATTACHMENT_LIMIT_BYTES, master.stat().st_size - 1)
    # Reserve room for muxing overhead. A measured retry handles short clips where
    # encoder buffering makes the actual average bitrate overshoot its target.
    total_kbps = int(byte_limit * 8 * 0.94 / source.duration / 1000)
    audio_kbps = min(96, max(24, total_kbps // 6))
    video_kbps = total_kbps - audio_kbps
    for _ in range(3):
        if video_kbps < 16:
            break
        run([
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(master.resolve()),
            "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "fast",
            "-pix_fmt", "yuv420p", "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps}k", "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart", str(destination),
        ])
        size = destination.stat().st_size
        if size <= byte_limit:
            encoded = probe(destination)
            if ((encoded.width, encoded.height, encoded.video_codec, encoded.audio_codec)
                    != (1080, 1920, "h264", "aac")
                    or encoded.video_duration is None or encoded.audio_duration is None
                    or abs(encoded.video_duration - (source.video_duration or source.duration)) > 0.15
                    or abs(encoded.audio_duration - (source.audio_duration or source.duration)) > 0.15):
                raise ValueError("Discord attachment failed video or audio verification. Local master retained.")
            return
        video_kbps = int((video_kbps + audio_kbps) * byte_limit / size * 0.9) - audio_kbps
    raise ValueError("Could not fit the full-length MP4 within the attachment size limit. Local master retained.")


async def _upload(session: ClientSession, webhook: str, path: Path, filename: str) -> None:
    with path.open("rb") as video:
        form = FormData()
        form.add_field("payload_json", json.dumps({"allowed_mentions": {"parse": []}}))
        form.add_field("files[0]", video, filename=filename, content_type="video/mp4")
        try:
            message = await _request(session, "Discord finished upload", "POST", webhook,
                                     params={"wait": "true"}, data=form, timeout_seconds=120, attempts=1)
        except ApiError as exc:
            if exc.status == 413 or (exc.status == 400 and exc.code == 40005):
                raise _AttachmentTooLarge("Finished MP4 is too large for this Discord channel. Local master retained.") from None
            if exc.status in (0, 408) or exc.status >= 500:
                raise FinishedDeliveryUncertain(
                    "Discord upload unconfirmed. Check the finished channel before retrying."
                ) from None
            raise ValueError(str(exc)) from None
    attachments = message.get("attachments")
    if (not message.get("id") or not isinstance(attachments, list)
            or not any(isinstance(item, dict) and item.get("filename") for item in attachments)):
        raise FinishedDeliveryUncertain("Discord did not confirm the attachment. Check the finished channel before retrying.")


async def deliver_finished(output: Path, receipt: Path, webhook: str) -> bool:
    """Return True after a new delivery, False for a previously confirmed upload."""
    webhook_id = webhook.rsplit("/", 2)[-2]
    digest = await asyncio.to_thread(file_hash, output)
    try:
        deliveries = json.loads(receipt.read_text(encoding="utf-8"))
    except FileNotFoundError:
        deliveries = {}
    if deliveries.get(webhook_id) == digest:
        return False

    async with ClientSession() as session:
        try:
            await _upload(session, webhook, output, output.name)
        except _AttachmentTooLarge:
            with tempfile.TemporaryDirectory(prefix="clipmatch-discord-") as directory:
                attachment = Path(directory) / "attachment.mp4"
                await asyncio.to_thread(_make_attachment, output, attachment)
                await _upload(session, webhook, attachment, output.name)
    deliveries[webhook_id] = digest
    try:
        atomic_json(receipt, deliveries)
    except OSError:
        raise FinishedDeliveryUncertain(
            "Discord confirmed the upload but its receipt could not be saved. Check the finished channel before retrying."
        ) from None
    return True
