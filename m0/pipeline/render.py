"""Local, deterministic 240-frame composition; never substitutes missing footage."""
import array
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import wave
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, FPS = 432, 768, 24


def ffmpeg():
    binary = os.environ.get('FFMPEG_BINARY') or shutil.which('ffmpeg')
    if not binary:
        import imageio_ffmpeg
        binary = imageio_ffmpeg.get_ffmpeg_exe()
    return binary


def run(args):
    result = subprocess.run([ffmpeg(), '-hide_banner', '-nostdin', '-y', *map(str, args)], capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError('Video processing failed: ' + result.stderr[-1500:])
    return result


def frame_count(path):
    result = run(['-i', path, '-map', '0:v:0', '-an', '-vf', 'fps=24', '-progress', 'pipe:1', '-f', 'null', '-'])
    values = re.findall(r'^frame=(\d+)$', result.stdout, re.M)
    if not values:
        raise RuntimeError('Cannot validate decoded video frames')
    return int(values[-1])


def font_path():
    candidates = [os.environ.get('CHINESE_FONT', ''), '/System/Library/Fonts/STHeiti Medium.ttc',
                  '/System/Library/Fonts/Supplemental/Songti.ttc',
                  '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError('Chinese font missing; configure CHINESE_FONT')


def preflight():
    run(['-version'])
    ImageFont.truetype(font_path(), 24)


def overlay(path, text, price=None, width=WIDTH, height=HEIGHT):
    scale = width / WIDTH
    image = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(font_path(), round(24 * scale))
    small = ImageFont.truetype(font_path(), round(16 * scale))
    draw.rounded_rectangle((round(14 * scale), round(14 * scale), round(172 * scale), round(45 * scale)),
                           radius=round(6 * scale), fill=(0, 0, 0, 185))
    draw.text((round(23 * scale), round(20 * scale)), 'AI 生成 · 原创配乐', font=small, fill='white')
    lines, line = [], ''
    for char in text:
        if char == '\n' or draw.textlength(line + char, font=font) > width - 64 * scale:
            lines.append(line)
            line = '' if char == '\n' else char
        else:
            line += char
    if line:
        lines.append(line)
    if len(lines) > 4:
        raise ValueError('Subtitle too long')
    line_h = round(34 * scale)
    y = height - round(72 * scale) - line_h * len(lines)
    draw.rounded_rectangle((round(18 * scale), y - round(12 * scale), width - round(18 * scale), height - round(58 * scale)),
                           radius=round(10 * scale), fill=(0, 0, 0, 185))
    for line in lines:
        draw.text(((width - draw.textlength(line, font=font)) / 2, y), line, font=font, fill='white')
        y += line_h
    if price:
        label = '价格 ¥' + price
        draw.rounded_rectangle((round(22 * scale), round(76 * scale), width - round(22 * scale), round(123 * scale)),
                               radius=round(10 * scale), fill=(190, 56, 14, 235))
        draw.text((round(34 * scale), round(86 * scale)), label, font=font, fill='white')
    image.save(path)


def music(path):
    """Procedural original instrumental, no licensed library or external service."""
    rate = 44100
    notes = [261.63, 329.63, 392.0, 329.63, 293.66, 349.23, 440.0, 349.23]
    samples = array.array('h')
    for i in range(rate * 10):
        t = i / rate
        beat = t % .5
        note = notes[int(t / .5) % len(notes)]
        envelope = min(t / .4, 1, (10 - t) / .6)
        value = (.12 * math.sin(2 * math.pi * note * t) * math.exp(-5 * beat)
                 + .06 * math.sin(2 * math.pi * note / 2 * t)
                 + .08 * math.sin(2 * math.pi * 65 * beat) * math.exp(-28 * beat)) * envelope
        samples.append(int(32767 * value))
    if __import__('sys').byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(samples.tobytes())


def compose(raw_paths, subtitles, price, directory, width=WIDTH, height=HEIGHT):
    if len(raw_paths) != 2 or len(subtitles) != 2:
        raise ValueError('Exactly two real shots required')
    directory = Path(directory)
    preflight()
    for i, (raw, text) in enumerate(zip(raw_paths, subtitles)):
        if frame_count(raw) < 120:
            raise ValueError('Real shot shorter than five seconds; no freeze or mock padding allowed')
        overlay(directory / f'overlay_{i}.png', text, price if i == 1 else None, width, height)
        run(['-i', raw, '-loop', '1', '-i', directory / f'overlay_{i}.png',
             '-filter_complex', f'[0:v]fps=24,trim=end_frame=120,setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v];[v][1:v]overlay=0:0:shortest=1,settb=1/24,setpts=N[out]',
             '-map', '[out]', '-an', '-r', '24', '-fps_mode', 'cfr', '-frames:v', '120', '-c:v', 'libx264', '-preset', 'fast', '-pix_fmt', 'yuv420p', directory / f'part_{i}.mp4'])
    music(directory / 'original_bgm.wav')
    temporary = directory / 'final.pending.mp4'
    run(['-i', directory / 'part_0.mp4', '-i', directory / 'part_1.mp4', '-i', directory / 'original_bgm.wav',
         '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0,settb=1/24,setpts=N[v]', '-map', '[v]', '-map', '2:a:0',
         '-t', '10', '-r', '24', '-fps_mode', 'cfr', '-frames:v', '240', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
         '-movflags', '+faststart+use_metadata_tags', '-metadata', 'ai_generated=true',
         '-metadata', 'comment=AI-generated visuals; original procedural music; not regulatory certification', temporary])
    final_frames = frame_count(temporary)
    if final_frames != 240:
        part_frames = [frame_count(directory / f'part_{index}.mp4') for index in range(2)]
        raise RuntimeError(f'Final duration validation failed: expected 240 frames, got {final_frames}; shots={part_frames}')
    final = directory / 'final.mp4'
    temporary.replace(final)
    return str(final)
