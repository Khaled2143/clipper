"""Write one clip's headlines on the worker CPU, using only evidence from the edit."""
from contextlib import closing
import json
from pathlib import Path
import re

from .local_copy import _FILLER, _WORDS, contextual_title, safe_text
from .title_feedback import STYLE_GUIDANCE


def _words(text):
    return {word.casefold().replace("’", "'").removesuffix("'s") for word in _WORDS.findall(text)}


_CONTEXT = {"stream", "clip", "game", "screen", "highlight", "moment", "someone", "something",
            "everybody", "people", "one", "let", "see", "say", "said", "play", "playing"}
_ATTRIBUTION = re.compile(r"\b(?:says|said|swears|screams?|shouts?|yells?|cries|sobs?|laughs?|"
                          r"panics?|ragequits?|breaks down|loses (?:it|his mind))\b", re.I)
_WARDROBE = re.compile(r"\b(?:shirts?|jerseys?|outfits?|tank top|hoodies?|headbands?|headphones?|headsets?|clad)\b", re.I)
_RECORDING = re.compile(r"\b(?:panels?|quadrants?|split[- ]screen|webcams?|facecams?|"
                        r"(?:two|three|four|multiple) (?:people|players|screens|views)|"
                        r"(?:clip|footage|video) (?:shows|features|depicts|captures))\b", re.I)
_OUTCOME_CLAIM = re.compile(r"\b(?:wins?|won|loses?|lost|beats|finishes|finished|completes|completed|"
                            r"crosses the finish line)\b", re.I)


def supported_draft(title, name, category, beats, outcome, observed="", *, implied_payoff=False):
    if (not safe_text(title) or not 4 <= len(title.split()) <= 12 or len(title) > 100
            or re.search(r'[#()"“”!:|]|(?:^|\s)[\'‘].+[\'’](?:\s|$)', title)
            or not re.search(r"\b" + re.escape(name) + r"\b", title, re.I) or _ATTRIBUTION.search(title)):
        return False
    evidence = " ".join([*(text for _, _, text in beats), observed])
    onscreen = outcome[0] if outcome else ""
    words = _words(title)
    context = _words(name + " " + (category or ""))
    questions = set()
    stated = _words(observed)
    for _, _, line in beats:
        if line.endswith("?"):
            questions.update(_words(line))
        else:
            stated.update(_words(line))
    if not title.endswith("?") and words & (questions - stated - _FILLER - context - _CONTEXT):
        return False
    if _WARDROBE.search(title) and not any(_WARDROBE.search(line) for _, _, line in beats):
        return False
    if _RECORDING.search(title) and not any(_RECORDING.search(line) for _, _, line in beats):
        return False
    if observed and not beats and not outcome and _OUTCOME_CLAIM.search(title):
        return False
    if set(re.findall(r"\d+", title)) - set(re.findall(r"\d+", f"{name} {category} {evidence} {onscreen}")):
        return False
    if outcome and not implied_payoff and not _words(onscreen) <= words:
        return False
    anchors = _words(evidence) - context - _FILLER - _CONTEXT - _words(onscreen)
    if not anchors:
        return bool(outcome) and not implied_payoff
    # A single shared word like "game" or the channel name cannot ground a new claim.
    return len((words - context) & anchors) >= min(2 if not outcome else 1, len(anchors))


def _answer(model, instruction, request, *, examples=(), tokens=55, prefix=""):
    # The pinned Qwen chat template supports a closed, empty thinking section for direct responses.
    def turn(role, content):
        return f"<|im_start|>{role}\n{content}<|im_end|>\n"

    prompt = turn("system", instruction)
    for question, answer in examples:
        prompt += turn("user", question) + turn("assistant", "<think>\n\n</think>\n\n" + answer)
    prompt += turn("user", request) + "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    result = model(prompt + prefix, max_tokens=tokens, temperature=0, stop=["<|im_end|>", "\n"], echo=False)
    return (prefix + result["choices"][0]["text"]).strip().strip('"').rstrip(".")


_VOICE = ("Write the one line a fan would text a friend with this clip. Pick the specific thing worth "
          "sending. If the timing makes a joke, let it land. If the moment is quiet, keep it plain. "
          "Use the channel name naturally. Skip camera or screen narration, stock hype, emojis, hashtags, "
          "invented reactions and speaker attribution. Output just the title.")
_EXAMPLES = (
    ("Channel: Lena. Seen: A key snaps in a lock. Heard: That was the spare.",
     "There goes Lena’s spare key"),
    ("Channel: Niko. Heard: The food truck takes arcade tokens.",
     "Niko can buy lunch with arcade tokens"),
)


def _visual_reader(model, projector, frames):
    from llama_cpp import mtmd_cpp
    from llama_cpp._logger import llama_log_callback
    from llama_cpp.llama_chat_format import MTMDChatHandler

    mtmd_cpp.mtmd_log_set(llama_log_callback, None)
    mtmd_cpp.mtmd_helper_log_set(llama_log_callback, None)
    vision = MTMDChatHandler(str(projector), verbose=False, use_gpu=False)
    content = []
    for index, image in enumerate(frames, 1):
        content += [{"type": "text", "text": f"Instant {index}:"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}]

    def read(question, tokens):
        messages = [{"role": "user", "content": [{"type": "text", "text": question}, *content]}]
        reply = vision(llama=model, messages=messages, enable_thinking=False, max_tokens=tokens, temperature=0)
        text = " ".join(reply["choices"][0]["message"]["content"].split())
        return text[:400] if safe_text(text) else ""
    return read


def payoff_setup(beats, outcome):
    """Use a nearby, completed statement as the setup for a timing joke."""
    if outcome:
        for beat in reversed(beats):
            _, end, line = beat
            if (0 <= outcome[1] - end <= 8 and not
                    re.search(r"\?|\b(?:no|not|never|if|maybe|might|could|would|without)\b|n['’]t\b", line, re.I)):
                return beat
    return None


def write_titles(model_path, name, category, beats, outcome, recent, *, frames=None, preferences=()):
    """Return verified alternatives in preference order. The model lives only for this clip."""
    if (not beats and not frames) or not Path(model_path).is_file():
        return []
    from llama_cpp import Llama

    context = f"Channel: {name}." + (f" Game: {category}." if category else "")
    voice = _VOICE
    style = ""
    if preferences:
        voice += " " + STYLE_GUIDANCE
        # Keep examples out of the evidence sent to the factual checks and escape chat role markers.
        style = "\nTitle preferences: " + json.dumps(preferences, ensure_ascii=False).replace("<", "\\u003c")
    with closing(Llama(model_path=str(model_path), n_ctx=2048, n_batch=256, n_threads=2, n_threads_batch=2,
                       n_gpu_layers=0, verbose=False)) as model:
        setup = payoff_setup(beats, outcome)
        if setup:
            # A literal afterlife farewell followed by an in-game death supports this particular joke.
            farewell = re.fullmatch(r"(?:(?:I'll|I’ll|I will) )?(?:see|meet) you in (Valhalla|heaven)"
                                    r"[.!]?", setup[2], re.I)
            if farewell and outcome[0] in {"wasted", "you died"}:
                destination = "Valhalla" if farewell[1].casefold() == "valhalla" else "heaven"
                title = f"{name} made it to {destination} real quick"
                if title not in recent:
                    return [title]
            facts = f"{context} Heard: {setup[2]}. In-game result seconds later: {outcome[0]}."
            examples = (
                ("Channel: Niko. Heard: We're going home with the trophy. In-game result seconds later: defeat.",
                 "About that trophy, Niko"),
                ("Channel: Lena. Heard: This is my last lockpick. In-game result seconds later: busted.",
                 "So much for Lena’s last lockpick"),
            )
            title = _answer(model, voice, facts + style, examples=examples)
            if title not in recent and supported_draft(title, name, category, [setup], outcome, implied_payoff=True):
                verdict = _answer(model, "Does the joke follow directly from these words and the game's result? "
                                  "Allow figurative wording for an in-game event. Reject invented actions, people, "
                                  "reactions, causes, or items even if plausible. Answer only YES or NO.",
                                  facts + "\nTitle: " + title, tokens=4)
                if verdict.upper() == "YES":
                    return [title]

        def draft(facts, lines, visible):
            evidence = context + " " + facts
            title = _answer(model, voice, evidence + style, examples=_EXAMPLES)
            def candidates():
                yield title
                yield _answer(model, "Turn the supplied line into a short conversational headline. "
                              "Keep the actor, object and location straight. Preserve questions. An "
                              "instruction tells what is possible; it does not prove anyone did it. "
                              "Use 4-8 words. Write only the headline, without any channel name.",
                              visible or lines[0][2], examples=(
                                  ("Collect spare lives at the green door.", "Spare lives are waiting at the green door"),
                                  ("Check who's on the radio.", "Who's on the radio?"),
                                  ("The bike skids into the gravel.", "The bike skids into the gravel")))
            for candidate in candidates():
                if name.casefold() not in candidate.casefold() and len(candidate.split()) <= 8:
                    candidate = contextual_title(candidate, name)
                if not supported_draft(candidate, name, category, lines, None, visible) or candidate in recent:
                    continue
                verdict = _answer(model, "Can all the material claims in the headline be verified by the evidence? "
                                  "Keep the subject, object and location straight. Talking about an action does "
                                  "not prove it happened. Reply with YES or NO only.",
                                  evidence + "\nHeadline: " + candidate, tokens=4)
                if verdict.upper() == "YES":
                    return candidate
            return None

        if beats:
            title = draft("Heard in the clip: " + " ".join(b[2] for b in beats), beats, "")
            if title:
                return [title]
        # A separate visual draft cannot turn overheard speculation into a visible fact.
        if frames:
            read_frames = _visual_reader(model, Path(model_path).with_name("mmproj-F16.gguf"), frames)
            observed = read_frames("These are four timestamps from ONE continuous clip, taken in chronological "
                                   "order. Track the same subject and object across time. Ignore the inset "
                                   "webcam, clothes, timers and chat. Describe a specific change in the main "
                                   "scene. Reply with one short factual sentence.", 52)
            if observed:
                def visual_titles():
                    yield draft("Visible in the clip: " + observed, (), observed)
                    # A visible activity can be named even when the result of a race or shot is unknown.
                    topic = _answer(model, "Extract one specific activity or object from this description, "
                                    "as a 2-6 word noun phrase. Keep the concrete words. Omit the outcome, "
                                    "camera, clothing, names and numbers. Reply with the noun phrase alone.",
                                    observed, tokens=20, examples=(
                                        ("The player misses a shot during a street basketball game.", "a street basketball game"),
                                        ("A player browses the supermarket shelves.", "the supermarket shelves")))
                    if safe_text(topic) and 2 <= len(topic.split()) <= 6:
                        yield contextual_title(topic[0].upper() + topic[1:], name)
                for title in visual_titles():
                    if title and supported_draft(title, name, category, (), None, observed) and title not in recent:
                        answer = read_frames("These pictures are successive moments in one video. Could this "
                                             "headline accurately describe what is visible? Reject any invented "
                                             "actions, identity or outcome. Reply YES or NO only. Headline: " + title, 4)
                        if answer.strip(".").upper() == "YES":
                            return [title]
        return []
