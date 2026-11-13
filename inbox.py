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
from pathlib import Path

import channels
import store
import tg

# Windows 控制台多为 GBK，emoji 会直接抛 UnicodeEncodeError。
# 只把不可编码字符替换掉，不改编码，避免中文乱码。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

URL_RE = channels.URL_RE
ROUTES = channels.ROUTES

# 刚收到的文件记在这里，方便下一条消息说「这两个存成视频转写稿」。
# 只放内存：bot 重启后失效，30 分钟也够用了。
RECENT_TTL = channels.RECENT_TTL
RECENT_LIMIT = 10
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
    """兼容旧调用：抓一篇网页的实际实现已经搬到 channels.process。"""
    return channels.process(url, kb)


def split_prefix(text):
    return channels.split_prefix(text)


def save_text(kb, text, rel_dir="001-inbox", kind="idea"):
    return channels.save_text(kb, text, rel_dir, kind, channel="telegram")


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
    """CLI 用：同步处理（视频也等它跑完），返回可直接打印的回执。"""
    return "\n\n".join(channels.fetch_sync(url, kb) for url in urls)


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


def handle_message(msg, kb):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    channel = channels.TelegramChannel()

    # /learn <视频编号>：在 003-notes 建一篇关联 raw 视频的学习笔记草稿
    if channels.learn(kb, channel, chat_id, text):
        return

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
            return channel.send_text(
                chat_id,
                "找不到这条文件的入库记录（bot 可能重启过）。重新发一次文件，在说明里带上 /video。",
            )
        moved = move_files(kb, [target], rel_dir)
        _recent[chat_id] = [i for i in _recent.get(chat_id, []) if i is not target]
        channels.clear_pending(channel, chat_id)
        if not moved:
            return channel.send_text(chat_id, f"这个文件已经在 {rel_dir} 里了。")
        return channel.send_text(chat_id, f"✅ 已移入 {rel_dir}\n{moved[0].name}")

    if msg.get("document"):
        folder = rel_dir or channels.take_pending(channel, chat_id)
        reply = handle_document(msg, kb, folder)
        if not folder:
            reply += "\n想存成视频转写稿？在刚才那条文件消息上回复 /video。"
        return channel.send_text(chat_id, reply)

    urls = URL_RE.findall(body)
    if urls:
        channels.handle_urls(
            kb, channel, chat_id, urls,
            force_video=(rel_dir == ROUTES["/video"]),
        )
        return

    route_cmd = detect_file_route(body)
    if route_cmd:
        folder, count = route_cmd
        items = recent_files(chat_id)
        picked = items if not count else items[-count:]
        moved = move_files(kb, picked, folder) if picked else []
        moved_ids = {id(i) for i in picked}
        _recent[chat_id] = [i for i in _recent.get(chat_id, []) if id(i) not in moved_ids]
        channels.clear_pending(channel, chat_id)
        if not picked:
            return channel.send_text(
                chat_id,
                "最近 30 分钟没有可移动的文件。先发文件，或回复文件消息发 /video。",
            )
        if not moved:
            return channel.send_text(chat_id, f"这些文件已经在 {folder} 里了。")
        lines = [f"✅ 已存入 {folder}"]
        lines += [str(p.relative_to(kb)).replace("\\", "/") for p in moved]
        return channel.send_text(chat_id, "\n".join(lines))

    # 只发了一个前缀：记住目标目录，等下一条文件或文字
    if rel_dir and not body:
        channels.set_pending(channel, chat_id, rel_dir)
        return channel.send_text(
            chat_id,
            f"收到，接下来 {RECENT_TTL // 60} 分钟内发的文件或文字会存到 {rel_dir}。",
        )

    if body:
        folder = rel_dir or channels.take_pending(channel, chat_id) or "001-inbox"
        path = save_text(kb, body, folder, kind=folder.split("/")[-1])
        return channel.send_text(chat_id, f"已存入 {folder}\n{path.name}")
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
    if not os.environ.get("TOKEN", "").strip():
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

    if not os.environ.get("TOKEN", "").strip():
        if not os.environ.get("FEISHU_APP_ID", "").strip():
            sys.exit("`.env` 里既没有 TOKEN，也没有 FEISHU_APP_ID，没有可启动的通道。")
        print("[telegram] 未配置 TOKEN，只跑飞书通道", flush=True)
        while True:
            time.sleep(3600)

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
