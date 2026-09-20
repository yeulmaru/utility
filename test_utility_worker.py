import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import utility_worker as worker


class WorkerTests(unittest.TestCase):
    def test_public_url_boundaries(self):
        for url in ['https://youtu.be/example', 'https://vimeo.com/123', 'https://www.arte.tv/video']:
            self.assertEqual(worker.validate_url(url, resolve=False), url)
        for url in ['http://youtube.com/x', 'https://127.0.0.1/', 'https://169.254.169.254/',
                    'https://user:pass@youtube.com/x', 'file:///tmp/a', 'https://youtube.com:8080/x',
                    'https://host.internal/x', 'https://localhost/x']:
            with self.assertRaises(ValueError): worker.validate_url(url, resolve=False)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.1', 443))]):
            with self.assertRaises(ValueError): worker.validate_url('https://public.example.com/video')

    def test_actual_formats_and_untrusted_selection(self):
        info = {'formats': [
            {'format_id': 'audio', 'vcodec': 'none', 'acodec': 'aac'},
            {'format_id': 'x', 'vcodec': 'h264', 'acodec': 'none', 'width': 3840, 'height': 2160, 'fps': 59.94},
            {'format_id': 'combined', 'vcodec': 'h264', 'acodec': 'aac', 'width': 1280, 'height': 720},
            {'format_id': 'drm', 'vcodec': 'h264', 'width': 1920, 'height': 1080, 'has_drm': True}]}
        self.assertEqual(worker.choose_format(info, 'best'), 'bestvideo*+bestaudio/best')
        self.assertEqual(worker.choose_format(info, 'x'), 'x+bestaudio')
        self.assertEqual(worker.choose_format(info, 'combined'), 'combined')
        self.assertEqual(len(worker.formats_of(info)), 2)
        self.assertIsNone(worker.formats_of(info)[1]['fps'])
        for selection in ['best[height>0]', 'x/combined', 'drm', 'missing']:
            with self.assertRaises(ValueError): worker.choose_format(info, selection)

    def test_media_and_fps_boundaries(self):
        self.assertAlmostEqual(worker.frame_rate('30000/1001'), 29.97)
        for fps in [None, '0/0', 0, 'nan']:
            self.assertIsNone(worker.frame_rate(fps))
        for info in [{'duration': None}, {'duration': 1201}, {'duration': 1, 'is_live': True},
                     {'duration': 1, 'has_drm': True}, {'duration': 1, '_type': 'playlist'}]:
            with self.assertRaises(ValueError): worker.media_info(info)

    def test_direct_media_with_null_metadata(self):
        info = {'url': 'https://example.com/video.mp4', 'ext': 'mp4', 'format_id': 'mp4',
                'vcodec': None, 'acodec': None, 'duration': None, 'width': None, 'height': None}
        with patch.object(worker, 'validate_url'), patch.object(worker, 'probe', return_value={
                'width': 320, 'height': 176, 'duration': 10, 'fps': 25, 'audio': True,
                'videoCodec': 'h264', 'audioCodec': 'aac'}):
            worker.media_info(info)
        f = worker.formats_of(info)[0]
        self.assertEqual((f['width'], f['height'], f['fps'], f['audio']), (320, 176, 25, True))
        self.assertEqual(worker.choose_format(info, 'mp4'), 'mp4')

    def test_time_and_korean(self):
        self.assertEqual(worker.timestamp(59.9996), '00:01:00,000')
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            worker.save_cues([{'start': 0, 'end': 1, 'text': '예울마루\n테스트'}], {'duration': 2}, output)
            self.assertIn('예울마루\n테스트', (output / 'subtitles.srt').read_text(encoding='utf-8'))

    def test_invalid_cues(self):
        with tempfile.TemporaryDirectory() as temp:
            for cues in [[{'start': 1, 'end': 1, 'text': 'x'}], [{'start': 0, 'end': 3, 'text': 'x'}],
                         [{'start': 0, 'end': 1, 'text': ''}], [{'start': float('nan'), 'end': 1, 'text': 'x'}],
                         [{'start': 0, 'end': 1.5, 'text': 'a'}, {'start': 1, 'end': 2, 'text': 'b'}]]:
                with self.assertRaises(ValueError): worker.save_cues(cues, {'duration': 2}, Path(temp))


if __name__ == '__main__': unittest.main()
