"""Opt-in live HTTP test using a pinned recording of a Russian human speaker.

Run against a locally started API: python tests/real_speech_smoke.py --output /tmp/v6-smoke
Downloads one 128 KB Golos sample. Does not use TTS, mocks, or a paid ASR service.
"""
import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import requests

SAMPLE_URL = (
    'https://raw.githubusercontent.com/sberdevices/golos/'
    '1d7eac87f82dc2514d7c77450eb1b4017e6cb052/'
    'examples/data/001ce26c07c20eaa0d666b824c6c6924.wav'
)
SAMPLE_SHA256 = 'd066239503c4e7406ebeb47423334b5109aa6b30d62046d0338a04e41b4c52f5'
EXPECTED = 'шестьдесят тысяч тенге сколько будет стоить'


def run(command):
    return subprocess.check_output(command, stderr=subprocess.PIPE, text=True)


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))


def smoke(base_url, output, audio_fixture=None):
    output.mkdir(parents=True, exist_ok=True)
    if audio_fixture:
        recording = audio_fixture.read_bytes()
    else:
        with urlopen(SAMPLE_URL, timeout=30) as response:
            recording = response.read(1024 * 1024)
    assert hashlib.sha256(recording).hexdigest() == SAMPLE_SHA256, 'Golos fixture checksum mismatch'
    audio = output / 'golos.wav'
    audio.write_bytes(recording)
    source = output / 'source.mp4'
    # Only the picture is generated. Speech is the unmodified human recording.
    run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
         'color=c=blue:s=180x320:r=25:d=4', '-i', str(audio), '-map', '0:v', '-map', '1:a',
         '-c:v', 'libx264', '-threads', '1', '-c:a', 'aac', '-shortest', str(source)])
    session = requests.Session()
    # Local test servers must not be sent to the egress proxy.
    if base_url.startswith(('http://127.0.0.1:', 'http://localhost:')):
        session.trust_env = False
    elapsed = {}

    def post(endpoint):
        started = time.monotonic()
        with source.open('rb') as stream:
            response = session.post(base_url.rstrip('/') + endpoint,
                                    files={'file': ('source.mp4', stream, 'video/mp4')}, timeout=240)
        elapsed[endpoint] = round(time.monotonic() - started, 3)
        response.raise_for_status()
        return response

    report = post('/smart-speech-v6?diagnostics_only=true').json()
    text = ' '.join(word['word'] for word in report['words'])
    assert text == EXPECTED, (text, EXPECTED)
    previous = 0.0
    for word in report['words']:
        assert previous <= word['start'] < word['end'] <= report['duration']
        assert 0 <= word['conf'] <= 1
        previous = word['end']
    assert not report['proposed_deletions'], 'Normal speech must be preserved'
    (output / 'diagnostics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    exports = {}
    for version in ('v5', 'v6'):
        response = post('/smart-speech-' + version)
        target = output / (version + '.mp4')
        target.write_bytes(response.content)
        metadata = probe(target)
        video = next(stream for stream in metadata['streams'] if stream['codec_type'] == 'video')
        sound = next(stream for stream in metadata['streams'] if stream['codec_type'] == 'audio')
        assert video['codec_name'] == 'h264' and sound['codec_name'] == 'aac'
        assert (video['width'], video['height']) == (180, 320)
        if version == 'v6':
            assert video['r_frame_rate'] == '25/1'
            frames = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                     '-show_entries', 'frame=pts_time', '-of', 'json', str(target)]))['frames']
            timestamps = [float(frame['pts_time']) for frame in frames]
            assert len(timestamps) == 100
            assert all(abs(b - a - 0.04) < 0.0001 for a, b in zip(timestamps, timestamps[1:]))
            assert abs(float(video['duration']) - float(sound['duration'])) <= 0.05
            assert abs(float(metadata['format']['duration']) - 4.0) <= 0.08
        exports[version] = {'duration': metadata['format']['duration'], 'fps': video['r_frame_rate'],
                            'average_fps_metadata': video['avg_frame_rate'],
                            'size': [video['width'], video['height']]}
    summary = {'source': SAMPLE_URL, 'sample_sha256': SAMPLE_SHA256,
               'reference': EXPECTED, 'recognized': text, 'word_errors': 0, 'reference_words': 6,
               'exports': exports, 'elapsed_seconds': elapsed,
               'limits': 'One 4-second human utterance; generated picture does not test visible lip sync or natural failed takes.'}
    (output / 'result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8006')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--audio', type=Path, help='Use a previously downloaded, checksum-verified fixture offline')
    args = parser.parse_args()
    smoke(args.base_url, args.output, args.audio)
