"""Aspect-preserving vertical layouts and atomic, verified video rendering."""
from dataclasses import asdict
import os
from pathlib import Path
import tempfile

from .cache import fingerprint, file_hash
from .captions import ass_document, caption_events, overlay_events, event, escape_ass
from .media import MediaInfo, SourceWindow, probe, run, tool_versions
from .spec import ClipSpec, LayoutSegment, Rect, Settings
from .transcribe import Word


def validate_crop(rect: tuple[int,int,int,int] | None, info: MediaInfo) -> None:
    if rect is None:
        return
    x,y,w,h = rect
    if x<0 or y<0 or w<=0 or h<=0 or x+w>info.width or y+h>info.height:
        raise ValueError('facecam_rect exceeds source frame bounds')


def gameplay_rect(facecam_rect: Rect, width: int, height: int) -> Rect | None:
    """Choose disjoint gameplay with room for the largest 1080:1200 source window."""
    x, y, w, h = facecam_rect
    candidates = [(0, 0, x, height), (x + w, 0, width - x - w, height),
                  (0, 0, width, y), (0, y + h, width, height - y - h)]
    def panel_fit(rect):
        w, h = rect[2:]
        return min(w / 1080, h / 1200), w * h

    return max((r for r in candidates if r[2] >= 2 and r[3] >= 2), key=panel_fit, default=None)


def _adaptive_filter(segments: tuple[LayoutSegment, ...], info: MediaInfo, fps: int) -> str:
    # A returning crop reuses its existing filter branch. All branches consume the
    # same CFR video clock, and overlays switch on [start, end) edit-relative times.
    intervals = {}
    for i, segment in enumerate(segments):
        end = segments[i + 1].start if i + 1 < len(segments) else None
        intervals.setdefault(segment.facecam_rect, []).append((segment.start, end))
    crops = list(intervals)
    if not crops:
        raise ValueError('Adaptive layout requires at least one segment')
    source = f'[0:v:0]fps={fps}'
    if len(crops) > 1:
        source += f',split={len(crops)}'
    graph = [source + ''.join(f'[src{i}]' for i in range(len(crops)))]

    def cover(panel_height, center_x=.5, center_y=.5):
        # Crop in source pixels before scaling. SAR converts the desired display
        # aspect into source coordinates, including anamorphic inputs.
        sar = 'if(gt(sar,0),sar,1)'
        return (f"crop=w='max(1,min(iw,ih*1080/{panel_height}/{sar}))':"
                f"h='max(1,min(ih,iw*{sar}*{panel_height}/1080))':"
                f"x='clip(iw*{center_x}-ow/2,0,iw-ow)':"
                f"y='clip(ih*{center_y}-oh/2,0,ih-oh)':exact=1,"
                f"scale=1080:{panel_height},setsar=1")

    for i, crop in enumerate(crops):
        if crop is None:
            graph.append(f"[src{i}]{cover(1920)}[view{i}]")
        else:
            validate_crop(crop, info)
            game = gameplay_rect(crop, info.width, info.height)
            if game is None:
                raise ValueError('The facecam_rect leaves no separate gameplay area')
            cx, cy, cw, ch = crop
            gx, gy, gw, gh = game
            # Keep the original action center when the camera exclusion permits it.
            # The crop clamps to the nearest legal position inside the game region.
            game_x = (info.width / 2 - gx) / gw
            game_y = (info.height / 2 - gy) / gh
            graph.extend([f'[src{i}]split[cam{i}][game{i}]',
                          f'[cam{i}]crop={cw}:{ch}:{cx}:{cy}:exact=1,{cover(720)}[upper{i}]',
                          f'[game{i}]crop={gw}:{gh}:{gx}:{gy}:exact=1,{cover(1200, game_x, game_y)}[lower{i}]',
                          f'[upper{i}][lower{i}]vstack[view{i}]'])

    visible = 'view0'
    for i, crop in enumerate(crops[1:], 1):
        enable = '+'.join(f'gte(t,{start})' if end is None else f'gte(t,{start})*lt(t,{end})'
                          for start, end in intervals[crop])
        graph.append(f"[{visible}][view{i}]overlay=enable='{enable}':shortest=1[mix{i}]")
        visible = f'mix{i}'
    graph.append(f'[{visible}]setsar=1[base]')
    return ';'.join(graph)


def layout_filter(rect: Rect | None, fps: int, layout: str = 'manual', *,
                  segments: tuple[LayoutSegment, ...] = (), info: MediaInfo | None = None) -> str:
    if layout == 'adaptive':
        if info is None:
            raise ValueError('Adaptive layout requires source geometry')
        return _adaptive_filter(segments, info, fps)
    source = f'[0:v:0]scale=trunc(iw*sar/2)*2:ih,setsar=1,fps={fps}'
    if layout == 'stacked':
        if rect is None:
            raise ValueError('The stacked layout requires facecam_rect.')
        x, y, w, h = rect
        # Work in square pixels before scaling either panel to cover its entire area.
        square = 'scale=trunc(iw*sar/2)*2:ih,setsar=1'
        def cover(height):
            return (f'scale=1080:{height}:force_original_aspect_ratio=increase:'
                    f'force_divisible_by=2,crop=1080:{height},setsar=1')
        return (f'[0:v:0]split=2[camsrc][gamesrc];'
                f'[camsrc]crop={w}:{h}:{x}:{y},{square},{cover(720)}[cam];'
                f'[gamesrc]{square},{cover(1200)}[game];'
                f'[cam][game]vstack=inputs=2:shortest=1,fps={fps},setsar=1[base]')
    if rect is None:
        return (source+',split=2[bgsrc][fgsrc];'
                '[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,'
                'boxblur=12:2,scale=1080:1920[bg];'
                '[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];'
                '[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1[base]')
    x,y,w,h = rect
    return (f'[0:v:0]split=2[camsrc][gamesrc];'
            f'[camsrc]crop={w}:{h}:{x}:{y},scale=trunc(iw*sar/2)*2:ih,setsar=1,scale=1080:600:force_original_aspect_ratio=decrease:'
            'force_divisible_by=2,pad=1080:600:(ow-iw)/2:(oh-ih)/2,setsar=1[cam];'
            f'[gamesrc]scale=trunc(iw*sar/2)*2:ih,setsar=1,scale=1080:800:'
            'force_original_aspect_ratio=decrease:force_divisible_by=2,'
            'pad=1080:800:(ow-iw)/2:(oh-ih)/2[game];'
            f'color=c=black:s=1080x1920:r={fps}[canvas];'
            '[canvas][cam]overlay=0:200:shortest=1[upper];'
            f'[upper][game]overlay=0:800:shortest=1,fps={fps},setsar=1[base]')



def resolve_fonts(font: str, text: str) -> dict:
    """Ask the same libass renderer to shape all required glyphs before caching a render."""
    from .spec import Settings
    messages = []
    with tempfile.TemporaryDirectory(prefix='clipmatch-font-') as directory:
        directory = Path(directory)
        settings = Settings(font=font)
        (directory / 'fontcheck.ass').write_text(
            ass_document([event(0, 1, 'Hook', escape_ass(text))], settings), encoding='utf-8')
        run(['ffmpeg', '-v', 'info', '-f', 'lavfi', '-i', 'color=black:s=64x64:r=1:d=1',
             '-vf', 'ass=filename=fontcheck.ass', '-frames:v', '1', '-f', 'null', '-'],
            cwd=directory, diagnostics=messages)
    if any('failed to find any fallback' in line.lower() for line in messages):
        raise ValueError('No installed font can render a required glyph; install a suitable font')
    selections = sorted(set(line.split(' -> ', 1)[1].split(',', 1)[0].strip()
                            for line in messages if 'fontselect:' in line and ' -> ' in line))
    if not selections:
        raise ValueError('Could not validate libass font resolution; check installed fonts and FFmpeg')
    identities = []
    inventory_needed = False
    for selected in selections:
        path = Path(selected)
        if path.is_file():
            identities.append({'font': path.name, 'sha256': file_hash(path)})
        else:
            identities.append({'font': selected})
            inventory_needed = True
    inventory = []
    if inventory_needed:
        # Some font backends report PostScript names rather than files. Include the
        # installed-font inventory so a changed font environment invalidates the cache.
        roots = [Path('/System/Library/Fonts'), Path('/Library/Fonts'), Path.home()/'Library/Fonts',
                 Path('/usr/share/fonts'), Path('/usr/local/share/fonts'),
                 Path.home()/'.local/share/fonts', Path.home()/'.fonts']
        if os.environ.get('WINDIR'):
            roots.append(Path(os.environ['WINDIR'])/'Fonts')
        if os.environ.get('LOCALAPPDATA'):
            roots.append(Path(os.environ['LOCALAPPDATA'])/'Microsoft/Windows/Fonts')
        for root in roots:
            if root.is_dir():
                for path in root.rglob('*'):
                    if path.suffix.lower() in ('.ttf', '.otf', '.ttc') and path.is_file():
                        stat = path.stat()
                        inventory.append((str(path), stat.st_size, stat.st_mtime_ns))
    return {'requested': font, 'selected': [Path(s).name for s in selections],
            'identities': identities, 'inventory_key': fingerprint({'files': sorted(inventory)})}

def render_key(clip: ClipSpec, window: SourceWindow, words: list[Word],
               settings: Settings, clean: bool, font_identity: dict | None = None) -> str:
    fonts = font_identity if font_identity is not None else resolve_fonts(
        settings.font, clip.hook + ' ' + clip.label + ' ' + ' '.join(w.text for w in words))
    stage = {'stacked': 'render-stacked-v1', 'adaptive': 'render-adaptive-v3'}.get(settings.layout, 'render-v2')
    inputs = dict(stage=stage, source=window.identity, start=window.trim_start,
                            duration=window.duration, hook=clip.hook, label=clip.label,
                            crop=clip.facecam_rect, words=[asdict(w) for w in words],
                            font=settings.font, fps=settings.fps, crf=settings.crf,
                            preset=settings.preset, clean=clean, fonts=fonts, tools=tool_versions())
    if settings.layout == 'adaptive':
        inputs['layout_segments'] = [asdict(segment) for segment in clip.layout_segments]
    return fingerprint(inputs)


def render_clip(clip: ClipSpec, window: SourceWindow, words: list[Word], settings: Settings,
                output: Path, *, clean: bool=False) -> MediaInfo:
    info = probe(window.path)
    validate_crop(clip.facecam_rect,info)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='render-',dir=output.parent) as directory:
        directory = Path(directory).resolve()
        events = (overlay_events(clip.hook,clip.label,window.duration,settings.layout)
                  + caption_events(words,window.duration,settings.layout))
        # Relative ASS name in a controlled cwd avoids Windows drive/filter escaping entirely.
        (directory/'overlays.ass').write_text(ass_document(events,settings),encoding='utf-8')
        graph = layout_filter(clip.facecam_rect, settings.fps, settings.layout,
                              segments=clip.layout_segments, info=info)
        graph += ';[base]null[outv]' if clean else ';[base]ass=filename=overlays.ass[outv]'
        temporary = directory/'output.mp4'
        run(['ffmpeg','-v','error','-y','-ss',str(window.trim_start),'-i',str(window.path.resolve()),
             '-t',str(window.duration),'-filter_complex',graph,'-map','[outv]','-map','0:a:0',
             '-af','aresample=async=1:first_pts=0,apad','-c:v','libx264','-preset',settings.preset,'-crf',str(settings.crf),
             '-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-b:a','192k',
             '-movflags','+faststart',str(temporary)],cwd=directory)
        result = probe(temporary)
        if ((result.width,result.height)!=(1080,1920) or not result.has_audio
                or result.video_codec!='h264' or result.audio_codec!='aac'
                or abs(result.duration-window.duration)>.15
                or result.video_duration is None or abs(result.video_duration-window.duration)>.15
                or result.audio_duration is None or abs(result.audio_duration-window.duration)>.15):
            raise ValueError('Rendered video failed geometry/codec/audio/duration verification')
        os.replace(temporary,output)
        return result
