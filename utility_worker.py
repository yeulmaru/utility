"""Portable media worker; no web server, credentials, or MISO API assumptions."""
import argparse
import hashlib
import json
import math
import ipaddress
import re
import shutil
import socket
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

MAX_SECONDS = 1200
MAX_BYTES = 250 * 1024 * 1024
PROVIDERS = ('youtube.com', 'youtu.be', 'vimeo.com', 'instagram.com', 'facebook.com', 'fb.watch', 'threads.net', 'threads.com', 'x.com', 'twitter.com')


def run(command, timeout=120):
    result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        # Subprocess logs can contain private URLs or media metadata.
        raise RuntimeError('MEDIA_COMMAND_FAILED')
    return result.stdout


def validate_url(value, resolve=True):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError('INVALID_URL')
    url = urlsplit(value)
    host = (url.hostname or '').lower()
    if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
        raise ValueError('PUBLIC_HTTPS_REQUIRED')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or '.' not in host:
        raise ValueError('PUBLIC_HOST_REQUIRED')
    if host.endswith(('.local', '.internal', '.localhost', '.test', '.invalid')):
        raise ValueError('PUBLIC_HOST_REQUIRED')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError('PUBLIC_HOST_REQUIRED')
    if resolve:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('PUBLIC_HOST_REQUIRED')
    return value


def probe(path):
    data = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))
    duration = float(data.get('format', {}).get('duration', 0))
    if not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS:
        raise ValueError('DURATION_LIMIT_OR_UNKNOWN')
    streams = data.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), {})
    return {'duration': duration, 'width': video.get('width', 0), 'height': video.get('height'), 'fps': frame_rate(video.get('avg_frame_rate')), 
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


def frame_rate(value):
    try:
        parts = str(value).split('/')
        number = float(parts[0]) / (float(parts[1]) if len(parts) == 2 else 1)
        return round(number, 3) if math.isfinite(number) and number > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


class Quiet:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def media_info(info):
    duration = info.get('duration')
    if info.get('_type') in ('playlist', 'multi_video') or info.get('is_live') or info.get('has_drm'):
        raise ValueError('PLAYLIST_LIVE_OR_DRM_UNSUPPORTED')
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS:
        raise ValueError('DURATION_LIMIT_OR_UNKNOWN')
    return info


def formats_of(info):
    formats = []
    for f in info.get('formats') or [info]:
        ident = str(f.get('format_id') or '')
        if f.get('has_drm') or not f.get('vcodec') or f.get('vcodec') == 'none':
            continue
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', ident):
            continue
        if not f.get('width') or not f.get('height') or f.get('protocol') in ('mhtml', 'rtmp'):
            continue
        formats.append({'id': ident, 'width': f['width'], 'height': f['height'],
                        'fps': frame_rate(f.get('fps')), 'ext': f.get('ext'),
                        'audio': bool(f.get('acodec') and f['acodec'] != 'none'),
                        'bytes': f.get('filesize') or f.get('filesize_approx'),
                        'codec': f.get('vcodec')})
    if not formats:
        raise ValueError('NO_VIDEO_FORMATS')
    return formats[-300:]


def ydl_options(temp=None):
    options = {'noplaylist': True, 'socket_timeout': 25, 'retries': 2, 'fragment_retries': 2,
               'logger': Quiet(), 'quiet': True, 'noprogress': True,
               'js_runtimes': {'node': {}}, 'overwrites': False, 'cachedir': False}
    if temp:
        options.update(outtmpl=str(Path(temp) / 'source.%(ext)s'), max_filesize=MAX_BYTES,
                       merge_output_format='mkv', format='bestvideo*+bestaudio/best')
    return options


def analyze(url, output):
    import yt_dlp
    validate_url(url)
    with yt_dlp.YoutubeDL(ydl_options()) as ydl:
        info = media_info(ydl.extract_info(url, download=False))
    formats = formats_of(info)
    thumbnail = info.get('thumbnail') or ''
    if thumbnail:
        try: validate_url(thumbnail)
        except ValueError: thumbnail = ''
    metadata = {'title': str(info.get('title') or '영상')[:300], 'duration': info['duration'],
                'thumbnail': thumbnail, 'formats': formats, 'extractor': info.get('extractor_key'),
                'default': 'best', 'maxSeconds': MAX_SECONDS, 'maxBytes': MAX_BYTES}
    (output / 'analysis.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
    return metadata


def choose_format(info, selection):
    valid = formats_of(info)
    if selection == 'best':
        return 'bestvideo*+bestaudio/best'
    selected = next((f for f in valid if f['id'] == selection), None)
    if selected is None:
        raise ValueError('FORMAT_NO_LONGER_AVAILABLE')
    return selection if selected['audio'] else selection + '+bestaudio'


def download(url, output, selection='best'):
    import yt_dlp
    validate_url(url)
    with tempfile.TemporaryDirectory(prefix='ym-media-') as temp:
        options = ydl_options(temp)
        with yt_dlp.YoutubeDL(options) as ydl:
            info = media_info(ydl.extract_info(url, download=False))
            ydl.params['format'] = choose_format(info, selection)
            # Fresh extraction validates the selected format; no client expressions are executed.
            ydl.params['match_filter'] = lambda item, *, incomplete: None if incomplete else (media_info(item) and None)
            ydl.extract_info(url, download=True)
        files = [p for p in Path(temp).glob('source.*') if p.suffix not in ('.part', '.ytdl')]
        if len(files) != 1 or not 0 < files[0].stat().st_size <= MAX_BYTES:
            raise ValueError('DOWNLOAD_INCOMPLETE_OR_SIZE_LIMIT')
        metadata = probe(files[0])
        target = output / ('video' + files[0].suffix)
        shutil.copyfile(files[0], target)
        # Keep original video/audio streams, with no forced lossy encoding or height cap.
        return {**metadata, 'title': str(info.get('title') or '영상')[:300],
                'filename': target.name, 'selection': selection, 'streamCopy': True}


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
    parser.add_argument('kind', choices=['smoke', 'analyze', 'download', 'transcribe'])
    parser.add_argument('--input', type=Path)
    parser.add_argument('--url', default=os.environ.get('YM_SOURCE_URL', ''))
    parser.add_argument('--language', choices=['ko', 'auto', 'en', 'ja', 'zh'], default='ko')
    parser.add_argument('--format-id', default='best')
    parser.add_argument('--output', type=Path, default=Path('output'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        if args.kind == 'smoke': result = smoke(args.output)
        elif args.kind == 'analyze': result = analyze(args.url, args.output)
        elif args.kind == 'download': result = download(args.url, args.output, args.format_id)
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
