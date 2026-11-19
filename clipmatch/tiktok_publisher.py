"""Create direct TikTok video posts on an owner's connected Buffer channel."""
import asyncio
import re
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout


POST_FIELDS = "id channelId status externalLink error { message }"
CREATE = """
mutation Publish($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { %s } }
    ... on MutationError { message }
  }
}""" % POST_FIELDS
CHANNEL_FIELDS = "id service serviceId name isDisconnected isLocked isQueuePaused"


class BufferFailure(Exception):
    def __init__(self, kind, delay=1800):
        super().__init__(kind)
        self.kind = kind
        self.delay = delay


class Buffer:
    def __init__(self, session, key):
        self.session = session
        self.key = key

    async def request(self, query, variables=None):
        try:
            async with self.session.post(
                "https://api.buffer.com", headers={"Authorization": f"Bearer {self.key}"},
                json={"query": query, "variables": variables or {}}, allow_redirects=False,
                timeout=ClientTimeout(total=60),
            ) as response:
                if response.status == 429:
                    retry = response.headers.get("Retry-After", "")
                    raise BufferFailure("rate limit", int(retry) if retry.isdecimal() else 3600)
                if response.status in (401, 403):
                    raise BufferFailure("check Buffer key and permissions", 3600)
                if response.status != 200:
                    raise BufferFailure("unconfirmed")
                payload = await response.json()
        except (ClientError, ValueError, asyncio.TimeoutError):
            raise BufferFailure("unconfirmed") from None
        if not isinstance(payload, dict):
            raise BufferFailure("unconfirmed")
        data = payload.get("data")
        if errors := payload.get("errors"):
            # Errors on nullable post fields can coexist with a successful mutation.
            created = (data.get("createPost") or {}).get("post") if isinstance(data, dict) else None
            if isinstance(created, dict) and created.get("id"):
                return data
            codes = {(e.get("extensions") or {}).get("code") for e in errors if isinstance(e, dict)}
            if "RATE_LIMIT_EXCEEDED" in codes:
                raise BufferFailure("rate limit", 3600)
            if codes & {"UNAUTHORIZED", "FORBIDDEN"}:
                raise BufferFailure("check Buffer key and permissions", 3600)
            if codes == {"NOT_FOUND"}:
                raise BufferFailure("not found")
            raise BufferFailure("unconfirmed")
        if not isinstance(data, dict):
            raise BufferFailure("unconfirmed")
        return data

    async def destination(self, selected_id=None):
        if selected_id:
            result = await self.request(
                "query($id: ChannelId!) { channel(input:{id:$id}) { " + CHANNEL_FIELDS + " } }",
                {"id": selected_id})
            channels = [result.get("channel")]
        else:
            result = await self.request("{ account { organizations { id } } }")
            channels = []
            for org in (result.get("account") or {}).get("organizations", []):
                result = await self.request(
                    "query($id: OrganizationId!) { channels(input:{organizationId:$id}) { "
                    + CHANNEL_FIELDS + " } }", {"id": org["id"]})
                channels.extend(result.get("channels") or [])
        tiktok = [c for c in channels if c and c.get("service") == "tiktok"]
        if len(tiktok) != 1 or not tiktok[0].get("serviceId"):
            raise BufferFailure("connect one TikTok account or select BUFFER_TIKTOK_CHANNEL_ID", 3600)
        channel = tiktok[0]
        if channel.get("isDisconnected") or channel.get("isLocked") or channel.get("isQueuePaused"):
            raise BufferFailure("reconnect or resume the TikTok channel in Buffer", 3600)
        return channel

    async def posts(self, ids):
        # One read confirms a group of posts, including on Buffer's free API plan.
        arguments = ", ".join(f"$id{i}:PostId!" for i in range(len(ids)))
        fields = " ".join(f"p{i}:post(input:{{id:$id{i}}}){{{POST_FIELDS}}}" for i in range(len(ids)))
        try:
            result = await self.request(f"query({arguments}){{{fields}}}", {f"id{i}": id_ for i, id_ in enumerate(ids)})
        except BufferFailure as exc:
            if exc.kind != "not found":
                raise
            if len(ids) == 1:
                return [None]
            middle = len(ids) // 2
            return await self.posts(ids[:middle]) + await self.posts(ids[middle:])
        return [result.get(f"p{i}") for i in range(len(ids))]


def _cap(message):
    return bool(re.search(r"(?:daily|24.hour|weekly) (?:video |post(?:ing)? |upload )*(?:limit|cap|quota)"
                          r"|too many posts|(?:posting|daily upload) (?:cap|quota)"
                          r"|maximum number of posts|posting too frequently", message, re.I))


def public_video_url(value):
    if not isinstance(value, str):
        return False
    parts = urlsplit(value)
    return (parts.scheme == "https" and parts.netloc.lower() in {"www.tiktok.com", "tiktok.com"}
            and re.fullmatch(r"/@[\w.-]+/video/\d+/?", parts.path) is not None)


def post_result(post, channel_id):
    if not isinstance(post, dict) or not post.get("id") or post.get("channelId") != channel_id:
        return {"phase": "review", "remote_id": post.get("id") if isinstance(post, dict) else None,
                "reason": "verify the post and its TikTok destination in Buffer"}
    state = {"remote_id": post["id"], "phase": "pending", "delay": 10800}
    status = post.get("status")
    if status == "sent":
        url = post.get("externalLink")
        if public_video_url(url):
            state.update(url=url, delay=0)
        else:
            state["reason"] = "sent by Buffer, awaiting the TikTok link"
    elif status == "error":
        if _cap(str((post.get("error") or {}).get("message", ""))):
            # This specific attempt has definitively failed to publish.
            return {"phase": "pending", "failed_id": post["id"], "delay": 21600,
                    "cooldown": 21600, "reason": "TikTok posting limit"}
        state.update(phase="review", reason="Buffer reported a publishing error")
    elif status not in {"scheduled", "sending"}:
        state.update(phase="review", reason="check the post status in Buffer")
    return state


async def create_tiktok(api, channel_id, caption, media_url, checkpoint):
    checkpoint({"phase": "pending", "creating": True})
    try:
        response = await api.request(CREATE, {"input": {
            "channelId": channel_id, "assets": [{"video": {"url": media_url}}],
            "text": caption, "schedulingType": "automatic", "mode": "shareNow", "needsApproval": False,
        }})
    except BufferFailure as exc:
        if exc.kind in {"unconfirmed", "not found"}:
            return {"phase": "review", "reason": "Buffer may have created the post. Check its queue and sent posts."}
        return {"phase": "pending", "delay": exc.delay, "cooldown": exc.delay, "reason": exc.kind}
    result = response.get("createPost") or {}
    if post := result.get("post"):
        state = post_result(post, channel_id)
        if state["phase"] == "pending" and state.get("remote_id") and not (state.get("url") or post.get("error")):
            # The first check can join the next available shared Buffer batch.
            state["delay"] = 60
        return state
    if _cap(str(result.get("message", ""))):
        return {"phase": "pending", "delay": 21600, "cooldown": 21600, "reason": "TikTok posting limit"}
    return {"phase": "review", "reason": "Buffer did not confirm post creation. Check the account and media."}


async def confirm_public(session, state):
    """A sent receipt and an unauthenticated public embed verify the destination."""
    video_url = "https://www.tiktok.com" + urlsplit(state["url"]).path
    try:
        async with session.get("https://www.tiktok.com/oembed", params={"url": video_url},
                               allow_redirects=False, timeout=ClientTimeout(total=15)) as response:
            if response.status == 200:
                result = await response.json(content_type=None)
                if isinstance(result, dict) and result.get("provider_name", "").casefold() == "tiktok":
                    return {**state, "url": video_url, "phase": "published", "reason": "public TikTok video verified"}
    except (ClientError, ValueError, asyncio.TimeoutError):
        pass
    return {**state, "delay": 3600, "reason": "sent by Buffer, public TikTok embed not confirmed"}
