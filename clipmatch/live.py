"""Command line entry point for Twitch authorization and chat monitoring."""
import argparse
import asyncio
from contextlib import closing
import os
from pathlib import Path
import signal
import sqlite3
import sys

from aiohttp import ClientSession
from dotenv import load_dotenv

from .live_api import ApiError, LiveAPI
from .live_auth import authorize
from .live_events import listen
from .live_worker import ClipState, Worker, WorkerConfig, log, state_directory


async def retry_service(operation, *args):
    delay = 1
    while True:
        try:
            return await operation(*args)
        except ApiError as exc:
            if exc.status not in (0, 429) and exc.status < 500:
                raise
            log("twitch_retry", operation=operation.__name__, reason=str(exc), delay=delay)
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)


async def run_worker(config):
    token_path = config.state_dir / "refresh-token"
    async with ClientSession() as session:
        api = LiveAPI(session, config.client_id, config.client_secret, config.refresh_token, token_path)
        user_id = await retry_service(api.validate)
        channels = await retry_service(api.resolve_channels, config.channels)
        if not channels:
            raise ValueError("Twitch could not find any of the configured CHANNELS.")
        with closing(ClipState(config.state_dir)) as state:
            render_queue = state.renders if config.finish_clips else None
            if render_queue is not None:
                render_queue.deliver_finished = config.deliver_finished
            worker = Worker(api, session, config.webhook_url, channels, config.settings, state,
                            render_queue=render_queue, discord_delivery=config.discord_delivery,
                            discord_watch_alerts=config.discord_watch_alerts)
            if config.discord_watch_alerts:
                worker.announce_watchlist()
            for missing in sorted(set(config.channels) - set(channels.values())):
                worker.channel_error(missing, "Twitch could not find this channel")
            worker.resume()
            log("watching", channels=list(channels.values()), finish_clips=config.finish_clips,
                discord_delivery=config.discord_delivery, discord_watch_alerts=worker.discord_watch_alerts,
                public_posting=config.public_posting)

            async def tick():
                while True:
                    await asyncio.sleep(.25)
                    worker.tick()

            async def validate_hourly():
                while True:
                    await asyncio.sleep(3600)
                    if await retry_service(api.validate) != user_id:
                        raise ValueError("Twitch account changed. Restart the worker after authorization.")

            tasks = [
                asyncio.create_task(listen(session, api, channels, user_id, worker.live, worker.chat,
                                           worker.gap, worker.channel_error, on_pause=worker.pause)),
                asyncio.create_task(tick()),
                asyncio.create_task(validate_hourly()),
            ]
            if render_queue is not None:
                tasks.append(asyncio.create_task(render_queue.run(log)))
                if config.public_posting:
                    from .publishing import run_publisher
                    tasks.append(asyncio.create_task(run_publisher(state, session, os.environ, log)))
            try:
                finished, _ = await asyncio.wait([*tasks, worker.failure], return_when=asyncio.FIRST_COMPLETED)
                if worker.failure in finished:
                    raise worker.failure.result()
                for task in finished:
                    task.result()
                raise RuntimeError("A worker service stopped unexpectedly.")
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await worker.stop()


async def _run(args):
    if args.command == "authorize":
        await authorize(os.environ, irc_fallback=args.irc_fallback)
        return
    if args.command in {"enqueue-render", "retry-render"}:
        directory = args.state_dir or state_directory(os.environ)
        with closing(ClipState(directory)) as state:
            if args.command == "enqueue-render":
                from .finish_clip import validate_clip
                url = validate_clip(args.channel, args.clip_id, args.url)
                added = state.renders.enqueue(args.clip_id, args.channel, url)
                log("render_queued" if added else "render_already_recorded", clip_id=args.clip_id)
            else:
                if not state.renders.retry(args.clip_id):
                    raise ValueError("No render is awaiting review for that clip ID.")
                log("render_requeued", clip_id=args.clip_id)
        return
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    try:
        await run_worker(WorkerConfig.from_env(os.environ))
    finally:
        if sys.platform != "win32":
            loop.remove_signal_handler(signal.SIGTERM)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create Twitch clips when live chat spikes.")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("authorize", help="Connect your Twitch account on this computer")
    auth.add_argument("--irc-fallback", action="store_true", help="Also authorize read-only IRC chat")
    commands.add_parser("watch", help="Monitor CHANNELS and deliver clips to Discord")
    enqueue = commands.add_parser("enqueue-render", help="Queue an existing public Twitch clip for the watcher")
    enqueue.add_argument("--channel", required=True, help="Broadcaster's Twitch login")
    enqueue.add_argument("--url", required=True, help="Public Twitch clip URL")
    retry = commands.add_parser("retry-render", help="Requeue a failed render after checking the finished channel")
    for command in (enqueue, retry):
        command.add_argument("--clip-id", required=True, help="Twitch clip ID")
        command.add_argument("--state-dir", type=Path, help="Use the watcher's state directory")
    args = parser.parse_args(argv)
    load_dotenv(".env", override=False, interpolate=False)
    try:
        asyncio.run(_run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"clipmatch-live: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
