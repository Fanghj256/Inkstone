"""通道无关的消息处理。

Telegram 和飞书只负责“怎么收、怎么发”；意图解析、抓取、视频排队、落盘都在这里。
新增一个通道 = 实现 `send_text` / `send_file`，然后调用 `handle_text`。
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import extract
import store
import tg
import video

# Telegram 单条消息上限 4096，留一点余量；飞书适配器会用自己的上限再截一次
MSG_LIMIT = 4000

URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]，。！？；：、》]+")

# 消息前缀决定落到哪一层。不写前缀就用默认规则（文本→inbox，文件→files）
ROUTES = {
    "/note": "003-notes",
    "/video": "003-notes/video trans notes",
    "/idea": "001-inbox",
}

# 「这几个文件放到视频转写稿」这类指令的目标目录
ROUTE_TARGETS = (
    ("003-notes/video trans notes", ("视频转写", "转写稿", "视频稿", "视频文字稿")),
    ("003-notes", ("笔记",)),
    ("001-inbox", ("随手记", "收件箱")),
)

RECENT_TTL = 30 * 60

_pending = {}          # (channel, chat_id) -> (folder, expires_at)
_pending_lock = threading.Lock()

_video_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video")


class TelegramChannel:
    name = "telegram"
    limit = MSG_LIMIT

    def send_text(self, chat_id, text):
        tg.api("sendMessage", chat_id=chat_id, text=text[:self.limit])

    def send_file(self, chat_id, path, caption=""):
        tg.send_document(path, chat_id=chat_id, caption=caption)


def _key(channel, chat_id):
    return (getattr(channel, "name", str(channel)), str(chat_id))


def set_pending(channel, chat_id, folder):
    with _pending_lock:
        _pending[_key(channel, chat_id)] = (folder, time.time() + RECENT_TTL)


def take_pending(channel, chat_id):
    with _pending_lock:
        item = _pending.pop(_key(channel, chat_id), None)
    if not item:
        return None
    folder, expires = item
    return folder if time.time() <= expires else None


def clear_pending(channel, chat_id):
    with _pending_lock:
        _pending.pop(_key(channel, chat_id), None)


def split_prefix(text):
    """把 `/note xxx` 拆成 (目标目录, 正文)。没有前缀就返回 (None, 原文)。"""
    t = text.lstrip()
    for key, folder in ROUTES.items():
        if t.lower().startswith(key):
            return folder, t[len(key):].strip()
    return None, text


def save_text(kb, text, rel_dir="001-inbox", kind="idea", channel="telegram"):
    """没有链接的文字：随手记 / 笔记 / 转写稿。"""
    body = text.strip()
    hint = body.splitlines()[0][:24] if body else "note"
    meta = {
        "created": store.now_iso(),
        "source": channel,
        "kind": kind,
        "chars": len(body),
    }
    path = store.write_simple(kb, rel_dir, hint, meta, body)
    store.git_commit(kb, path, f"{kind}: {hint}")
    return path


def process(url, kb):
    """抓一篇网页。返回 (状态, meta, 路径)。"""
    existing = store.find_existing(kb, url)
    if existing:
        return "skip", {"title": existing.stem}, existing

    data = extract.fetch(url)
    text = data["text"].strip()
    if len(text) < 50:
        raise RuntimeError(f"正文只有 {len(text)} 字，判定为抓取失败")

    meta = {
        "url": store.canonical(url),
        "title": (data["title"] or url)[:200],
        "source": data["source"],
        "author": (data["author"] or "")[:80],
        "fetched_at": store.now_iso(),
        "fetched_via": data["extra"].get("extractor", ""),
        "chars": len(text),
    }
    if len(text) < (80 if data["source"] == "weibo" else 300):
        meta["incomplete"] = True          # 大概率被反爬截断了，回头需要人工补

    for key, value in data["extra"].items():
        if key not in meta and key != "extractor" and value not in (None, ""):
            meta[key] = value

    path = store.write_note(kb, meta, text)
    store.git_commit(kb, path, meta["title"])
    return "ok", meta, path


def process_one(url, kb, progress=None):
    """按链接类型选视频或网页管线。视频会阻塞，调用方自己决定放不放进队列。"""
    if video.is_video_url(url):
        return video.process(url, kb, progress=progress)
    return process(url, kb)


def video_receipt(meta, path):
    vid = meta.get("video_id") or path.name[-8:]
    return (
        f"✅ 视频已入库\n{meta['title']}\n"
        f"{meta['source']} · {meta['duration_hms']} · "
        f"转录 {meta['chars']:,} 字 · 截图 {meta['frames']} 张\n"
        f"{path.parent.name}/{path.name}\n\n"
        f"整理成学习笔记：/learn {vid}"
    )


def note_receipt(meta, path):
    note = f"\n⚠️ 正文只有 {meta['chars']} 字，可能没抓全" if meta.get("incomplete") else ""
    return (
        f"✅ 已入库\n{meta['title']}\n"
        f"{meta['source']} · {meta['chars']:,} 字\n"
        f"{path.parent.name}/{path.name}{note}"
    )


def skip_receipt(path):
    return f"⏭ 已经存过了\n{path.name}"


def fetch_sync(url, kb):
    """同步抓一条，返回给 CLI / 回执用的文字。"""
    try:
        status, meta, path = process_one(url, kb)
        return skip_receipt(path) if status == "skip" else (
            video_receipt(meta, path) if video.is_video_url(url) else note_receipt(meta, path)
        )
    except Exception as exc:  # noqa: BLE001
        store.save_failure(kb, url, exc)
        return f"⚠️ 抓取失败\n{url}\n{exc}"


def _video_job(channel, chat_id, url, kb):
    def progress(text):
        try:
            channel.send_text(chat_id, text)
        except Exception:  # noqa: BLE001
            pass

    try:
        status, meta, path = video.process(url, kb, progress=progress)
    except Exception as exc:  # noqa: BLE001
        # 只有处理失败才留失败记录；发送失败不算法失败
        store.save_failure(kb, url, exc)
        try:
            channel.send_text(chat_id, f"⚠️ 视频处理失败\n{url}\n{exc}")
        except Exception:  # noqa: BLE001
            pass
        return

    if status == "skip":
        try:
            channel.send_text(chat_id, skip_receipt(path))
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        channel.send_text(chat_id, video_receipt(meta, path))
        doc = path / "note.html"
        if not doc.exists() or doc.stat().st_size > 45 * 1024 * 1024:
            doc = path / "transcript.md"
        channel.send_file(chat_id, doc, caption=f"{meta['title']} · 完整转录 + 关键画面")
        txt = path / "transcript.txt"
        if txt.exists():
            channel.send_file(chat_id, txt, caption="纯文本转录，可直接发给 AI")
    except Exception as exc:  # noqa: BLE001
        try:
            channel.send_text(chat_id, f"笔记已保存，但消息/文件发送失败：{exc}")
        except Exception:  # noqa: BLE001
            pass


def enqueue_videos(channel, chat_id, urls, kb):
    for url in urls:
        _video_pool.submit(_video_job, channel, chat_id, url, kb)


def handle_urls(kb, channel, chat_id, urls, force_video=False):
    """抓一批链接，回一条汇总。视频进单线程队列，网页同步抓。"""
    if force_video:
        video_urls, other_urls = list(urls), []
    else:
        video_urls = [u for u in urls if video.is_video_url(u)]
        other_urls = [u for u in urls if u not in video_urls]

    parts = []
    if video_urls:
        enqueue_videos(channel, chat_id, video_urls, kb)
        parts.append(
            f"收到 {len(video_urls)} 个视频，开始转写…\n"
            "完成后把转录和关键截图发你。"
        )

    if other_urls:
        lines = []
        for url in other_urls:
            try:
                status, meta, path = process(url, kb)
                lines.append(skip_receipt(path) if status == "skip" else note_receipt(meta, path))
            except Exception as exc:  # noqa: BLE001
                store.save_failure(kb, url, exc)
                lines.append(f"⚠️ 抓取失败\n{url}\n{exc}")
        if len(other_urls) > 1:
            lines.insert(0, f"收到 {len(other_urls)} 个链接，开始抓取…")
        parts.append("\n\n".join(lines))

    if parts:
        channel.send_text(chat_id, "\n\n".join(parts))


def learn(kb, channel, chat_id, text):
    """/learn <视频编号>：建一篇关联 raw 视频的学习笔记草稿。返回是否已处理。"""
    m = re.match(r"^/learn\b\s*(.*)$", text or "", re.I | re.S)
    if not m:
        return False

    rest = m.group(1).strip()
    if not rest:
        channel.send_text(
            chat_id,
            "用法：/learn <视频编号、BV号、链接或文件夹指纹>\n"
            "先发视频链接入库，再用回执里的编号建笔记。",
        )
        return True

    ref, _, extra = rest.partition(" ")
    try:
        status, path, meta = video.note_draft(kb, ref, extra)
    except LookupError as exc:
        channel.send_text(chat_id, f"⚠️ {exc}")
        return True
    except Exception as exc:  # noqa: BLE001
        channel.send_text(chat_id, f"⚠️ 建笔记失败：{exc}")
        return True

    if status == "exists":
        channel.send_text(
            chat_id,
            f"这篇视频已经有笔记了，没有覆盖：\n003-notes/{path.name}",
        )
        return True

    channel.send_text(
        chat_id,
        f"✅ 已建学习笔记草稿\n{meta.get('title')}\n"
        f"003-notes/{path.name}\n\n"
        "打开它，和 AI 一起读原文、填内容；定稿后把 frontmatter 里的 "
        "draft: true 去掉。",
    )
    return True


def handle_text(kb, channel, chat_id, text):
    """通道收到的纯文字：/learn、链接、前缀、随手记都走这里。"""
    if learn(kb, channel, chat_id, text):
        return

    rel_dir, body = split_prefix(text)
    urls = URL_RE.findall(body)

    if urls:
        handle_urls(
            kb, channel, chat_id, urls,
            force_video=(rel_dir == ROUTES["/video"]),
        )
        return

    # 只发了一个前缀：记住目标目录，等下一条文件或文字
    if rel_dir and not body:
        set_pending(channel, chat_id, rel_dir)
        channel.send_text(
            chat_id,
            f"收到，接下来 {RECENT_TTL // 60} 分钟内发的文件或文字会存到 {rel_dir}。",
        )
        return

    if body:
        folder = rel_dir or take_pending(channel, chat_id) or "001-inbox"
        path = save_text(
            kb, body, folder,
            kind=folder.split("/")[-1],
            channel=getattr(channel, "name", "telegram"),
        )
        channel.send_text(chat_id, f"已存入 {folder}\n{path.name}")
