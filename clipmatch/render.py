"""Aspect-preserving vertical layouts and atomic, verified video rendering."""
from dataclasses import asdict
import os
from pathlib import Path
import tempfile

from .cache import fingerprint
from .captions import ass_document, caption_events, overlay_events
from .media import MediaInfo, SourceWindow, probe, run, tool_versions
from .spec import ClipSpec, Settings
from .transcribe import Word


def validate_crop(rect: tuple[int,int,int,int] | None, info: MediaInfo) -> None:
    if rect is None:
        return
    x,y,w,h = rect
    if x<0 or y<0 or w<=0 or h<=0 or x+w>info.width or y+h>info.height:
        raise ValueError('facecam_rect exceeds source frame bounds')


def layout_filter(rect: tuple[int,int,int,int] | None, fps: int) -> str:
    source = f'[0:v:0]scale=trunc(iw*sar/2)*2:ih,setsar=1,fps={fps}'
    if rect is None:
        return (source+',split=2[bgsrc][fgsrc];'
                '[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,'
                'boxblur=12:2,scale=1080:1920[bg];'
                '[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];'
                '[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1[base]')
    x,y,w,h = rect
    return (f'[0:v:0]split=2[camsrc][gamesrc];'
            f'[camsrc]crop={w}:{h}:{x}:{y},scale=1080:600:force_original_aspect_ratio=decrease:'
            'force_divisible_by=2,pad=1080:600:(ow-iw)/2:(oh-ih)/2,setsar=1[cam];'
            f'[gamesrc]scale=trunc(iw*sar/2)*2:ih,setsar=1,scale=1080:800:'
            'force_original_aspect_ratio=decrease:force_divisible_by=2,'
            'pad=1080:800:(ow-iw)/2:(oh-ih)/2[game];'
            f'color=c=black:s=1080x1920:r={fps}[canvas];'
            '[canvas][cam]overlay=0:200:shortest=1[upper];'
            f'[upper][game]overlay=0:800:shortest=1,fps={fps},setsar=1[base]')


def render_key(clip: ClipSpec, window: SourceWindow, words: list[Word],
               settings: Settings, clean: bool) -> str:
    return fingerprint(dict(stage='render-v1', source=window.identity, start=window.trim_start,
                            duration=window.duration, hook=clip.hook, label=clip.label,
                            crop=clip.facecam_rect, words=[asdict(w) for w in words],
                            font=settings.font, fps=settings.fps, crf=settings.crf,
                            preset=settings.preset, clean=clean, tools=tool_versions()))


def render_clip(clip: ClipSpec, window: SourceWindow, words: list[Word], settings: Settings,
                output: Path, *, clean: bool=False) -> MediaInfo:
    info = probe(window.path)
    validate_crop(clip.facecam_rect,info)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='render-',dir=output.parent) as directory:
        directory = Path(directory).resolve()
        events = overlay_events(clip.hook,clip.label,window.duration)+caption_events(words,window.duration)
        # Relative ASS name in a controlled cwd avoids Windows drive/filter escaping entirely.
        (directory/'overlays.ass').write_text(ass_document(events,settings),encoding='utf-8')
        graph = layout_filter(clip.facecam_rect,settings.fps)
        graph += ';[base]null[outv]' if clean else ';[base]ass=filename=overlays.ass[outv]'
        temporary = directory/'output.mp4'
        run(['ffmpeg','-v','error','-y','-ss',str(window.trim_start),'-i',str(window.path.resolve()),
             '-t',str(window.duration),'-filter_complex',graph,'-map','[outv]','-map','0:a:0',
             '-af','apad','-c:v','libx264','-preset',settings.preset,'-crf',str(settings.crf),
             '-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-b:a','192k',
             '-movflags','+faststart',str(temporary)],cwd=directory)
        result = probe(temporary)
        if ((result.width,result.height)!=(1080,1920) or not result.has_audio
                or result.video_codec!='h264' or result.audio_codec!='aac'
                or abs(result.duration-window.duration)>.15):
            raise ValueError('Rendered video failed geometry/codec/audio/duration verification')
        os.replace(temporary,output)
        return result
