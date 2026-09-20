import tempfile
from pathlib import Path
import unittest
import utility_worker as worker


class WorkerTests(unittest.TestCase):
    def test_url_boundaries(self):
        self.assertEqual(worker.validate_url('https://youtu.be/example'), 'https://youtu.be/example')
        for url in ['http://youtube.com/x', 'https://youtube.com.attacker.test/x', 'https://127.0.0.1/',
                    'https://user:pass@youtube.com/x', 'file:///tmp/a', 'https://youtube.com:8080/x']:
            with self.assertRaises(ValueError): worker.validate_url(url)

    def test_time_and_korean(self):
        self.assertEqual(worker.timestamp(59.9996), '00:01:00,000')
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            worker.save_cues([{'start': 0, 'end': 1, 'text': '예울마루\n테스트'}], {'duration': 2}, output)
            self.assertIn('예울마루\n테스트', (output / 'subtitles.srt').read_text(encoding='utf-8'))
            self.assertTrue((output / 'subtitles.vtt').read_text(encoding='utf-8').startswith('WEBVTT'))

    def test_invalid_cues(self):
        with tempfile.TemporaryDirectory() as temp:
            for cues in [[{'start': 1, 'end': 1, 'text': 'x'}], [{'start': 0, 'end': 3, 'text': 'x'}],
                         [{'start': 0, 'end': 1, 'text': ''}], [{'start': float('nan'), 'end': 1, 'text': 'x'}],
                         [{'start': 0, 'end': 1.5, 'text': 'a'}, {'start': 1, 'end': 2, 'text': 'b'}]]:
                with self.assertRaises(ValueError): worker.save_cues(cues, {'duration': 2}, Path(temp))


if __name__ == '__main__': unittest.main()
