"""FFmpeg 合成：统一规格 → 拼接 → 可选 BGM → 软字幕。"""
from __future__ import annotations

import subprocess
from pathlib import Path

OUT_SIZES = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (960, 960),
             "3:4": (720, 960), "4:3": (960, 720)}


SUB_FONT = "Noto Sans CJK SC"


def _escape_filter_path(p: Path) -> str:
    """ffmpeg 滤镜参数里的路径需要转义冒号、引号等。"""
    s = str(Path(p).resolve()).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def reframe(src: Path, dest: Path, ratio: str) -> Path:
    """转画幅：前景完整缩放居中，背景用同一画面放大裁切并模糊。"""
    w, h = OUT_SIZES[ratio]
    vf = (f"split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:2[bg];"
          f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
    _run(["ffmpeg", "-y", "-i", str(src), "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy",
          "-movflags", "+faststart", str(dest)])
    return dest


def segment_overlap(durs: list[float], transition: float) -> float:
    """实际转场时长：不超过最短镜头的一半；只有一个镜头时为 0。"""
    if transition <= 0 or len(durs) < 2:
        return 0.0
    return round(min(transition, min(durs) / 2), 3)


def effective_lengths(durs: list[float], overlap: float) -> list[float]:
    return [d - overlap for d in durs[:-1]] + durs[-1:] if overlap else list(durs)


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


def _ass_ts(sec: float) -> str:
    cs = int(round(sec * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def write_ass(segments: list[tuple[float, str]], dest: Path, w: int, h: int) -> None:
    """生成 ASS 字幕，虚拟分辨率 = 视频分辨率，字号和边距直接按像素算。"""
    size = round(min(w, h) * 0.065)       # 字号约为短边的 6.5%（手机上清晰可读）
    margin_v = round(h * 0.08)            # 距底部 8%
    margin_lr = round(w * 0.06)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{SUB_FONT},{size},&H00FFFFFF,&H000000FF,&H99000000,&H00000000,1,0,0,0,100,100,0,0,1,"
        f"{max(1, round(size / 16))},0,2,{margin_lr},{margin_lr},{margin_v},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    t = 0.0
    for dur, text in segments:
        text = text.strip().replace("{", "（").replace("}", "）").replace("\n", "\\N")
        if text:
            lines.append(f"Dialogue: 0,{_ass_ts(t)},{_ass_ts(t + dur)},Default,,0,0,0,,{text}")
        t += dur
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
            narration: list[tuple[float, Path]] | None = None, burn_subs: bool = False,
            transition: float = 0.0) -> Path:
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
    durs = [probe_duration(p) for p in normed]
    tr = segment_overlap(durs, transition)
    joined = work / "joined.mp4"
    if tr:  # 淡入淡出：相邻镜头重叠 tr 秒
        inputs, chain, prev, offset = [], [], "[0:v]", 0.0
        for p in normed:
            inputs += ["-i", str(p)]
        for k in range(1, len(normed)):
            offset += durs[k - 1] - tr
            out_label = f"[x{k}]"
            chain.append(f"{prev}[{k}:v]xfade=transition=fade:duration={tr:.3f}:offset={offset:.3f}{out_label}")
            prev = out_label
        _run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain), "-map", prev,
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(joined)])
    else:
        lst = work / "list.txt"
        lst.write_text("".join(f"file '{p.resolve()}'\n" for p in normed), encoding="utf-8")
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
    total = probe_duration(joined)
    segs = effective_lengths(durs, tr)  # 每镜在成片里占的时长（扣掉与下一镜的重叠）

    srt = work / "subs.srt"
    has_subs = write_srt(list(zip(segs, narrations)), srt)

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
    vcodec = ["-c:v", "copy"]
    if burn_subs and has_subs:  # 字幕画进画面（各平台都能看到）；同时仍保留软字幕轨
        ass = work / "subs.ass"
        write_ass(list(zip(segs, narrations)), ass, w, h)
        filters.append(f"[0:v]ass={_escape_filter_path(ass)}[vout]")
        cmd += ["-map", "[vout]"]
        vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-map", "0:v"]
    if audio_labels:
        mix = "".join(audio_labels)
        filters.append(f"{mix}amix=inputs={len(audio_labels)}:duration=longest:normalize=0,"
                       f"apad,atrim=0:{total:.3f}[aout]")
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
    if filters:
        cmd += ["-filter_complex", ";".join(filters)]
    if sub_idx is not None:
        cmd += ["-map", f"{sub_idx}:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=chi"]
    cmd += [*vcodec, "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    _run(cmd)
    if has_subs:
        (out.with_suffix(".srt")).write_text(srt.read_text(encoding="utf-8"), encoding="utf-8")
    return out
