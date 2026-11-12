import json
import tempfile
from types import SimpleNamespace as NS
import unittest
from pathlib import Path
from unittest.mock import patch
import wave

from clipmatch.spec import Settings
from clipmatch.transcribe import Word, transcribe
from clipmatch.captions import write_captions, escape_ass, ass_document


class CaptionTests(unittest.TestCase):
    def test_timestamps_escaping_and_empty_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            ass, srt = Path(d)/'a.ass', Path(d)/'a.srt'
            write_captions([Word(-1,.4,'Hello'), Word(1,1.4,'world'), Word(2,9,'wow')],
                           ass,srt,3,Settings())
            subtitle = srt.read_text()
            self.assertIn('00:00:01,000 --> 00:00:01,400', subtitle)
            self.assertIn('00:00:02,000 --> 00:00:03,000', subtitle)
            self.assertIn('0:00:01.00,0:00:01.40', ass.read_text())
            self.assertNotIn('{\\pos', escape_ass('{\\pos(1,2)}'))
            self.assertNotIn('\n', escape_ass('line\nnext'))
            write_captions([],ass,srt,3,Settings())
            self.assertEqual(srt.read_text(), '')
            self.assertNotIn('Dialogue:', ass.read_text())

    def test_transcription_cache_independent_of_render_style(self):
        with tempfile.TemporaryDirectory() as d:
            audio = Path(d)/'audio.wav'
            with wave.open(str(audio), 'wb') as out:
                out.setparams((1,2,16000,0,'NONE','not compressed'))
                out.writeframes(b'\0\0'*48000)
            model = NS(transcribe=lambda *a, **kw: (iter([NS(words=[NS(start=1.,end=1.4,word=' world')])]), NS(language='en')))
            target = Path(d)/'transcript.json'
            with patch('clipmatch.transcribe.load_model', return_value=(model,'cpu','int8')):
                words = transcribe(audio,Settings(),target)
            self.assertEqual(words, [Word(1,1.4,'world')])
            with patch('clipmatch.transcribe.load_model', side_effect=AssertionError('expensive reload')):
                self.assertEqual(transcribe(audio,Settings(font='Verdana'),target), words)
            with patch('clipmatch.transcribe.load_model', return_value=(model,'cpu','int8')) as load:
                transcribe(audio,Settings(model='base'),target)
                self.assertEqual(load.call_count,1)
            self.assertEqual(json.loads(target.read_text())['words'][0]['start'],1)

    def test_interrupted_generator_does_not_publish_transcript(self):
        def broken():
            yield NS(words=[])
            raise RuntimeError('GPU failure')
        with tempfile.TemporaryDirectory() as d:
            audio = Path(d)/'audio.wav'
            with wave.open(str(audio),'wb') as out:
                out.setparams((1,2,16000,0,'NONE','not compressed'))
                out.writeframes(b'\0\0'*16000)
            model = NS(transcribe=lambda *a,**kw: (broken(),NS(language='en')))
            target = Path(d)/'transcript.json'
            with patch('clipmatch.transcribe.load_model',return_value=(model,'cpu','int8')):
                with self.assertRaises(RuntimeError):
                    transcribe(audio,Settings(),target)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix('.manifest.json').exists())
