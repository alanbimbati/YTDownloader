import json
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


@dataclass
class Video:
    path: str
    width: int
    height: int
    duration: float
    size: int

    @property
    def vertical(self) -> bool:
        return self.height > self.width

    @property
    def dim(self) -> str:
        return f"{self.width}x{self.height}"


def probe(path: str) -> Video:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-show_format", "-of", "json", path],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    stream = data["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    # Phones record landscape pixels plus a rotation flag: the orientation people see is the rotated one.
    rotation = int(stream.get("tags", {}).get("rotate", 0) or 0)
    for side in stream.get("side_data_list", []):
        rotation = int(side.get("rotation", rotation) or rotation)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return Video(path, width, height, float(data["format"].get("duration") or 0), os.path.getsize(path))


def extract_audio(video: str, out: str, max_seconds: int) -> str:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", video, "-t", str(max_seconds), "-vn", "-ac", "1", "-ar", "16000", out],
        check=True,
    )
    return out


def thumbnail(video: Video, out: str) -> Optional[str]:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(min(5.0, video.duration / 2)), "-i", video.path,
             "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", out],
            check=True,
        )
        return out
    except subprocess.CalledProcessError:
        return None


def shrink(video: Video, out: str, max_bytes: int) -> str:
    """A copy under max_bytes, for platforms that cap the file size. Software x264: slow on the Pi, but it only runs once."""
    if video.size <= max_bytes:
        return video.path
    if os.path.exists(out) and os.path.getsize(out) <= max_bytes:
        return out
    # 95% of the budget, audio included, spread over the duration.
    kbps = int(max_bytes * 8 * 0.95 / max(video.duration, 1) / 1000) - 128
    scale = "scale=-2:'min(1080,ih)'" if not video.vertical else "scale='min(1080,iw)':-2"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", video.path, "-vf", scale, "-c:v", "libx264", "-preset", "veryfast",
         "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out],
        check=True,
    )
    return out


def jpeg(image: str, out: str) -> str:
    """YouTube takes thumbnails up to 2MB: a 1280px JPEG always fits."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", image, "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", out], check=True)
    return out
