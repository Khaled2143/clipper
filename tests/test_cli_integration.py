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


if __name__=='__main__':
    unittest.main()
