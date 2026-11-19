"""Manual render orchestration; expensive stages have independent completion records."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys

from . import __version__
from .cache import atomic_json, cache_valid, complete, fingerprint
from .captions import overlay_events, write_captions
from .media import acquire, extract_audio, preflight, probe, tool_versions
from .render import render_clip, render_key, validate_crop, resolve_fonts
from .spec import load_job
from .transcribe import transcribe


def render_job(spec: Path, config: Path | None, *, force: bool=False,
               clean: bool=False, no_captions: bool=False, send_finished: bool=False) -> int:
    if send_finished:
        if no_captions:
            raise ValueError("Finished-channel exports require transcription. Remove --no-captions.")
        try:
            from .finished import deliver_finished, finished_webhook_url
        except ModuleNotFoundError as exc:
            if exc.name not in {"aiohttp", "dotenv"}:
                raise
            raise ValueError('Discord delivery requires pip install -e ".[live]".') from None
        webhook = finished_webhook_url()
    settings, clips = load_job(spec,config)
    if settings.layout == "stacked" and no_captions:
        raise ValueError("The stacked layout requires transcription. Remove --no-captions.")
    # A destination must never replace any input, even a later clip's source.
    sources = {Path(c.source).resolve() for c in clips
               if not c.source.startswith(('http://', 'https://'))}
    protected = sources | {spec.resolve()}
    if config:
        protected.add(config.resolve())
    for c in clips:
        if send_finished and c.facecam_rect is None:
            raise ValueError(f"{c.id}: set facecam_rect for the finished channel's facecam-over-gameplay layout.")
        destinations = [settings.output_dir / (c.id + suffix)
                        for suffix in ('.mp4', '.ass', '.srt', '.json', '.manifest.json')]
        if clean:
            destinations += [settings.output_dir / (c.id + suffix)
                             for suffix in ('.clean.mp4', '.clean.manifest.json')]
        if send_finished:
            destinations.append(settings.work_dir / c.id / 'finished-discord.json')
        destinations += [settings.work_dir / c.id / name for name in
                         ('audio.wav', 'audio.manifest.json', 'transcript.json',
                          'transcript.manifest.json', 'section.mp4', 'section.manifest.json',
                          'section.origin.json')]
        if any(destination.resolve() in protected for destination in destinations):
            raise ValueError(f'{c.id}: output/cache destination collides with a protected input')
    # Reject cheap, deterministic errors for the entire batch before downloading anything.
    for clip in clips:
        overlay_events(clip.hook,clip.label,clip.end-clip.start,settings.layout)
    preflight()
    for clip in clips:
        if not clip.source.startswith(('http://','https://')):
            info = probe(Path(clip.source))
            validate_crop(clip.facecam_rect,info)
            if not info.has_audio:
                raise ValueError(f'{clip.id}: source has no audio stream')
            if clip.end > min(info.duration, getattr(info, 'video_duration', None) or info.duration)+.05:
                raise ValueError(f'{clip.id}: selected end exceeds source duration')
    failures=0
    for clip in clips:
        def progress(message):
            print(f'[{clip.id}] {message}',flush=True)
        try:
            work = settings.work_dir/clip.id
            progress('acquire/validate selected window (reuse completed download when valid)')
            window = acquire(clip,settings,work,force)
            words=[]
            warnings=['layout_requires_visual_review', 'mixed_audio_speaker_attribution_unverified']
            transcript_provenance=None
            if not no_captions:
                audio=work/'audio.wav'
                manifest=work/'audio.manifest.json'
                key=fingerprint(dict(stage='audio-v2',source=window.identity,trim=window.trim_start,
                                     duration=window.duration,tools=tool_versions()))
                if force or not cache_valid(audio,manifest,key):
                    progress('extract selected-window audio')
                    extract_audio(window,audio)
                    complete(audio,manifest,key,'audio',tool_versions())
                else:
                    progress('audio cache hit')
                progress('transcribe (reuse completed transcript when valid)')
                transcript_path=work/'transcript.json'
                words=transcribe(audio,settings,transcript_path,force)
                transcript_data=json.loads(transcript_path.read_text(encoding='utf-8'))
                transcript_provenance={k: transcript_data[k] for k in ('model','device','compute_type','tools','language')}
                if not words:
                    warnings.append('no_speech_detected')
            else:
                warnings.append('captions_disabled')
            settings.output_dir.mkdir(parents=True,exist_ok=True)
            output=settings.output_dir/f'{clip.id}.mp4'
            manifest=output.with_suffix('.manifest.json')
            fonts=resolve_fonts(settings.font, clip.hook+' '+clip.label+' '+' '.join(w.text for w in words))
            progress('resolved fonts: '+', '.join(fonts.get('selected', [])))
            key=render_key(clip,window,words,settings,False,fonts)
            if force or not cache_valid(output,manifest,key):
                progress('render 1080x1920 master and verify codecs/audio/duration')
                info=render_clip(clip,window,words,settings,output)
                complete(output,manifest,key,'render',tool_versions())
            else:
                progress('render cache hit')
                info=probe(output)
            # Sidecars regenerate cheaply, including after an interrupted sidecar write.
            if not no_captions:
                write_captions(words,output.with_suffix('.ass'),output.with_suffix('.srt'),window.duration,settings)
            else:
                output.with_suffix('.ass').unlink(missing_ok=True)
                output.with_suffix('.srt').unlink(missing_ok=True)
            def save_sidecar(selected_clean, clean_status):
                atomic_json(output.with_suffix('.json'),dict(
                    version=1,clipmatch=__version__,clip_id=clip.id,status='manual_render_unreviewed',
                    source=dict(url_or_path=clip.source,identity=window.identity,start=clip.start,end=clip.end),
                    hook=clip.hook,label=clip.label,facecam_rect=clip.facecam_rect,
                    output=dict(path=output.name,**asdict(info)),
                    clean_output=selected_clean.name if selected_clean else None, clean_export_status=clean_status,
                    captions_enabled=not no_captions,transcription=transcript_provenance,fonts=fonts,
                    tools=tool_versions(),warnings=warnings))
            save_sidecar(None, 'pending_or_failed' if clean else 'not_requested')
            clean_output=None
            if clean:
                clean_output=output.with_name(clip.id+'.clean.mp4')
                clean_manifest=clean_output.with_suffix('.manifest.json')
                clean_key=render_key(clip,window,[],settings,True,fonts)
                if force or not cache_valid(clean_output,clean_manifest,clean_key):
                    progress('render clean CapCut export')
                    render_clip(clip,window,[],settings,clean_output,clean=True)
                    complete(clean_output,clean_manifest,clean_key,'render_clean',tool_versions())
                else:
                    progress('clean export cache hit')
            save_sidecar(clean_output, 'complete' if clean else 'not_requested')
            progress(f'ready for manual review: {output}')
            if send_finished:
                sent = asyncio.run(deliver_finished(output, work / 'finished-discord.json', webhook))
                progress('uploaded MP4 to finished channel' if sent else 'MP4 already delivered to finished channel')
        except (OSError,ValueError,RuntimeError) as exc:
            failures+=1
            # Error text from media subprocesses is sanitized at their boundary.
            # Model runtime errors can contain remote paths; use a stable summary.
            message = 'Transcription/runtime failure; check model and GPU setup' if isinstance(exc,RuntimeError) else str(exc)
            print(f'[{clip.id}] failed: {message}',file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(prog='clipmatch',description='Resumable manual vertical clips from explicit source windows.')
    parser.add_argument('--version',action='version',version=__version__)
    sub=parser.add_subparsers(dest='command',required=True)
    render=sub.add_parser('render-manual',help='Render independent clips from a YAML specification')
    render.add_argument('spec',type=Path)
    render.add_argument('--config',type=Path,help='Optional config YAML; output/work paths resolve beside it')
    render.add_argument('--force',action='store_true',help='Rerun acquisition, audio, transcription and rendering for this job')
    render.add_argument('--clean',action='store_true',help='Also export video without burned-in text for CapCut')
    render.add_argument('--no-captions',action='store_true',help='Skip transcription and captions; hook and label remain')
    render.add_argument('--send-finished', action='store_true', help='Upload each finished MP4 to the finished-clips Discord channel')
    args=parser.parse_args(argv)
    try:
        return render_job(args.spec,args.config,force=args.force,clean=args.clean,no_captions=args.no_captions,
                          send_finished=args.send_finished)
    except (OSError,ValueError) as exc:
        print(f'clipmatch: {exc}',file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted; completed stages are preserved for the next run.',file=sys.stderr)
        return 130
