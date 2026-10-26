#!/usr/bin/env python3
"""
Daily audiobook video factory.

Required repository secrets:
  GEMINI_API_KEY
  YOUTUBE_CLIENT_ID
  YOUTUBE_CLIENT_SECRET
  YOUTUBE_REFRESH_TOKEN

Optional variables:
  GEMINI_MODELS              Comma-separated model IDs, in fallback order
  YOUTUBE_CATEGORY_ID         Default: 27 (Education)
  YOUTUBE_PRIVACY_STATUS      Default: public
  NARRATION_VOICE             Default: en-US-ChristopherNeural
  TARGET_MINUTES              Default: 45
  VIDEO_BITRATE               Default: 6000k
  MAX_SCRIPT_WORDS            Default: 7000

Queue format (one item per line):
  Topic title|cover_01.jpg

The queue is advanced only after a successful YouTube upload.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
import edge_tts
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parent
QUEUE_FILE = ROOT / "queue.txt"
CLIPS_DIR = ROOT / "clips"
MUSIC_DIR = ROOT / "music"
COVERS_DIR = ROOT / "covers"
OUTPUT_DIR = ROOT / "output"
WORK_DIR = ROOT / "work"

VOICE = os.getenv("NARRATION_VOICE", "en-US-ChristopherNeural")
TARGET_MINUTES = int(os.getenv("TARGET_MINUTES", "45"))
MINUTES_MIN = 40
MINUTES_MAX = 50
VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "6000k")
FPS = 30
WIDTH, HEIGHT = 1920, 1080

DEFAULT_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]
MODEL_ORDER = [m.strip() for m in os.getenv("GEMINI_MODELS", ",".join(DEFAULT_MODELS)).split(",") if m.strip()]

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def log(message: str) -> None:
    print(f"[audiobook-factory] {message}", flush=True)


def run(cmd: list[str], *, capture: bool = False) -> str:
    log("$ " + " ".join(str(x) for x in cmd))
    result = subprocess.run(
        cmd, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    return result.stdout.strip() if capture else ""


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub Actions secret/environment variable: {name}")
    return value


def list_assets(folder: Path, extensions: set[str]) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in extensions)


def read_queue() -> tuple[str, str, list[str]]:
    if not QUEUE_FILE.exists():
        raise FileNotFoundError(f"Missing {QUEUE_FILE}")
    lines = [line.strip() for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise RuntimeError("queue.txt has no topics. Add lines in: Topic Title|cover_01.jpg")
    first = lines[0]
    if "|" not in first:
        raise ValueError(f"Invalid queue line: {first!r}. Expected Topic Title|cover_filename.jpg")
    topic, cover_name = (part.strip() for part in first.split("|", 1))
    if not topic or not cover_name:
        raise ValueError(f"Invalid queue line: {first!r}")
    cover = (COVERS_DIR / cover_name).resolve()
    if cover.parent != COVERS_DIR.resolve() or not cover.is_file() or cover.suffix.lower() not in IMAGE_EXTS:
        raise FileNotFoundError(f"Cover '{cover_name}' not found in covers/. Add the matching image before running.")
    return topic, cover_name, lines


def advance_queue(lines: list[str]) -> None:
    # Called only after the YouTube upload succeeds.
    active = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    remaining = active[1:]
    comments = [line for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines()
                if line.lstrip().startswith("#")]
    QUEUE_FILE.write_text(
        "\n".join(comments + remaining) + ("\n" if comments or remaining else ""),
        encoding="utf-8",
    )


def gemini_text(client: genai.Client, prompt: str) -> str:
    errors: list[str] = []
    for model in MODEL_ORDER:
        try:
            log(f"Trying Gemini model: {model}")
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.85,
                    max_output_tokens=8192,
                ),
            )
            text = (response.text or "").strip()
            if len(text.split()) < 150:
                raise RuntimeError("Model returned too little usable text.")
            return text
        except Exception as exc:
            message = f"{model}: {type(exc).__name__}: {exc}"
            errors.append(message)
            log("Generation failed; trying next model. " + message[:400])
    raise RuntimeError("All configured Gemini models failed:\n" + "\n".join(errors))


def generate_script(topic: str) -> str:
    api_key = require_env("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    test_mode = os.getenv("FACTORY_TEST_MODE", "false").lower() == "true"
    # Multiple manageable sections are more reliable than one giant response.
    section_names = [
        "The uncomfortable truth", "The hidden cost of staying the same",
        "Rebuilding your identity", "Discipline when motivation disappears",
        "Becoming valuable through action", "Turning setbacks into leverage",
        "The daily system that compounds", "A powerful closing challenge",
    ]
    if test_mode:
        section_names = ["Short test narration", "Short closing"]
    sections: list[str] = []
    prior_summaries: list[str] = []
    for index, section_name in enumerate(section_names, 1):
        prior = "\n".join(prior_summaries[-3:]) or "This is the opening section."
        section_target = "150-220 spoken English words" if test_mode else "700-900 spoken English words"
        duration_target = "about 2 minutes for a test run" if test_mode else "about 40-50 minutes at a natural 130-145 words per minute (roughly 5,500-6,700 words total)"
        prompt = f"""
Write section {index} of {len(section_names)} for a long-form motivational audiobook narration titled/topic:
"{topic}"

SECTION THEME: {section_name}
TARGET: {section_target} for this section. The total finished narration should target {duration_target}.
Write original, thoughtful, emotionally intense, authoritative prose suitable for spoken narration.
Use natural paragraphs and varied sentence lengths. Include practical examples and concrete actions.
Do not mention AI, prompts, sections, word counts, or that this is a generated script.
Do not add headings, bullet points, markdown, stage directions, sound effects, or citations.
Avoid repetitive slogans and avoid copying passages from copyrighted books.
Continue coherently from this context of the previous sections:
{prior}
This section must develop its theme and not repeat earlier material. End with a natural transition
toward the next idea. Return only narration text.
"""
        section = gemini_text(client, prompt)
        sections.append(section)
        # Short context helps continuity without repeatedly sending the entire script.
        prior_summaries.append(f"Section {index} ({section_name}) summary: " + " ".join(section.split()[:90]))
        log(f"Generated section {index}/{len(section_names)} ({len(section.split())} words)")
    script = "\n\n".join(sections)
    words = len(script.split())
    if words < 4500:
        log(f"Warning: script has only {words} words; narration may be shorter than 40 minutes.")
    if words > int(os.getenv("MAX_SCRIPT_WORDS", "7000")):
        log(f"Warning: script has {words} words; trimming to configured maximum.")
        script = " ".join(script.split()[:int(os.getenv("MAX_SCRIPT_WORDS", "7000"))])
    return script


def split_text(text: str, max_chars: int = 2200) -> list[str]:
    # Keep paragraphs/sentences intact as much as possible for stable TTS requests.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        for sentence in sentences:
            if len(sentence) > max_chars:
                # Last-resort split on spaces for an exceptionally long sentence.
                words = sentence.split()
                pieces: list[str] = []
                piece = ""
                for word in words:
                    if piece and len(piece) + len(word) + 1 > max_chars:
                        pieces.append(piece)
                        piece = word
                    else:
                        piece = (piece + " " + word).strip()
                if piece:
                    pieces.append(piece)
            else:
                pieces = [sentence]
            for part in pieces:
                if current and len(current) + len(part) + 1 > max_chars:
                    chunks.append(current.strip())
                    current = part
                else:
                    current = (current + " " + part).strip()
    if current:
        chunks.append(current.strip())
    return [c for c in chunks if c]


async def synthesize_chunk(text: str, destination: Path) -> list[dict[str, Any]]:
    communicate = edge_tts.Communicate(text, VOICE, rate="+0%", pitch="+0Hz")
    boundaries: list[dict[str, Any]] = []
    with destination.open("wb") as audio_file:
        async for event in communicate.stream():
            if event["type"] == "audio":
                audio_file.write(event["data"])
            elif event["type"] == "WordBoundary":
                boundaries.append({
                    "text": str(event.get("text", "")).strip(),
                    "offset": int(event.get("offset", 0)) / 10_000_000.0,
                    "duration": int(event.get("duration", 0)) / 10_000_000.0,
                })
    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError(f"Edge TTS returned an empty audio chunk: {destination.name}")
    return boundaries


def probe_duration(path: Path) -> float:
    output = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ], capture=True)
    return float(output)


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def synthesize_script(script: str, work_dir: Path) -> tuple[Path, Path, Path, float]:
    chunks = split_text(script)
    if not chunks:
        raise RuntimeError("Generated script is empty.")
    chunk_files: list[Path] = []
    all_words: list[dict[str, Any]] = []
    offset_seconds = 0.0
    for index, chunk in enumerate(chunks, 1):
        audio_path = work_dir / f"voice_{index:03d}.mp3"
        log(f"Synthesizing audio chunk {index}/{len(chunks)} ({len(chunk)} chars)")
        boundaries = asyncio.run(synthesize_chunk(chunk, audio_path))
        duration = probe_duration(audio_path)
        for boundary in boundaries:
            all_words.append({
                "text": boundary["text"],
                "start": offset_seconds + boundary["offset"],
                "end": offset_seconds + boundary["offset"] + max(boundary["duration"], 0.08),
            })
        offset_seconds += duration
        chunk_files.append(audio_path)

    concat_file = work_dir / "audio_concat.txt"
    concat_file.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in chunk_files),
        encoding="utf-8",
    )
    voice_path = work_dir / "voice.mp3"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
         "-safe", "0", "-i", str(concat_file), "-c", "copy", str(voice_path)])

    srt_path = work_dir / "captions.srt"
    ass_path = work_dir / "captions.ass"
    groups: list[list[dict[str, Any]]] = []
    # 3-4 words per caption, similar to the supplied reference.
    i = 0
    while i < len(all_words):
        group_size = random.choice([3, 4])
        group = all_words[i:i + group_size]
        if group:
            groups.append(group)
        i += group_size

    with srt_path.open("w", encoding="utf-8") as srt:
        for number, group in enumerate(groups, 1):
            start = group[0]["start"]
            end = max(group[-1]["end"], start + 0.35)
            text = " ".join(word["text"] for word in group).strip()
            srt.write(f"{number}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n\n")

    # ASS subtitle styling: bold white text, black outline, amber/orange backing box,
    # positioned in the right-center so the book cover remains unobstructed.
    header = r"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial Black,68,&H00FFFFFF,&H0000A5FF,&H00000000,&H0000A5FF,-1,0,3,2,1,6,830,105,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with ass_path.open("w", encoding="utf-8") as ass:
        ass.write(header)
        for group in groups:
            start = group[0]["start"]
            end = max(group[-1]["end"], start + 0.35)
            text = " ".join(w["text"] for w in group)
            text = text.replace("{", r"\{").replace("}", r"\}")
            ass.write(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Caption,,0,0,0,,{text}\n")

    actual_duration = probe_duration(voice_path)
    minutes = actual_duration / 60
    log(f"Voice duration: {minutes:.1f} minutes ({len(script.split())} script words)")
    if minutes < MINUTES_MIN or minutes > MINUTES_MAX:
        log(f"WARNING: narration is outside target {MINUTES_MIN}-{MINUTES_MAX} minutes.")
    return voice_path, srt_path, ass_path, actual_duration


def ffmpeg_escape_filter_path(path: Path) -> str:
    # Escape path characters for FFmpeg filter arguments on Linux.
    return str(path.resolve()).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")


def assemble_video(topic: str, cover: Path, voice: Path, ass: Path, duration: float, work_dir: Path) -> Path:
    clips = list_assets(CLIPS_DIR, VIDEO_EXTS)
    music = list_assets(MUSIC_DIR, AUDIO_EXTS)
    if not clips:
        raise RuntimeError("clips/ is empty. Add at least one background video file.")
    if not music:
        raise RuntimeError("music/ is empty. Add at least one ambient music audio file.")
    clip = random.choice(clips)
    music_track = random.choice(music)
    output = OUTPUT_DIR / "audiobook_video.mp4"
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Layout scaled from the supplied reference: large stationary cover at left, captions at right.
    # Cover position/size can be overridden through repository variables.
    cover_x = int(os.getenv("COVER_X", "260"))
    cover_y = int(os.getenv("COVER_Y", "150"))
    cover_w = int(os.getenv("COVER_WIDTH", "540"))
    cover_h = int(os.getenv("COVER_HEIGHT", "820"))
    ass_path = ffmpeg_escape_filter_path(ass)
    cover_path = ffmpeg_escape_filter_path(cover)
    vf = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},fps={FPS},"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.08:t=fill,"
        f"movie='{cover_path}',scale={cover_w}:{cover_h}:force_original_aspect_ratio=decrease,"
        f"pad={cover_w}:{cover_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"format=rgba[cover];[in][cover]overlay={cover_x}:{cover_y}:shortest=1,"
        f"subtitles='{ass_path}'"
    )
    # Use a looped input for background and music. The voice defines exact output duration.
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-stream_loop", "-1", "-i", str(clip),
        "-i", str(voice),
        "-stream_loop", "-1", "-i", str(music_track),
        "-filter_complex",
        (
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1[bg];"
            f"[bg]drawbox=x=0:y=0:w=iw:h=ih:color=black@0.10:t=fill[dim];"
            f"[dim][2:a]amix=inputs=1:duration=first:dropout_transition=2[unused]"
        ),
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf",
        (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.10:t=fill,"
            f"movie='{cover_path}',scale={cover_w}:{cover_h}:force_original_aspect_ratio=decrease,"
            f"pad={cover_w}:{cover_h}:(ow-iw)/2:(oh-ih)/2:color=black[cover];"
            f"[in][cover]overlay={cover_x}:{cover_y},subtitles='{ass_path}'"
        ),
        "-filter_complex",
        (
            f"[2:a]volume=0.08[bgm];[1:a][bgm]amix=inputs=2:duration=first:"
            f"dropout_transition=3:normalize=0[aout]"
        ),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_BITRATE, "-bufsize", "12000k",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", "-shortest", str(output),
    ]
    # Keep the FFmpeg graph simple and valid: one video filter chain and one audio mix graph.
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-stream_loop", "-1", "-i", str(clip),
        "-i", str(voice),
        "-stream_loop", "-1", "-i", str(music_track),
        "-t", f"{duration:.3f}",
        "-filter_complex",
        (
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.10:t=fill[bg];"
            f"[2:a]volume=0.08[bgm];[1:a][bgm]amix=inputs=2:duration=first:"
            f"dropout_transition=3:normalize=0[aout]"
        ),
        "-vf",
        (
            f"movie='{cover_path}',scale={cover_w}:{cover_h}:force_original_aspect_ratio=decrease,"
            f"pad={cover_w}:{cover_h}:(ow-iw)/2:(oh-ih)/2:color=black[cover];"
            f"[in][cover]overlay={cover_x}:{cover_y},subtitles='{ass_path}'"
        ),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_BITRATE, "-bufsize", "12000k",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(output),
    ]
    # FFmpeg -vf cannot reference a separate movie source this way on all versions.
    # Use a single filter_complex graph for background, cover overlay, subtitles, and audio.
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-stream_loop", "-1", "-i", str(clip),
        "-i", str(voice),
        "-stream_loop", "-1", "-i", str(music_track),
        "-t", f"{duration:.3f}",
        "-filter_complex",
        (
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.10:t=fill[bg];"
            f"[bg]subtitles='{ass_path}'[sub];"
            f"[2:a]volume=0.08[bgm];[1:a][bgm]amix=inputs=2:duration=first:"
            f"dropout_transition=3:normalize=0[aout]"
        ),
        "-map", "[sub]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_BITRATE, "-bufsize", "12000k",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(output),
    ]
    # Composite the book cover as an FFmpeg overlay input, which is robust and deterministic.
    # Add the still cover as a looped input and build a single video graph.
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-stream_loop", "-1", "-i", str(clip),
        "-i", str(voice),
        "-stream_loop", "-1", "-i", str(music_track),
        "-loop", "1", "-i", str(cover),
        "-t", f"{duration:.3f}",
        "-filter_complex",
        (
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.10:t=fill[bg];"
            f"[3:v]scale={cover_w}:{cover_h}:force_original_aspect_ratio=decrease,"
            f"pad={cover_w}:{cover_h}:(ow-iw)/2:(oh-ih)/2:color=black,format=rgba[book];"
            f"[bg][book]overlay={cover_x}:{cover_y}:shortest=1[withbook];"
            f"[withbook]subtitles='{ass_path}'[vout];"
            f"[2:a]volume=0.08[bgm];[1:a][bgm]amix=inputs=2:duration=first:"
            f"dropout_transition=3:normalize=0[aout]"
        ),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_BITRATE, "-bufsize", "12000k",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(output),
    ]
    run(cmd)
    if not output.exists() or output.stat().st_size < 1_000_000:
        raise RuntimeError("FFmpeg did not create a valid video output.")
    log(f"Rendered {output} ({output.stat().st_size / (1024*1024):.1f} MiB)")
    return output


def upload_youtube(video_path: Path, topic: str) -> str:
    client_id = require_env("YOUTUBE_CLIENT_ID")
    client_secret = require_env("YOUTUBE_CLIENT_SECRET")
    refresh_token = require_env("YOUTUBE_REFRESH_TOKEN")
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=YOUTUBE_SCOPES,
    )
    credentials.refresh(Request())
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    description = (
        f"{topic}\n\n"
        "A long-form motivational audiobook about mindset, discipline, personal growth, and taking action.\n\n"
        "Listen, reflect, and put one idea into practice today.\n\n"
        "#Audiobook #Motivation #PersonalDevelopment"
    )
    body = {
        "snippet": {
            "title": topic[:95],
            "description": description[:4900],
            "tags": ["audiobook", "motivation", "personal development", "mindset",
                     "discipline", "self improvement", "success", "inspirational"],
            "categoryId": os.getenv("YOUTUBE_CATEGORY_ID", "27"),
        },
        "status": {
            "privacyStatus": ("unlisted" if os.getenv("FACTORY_TEST_MODE", "false").lower() == "true" else os.getenv("YOUTUBE_PRIVACY_STATUS", "public")),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log(f"YouTube upload progress: {status.progress() * 100:.1f}%")
    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"YouTube API returned no video ID: {response}")
    log(f"Published video: https://www.youtube.com/watch?v={video_id}")
    return video_id


def main() -> int:
    for folder in [CLIPS_DIR, MUSIC_DIR, COVERS_DIR, OUTPUT_DIR, WORK_DIR]:
        folder.mkdir(parents=True, exist_ok=True)
    topic, cover_name, queue_lines = read_queue()
    cover = COVERS_DIR / cover_name
    log(f"Starting topic: {topic}")
    log(f"Using cover: {cover_name}")

    # Remove stale intermediates so a failed run cannot accidentally upload an old result.
    for path in WORK_DIR.iterdir():
        if path.is_file():
            path.unlink()
    output = OUTPUT_DIR / "audiobook_video.mp4"
    if output.exists():
        output.unlink()

    script = generate_script(topic)
    script_path = WORK_DIR / "narration.txt"
    script_path.write_text(script, encoding="utf-8")
    voice, srt, ass, duration = synthesize_script(script, WORK_DIR)
    video = assemble_video(topic, cover, voice, ass, duration, WORK_DIR)
    video_id = upload_youtube(video, topic)

    # Save useful run metadata and advance queue only after upload success.
    (OUTPUT_DIR / "last_upload.json").write_text(json.dumps({
        "topic": topic, "cover": cover_name, "youtube_video_id": video_id,
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "duration_seconds": round(duration, 2), "video_file": video.name,
    }, indent=2), encoding="utf-8")
    if os.getenv("FACTORY_TEST_MODE", "false").lower() == "true":
        log("Test mode: upload is unlisted and queue was not advanced.")
    else:
        advance_queue(queue_lines)
        log("Queue advanced after successful YouTube upload.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log(f"FATAL: {type(exc).__name__}: {exc}")
        raise
