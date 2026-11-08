"""视频 → 完整转录 + 关键画面截图。

只做两件事：把声音变成文字，把重要画面截下来。
不生成摘要，不改写原文——后续要不要总结，交给用户自己把 transcript.txt 发给 AI。

输出（KB 内）：
  002-references/raw/video/YYYYMMDD-<标题>-<url指纹>/
    transcript.md     带时间戳、关键截图的 Markdown
    transcript.txt    纯文本，方便复制给 AI
    note.html         自包含 HTML（图片内嵌），手机上看最方便
    meta.json         元信息 + 选帧记录
    frames/*.jpg      关键画面
"""

import base64
import difflib
import html
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import wave
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
import requests
import yaml
import yt_dlp

try:
    from zhconv import convert as _zh_convert
except ImportError:  # 可选：没装就保留模型原样
    _zh_convert = None

import store

_ocr = None
_whisper = None
_whisper_name = None


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def is_video_url(url):
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":")[0]
    path = (parsed.path or "").lower()

    if host in ("b23.tv", "www.b23.tv"):
        return True
    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        return path.startswith(("/video/", "/bangumi/"))
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com",
                "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"):
        return True
    if host == "douyin.com" or host.endswith(".douyin.com"):
        return True
    if host == "kuaishou.com" or host.endswith(".kuaishou.com"):
        return True
    if host == "vimeo.com" or host.endswith(".vimeo.com"):
        return True
    return False


def _env(name, default=""):
    return os.environ.get(name, "").strip() or default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _proxy():
    return _env("PROXY") or None


def _requests_proxies():
    proxy = _proxy()
    return {"http": proxy, "https": proxy} if proxy else None


MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)


def resolve_url(url):
    """抖音短链 / 分享页 → 标准 douyin.com/video/<id>。

    yt-dlp 的 Douyin 提取器只认 /video/<id>，不认 v.douyin.com 和
    iesdouyin.com/share/video/...，所以先把 ID 抠出来。
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":")[0]
    if "douyin" not in host:
        return url

    match = re.search(r"/(?:share/)?video/(\d+)", parsed.path)
    if not match:
        match = re.search(r"(?:modal_id|aweme_id)=(\d+)", parsed.query)
    if match:
        return f"https://www.douyin.com/video/{match.group(1)}"

    if host in ("v.douyin.com", "www.v.douyin.com"):
        try:
            resp = requests.get(
                url, headers={"User-Agent": MOBILE_UA},
                proxies=_requests_proxies(), timeout=30, allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"抖音短链展开失败：{exc}") from exc
        for text in (resp.url, resp.text[:200000]):
            match = re.search(r"(?:/video/|itemId[\"']?\s*[:=]\s*[\"']?)(\d{6,})", text)
            if match:
                return f"https://www.douyin.com/video/{match.group(1)}"
        raise RuntimeError(
            "抖音短链没解析出视频 ID：链接可能失效，或抖音要求浏览器验证。"
        )
    return url


def _run(cmd, timeout=900, cwd=None):
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, cwd=cwd,
    )
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-1200:]
        raise RuntimeError(f"{Path(cmd[0]).name} 失败：{tail}")
    return proc


def _ydl_opts(**extra):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    proxy = _proxy()
    if proxy:
        opts["proxy"] = proxy
    cookies = _env("VIDEO_COOKIES")
    if cookies:
        opts["cookiefile"] = cookies
    else:
        browser = _env("VIDEO_COOKIES_FROM_BROWSER")
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
    opts.update(extra)
    return opts


def _fmt_ts(seconds, dash=False):
    total = int(max(0, seconds))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    sep = "-" if dash else ":"
    if h:
        return f"{h:02d}{sep}{m:02d}{sep}{s:02d}"
    return f"{m:02d}{sep}{s:02d}"


def _fmt_duration(seconds):
    return _fmt_ts(seconds)


def _is_cjk(text):
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return cjk >= max(1, len(text.strip()) * 0.15)


def _join(a, b):
    return (a + b) if _is_cjk(a + b) else (a + " " + b)


# --------------------------------------------------------------------------
# 元信息 / 字幕
# --------------------------------------------------------------------------

def metadata(url):
    url = resolve_url(url)
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True)) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        if "douyin.com" in url and "cookies" in str(exc).lower():
            raise RuntimeError(
                "抖音需要 fresh cookies（不一定要登录）：用浏览器扩展导出 cookies.txt，"
                "在 .env 里设 VIDEO_COOKIES=文件路径，然后重启 bot。"
            ) from exc
        raise

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("这个链接里没有可处理的视频")
        info = entries[0]
    return info


def _parse_timestamp(value):
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        raise ValueError(value)
    return int(h) * 3600 + int(m) * 60 + float(s)


def _clean_cue(text):
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _parse_vtt(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows, i = [], 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(
            r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s*-->\s*"
            r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})",
            line,
        )
        if not m:
            i += 1
            continue
        start, end = _parse_timestamp(m.group(1)), _parse_timestamp(m.group(2))
        i += 1
        buf = []
        while i < len(lines) and lines[i].strip():
            buf.append(lines[i])
            i += 1
        body = _clean_cue(" ".join(buf))
        if body:
            rows.append({"start": start, "end": end, "text": body})
        i += 1
    return _dedupe_segments(rows)


def _parse_srt(text):
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    rows = []
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        idx = 0
        if lines[0].strip().isdigit():
            idx = 1
        if idx >= len(lines) or "-->" not in lines[idx]:
            continue
        start_s, end_s = [x.strip() for x in lines[idx].split("-->", 1)]
        try:
            start = _parse_timestamp(start_s)
            end = _parse_timestamp(end_s.split()[0])
        except ValueError:
            continue
        body = _clean_cue(" ".join(lines[idx + 1:]))
        if body:
            rows.append({"start": start, "end": end, "text": body})
    return _dedupe_segments(rows)


def _parse_json_subs(text):
    data = json.loads(text)
    rows = []

    # YouTube json3
    for event in data.get("events") or []:
        segs = event.get("segs") or []
        body = _clean_cue("".join(s.get("utf8", "") for s in segs))
        if not body:
            continue
        start = (event.get("tStartMs") or 0) / 1000.0
        end = start + (event.get("dDurationMs") or 0) / 1000.0
        rows.append({"start": start, "end": end, "text": body})

    # Bilibili AI subtitle
    if not rows:
        for item in data.get("body") or []:
            body = _clean_cue(str(item.get("content") or ""))
            if body:
                rows.append({
                    "start": float(item.get("from") or 0),
                    "end": float(item.get("to") or 0),
                    "text": body,
                })
    return _dedupe_segments(rows)


def _dedupe_segments(rows):
    rows = sorted(rows, key=lambda r: (r["start"], r["end"]))
    out = []
    for row in rows:
        text = row["text"].strip()
        if not text:
            continue
        if out:
            prev = out[-1]
            if text == prev["text"]:
                prev["end"] = max(prev["end"], row["end"])
                continue
            # YouTube 滚动字幕常见：后一条把前一条整句重复一遍
            if text.startswith(prev["text"]) and len(prev["text"]) >= 8:
                text = text[len(prev["text"]):].strip(" ，,。.")
                if not text:
                    prev["end"] = max(prev["end"], row["end"])
                    continue
        out.append({"start": row["start"], "end": row["end"], "text": text})
    return out


def parse_subtitle(path):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    suffix = path.suffix.lower()
    if suffix in (".json", ".json3"):
        return _parse_json_subs(text)
    if suffix == ".srt":
        return _parse_srt(text)
    return _parse_vtt(text)


def download_subtitles(url, workdir):
    opts = _ydl_opts(
        skip_download=True,
        writesubtitles=True,
        writeautomaticsub=True,
        subtitleslangs=["zh.*", "en.*"],
        subtitlesformat="vtt/srt/best",
        outtmpl=str(workdir / "sub.%(language)s.%(ext)s"),
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:  # noqa: BLE001
        return None

    files = sorted(workdir.glob("sub.*"))
    if not files:
        return None

    def rank(path):
        name = path.name.lower()
        return (0 if "zh" in name else 1 if "en" in name else 2, len(name))

    for path in sorted(files, key=rank):
        try:
            rows = parse_subtitle(path)
        except Exception:  # noqa: BLE001
            continue
        if rows and sum(len(r["text"]) for r in rows) >= 30:
            return rows, f"platform-subtitle:{path.suffix.lstrip('.')}"
    return None


# --------------------------------------------------------------------------
# 下载 / 转写
# --------------------------------------------------------------------------

def download_video(url, duration, workdir):
    height = 720 if duration <= 5400 else 480
    opts = _ydl_opts(
        format=f"bv*[height<={height}]+ba/b[height<={height}]/b",
        merge_output_format="mp4",
        outtmpl=str(workdir / "video.%(ext)s"),
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    files = [
        p for p in workdir.glob("video.*")
        if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".flv", ".mov")
    ]
    if not files:
        raise RuntimeError("视频下载失败：yt-dlp 没有产出文件")
    return max(files, key=lambda p: p.stat().st_size)


def _probe_duration(path):
    proc = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ], timeout=120)
    return float(proc.stdout.strip() or 0)


def _whisper_model():
    global _whisper, _whisper_name
    name = _env("WHISPER_MODEL", "small")
    if _whisper is None or _whisper_name != name:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(name, device="cpu", compute_type="int8")
        _whisper_name = name
    return _whisper, name


def transcribe(video_path, workdir):
    audio_path = workdir / "audio.wav"
    _run([
        "ffmpeg", "-hide_banner", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        "-f", "wav", str(audio_path),
    ], timeout=1800)

    with wave.open(str(audio_path), "rb") as wav:
        audio = np.frombuffer(
            wav.readframes(wav.getnframes()), dtype=np.int16,
        ).astype(np.float32) / 32768.0

    model, name = _whisper_model()
    language = _env("WHISPER_LANG")
    if not language:
        try:
            language, _prob, _all = model.detect_language(audio, vad_filter=True)
        except Exception:  # noqa: BLE001
            language = None

    prompt = "以下是普通话的句子，请用简体中文并加上标点。" if language == "zh" else None
    segments, info = model.transcribe(
        audio,
        language=language,
        initial_prompt=prompt,
        vad_filter=True,
        beam_size=_env_int("WHISPER_BEAM", 5),
        condition_on_previous_text=False,
    )
    rows = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        if language == "zh" and _zh_convert:
            text = _zh_convert(text, "zh-cn")
        rows.append({"start": segment.start, "end": segment.end, "text": text})
    if not rows:
        raise RuntimeError("没有识别到语音：视频可能是纯音乐或无人声")
    return rows, f"faster-whisper/{name}", info.language


# --------------------------------------------------------------------------
# 抽帧 / OCR 打分 / 选帧
# --------------------------------------------------------------------------

def _frame_interval(duration):
    if duration <= 600:
        return 8
    if duration <= 1800:
        return 12
    if duration <= 3600:
        return 20
    return 30


def _parse_showinfo(stderr):
    return [float(x) for x in re.findall(r"pts_time:([\d.]+)", stderr or "")]


def _frame_rows(frames_dir, timestamps, interval, kind):
    files = sorted(frames_dir.glob("*.jpg"))
    if len(timestamps) != len(files):
        if not interval:
            return []
        timestamps = [i * interval for i in range(len(files))]
    return [{"t": t, "path": p, "kind": kind} for t, p in zip(timestamps, files)]


def _extract_interval(video_path, duration, workdir):
    interval = _frame_interval(duration)
    frames_dir = workdir / "cand_interval"
    frames_dir.mkdir(parents=True, exist_ok=True)

    proc = _run([
        "ffmpeg", "-hide_banner", "-y", "-i", str(video_path),
        "-vf", f"fps=1/{interval},showinfo", "-q:v", "2",
        str(frames_dir / "%05d.jpg"),
    ], timeout=2400)
    return _frame_rows(frames_dir, _parse_showinfo(proc.stderr), interval, "interval")


def _extract_scene(video_path, workdir):
    threshold = _env("VIDEO_SCENE_THRESHOLD", "0.30")
    frames_dir = workdir / "cand_scene"
    frames_dir.mkdir(parents=True, exist_ok=True)

    try:
        proc = _run([
            "ffmpeg", "-hide_banner", "-y", "-i", str(video_path),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-fps_mode", "vfr", "-q:v", "2",
            str(frames_dir / "%05d.jpg"),
        ], timeout=2400)
    except Exception:  # noqa: BLE001
        return []

    rows = _frame_rows(frames_dir, _parse_showinfo(proc.stderr), 0, "scene")
    cap = _env_int("VIDEO_MAX_SCENE_FRAMES", 400)
    if len(rows) > cap:
        step = math.ceil(len(rows) / cap)
        rows = rows[::step]
    return rows


def _priority(row):
    if row.get("anchor") or row.get("kind") == "anchor":
        return 3
    return 2 if row.get("kind") == "scene" else 1


def merge_candidates(*groups):
    """按 2 秒桶合并候选帧：锚点 > 场景变化 > 固定间隔。"""
    buckets = {}
    for rows in groups:
        for row in rows:
            key = round(row["t"] / 2)
            if key not in buckets or _priority(row) > _priority(buckets[key]):
                buckets[key] = row

    rows = sorted(buckets.values(), key=lambda r: r["t"])
    cap = _env_int("VIDEO_MAX_CANDIDATES", 450)
    if len(rows) > cap:
        anchors = [r for r in rows if _priority(r) == 3]
        rest = [r for r in rows if _priority(r) < 3]
        anchors = anchors[:cap]
        remaining = max(1, cap - len(anchors))
        step = max(1, math.ceil(len(rest) / remaining))
        rows = sorted(anchors + rest[::step], key=lambda r: r["t"])[:cap]
    return rows


def add_anchor_frames(video_path, candidates, paragraphs, workdir):
    """在每段转写的起点补一帧，解决“讲了半天但画面没跟上”的问题。"""
    starts = [p["start"] for p in paragraphs if p["start"] > 0.5]
    if not starts:
        return candidates

    max_anchors = _env_int("VIDEO_MAX_ANCHORS", 30)
    if len(starts) > max_anchors:
        step = math.ceil(len(starts) / max_anchors)
        starts = starts[::step]

    frames_dir = workdir / "cand_anchor"
    frames_dir.mkdir(parents=True, exist_ok=True)
    extra = []

    for i, t in enumerate(starts):
        # 优先用锚点前 2.5 秒内的帧；锚点后最多 1.5 秒，避免把片尾卡当成结论页
        near = next((r for r in candidates if 0 <= t - r["t"] <= 2.5), None)
        if not near:
            near = next((r for r in candidates if 0 < r["t"] - t <= 1.5), None)
        if near:
            near["anchor"] = True
            if near.get("kind") == "interval":
                near["kind"] = "anchor"
            continue

        path = frames_dir / f"{i:05d}.jpg"
        try:
            _run([
                "ffmpeg", "-hide_banner", "-y",
                "-ss", f"{max(0.0, t):.3f}", "-i", str(video_path),
                "-frames:v", "1", "-q:v", "2", str(path),
            ], timeout=120)
        except Exception:  # noqa: BLE001
            continue
        if path.exists() and path.stat().st_size > 0:
            extra.append({"t": t, "path": path, "kind": "anchor", "anchor": True})

    return merge_candidates(candidates, extra)


def extract_candidates(video_path, duration, workdir):
    interval_rows = _extract_interval(video_path, duration, workdir)
    scene_rows = _extract_scene(video_path, workdir)
    return merge_candidates(interval_rows, scene_rows)


def _ocr_engine():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr


def _dhash(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]
    return sum(1 << i for i, bit in enumerate(diff.flatten()) if bit)


def _hamming(a, b):
    return (a ^ b).bit_count()


def score_frames(rows):
    engine = _ocr_engine()
    prev_small = None
    out = []

    for row in rows:
        img = cv2.imread(str(row["path"]))
        if img is None:
            continue
        h, w = img.shape[:2]

        chars, area, texts = 0, 0.0, []
        try:
            result, _ = engine(str(row["path"]))
        except Exception:  # noqa: BLE001
            result = None
        for box, text, conf in (result or []):
            if float(conf) < 0.5:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            # 去掉顶部台标、底部字幕条和太小的水印
            if y1 < h * 0.06 or y0 > h * 0.80:
                continue
            box_area = (x1 - x0) * (y1 - y0) / (w * h)
            if box_area < 0.0015:
                continue
            text = text.strip()
            if len(text) < 2:
                continue
            chars += len(text)
            area += box_area
            texts.append(text)

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (64, 36)).astype(np.float32)
        diff = 0.0 if prev_small is None else float(np.abs(small - prev_small).mean() / 255)
        prev_small = small

        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        text_score = min(1.0, chars / 50.0) * 0.65 + min(1.0, area / 0.08) * 0.35
        visual_score = min(1.0, diff / 0.30) * 0.55 + min(1.0, sharp / 600.0) * 0.20
        text_blob = " | ".join(texts)[:300]

        # 文字新颖度：和前面几帧文字越像，越不值得重复截
        novelty = 0.5
        if text_blob:
            recent = [r["text"] for r in out[-8:] if r.get("text")]
            if recent:
                novelty = 1.0 - max(_similar(text_blob, prev) for prev in recent)

        scene_boost = 0.25 if row.get("kind") == "scene" else 0.0
        anchor_boost = 0.30 if row.get("anchor") else 0.0
        score = (
            text_score * 1.25 + visual_score * 0.75
            + scene_boost + anchor_boost + novelty * 0.35
        )
        if chars == 0 and diff < 0.08:
            score *= 0.5

        row.update({
            "chars": chars,
            "area": round(area, 4),
            "text": text_blob,
            "diff": round(diff, 4),
            "sharp": round(sharp, 1),
            "novelty": round(novelty, 3),
            "hash": _dhash(img),
            "score": round(score, 4),
        })
        out.append(row)

    # 文字型视频里，纯水印/片尾卡不应该占据一个关键帧名额
    if out:
        median_chars = sorted(r["chars"] for r in out)[len(out) // 2]
        if median_chars >= 20:
            for row in out:
                if row["chars"] < 5:
                    row["score"] = round(row["score"] * 0.55, 4)
                elif row["chars"] < 15:
                    row["score"] = round(row["score"] * 0.75, 4)
                elif row["chars"] < 30:
                    row["score"] = round(row["score"] * 0.85, 4)
    return out


def _similar(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def select_frames(rows, duration):
    if duration <= 300:
        max_frames = 8
    elif duration <= 1200:
        max_frames = 16
    elif duration <= 3600:
        max_frames = 22
    else:
        max_frames = 28
    override = _env_int("VIDEO_MAX_FRAMES", 0)
    if override > 0:
        max_frames = override
    min_gap = 15 if duration <= 1200 else 30

    picked = []

    def acceptable(row):
        if any(abs(row["t"] - p["t"]) < min_gap for p in picked):
            return False
        if any(_hamming(row["hash"], p["hash"]) <= 5 for p in picked):
            return False
        # 文字像、画面也像，才算重复；文字像但画面结构变了要保留
        if row["text"] and any(
            p["text"]
            and _similar(row["text"], p["text"]) > 0.92
            and _hamming(row["hash"], p["hash"]) <= 12
            for p in picked
        ):
            return False
        return True

    # 先保证每个时间段各有一张：避免 16 秒连拍、中间却空 72 秒
    size = max(1.0, duration / max_frames)
    buckets = [[] for _ in range(max_frames)]
    for row in rows:
        idx = min(max_frames - 1, max(0, int(row["t"] / size)))
        buckets[idx].append(row)

    scores = sorted(r["score"] for r in rows)
    median_score = scores[len(scores) // 2] if scores else 0.0
    min_coverage = max(0.25, median_score * 0.55)

    for bucket in buckets:
        best = max(bucket, key=lambda r: r["score"]) if bucket else None
        if best and best["score"] < min_coverage and len(picked) >= max_frames * 0.5:
            continue
        for row in sorted(bucket, key=lambda r: r["score"], reverse=True):
            if acceptable(row):
                row["reason"] = "coverage"
                picked.append(row)
                break

    # 还有名额，再按全局分数补
    for row in sorted(rows, key=lambda r: r["score"], reverse=True):
        if len(picked) >= max_frames:
            break
        if any(row is p for p in picked):
            continue
        if acceptable(row):
            row["reason"] = "score"
            picked.append(row)

    return sorted(picked, key=lambda r: r["t"])


# --------------------------------------------------------------------------
# 合成
# --------------------------------------------------------------------------

def merge_paragraphs(segments):
    paragraphs = []
    current = None
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        if current is None:
            current = {"start": seg["start"], "end": seg["end"], "text": text}
            continue
        gap = seg["start"] - current["end"]
        too_long = len(current["text"]) + len(text) > 160
        too_wide = seg["end"] - current["start"] > 30
        if gap <= 2.0 and not too_long and not too_wide:
            current["text"] = _join(current["text"], text)
            current["end"] = seg["end"]
        else:
            paragraphs.append(current)
            current = {"start": seg["start"], "end": seg["end"], "text": text}
    if current:
        paragraphs.append(current)
    return paragraphs


def _frame_name(t):
    return f"{_fmt_ts(t, dash=True)}.jpg"


def _video_link(url, t):
    if "bilibili.com" in url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}t={int(t)}"
    if "youtube.com" in url or "youtu.be" in url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}t={int(t)}s"
    return url


def build_markdown(meta, paragraphs, frames):
    by_para = {i: [] for i in range(len(paragraphs))}
    for frame in frames:
        idx = 0
        for i, p in enumerate(paragraphs):
            if p["start"] <= frame["t"]:
                idx = i
            else:
                break
        if paragraphs:
            by_para[idx].append(frame)

    lines = []
    for i, p in enumerate(paragraphs):
        lines.append(f"**[{_fmt_ts(p['start'])}]** {p['text']}")
        for frame in by_para.get(i, []):
            lines.append("")
            lines.append(f"![{_fmt_ts(frame['t'])}](frames/{frame['file']})")
        lines.append("")

    front_meta = {k: v for k, v in meta.items() if not k.startswith("_")}
    front = yaml.safe_dump(front_meta, allow_unicode=True, sort_keys=False, width=1000).strip()
    return (
        f"---\n{front}\n---\n\n"
        f"# {meta['title']}\n\n"
        f"> 原视频：[{meta['url']}]({meta['url']})  \n"
        f"> 时长：{meta['duration_hms']}　上传者：{meta.get('author') or '未知'}　"
        f"转录：{meta['transcript_source']}　截图：{meta['frames']} 张\n\n"
        f"## 转录\n\n" + "\n".join(lines).rstrip() + "\n"
    )


def build_html(meta, paragraphs, frames):
    by_para = {i: [] for i in range(len(paragraphs))}
    for frame in frames:
        idx = 0
        for i, p in enumerate(paragraphs):
            if p["start"] <= frame["t"]:
                idx = i
            else:
                break
        if paragraphs:
            by_para[idx].append(frame)

    def esc(value):
        return html.escape(str(value or ""), quote=True)

    blocks = []
    for i, p in enumerate(paragraphs):
        imgs = []
        for frame in by_para.get(i, []):
            path = meta["_folder"] / "frames" / frame["file"]
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            imgs.append(
                f'<figure><img loading="lazy" src="data:image/jpeg;base64,{b64}" '
                f'alt="{esc(_fmt_ts(frame["t"]))}">'
                f'<figcaption>{esc(_fmt_ts(frame["t"]))}</figcaption></figure>'
            )
        blocks.append(
            f'<section class="seg" id="t{int(p["start"])}">'
            f'<a class="ts" href="{esc(_video_link(meta["url"], p["start"]))}" '
            f'target="_blank" rel="noreferrer">{esc(_fmt_ts(p["start"]))}</a>'
            f'<div class="text">{esc(p["text"])}</div>'
            + "".join(imgs) +
            '</section>'
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(meta["title"])}</title>
<style>
  :root {{ color-scheme: light dark; --fg:#1f2328; --muted:#6b7280; --line:#e5e7eb; --bg:#fff; --accent:#2563eb; --card:#f8fafc; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --fg:#e6edf3; --muted:#9ca3af; --line:#30363d; --bg:#0d1117; --accent:#58a6ff; --card:#161b22; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.85 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  main {{ max-width: 820px; margin: 0 auto; padding: 0 18px 64px; }}
  header {{ max-width: 820px; margin: 0 auto; padding: 28px 18px 10px; }}
  h1 {{ font-size: 1.55rem; line-height: 1.35; margin: 0 0 10px; }}
  .meta {{ color: var(--muted); font-size: .9rem; }}
  .bar {{ position: sticky; top: 0; z-index: 2; background: color-mix(in srgb, var(--bg) 88%, transparent); backdrop-filter: blur(8px); border-bottom: 1px solid var(--line); padding: 10px 18px; display:flex; gap:10px; align-items:center; }}
  .bar a, .bar button {{ font: inherit; font-size: .9rem; color: var(--accent); background: none; border: 1px solid var(--line); border-radius: 999px; padding: 5px 12px; cursor: pointer; text-decoration: none; }}
  .seg {{ position: relative; padding: 6px 0 12px 62px; border-bottom: 1px solid var(--line); }}
  .ts {{ position:absolute; left:0; top:8px; font-variant-numeric: tabular-nums; font-size:.82rem; color:var(--accent); text-decoration:none; }}
  .text {{ white-space: pre-wrap; }}
  figure {{ margin: 14px 0 4px; }}
  img {{ max-width:100%; height:auto; border-radius:10px; border:1px solid var(--line); display:block; }}
  figcaption {{ color:var(--muted); font-size:.8rem; margin-top:5px; }}
  .tip {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; color:var(--muted); font-size:.88rem; margin:18px 0; }}
  @media (max-width: 560px) {{ .seg {{ padding-left: 52px; }} h1 {{ font-size:1.3rem; }} }}
</style>
</head>
<body>
<header>
  <h1>{esc(meta["title"])}</h1>
  <div class="meta">{esc(meta.get("author") or "未知")} · {esc(meta["duration_hms"])} · 转录 {meta["chars"]:,} 字 · 关键画面 {meta["frames"]} 张</div>
</header>
<div class="bar"><button onclick="copyAll()">复制全文</button><a href="{esc(meta["url"])}" target="_blank" rel="noreferrer">打开原视频</a><span id="toast" class="meta"></span></div>
<main>
<div class="tip">下面是完整转录，未做摘要。时间戳可跳回原片；截图是按画面文字密度和变化自动选的。</div>
{"".join(blocks)}
</main>
<script>
async function copyAll() {{
  const text = [...document.querySelectorAll('.seg .text')].map(n => n.innerText).join('\\n\\n');
  try {{ await navigator.clipboard.writeText(text); document.getElementById('toast').textContent = '已复制'; }}
  catch (e) {{ document.getElementById('toast').textContent = '复制失败，请手动选择'; }}
  setTimeout(() => document.getElementById('toast').textContent = '', 1800);
}}
</script>
</body>
</html>
"""


def _write_outputs(kb, final, meta, paragraphs, frames):
    frames_dir = final / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for frame in frames:
        img = cv2.imread(str(frame["path"]))
        if img is None:
            continue
        if img.shape[1] > 1280:
            scale = 1280 / img.shape[1]
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        name = _frame_name(frame["t"])
        # Windows 下 cv2.imwrite 遇到中文路径会静默失败，改成 encode + write_bytes
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 86])
        if not ok:
            continue
        (frames_dir / name).write_bytes(buf.tobytes())
        saved.append({
            "t": round(frame["t"], 1),
            "file": name,
            "chars": frame["chars"],
            "area": frame["area"],
            "score": frame["score"],
            "novelty": frame.get("novelty", 0),
            "kind": frame.get("kind", ""),
            "anchor": bool(frame.get("anchor")),
            "reason": frame.get("reason", ""),
            "text": frame["text"][:200],
        })

    meta["frames"] = len(saved)
    meta["_folder"] = final
    (final / "transcript.md").write_text(
        build_markdown(meta, paragraphs, saved), encoding="utf-8",
    )
    (final / "transcript.txt").write_text(
        "\n\n".join(f"[{_fmt_ts(p['start'])}] {p['text']}" for p in paragraphs) + "\n",
        encoding="utf-8",
    )
    (final / "note.html").write_text(
        build_html(meta, paragraphs, saved), encoding="utf-8",
    )

    meta_out = {k: v for k, v in meta.items() if not k.startswith("_")}
    meta_out["frame_list"] = saved
    (final / "meta.json").write_text(
        json.dumps(meta_out, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return meta_out


# --------------------------------------------------------------------------
# 从已入库视频建学习笔记
# --------------------------------------------------------------------------

NOTE_TEMPLATE = """# {title} · 学习笔记

> 原文：[transcript.md]({transcript})  
> 关键画面：[frames/]({frames})  
> 视频：[{url}]({url})  
> 视频编号：{video_id}　时长：{duration}　上传者：{author}

## 一句话总结

## 我学到了什么

## 关键概念 / 步骤

## 关键画面

{frame_block}

## 还没弄懂 / 待验证
"""


def find_video(kb, ref):
    """按 BV号 / YouTube ID / URL / 文件夹指纹 / 标题片段找已入库视频。"""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("缺少视频编号")

    low = ref.lower()
    matches = []
    for meta_path in (kb / "002-references" / "raw" / "video").glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        folder = meta_path.parent
        hay = "\n".join([
            str(meta.get("video_id") or ""),
            str(meta.get("url") or ""),
            folder.name,
            str(meta.get("title") or ""),
        ]).lower()
        exact = (
            str(meta.get("video_id") or "").lower() == low
            or str(meta.get("url") or "").lower() == low
            or folder.name.lower().endswith(low)
        )
        if exact or low in hay:
            matches.append((meta, folder))

    if not matches:
        raise LookupError(f"没找到视频：{ref}")
    if len(matches) > 1:
        exact = [
            (m, f) for m, f in matches
            if str(m.get("video_id") or "").lower() == low
            or str(m.get("url") or "").lower() == low
            or f.name.lower().endswith(low)
        ]
        if len(exact) == 1:
            return exact[0]
        names = "\n".join(
            f"- {m.get('title')}（{m.get('video_id') or f.name[-8:]}）"
            for m, f in matches[:8]
        )
        raise LookupError(f"匹配到多个视频，请用更具体的编号：\n{names}")
    return matches[0]


def note_draft(kb, ref, extra=""):
    """为某个已入库视频建一篇 003-notes 草稿。不覆盖已有笔记。"""
    meta, folder = find_video(kb, ref)
    note_dir = kb / "003-notes"
    note_dir.mkdir(parents=True, exist_ok=True)

    video_hash = store.url_hash(meta.get("url") or folder.name)
    date = datetime.now(store.CST).strftime("%Y%m%d")
    title = meta.get("title") or folder.name

    # 同一视频的笔记可能在不同日期建过，按指纹找，避免重复
    existing = next(note_dir.glob(f"*-{video_hash}-学习笔记.md"), None)
    if existing:
        return "exists", existing, meta

    path = note_dir / f"{date}-{store.slugify(title, 40)}-{video_hash}-学习笔记.md"

    rel = f"../002-references/raw/video/{folder.name}"
    frame_lines = [
        f"- [{_fmt_ts(f.get('t', 0))}]({rel}/frames/{f['file']})"
        for f in (meta.get("frame_list") or []) if f.get("file")
    ]
    frame_block = "\n".join(frame_lines) or f"- [frames/]({rel}/frames/)"

    body = NOTE_TEMPLATE.format(
        title=title,
        transcript=f"{rel}/transcript.md",
        frames=f"{rel}/frames/",
        url=meta.get("url") or "",
        video_id=meta.get("video_id") or video_hash,
        duration=meta.get("duration_hms") or "",
        author=meta.get("author") or "未知",
        frame_block=frame_block,
    )
    if extra.strip():
        body += f"\n## 初始笔记\n\n{extra.strip()}\n"

    note_meta = {
        "created": store.now_iso(),
        "draft": True,
        "source_video": f"002-references/raw/video/{folder.name}",
        "source_url": meta.get("url") or "",
        "video_id": meta.get("video_id") or "",
        "video_title": title,
    }
    front = yaml.safe_dump(note_meta, allow_unicode=True, sort_keys=False, width=1000).strip()
    content = f"---\n{front}\n---\n\n{body.strip()}\n"

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    store.git_commit(kb, path, f"学习笔记：{title}")
    return "ok", path, meta


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def process(url, kb, progress=None):
    def note(text):
        if progress:
            try:
                progress(text)
            except Exception:  # noqa: BLE001
                pass

    info = metadata(url)
    canonical = info.get("webpage_url") or resolve_url(url)
    video_hash = store.url_hash(canonical)
    video_root = kb / "002-references" / "raw" / "video"
    existing = next(
        (p for p in video_root.glob(f"*-{video_hash}") if p.is_dir()), None,
    )
    if existing:
        meta_file = existing / "meta.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            return "skip", meta, existing
        raise RuntimeError(f"上次处理留下了不完整的目录，请先删除：{existing}")

    duration = float(info.get("duration") or 0)
    max_duration = _env_int("VIDEO_MAX_DURATION", 7200)
    if duration > max_duration:
        raise RuntimeError(
            f"视频 {_fmt_duration(duration)} 超过当前上限 {_fmt_duration(max_duration)}；"
            "可以调大 VIDEO_MAX_DURATION"
        )

    title = (info.get("title") or url).strip()
    note(f"已识别：{title}（{_fmt_duration(duration)}），开始下载视频…")
    work = Path(tempfile.mkdtemp(prefix="kb-video-"))
    try:
        subs = download_subtitles(canonical, work)
        video_path = download_video(canonical, duration, work)
        if not duration:
            duration = _probe_duration(video_path)
        if duration > max_duration:
            raise RuntimeError(
                f"视频 {_fmt_duration(duration)} 超过当前上限 {_fmt_duration(max_duration)}；"
                "可以调大 VIDEO_MAX_DURATION"
            )

        if subs:
            segments, transcript_source = subs
            transcript_lang = ""
            note("拿到平台字幕，开始选关键画面…")
        else:
            minutes = max(1, round(duration / 60 * 0.6))
            note(f"视频已下载，开始本地转写…（CPU 预计约 {minutes} 分钟）")
            segments, transcript_source, transcript_lang = transcribe(video_path, work)

        paragraphs = merge_paragraphs(segments)
        if not paragraphs:
            raise RuntimeError("转写结果为空")
        note(f"转写完成：{len(paragraphs)} 段 / {sum(len(p['text']) for p in paragraphs):,} 字，开始选关键画面…")

        candidates = extract_candidates(video_path, duration, work)
        candidates = add_anchor_frames(video_path, candidates, paragraphs, work)
        note(f"候选帧 {len(candidates)} 张（场景/间隔/段落锚点），开始 OCR 打分…")
        scored = score_frames(candidates)
        selected = select_frames(scored, duration)
        if not selected:
            raise RuntimeError("没有选出关键画面")
        note(f"已选 {len(selected)} 张关键画面，正在生成笔记…")

        date = datetime.now(store.CST).strftime("%Y%m%d")
        folder_name = f"{date}-{store.slugify(title, 40)}-{video_hash}"
        final = video_root / folder_name
        meta = {
            "url": store.canonical(canonical),
            "title": title,
            "source": "bilibili" if "bilibili" in (info.get("extractor_key") or "").lower()
                      else "youtube" if "youtube" in (info.get("extractor_key") or "").lower()
                      else "video",
            "video_id": info.get("id") or "",
            "author": (info.get("uploader") or "")[:80],
            "duration": int(duration),
            "duration_hms": _fmt_duration(duration),
            "fetched_at": store.now_iso(),
            "transcript_source": transcript_source,
            "transcript_lang": transcript_lang,
            "chars": sum(len(p["text"]) for p in paragraphs),
            "candidates": len(scored),
            "frames": len(selected),
        }
        meta = _write_outputs(kb, final, meta, paragraphs, selected)
        store.git_commit(kb, final, title)
        return "ok", meta, final

    finally:
        shutil.rmtree(work, ignore_errors=True)
