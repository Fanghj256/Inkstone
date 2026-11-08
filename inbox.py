"""收件箱主程序。

两种用法：

  python inbox.py                        # 常驻，收 Telegram 消息
  python inbox.py --url https://...      # 手动测一个链接（不经过 Telegram）

流程：取消息 → 拆链接 → 按域名分派提取器 → 写 YAML + 原子落盘 → git commit → 回执
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import extract
import store
import tg
import video

# Windows 控制台多为 GBK，emoji 会直接抛 UnicodeEncodeError。
# 只把不可编码字符替换掉，不改编码，避免中文乱码。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]，。！？；：、》]+")

# 消息前缀决定落到哪一层。不写前缀就用默认规则（文本→inbox，文件→files）
ROUTES = {
    "/note": "003-notes",
    "/video": "003-notes/video trans notes",
    "/idea": "001-inbox",
}

# 刚收到的文件记在这里，方便下一条消息说「这两个存成视频转写稿」。
# 只放内存：bot 重启后失效，30 分钟也够用了。
RECENT_TTL = 30 * 60
RECENT_LIMIT = 10
_pending_route = {}   # chat_id -> (目录, 过期时间)
_recent = {}          # chat_id -> [{"path", "name", "unique", "at"}]

# 「这几个文件放到视频转写稿」这类指令的目标目录
ROUTE_TARGETS = (
    ("003-notes/video trans notes", ("视频转写", "转写稿", "视频稿", "视频文字稿")),
    ("003-notes", ("笔记",)),
    ("001-inbox", ("随手记", "收件箱")),
)
_DIRECTIVE_RE = re.compile(
    r"存(?:入|到|成|为|进)|放(?:到|入|进)|移(?:到|入)|归(?:到|入)|"
    r"改(?:成|为|到)|转(?:成|到|为)|作为|当做"
)
_REFER_RE = re.compile(r"这|那|刚才|刚刚|上面|前面|刚发|文件|\.md", re.I)
_QUESTION_RE = re.compile(r"[？?]|哪|吗|怎么|如何")
_COUNT_RE = re.compile(r"([一二两三四五六几])\s*(?:个|篇|份)")
_COUNT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "几": 3}


def kb_root():
    raw = os.environ.get("KB_ROOT", "").strip()
    if not raw:
        sys.exit("`.env` 里没有 KB_ROOT。")
    return Path(raw)


def process(url, kb):
    """抓一篇。返回 (状态, meta, 路径)。"""
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


def split_prefix(text):
    """把 `/note xxx` 拆成 (目标目录, 正文)。没有前缀就返回 (None, 原文)。"""
    t = text.lstrip()
    for key, folder in ROUTES.items():
        if t.lower().startswith(key):
            return folder, t[len(key):].strip()
    return None, text


def save_text(kb, text, rel_dir="001-inbox", kind="idea"):
    """没有链接的文字：随手记 / 笔记 / 转写稿。"""
    body = text.strip()
    hint = body.splitlines()[0][:24] if body else "note"
    meta = {
        "created": store.now_iso(),
        "source": "telegram",
        "kind": kind,
        "chars": len(body),
    }
    path = store.write_simple(kb, rel_dir, hint, meta, body)
    store.git_commit(kb, path, f"{kind}: {hint}")
    return path


def set_pending(chat_id, folder):
    """单独发 /video 时先把目标目录记下来，留给下一条文件或文字用。"""
    _pending_route[chat_id] = (folder, time.time() + RECENT_TTL)


def take_pending(chat_id):
    item = _pending_route.pop(chat_id, None)
    if not item:
        return None
    folder, expires = item
    return folder if time.time() <= expires else None


def remember_file(chat_id, doc, path):
    items = _recent.setdefault(chat_id, [])
    items.append({
        "path": path,
        "name": path.name,
        "unique": doc.get("file_unique_id", ""),
        "at": time.time(),
    })
    del items[:-RECENT_LIMIT]


def recent_files(chat_id):
    now = time.time()
    items = [
        i for i in _recent.get(chat_id, [])
        if now - i["at"] <= RECENT_TTL and i["path"].exists()
    ]
    _recent[chat_id] = items
    return items


def detect_file_route(text):
    """识别「这两个存入视频转写稿」这类指令，返回 (目标目录, 取最近几个)。"""
    t = text.strip()
    if not t or len(t) > 40 or _QUESTION_RE.search(t):
        return None
    if not (_DIRECTIVE_RE.search(t) and _REFER_RE.search(t)):
        return None
    folder = next((f for f, words in ROUTE_TARGETS if any(w in t for w in words)), None)
    if not folder:
        return None
    m = _COUNT_RE.search(t)
    return folder, (_COUNT.get(m.group(1)) if m else None)


def move_files(kb, items, folder):
    """把最近收到的文件挪到目标目录，返回新路径。"""
    moved = []
    for item in items:
        src = item["path"]
        dst = kb / folder / src.name
        if src.parent == dst.parent:
            continue
        if dst.exists():
            dst = dst.with_name(f"{dst.stem}-{int(time.time())}{dst.suffix}")
        store.git_move(kb, src, dst, f"move: {src.name} -> {folder}")
        moved.append(dst)
    return moved


def handle_urls(urls, kb):
    lines = []
    for url in urls:
        try:
            if video.is_video_url(url):
                status, meta, path = video.process(url, kb)
                if status == "skip":
                    lines.append(f"⏭ 已经存过了\n{path.name}")
                else:
                    lines.append(
                        f"✅ 视频已入库\n{meta['title']}\n"
                        f"{meta['source']} · {meta['duration_hms']} · "
                        f"转录 {meta['chars']:,} 字 · 截图 {meta['frames']} 张\n"
                        f"{path.parent.name}/{path.name}\n"
                        f"整理成学习笔记：/learn {meta.get('video_id') or path.name[-8:]}"
                    )
                continue

            status, meta, path = process(url, kb)
            if status == "skip":
                lines.append(f"⏭ 已经存过了\n{path.name}")
            else:
                note = "\n⚠️ 正文只有 %d 字，可能没抓全" % meta["chars"] if meta.get("incomplete") else ""
                lines.append(
                    f"✅ 已入库\n{meta['title']}\n"
                    f"{meta['source']} · {meta['chars']:,} 字\n"
                    f"{path.parent.name}/{path.name}{note}"
                )
        except Exception as exc:  # noqa: BLE001
            store.save_failure(kb, url, exc)
            lines.append(f"⚠️ 抓取失败\n{url}\n{exc}")
    return "\n\n".join(lines)


def handle_document(msg, kb, rel_dir=None):
    doc = msg["document"]
    size = doc.get("file_size", 0)
    if size > 19 * 1024 * 1024:
        return f"⚠️ 文件 {size / 1048576:.1f} MB，超过 bot 的 20MB 下载上限。改走本地目录或网盘。"

    info = tg.api("getFile", file_id=doc["file_id"])
    token = os.environ["TOKEN"]
    url = f"https://api.telegram.org/file/bot{token}/{info['file_path']}"
    resp = tg.requests.get(url, proxies=tg.proxies(), timeout=120)
    resp.raise_for_status()

    folder = rel_dir or "002-references/files"
    path = store.save_file(kb, doc.get("file_name", "unnamed"), resp.content, folder)
    store.git_commit(kb, path, f"file: {path.name}")
    remember_file(msg["chat"]["id"], doc, path)
    return f"✅ 文件已存入 {folder}\n{path.name}\n{len(resp.content)} 字节"


_video_pool = ThreadPoolExecutor(max_workers=1)


def enqueue_videos(urls, chat_id, kb):
    """视频转写很慢，放到单线程队列里跑，不要让 Telegram 轮询卡住。"""
    for url in urls:
        _video_pool.submit(process_video_job, chat_id, url, kb)


def process_video_job(chat_id, url, kb):
    def notify(text):
        try:
            tg.api("sendMessage", chat_id=chat_id, text=text[:4000])
        except Exception:  # noqa: BLE001
            pass

    try:
        status, meta, path = video.process(url, kb, progress=notify)
    except Exception as exc:  # noqa: BLE001
        # 只有处理失败才留失败记录；发送失败不算法失败
        store.save_failure(kb, url, exc)
        try:
            tg.api(
                "sendMessage", chat_id=chat_id,
                text=f"⚠️ 视频处理失败\n{url}\n{exc}"[:4000],
            )
        except Exception:  # noqa: BLE001
            pass
        return

    if status == "skip":
        try:
            tg.api(
                "sendMessage", chat_id=chat_id,
                text=f"⏭ 已经存过了\n{path.name}"[:4000],
            )
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        reply = (
            f"✅ 视频已入库\n{meta['title']}\n"
            f"{meta['source']} · {meta['duration_hms']} · "
            f"转录 {meta['chars']:,} 字 · 截图 {meta['frames']} 张\n"
            f"{path.parent.name}/{path.name}\n\n"
            f"整理成学习笔记：/learn {meta.get('video_id') or path.name[-8:]}"
        )
        tg.api("sendMessage", chat_id=chat_id, text=reply[:4000])

        doc = path / "note.html"
        if not doc.exists() or doc.stat().st_size > 45 * 1024 * 1024:
            doc = path / "transcript.md"
        tg.send_document(
            doc, chat_id=chat_id,
            caption=f"{meta['title']} · 完整转录 + 关键画面",
        )
        txt = path / "transcript.txt"
        if txt.exists() and txt != doc:
            tg.send_document(
                txt, chat_id=chat_id,
                caption="纯文本转录，可直接发给 AI",
            )
    except Exception as exc:  # noqa: BLE001
        try:
            tg.api(
                "sendMessage", chat_id=chat_id,
                text=f"笔记已保存，但消息/文件发送失败：{exc}"[:4000],
            )
        except Exception:  # noqa: BLE001
            pass


def handle_message(msg, kb):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()

    # /learn <视频编号>：在 003-notes 建一篇关联 raw 视频的学习笔记草稿
    learn = re.match(r"^/learn\b\s*(.*)$", text, re.I | re.S)
    if learn:
        rest = learn.group(1).strip()
        if not rest:
            return tg.api(
                "sendMessage", chat_id=chat_id,
                text="用法：/learn <视频编号、BV号、链接或文件夹指纹>\n"
                     "先发视频链接入库，再用回执里的编号建笔记。",
            )
        ref, _, extra = rest.partition(" ")
        try:
            status, path, meta = video.note_draft(kb, ref, extra)
        except LookupError as exc:
            return tg.api("sendMessage", chat_id=chat_id, text=f"⚠️ {exc}"[:4000])
        except Exception as exc:  # noqa: BLE001
            return tg.api("sendMessage", chat_id=chat_id, text=f"⚠️ 建笔记失败：{exc}"[:4000])

        if status == "exists":
            return tg.api(
                "sendMessage", chat_id=chat_id,
                text=f"这篇视频已经有笔记了，没有覆盖：\n003-notes/{path.name}"[:4000],
            )
        return tg.api(
            "sendMessage", chat_id=chat_id,
            text=(
                f"✅ 已建学习笔记草稿\n{meta.get('title')}\n"
                f"003-notes/{path.name}\n\n"
                "打开它，和 AI 一起读原文、填内容；定稿后把 frontmatter 里的 "
                "draft: true 去掉。"
            )[:4000],
        )

    rel_dir, body = split_prefix(text)
    replied = msg.get("reply_to_message") or {}

    # 回复某个文件消息再发 /video：把那个文件挪过去
    if rel_dir and not msg.get("document") and not body and replied.get("document"):
        doc = replied["document"]
        target = next((
            i for i in reversed(recent_files(chat_id))
            if (doc.get("file_unique_id") and i["unique"] == doc["file_unique_id"])
            or (doc.get("file_name") and i["name"] == doc["file_name"])
        ), None)
        if not target:
            return tg.api(
                "sendMessage", chat_id=chat_id,
                text="找不到这条文件的入库记录（bot 可能重启过）。重新发一次文件，在说明里带上 /video。",
            )
        moved = move_files(kb, [target], rel_dir)
        _recent[chat_id] = [i for i in _recent.get(chat_id, []) if i is not target]
        _pending_route.pop(chat_id, None)
        if not moved:
            return tg.api("sendMessage", chat_id=chat_id, text=f"这个文件已经在 {rel_dir} 里了。")
        return tg.api("sendMessage", chat_id=chat_id, text=f"✅ 已移入 {rel_dir}\n{moved[0].name}")

    if msg.get("document"):
        folder = rel_dir or take_pending(chat_id)
        reply = handle_document(msg, kb, folder)
        if not folder:
            reply += "\n想存成视频转写稿？在刚才那条文件消息上回复 /video。"
        return tg.api("sendMessage", chat_id=chat_id, text=reply[:4000])

    urls = URL_RE.findall(body)
    if urls:
        force_video = rel_dir == ROUTES["/video"]
        if force_video:
            video_urls, other_urls = urls, []
        else:
            video_urls = [u for u in urls if video.is_video_url(u)]
            other_urls = [u for u in urls if u not in video_urls]

        parts = []
        if video_urls:
            enqueue_videos(video_urls, chat_id, kb)
            parts.append(
                f"收到 {len(video_urls)} 个视频，开始转写…\n"
                "完成后把转录和关键截图发你。"
            )
        if other_urls:
            parts.append(f"收到 {len(other_urls)} 个链接，开始抓取…")
            parts.append(handle_urls(other_urls, kb))
        return tg.api("sendMessage", chat_id=chat_id, text="\n\n".join(parts)[:4000])

    route_cmd = detect_file_route(body)
    if route_cmd:
        folder, count = route_cmd
        items = recent_files(chat_id)
        picked = items if not count else items[-count:]
        moved = move_files(kb, picked, folder) if picked else []
        moved_ids = {id(i) for i in picked}
        _recent[chat_id] = [i for i in _recent.get(chat_id, []) if id(i) not in moved_ids]
        _pending_route.pop(chat_id, None)
        if not picked:
            return tg.api(
                "sendMessage", chat_id=chat_id,
                text="最近 30 分钟没有可移动的文件。先发文件，或回复文件消息发 /video。",
            )
        if not moved:
            return tg.api("sendMessage", chat_id=chat_id, text=f"这些文件已经在 {folder} 里了。")
        lines = [f"✅ 已存入 {folder}"]
        lines += [str(p.relative_to(kb)).replace("\\", "/") for p in moved]
        return tg.api("sendMessage", chat_id=chat_id, text="\n".join(lines)[:4000])

    # 只发了一个前缀：记住目标目录，等下一条文件或文字
    if rel_dir and not body:
        set_pending(chat_id, rel_dir)
        return tg.api(
            "sendMessage", chat_id=chat_id,
            text=f"收到，接下来 30 分钟内发的文件或文字会存到 {rel_dir}。",
        )

    if body:
        folder = rel_dir or take_pending(chat_id) or "001-inbox"
        path = save_text(kb, body, folder, kind=folder.split("/")[-1])
        return tg.api("sendMessage", chat_id=chat_id, text=f"已存入 {folder}\n{path.name}")
    return None


def _serve(kb):
    me = tg.api("getMe")
    print(f"[{time.strftime('%H:%M:%S')}] 收件箱已启动 @{me.get('username')}，Ctrl+C 退出。", flush=True)

    offset = 0
    while True:
        try:
            updates = tg.api(
                "getUpdates", http_timeout=60, timeout=35,
                offset=offset, allowed_updates=["message", "channel_post"],
            )
        except ConnectionError as exc:
            print(f"[{time.strftime('%H:%M:%S')}] {exc}，10 秒后重试", flush=True)
            time.sleep(10)
            continue
        except RuntimeError as exc:
            print(f"[{time.strftime('%H:%M:%S')}] 拉取失败：{exc}", flush=True)
            time.sleep(5)
            continue

        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("channel_post")
            if not msg:
                continue
            try:
                handle_message(msg, kb)
            except Exception as exc:  # noqa: BLE001
                print(f"[{time.strftime('%H:%M:%S')}] 处理消息出错：{exc}", flush=True)


def notify_failed(kb):
    """启动时把 failed/ 汇总发一条，别让失败静默堆着。"""
    folder = kb / "002-references" / "failed"
    if not folder.exists():
        return

    files = sorted(folder.glob("*.json"))
    if not files:
        return

    lines = []
    for f in files[-10:]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        lines.append(f"· {str(d.get('url', ''))[:90]}\n  {str(d.get('error', ''))[:100]}")

    text = f"⚠️ failed/ 里有 {len(files)} 条抓取失败记录"
    if lines:
        text += "，最近 10 条：\n\n" + "\n".join(lines)
    text += "\n\n重跑：python inbox.py --url <链接>；处理完可以把对应 json 删掉。"
    try:
        tg.send(text[:4000])
    except Exception as exc:  # noqa: BLE001
        print(f"失败汇总发送失败：{exc}", flush=True)


def _delayed_summary(kb):
    time.sleep(5)
    notify_failed(kb)


def run_bot():
    """守护外壳：进程内任何异常都不退出，30 秒后重来。开机自启靠它兜底。"""
    kb = kb_root()

    # 飞书通道和 Telegram 并行跑，两个入口共用同一套抓取与落盘
    if os.environ.get("FEISHU_APP_ID", "").strip():
        try:
            import feishu
            threading.Thread(
                target=feishu.start, args=(kb,), daemon=True, name="feishu",
            ).start()
            print("[feishu] 通道已启用", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[feishu] 启动失败：{exc}", flush=True)

    threading.Thread(target=_delayed_summary, args=(kb,), daemon=True).start()

    while True:
        try:
            _serve(kb)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[{time.strftime('%H:%M:%S')}] 收件箱异常：{exc}，30 秒后重启", flush=True)
            time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="个人知识库收件箱")
    parser.add_argument("--url", action="append", default=[], help="手动处理链接，可重复")
    parser.add_argument("--text", help="写一条文字（支持 /note /video /idea 前缀）")
    args = parser.parse_args()

    tg.load_env()
    kb = kb_root()

    if args.url:
        print(handle_urls(args.url, kb))
        return
    if args.text:
        rel_dir, body = split_prefix(args.text)
        folder = rel_dir or "001-inbox"
        print(save_text(kb, body, folder, kind=folder.split("/")[-1]))
        return

    run_bot()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止。")
