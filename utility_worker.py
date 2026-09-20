"""Portable media worker; no web server, credentials, or MISO API assumptions."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

MAX_SECONDS = 1200
MAX_BYTES = 250 * 1024 * 1024
PROVIDERS = ('youtube.com', 'youtu.be', 'vimeo.com', 'instagram.com', 'facebook.com', 'fb.watch', 'threads.net', 'threads.com')


def run(command, timeout=120):
    result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        # Subprocess logs can contain private URLs or media metadata.
        raise RuntimeError('MEDIA_COMMAND_FAILED')
    return result.stdout


def validate_url(value):
    url = urlsplit(value)
    host = (url.hostname or '').lower()
    if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
        raise ValueError('HTTPS_PROVIDER_URL_REQUIRED')
    if not any(host == domain or host.endswith('.' + domain) for domain in PROVIDERS):
        raise ValueError('UNSUPPORTED_PROVIDER')
    return value


def probe(path):
    data = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))
    duration = float(data.get('format', {}).get('duration', 0))
    if not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS:
        raise ValueError('DURATION_LIMIT_OR_UNKNOWN')
    streams = data.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), {})
    return {'duration': duration, 'width': video.get('width', 0), 'height': video.get('height', 0),
            'audio': any(s.get('codec_type') == 'audio' for s in streams)}


def timestamp(seconds, separator=','):
    millis = round(seconds * 1000)
    return f'{millis // 3600000:02}:{millis // 60000 % 60:02}:{millis // 1000 % 60:02}{separator}{millis % 1000:03}'


def save_cues(cues, metadata, output):
    previous_end = 0
    for cue in cues:
        start, end = cue['start'], cue['end']
        if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= metadata['duration'] + .05:
            raise ValueError('INVALID_CUE_TIME')
        if start < previous_end - .001 or not cue['text'].strip():
            raise ValueError('INVALID_CUE_ORDER_OR_TEXT')
        previous_end = end
    result = {**metadata, 'cues': cues, 'reviewRequired': True}
    (output / 'transcript.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    srt = '\n\n'.join(f"{i}\n{timestamp(c['start'])} --> {timestamp(c['end'])}\n{c['text']}" for i, c in enumerate(cues, 1))
    vtt = 'WEBVTT\n\n' + '\n\n'.join(f"{timestamp(c['start'], '.')} --> {timestamp(c['end'], '.')}\n{c['text']}" for c in cues)
    (output / 'subtitles.srt').write_text(srt + '\n', encoding='utf-8')
    (output / 'subtitles.vtt').write_text(vtt + '\n', encoding='utf-8')
    return result


def transcribe(path, output, language='ko'):
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError('INPUT_SIZE_LIMIT')
    metadata = probe(path)
    if not metadata['audio']:
        raise ValueError('NO_AUDIO_STREAM')
    from faster_whisper import WhisperModel
    device = os.environ.get('YM_ASR_DEVICE', 'cpu')
    model = WhisperModel('large-v3', device=device, compute_type='float16' if device == 'cuda' else 'int8')
    segments, info = model.transcribe(str(path), language=None if language == 'auto' else language,
                                    beam_size=5, vad_filter=True, word_timestamps=True,
                                    condition_on_previous_text=False)
    cues = []
    for segment in segments:
        text = segment.text.strip()
        start = max(0, float(segment.start), cues[-1]['end'] if cues else 0)
        end = min(metadata['duration'], float(segment.end))
        if text and end > start:
            cues.append({'start': start, 'end': end, 'text': text,
                         'words': [{'start': w.start, 'end': w.end, 'text': w.word, 'probability': w.probability}
                                   for w in (segment.words or [])]})
    return save_cues(cues, {**metadata, 'language': info.language, 'model': 'large-v3', 'engine': 'faster-whisper'}, output)


def download(url, output, height=1080):
    import yt_dlp
    validate_url(url)

    class Quiet:
        def debug(self, message): pass
        def warning(self, message): pass
        def error(self, message): pass

    def match(info, *, incomplete):
        duration = info.get('duration')
        if info.get('is_live'):
            return 'LIVE_NOT_SUPPORTED'
        if not incomplete and (duration is None or not 0 < duration <= MAX_SECONDS):
            return 'DURATION_LIMIT_OR_UNKNOWN'
        if duration and duration > MAX_SECONDS:
            return 'DURATION_LIMIT'
        return None

    with tempfile.TemporaryDirectory(prefix='ym-media-') as temp:
        options = {'outtmpl': str(Path(temp) / 'source.%(ext)s'), 'noplaylist': True,
                   'format': f'bv*[height<={height}]+ba/b[height<={height}]', 'merge_output_format': 'mp4',
                   'max_filesize': MAX_BYTES, 'socket_timeout': 30, 'retries': 2, 'fragment_retries': 2,
                   'match_filter': match, 'logger': Quiet(), 'quiet': True, 'noprogress': True,
                   'js_runtimes': {'node': {}}, 'overwrites': False}
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)
        files = [p for p in Path(temp).glob('source.*') if p.suffix not in ('.part', '.ytdl')]
        if len(files) != 1 or not 0 < files[0].stat().st_size <= MAX_BYTES:
            raise ValueError('DOWNLOAD_INCOMPLETE_OR_SIZE_LIMIT')
        metadata = probe(files[0])
        target = output / 'video.mp4'
        run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(files[0]), '-map', '0:v:0', '-map', '0:a:0?',
             '-c:v', 'libx264', '-preset', 'fast', '-crf', '22', '-pix_fmt', 'yuv420p',
             '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-c:a', 'aac', '-movflags', '+faststart', str(target)], 3600)
        checked = probe(target)
        if abs(checked['duration'] - metadata['duration']) > 1 or checked['audio'] != metadata['audio']:
            raise ValueError('OUTPUT_VERIFICATION_FAILED')
        if target.stat().st_size > MAX_BYTES:
            raise ValueError('OUTPUT_SIZE_LIMIT')
        return checked


def manifest(output, metadata):
    files = []
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != 'manifest.json':
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            files.append({'name': path.name, 'bytes': path.stat().st_size, 'sha256': digest})
    (output / 'manifest.json').write_text(json.dumps({'protocol': 'ym-utility-v1', 'state': 'done',
                                                    'metadata': metadata, 'files': files}, ensure_ascii=False), encoding='utf-8')


def smoke(output):
    video = output / 'smoke.mp4'
    run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:d=1',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-c:a', 'aac',
         '-pix_fmt', 'yuv420p', '-shortest', str(video)])
    metadata = probe(video)
    if not metadata['audio'] or metadata['width'] != 320:
        raise ValueError('SMOKE_FAILED')
    save_cues([{'start': 0, 'end': .9, 'text': '예울마루 테스트'}], metadata, output)
    return {**metadata, 'smokeOnly': True, 'speechRecognitionTested': False, 'siteDownloadTested': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('kind', choices=['smoke', 'download', 'transcribe'])
    parser.add_argument('--input', type=Path)
    parser.add_argument('--url', default=os.environ.get('YM_SOURCE_URL', ''))
    parser.add_argument('--language', choices=['ko', 'auto', 'en', 'ja', 'zh'], default='ko')
    parser.add_argument('--height', type=int, choices=[720, 1080], default=1080)
    parser.add_argument('--output', type=Path, default=Path('output'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        if args.kind == 'smoke': result = smoke(args.output)
        elif args.kind == 'download': result = download(args.url, args.output, args.height)
        elif args.input: result = transcribe(args.input, args.output, args.language)
        else: raise ValueError('INPUT_FILE_REQUIRED')
        manifest(args.output, result)
        print(json.dumps({'ok': True, 'kind': args.kind}))
    except Exception:
        # Do not expose source URL, transcript, local path or model response in public logs.
        print(json.dumps({'ok': False, 'code': 'UTILITY_PROCESSING_FAILED'}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
