"""FFmpeg 合成：统一规格 → 拼接 → 可选 BGM → 软字幕。"""
from __future__ import annotations

import subprocess
from pathlib import Path

OUT_SIZES = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (960, 960),
             "3:4": (720, 960), "4:3": (960, 720)}


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{' '.join(cmd[:6])}…\n{r.stderr[-1500:]}")


def _ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def write_srt(segments: list[tuple[float, str]], dest: Path) -> bool:
    """segments: [(时长, 字幕)]，空字幕跳过。返回是否写出了内容。"""
    lines, t, n = [], 0.0, 0
    for dur, text in segments:
        if text.strip():
            n += 1
            lines += [str(n), f"{_ts(t)} --> {_ts(t + dur)}", text.strip(), ""]
        t += dur
    if n:
        dest.write_text("\n".join(lines), encoding="utf-8")
    return bool(n)


def compose(clips: list[Path], narrations: list[str], ratio: str, out: Path,
            bgm: Path | None = None, fps: int = 24,
            narration: list[tuple[float, Path]] | None = None) -> Path:
    """narration: [(起始秒, 配音文件)]，按镜头起点放置；有配音时 BGM 自动压低。"""
    narration = narration or []
    work = out.parent / "_compose"
    work.mkdir(parents=True, exist_ok=True)
    w, h = OUT_SIZES.get(ratio, (1280, 720))
    normed = []
    for i, c in enumerate(clips, 1):
        n = work / f"n{i:02}.mp4"
        _run(["ffmpeg", "-y", "-i", str(c), "-an",
              "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(n)])
        normed.append(n)
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in normed), encoding="utf-8")
    joined = work / "joined.mp4"
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
    total = probe_duration(joined)

    srt = work / "subs.srt"
    has_subs = write_srt([(probe_duration(p), t) for p, t in zip(normed, narrations)], srt)

    cmd = ["ffmpeg", "-y", "-i", str(joined)]
    audio_labels, filters, k = [], [], 1
    if bgm:
        cmd += ["-stream_loop", "-1", "-i", str(bgm)]
        filters.append(f"[{k}:a]volume={0.25 if narration else 0.6}[bgm]")
        audio_labels.append("[bgm]")
        k += 1
    for i, (start, path) in enumerate(narration):
        cmd += ["-i", str(path)]
        ms = int(round(start * 1000))
        filters.append(f"[{k}:a]adelay={ms}|{ms},aresample=44100[n{i}]")
        audio_labels.append(f"[n{i}]")
        k += 1
    sub_idx = None
    if has_subs:
        cmd += ["-i", str(srt)]
        sub_idx = k
    cmd += ["-map", "0:v"]
    if audio_labels:
        mix = "".join(audio_labels)
        filters.append(f"{mix}amix=inputs={len(audio_labels)}:duration=longest:normalize=0,"
                       f"apad,atrim=0:{total:.3f}[aout]")
        cmd += ["-filter_complex", ";".join(filters), "-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
    if sub_idx is not None:
        cmd += ["-map", f"{sub_idx}:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=chi"]
    cmd += ["-c:v", "copy", "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    _run(cmd)
    if has_subs:
        (out.with_suffix(".srt")).write_text(srt.read_text(encoding="utf-8"), encoding="utf-8")
    return out
