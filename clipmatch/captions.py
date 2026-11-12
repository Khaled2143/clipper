"""Editable SRT plus safe ASS overlays and word-by-word highlighting."""
from pathlib import Path
import textwrap
import unicodedata
from .spec import Settings
from .transcribe import Word, clean_words


def escape_ass(text: str) -> str:
    # Fullwidth equivalents keep untrusted text out of ASS override syntax.
    return ' '.join(text.split()).replace('\\', '＼').replace('{', '｛').replace('}', '｝')


def timestamp(seconds: float, *, srt: bool=False) -> str:
    scale = 1000 if srt else 100
    ticks = max(0, round(seconds*scale))
    whole, fraction = divmod(ticks, scale)
    h, rest = divmod(whole, 3600)
    m, s = divmod(rest, 60)
    return f'{h:02}:{m:02}:{s:02},{fraction:03}' if srt else f'{h}:{m:02}:{s:02}.{fraction:02}'


def ass_document(events: list[str], settings: Settings) -> str:
    return f'''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{settings.font},56,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,5,100,180,0,1
Style: Hook,{settings.font},52,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,8,80,180,60,1
Style: Label,{settings.font},42,&H0000FFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,8,80,180,820,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
''' + '\n'.join(events) + '\n'


def event(start: float, end: float, style: str, text: str, layer: int=0) -> str:
    return f'Dialogue: {layer},{timestamp(start)},{timestamp(end)},{style},,0,0,0,,{text}'


def fitted_size(text: str, maximum: int, width: int) -> int:
    # Conservative em-width estimate; libass can still wrap within style margins.
    units = sum(1.1 if unicodedata.east_asian_width(c) in ('W', 'F') or c in 'WM@#%'
                else .4 if c.isspace() else .7 for c in text)
    size = min(maximum, int(width / max(1, units)))
    if size < 18:
        raise ValueError('Text is too long to fit readably; shorten the hook/label/caption')
    return size


def overlay_events(hook: str, label: str, duration: float) -> list[str]:
    if len(label) > 30:
        raise ValueError('label must be at most 30 characters; it will not be truncated')
    lines = textwrap.wrap(escape_ass(hook), width=27)
    if len(lines) > 3:
        raise ValueError('Hook is too long to fit safely; use at most about 75 characters')
    size = min([52, 120 // max(1, len(lines))] + [fitted_size(line, 52, 800) for line in lines])
    hook_text = r'{\fs' + str(size) + '}' + r'\N'.join(lines)
    label_size = fitted_size(escape_ass(label), 42, 800)
    label_text = r'{\fs' + str(label_size) + '}' + escape_ass(label)
    return [event(0, duration, 'Hook', hook_text, 1),
            event(0, duration, 'Label', label_text, 1)]


def caption_events(words: list[Word], duration: float) -> list[str]:
    words = clean_words(words, duration)
    events = []
    groups, current, chars = [], [], 0
    for word in words:
        if current and (len(current) >= 4 or chars+len(word.text) > 24
                        or word.start-current[-1].end > .7):
            groups.append(current)
            current, chars = [], 0
        current.append(word)
        chars += len(word.text)+1
    if current:
        groups.append(current)
    for group in groups:
        for active, word in enumerate(group):
            pieces = []
            for i, member in enumerate(group):
                # One long ASR token cannot overflow into platform UI.
                safe = escape_ass(member.text)
                pieces.append((r'{\c&H00FFFF&}' if i == active else r'{\c&HFFFFFF&}')+safe)
            end = min(word.end, group[active+1].start) if active+1<len(group) else word.end
            if end > word.start:
                size = fitted_size(' '.join(w.text for w in group), 56, 760)
                prefix = r'{\pos(490,1360)\fs' + str(size) + '}'
                events.append(event(word.start, end, 'Caption', prefix+' '.join(pieces)))
    return events


def write_captions(words: list[Word], ass_path: Path, srt_path: Path,
                   duration: float, settings: Settings) -> None:
    words = clean_words(words, duration)
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(ass_document(caption_events(words,duration),settings), encoding='utf-8')
    srt_path.write_text('\n\n'.join(f'{i}\n{timestamp(w.start,srt=True)} --> {timestamp(w.end,srt=True)}\n{w.text}'
                                   for i,w in enumerate(words,1))+ ('\n' if words else ''), encoding='utf-8')
