"""Offline render contract: generated source, real FFmpeg, fake speech model only."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from clipmatch.cli import main
from clipmatch.media import probe, preflight

MEDIA_AVAILABLE = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


class CliTests(unittest.TestCase):
    def test_finished_render_requires_captioning_and_a_facecam_layout(self):
        webhook = "https://discord.com/api/webhooks/1/token"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DISCORD_FINISHED_WEBHOOK_URL": webhook}):
            spec = Path(directory) / "job.yaml"
            clip = dict(id="one", source="a.mp4", start=0, end=1, label="A", hook="Hi")
            spec.write_text(json.dumps(dict(version=1, clips=[clip])))
            for flags, reason in ((["--no-captions"], "--no-captions"), ([], "facecam_rect")):
                with self.subTest(flags=flags), redirect_stderr(StringIO()) as error:
                    self.assertEqual(main(["render-manual", str(spec), "--send-finished", *flags]), 1)
                    self.assertIn(reason, error.getvalue())

    def test_send_finished_requires_its_own_webhook_before_rendering(self):
        with patch.dict(os.environ, {"DISCORD_FINISHED_WEBHOOK_URL": "", "DISCORD_WEBHOOK_URL": "capture"}):
            error = StringIO()
            with redirect_stderr(error):
                self.assertEqual(main(["render-manual", "unused.yaml", "--send-finished"]), 1)
            self.assertIn("DISCORD_FINISHED_WEBHOOK_URL", error.getvalue())

    def test_missing_tools_reports_nonzero_without_model(self):
        with tempfile.TemporaryDirectory() as d:
            spec=Path(d)/'job.yaml'
            spec.write_text(json.dumps(dict(version=1,clips=[dict(id='one',source='a.mp4',start=0,end=1,label='A',hook='Hi')])))
            error=StringIO()
            with patch('clipmatch.media.shutil.which',return_value=None),redirect_stderr(error):
                self.assertEqual(main(['render-manual',str(spec)]),1)
            self.assertIn('Missing ffmpeg',error.getvalue())

    def test_invalid_batch_validated_before_media_work(self):
        with tempfile.TemporaryDirectory() as d:
            spec=Path(d)/'job.yaml'
            spec.write_text('version: 1\nclips: []\n')
            with patch('clipmatch.cli.preflight',side_effect=AssertionError('must validate first')),redirect_stderr(StringIO()):
                self.assertEqual(main(['render-manual',str(spec)]),1)


@unittest.skipUnless(MEDIA_AVAILABLE,'Full ffmpeg and ffprobe are required for render integration')
class RenderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()/"source's folder 世界"
        self.root.mkdir()
        self.source=self.root/'source.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',
                        'testsrc=size=640x360:rate=30,drawbox=x=0:y=0:w=160:h=90:color=red:t=fill',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
                        '-t','4','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(self.source)],check=True)
        self.spec=self.root/'job.yaml'
        self.job=dict(version=1,settings=dict(preset='ultrafast'),clips=[
            dict(id='blur',source=str(self.source),start=1,end=2,label='A',hook="That's: exciting!"),
            dict(id='face',source=str(self.source),start=2,end=3,label='B',hook='Wait for it',facecam_rect=[0,0,160,90])])
        self.spec.write_text(json.dumps(self.job))
        self.model=NS(transcribe=lambda *a,**kw:(iter([NS(words=[NS(start=.2,end=.6,word=' hello'),NS(start=.6,end=.9,word=' world')])]),NS(language='en')))

    def invoke(self,*extra):
        with patch('clipmatch.transcribe.load_model',return_value=(self.model,'cpu','int8')),redirect_stdout(StringIO()):
            return main(['render-manual',str(self.spec),*extra])

    def test_two_layouts_captions_clean_exports_and_resume(self):
        self.assertEqual(self.invoke('--clean'),0)
        for name,start in [('blur',1),('face',2)]:
            output=self.root/'renders'/f'{name}.mp4'
            info=probe(output)
            self.assertEqual((info.width,info.height,info.video_codec,info.audio_codec),(1080,1920,'h264','aac'))
            self.assertAlmostEqual(info.duration,1,delta=.15)
            sidecar=json.loads(output.with_suffix('.json').read_text())
            self.assertEqual(sidecar['source']['start'],start)
            self.assertEqual(sidecar['status'],'manual_render_unreviewed')
            self.assertTrue(output.with_suffix('.srt').exists())
            self.assertTrue(output.with_name(name+'.clean.mp4').exists())
            text=output.with_suffix('.ass').read_text()
            self.assertIn('0:00:00.20,0:00:00.60',text)
        before=(self.root/'renders'/'blur.mp4').stat().st_mtime_ns
        with patch('clipmatch.cli.extract_audio',side_effect=AssertionError('audio cache miss')),patch('clipmatch.cli.render_clip',side_effect=AssertionError('video cache miss')):
            self.assertEqual(self.invoke('--clean'),0)
        self.assertEqual((self.root/'renders'/'blur.mp4').stat().st_mtime_ns,before)
        self.job['clips'][0]['hook']='A changed hook'
        self.spec.write_text(json.dumps(self.job))
        with patch('clipmatch.transcribe.load_model',side_effect=AssertionError('transcript cache miss')):
            with redirect_stdout(StringIO()):
                self.assertEqual(main(['render-manual',str(self.spec)]),0)
        self.assertNotEqual((self.root/'renders'/'blur.mp4').stat().st_mtime_ns,before)

    def test_invalid_crop_and_no_audio_fail_without_final_output(self):
        self.job['clips'][0]['facecam_rect']=[600,0,200,100]
        self.spec.write_text(json.dumps(self.job))
        with redirect_stderr(StringIO()):
            self.assertEqual(self.invoke(),1)
        self.assertFalse((self.root/'renders'/'blur.mp4').exists())
        silent_video=self.root/'noaudio.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(self.source),'-an','-c:v','copy',str(silent_video)],check=True)
        self.job['clips']=[dict(id='noaudio',source=str(silent_video),start=0,end=1,label='A',hook='Hi')]
        self.spec.write_text(json.dumps(self.job))
        with redirect_stderr(StringIO()):
            self.assertEqual(self.invoke(),1)
        self.assertFalse((self.root/'renders'/'noaudio.mp4').exists())

    def test_silent_audio_does_not_invent_captions(self):
        silent=self.root/'silent.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(self.source),'-af','volume=0','-c:v','copy',str(silent)],check=True)
        self.job['clips']=[dict(id='silent',source=str(silent),start=1,end=2,label='A',hook='Quiet disbelief')]
        self.spec.write_text(json.dumps(self.job))
        self.model=NS(transcribe=lambda *a,**kw:(iter([]),NS(language='en')))
        self.assertEqual(self.invoke(),0)
        self.assertEqual((self.root/'renders'/'silent.srt').read_text(),'')
        self.assertIn('no_speech_detected',json.loads((self.root/'renders'/'silent.json').read_text())['warnings'])

    def test_force_source_change_and_failed_transcription(self):
        self.job['clips']=self.job['clips'][:1]
        self.spec.write_text(json.dumps(self.job))
        self.assertEqual(self.invoke(),0)
        before=(self.root/'work'/'blur'/'transcript.json').stat().st_mtime_ns
        self.assertEqual(self.invoke('--force'),0)
        self.assertNotEqual((self.root/'work'/'blur'/'transcript.json').stat().st_mtime_ns,before)
        with self.source.open('ab') as f:
            f.write(b'changed-source-identity')
        self.assertEqual(self.invoke(),0)
        # Fail a fresh clip so no prior success can mask an incomplete stage.
        self.job['clips'][0]['id']='broken'
        self.spec.write_text(json.dumps(self.job))
        def broken(*a,**kw):
            raise RuntimeError('model failed')
        self.model=NS(transcribe=broken)
        with redirect_stderr(StringIO()):
            self.assertEqual(self.invoke(),1)
        self.assertFalse((self.root/'work'/'broken'/'transcript.manifest.json').exists())
        self.assertFalse((self.root/'renders'/'broken.manifest.json').exists())


    def test_delayed_audio_preserves_silence_and_caption_time(self):
        from clipmatch.media import SourceWindow, extract_audio
        import array
        import wave
        delayed=self.root/'delayed.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=blue:s=640x360:r=30:d=2',
                        '-itsoffset','0.5','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=1.5',
                        '-t','2','-c:v','libx264','-c:a','aac',str(delayed)],check=True)
        audio=self.root/'aligned.wav'
        extract_audio(SourceWindow(delayed,0,0,1.5,'fixture'),audio)
        def mean_absolute(path,start,end):
            with wave.open(str(path),'rb') as wav:
                wav.setpos(int(start*wav.getframerate()))
                samples=array.array('h',wav.readframes(int((end-start)*wav.getframerate())))
            return sum(abs(n) for n in samples)/len(samples)
        self.assertLess(mean_absolute(audio,.1,.3),20)
        self.assertGreater(mean_absolute(audio,.7,.9),100)
        self.job['clips']=[dict(id='delayed',source=str(delayed),start=0,end=1.5,label='A',hook='Delayed cue')]
        self.spec.write_text(json.dumps(self.job))
        self.assertEqual(self.invoke(),0)
        decoded=self.root/'decoded.wav'
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(self.root/'renders'/'delayed.mp4'),
                        '-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(decoded)],check=True)
        self.assertLess(mean_absolute(decoded,.1,.3),20)
        self.assertGreater(mean_absolute(decoded,.7,.9),100)

    def test_rotated_and_anamorphic_sources_render(self):
        rotated=self.root/'rotated.mp4'
        anamorphic=self.root/'anamorphic.mp4'
        help_text = subprocess.run(['ffmpeg','-hide_banner','-h','full'],check=True,
                                   capture_output=True,text=True,encoding='utf-8').stdout
        # New FFmpeg versions no longer turn a rotate tag into a display matrix.
        rotation_args = (['-display_rotation','90','-i',str(self.source),'-c','copy']
                         if '-display_rotation' in help_text else
                         ['-i',str(self.source),'-c','copy','-metadata:s:v:0','rotate=90'])
        subprocess.run(['ffmpeg','-v','error','-y',*rotation_args,str(rotated)],check=True)
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(self.source),'-vf','setsar=2',
                        '-c:v','libx264','-c:a','copy',str(anamorphic)],check=True)
        self.assertEqual((probe(rotated).width,probe(rotated).height),(360,640))
        self.job['clips']=[dict(id='rotated',source=str(rotated),start=1,end=2,label='A',hook='Turn around',facecam_rect=[0,0,90,160]),
                           dict(id='anamorphic',source=str(anamorphic),start=1,end=2,label='B',hook='Wide view',facecam_rect=[0,0,160,90])]
        self.spec.write_text(json.dumps(self.job))
        self.assertEqual(self.invoke(),0)
        for name in ('rotated','anamorphic'):
            self.assertEqual(probe(self.root/'renders'/f'{name}.mp4').video_duration,1)


class SourceProtectionTests(unittest.TestCase):
    def test_refuses_master_or_clean_output_collision_with_any_local_source(self):
        for name in ('one.mp4','one.clean.mp4'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                root=Path(d).resolve()
                (root/'renders').mkdir()
                original=root/'renders'/name
                original.write_bytes(b'irreplaceable original')
                clip=dict(id='one',source=str(original),start=0,end=1,label='A',hook='Hi')
                spec=root/'job.yaml'
                spec.write_text(json.dumps(dict(version=1,clips=[clip])))
                errors=StringIO()
                with patch('clipmatch.cli.preflight'),patch('clipmatch.cli.probe',return_value=NS(width=640,height=360,duration=2,has_audio=True)),patch('clipmatch.cli.acquire',side_effect=AssertionError('must reject before acquisition')),redirect_stderr(errors):
                    self.assertEqual(main(['render-manual',str(spec),'--clean']),1)
                self.assertIn('collid',errors.getvalue())
                self.assertEqual(original.read_bytes(),b'irreplaceable original')

    def test_refuses_overwriting_another_clips_source(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()
            (root/'renders').mkdir()
            first=root/'input.mp4'
            second=root/'renders'/'one.mp4'
            first.write_bytes(b'first source')
            second.write_bytes(b'second source')
            clips=[dict(id='one',source=str(first),start=0,end=1,label='A',hook='Hi'),
                   dict(id='two',source=str(second),start=0,end=1,label='B',hook='Hello')]
            spec=root/'job.yaml'
            spec.write_text(json.dumps(dict(version=1,clips=clips)))
            errors=StringIO()
            with patch('clipmatch.cli.preflight'),patch('clipmatch.cli.probe',return_value=NS(width=640,height=360,duration=2,has_audio=True)),patch('clipmatch.cli.acquire',side_effect=AssertionError('must reject before acquisition')),redirect_stderr(errors):
                self.assertEqual(main(['render-manual',str(spec)]),1)
            self.assertIn('collid',errors.getvalue())
            self.assertEqual(second.read_bytes(),b'second source')

class ProvenanceTests(unittest.TestCase):
    def test_clean_failure_does_not_leave_old_source_sidecar_on_new_master(self):
        from clipmatch.media import MediaInfo, SourceWindow
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()
            source=root/'source.mp4'; source.write_bytes(b'source')
            spec=root/'job.yaml'
            clip=dict(id='one',source=str(source),start=0,end=1,label='A',hook='Hi')
            job=dict(version=1,clips=[clip]); spec.write_text(json.dumps(job))
            info=MediaInfo(1080,1920,1,True,'h264','aac',1,1)
            def acquire(clip,*a,**kw):
                return SourceWindow(source,0,clip.start,1,'identity')
            fail_clean=False
            def render(clip,window,words,settings,output,*,clean=False):
                if clean and fail_clean:
                    raise ValueError('clean failed')
                output.parent.mkdir(parents=True,exist_ok=True)
                output.write_bytes(f'window {clip.start}'.encode())
                return info
            with patch('clipmatch.cli.preflight'),patch('clipmatch.cli.probe',return_value=MediaInfo(640,360,4,True)),patch('clipmatch.cli.acquire',side_effect=acquire),patch('clipmatch.cli.tool_versions',return_value={}),patch('clipmatch.cli.render_key',return_value='key'),patch('clipmatch.cli.resolve_fonts',return_value={}),patch('clipmatch.cli.render_clip',side_effect=render),redirect_stdout(StringIO()),redirect_stderr(StringIO()):
                self.assertEqual(main(['render-manual',str(spec),'--no-captions']),0)
                clip.update(start=1,end=2);spec.write_text(json.dumps(job)); fail_clean=True
                self.assertEqual(main(['render-manual',str(spec),'--no-captions','--clean','--force']),1)
            sidecar=json.loads((root/'renders'/'one.json').read_text())
            self.assertEqual(sidecar['source']['start'],1)
            self.assertIsNone(sidecar['clean_output'])
            self.assertEqual(sidecar['clean_export_status'],'pending_or_failed')


if __name__=='__main__':
    unittest.main()
