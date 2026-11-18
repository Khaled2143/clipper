"""One-time browser authorization for the live worker."""
import asyncio
import secrets
from urllib.parse import urlencode
import webbrowser

from aiohttp import ClientSession, web

from .live_api import REQUIRED_SCOPES, exchange_code, save_refresh_token
from .live_worker import required, state_directory


CALLBACK_PORT = 17563


async def authorize(env, *, irc_fallback=False):
    client_id = required(env, "TWITCH_CLIENT_ID")
    secret = required(env, "TWITCH_CLIENT_SECRET")
    path = state_directory(env) / "refresh-token"
    state = secrets.token_urlsafe(32)
    result = asyncio.get_running_loop().create_future()

    async def callback(request):
        if not secrets.compare_digest(request.query.get("state", "").encode(), state.encode()):
            return web.Response(status=400, text="Invalid sign-in state. Use the link from your terminal.")
        if result.done():
            return web.Response(text="Sign-in response already received.")
        result.set_result(request.query.get("code"))
        return web.Response(text="Twitch sign-in response received. Return to your terminal.")

    app = web.Application()
    app.router.add_get("/callback", callback)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", CALLBACK_PORT).start()
        redirect = f"http://localhost:{runner.addresses[0][1]}/callback"
        scopes = set(REQUIRED_SCOPES)
        if irc_fallback:
            scopes.add("chat:read")
        query = urlencode(dict(client_id=client_id, redirect_uri=redirect, response_type="code",
                               scope=" ".join(sorted(scopes)), state=state, force_verify="true"))
        url = f"https://id.twitch.tv/oauth2/authorize?{query}"
        print(f"Open this URL on this computer to connect your Twitch account:\n{url}", flush=True)
        await asyncio.to_thread(webbrowser.open, url)
        try:
            code = await asyncio.wait_for(result, 300)
        except TimeoutError:
            raise ValueError("Twitch sign-in timed out. Run authorize again.") from None
        if not code:
            raise ValueError("Twitch sign-in was declined. Run authorize again to connect.")
    finally:
        await runner.cleanup()
    async with ClientSession() as session:
        token = await exchange_code(session, client_id, secret, code, redirect)
    save_refresh_token(path, token)
    print(f"Twitch connected. Saved the refresh token to {path}.")
