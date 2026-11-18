"""Command line entry point for Twitch authorization and chat monitoring."""
import argparse
import asyncio
from contextlib import closing
import os
import signal
import sqlite3
import sys

from aiohttp import ClientSession
from dotenv import load_dotenv

from .live_api import ApiError, LiveAPI
from .live_auth import authorize
from .live_events import listen
from .live_worker import ClipState, Worker, WorkerConfig, log


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
            worker = Worker(api, session, config.webhook_url, channels, config.settings, state)
            for missing in sorted(set(config.channels) - set(channels.values())):
                worker.channel_error(missing, "Twitch could not find this channel")
            worker.resume()
            log("watching", channels=list(channels.values()))

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
