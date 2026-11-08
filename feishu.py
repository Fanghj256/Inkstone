"""飞书通道：长连接收消息，复用同一套抓取与落盘逻辑。

和 Telegram 的区别只在"怎么收、怎么回"，抓取和入库走的是同一批函数。
长连接不需要公网 IP，国内直连，不用挂代理。
"""

import json
import os
import re
import threading
import time
from pathlib import Path

import requests

BASE = "https://open.feishu.cn/open-apis"
_seen = set()
_seen_lock = threading.Lock()
_token = {"value": "", "expires_at": 0.0}


def _session():
    s = requests.Session()
    s.trust_env = False          # 飞书是国内服务，别让系统代理绕一圈
    return s


def _credentials():
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("`.env` 里缺 FEISHU_APP_ID 或 FEISHU_APP_SECRET")
    return app_id, app_secret


def tenant_token(force=False):
    """tenant_access_token 有效期 2 小时，缓存起来，提前 5 分钟换。"""
    if not force and _token["value"] and time.time() < _token["expires_at"]:
        return _token["value"]

    app_id, app_secret = _credentials()
    r = _session().post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret}, timeout=30,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书鉴权失败：{data.get('code')} {data.get('msg')}")

    _token["value"] = data["tenant_access_token"]
    _token["expires_at"] = time.time() + max(60, data.get("expire", 7200) - 300)
    return _token["value"]


def send_text(chat_id, text):
    r = _session().post(
        f"{BASE}/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={"Authorization": f"Bearer {tenant_token()}"},
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text[:3000]}, ensure_ascii=False),
        },
        timeout=30,
    )
    data = r.json()
    if data.get("code") != 0:
        print(f"[feishu] 发送失败 {data.get('code')} {data.get('msg')}")
    return data


def _clean(text):
    """群聊里 @机器人 会留下 @_user_1 这样的占位符。"""
    return re.sub(r"@_user_\d+", "", text).strip()


# --------------------------------------------------------------------------
# 关机补收：飞书群消息可以按时间段拉回来，Telegram 做不到
# --------------------------------------------------------------------------

STATE_FILE = Path(__file__).resolve().parent / "feishu_state.json"


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _list_chats():
    items, page = [], None
    while True:
        params = {"page_size": 50}
        if page:
            params["page_token"] = page
        r = _session().get(
            f"{BASE}/im/v1/chats", params=params,
            headers={"Authorization": f"Bearer {tenant_token()}"}, timeout=30,
        ).json()
        data = r.get("data") or {}
        items.extend(data.get("items") or [])
        page = data.get("page_token")
        if not data.get("has_more"):
            break
    return items


def _history(chat_id, start_ts, end_ts):
    r = _session().get(
        f"{BASE}/im/v1/messages",
        params={
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(int(start_ts)),
            "end_time": str(int(end_ts)),
            "sort_type": "ByCreateTimeAsc",
            "page_size": 50,
        },
        headers={"Authorization": f"Bearer {tenant_token()}"}, timeout=30,
    ).json()
    if r.get("code") != 0:
        print(f"[feishu] 拉历史失败 {r.get('code')} {r.get('msg')}", flush=True)
        return []
    return (r.get("data") or {}).get("items") or []


def backfill(kb):
    """开机时把关机期间群里的链接补收一遍。首次运行只看最近 24 小时。"""
    import inbox

    state = _load_state()
    now = int(time.time())
    total = 0

    for chat in _list_chats():
        chat_id = chat.get("chat_id")
        if not chat_id:
            continue
        since = int(state.get(chat_id, now - 86400))

        for m in _history(chat_id, since, now):
            mid = m.get("message_id")
            sender = (m.get("sender") or {}).get("sender_type")
            if not mid or sender == "app":
                continue
            if m.get("msg_type") != "text":
                continue
            with _seen_lock:
                if mid in _seen:
                    continue
                _seen.add(mid)

            try:
                text = _clean(json.loads((m.get("body") or {}).get("content", "{}")).get("text", ""))
            except Exception:  # noqa: BLE001
                continue

            urls = inbox.URL_RE.findall(text)
            if not urls:
                continue

            total += len(urls)
            print(f"[feishu] 补收 {chat_id}: {urls[0]}", flush=True)
            try:
                send_text(chat_id, f"补收 {len(urls)} 个链接（关机期间的），开始抓取…")
                send_text(chat_id, inbox.handle_urls(urls, kb))
            except Exception as exc:  # noqa: BLE001
                print(f"[feishu] 补收失败：{exc}", flush=True)

        state[chat_id] = now

    _save_state(state)
    if total:
        print(f"[feishu] 本次补收 {total} 个链接", flush=True)



def download_resource(message_id, file_key, kind="file"):
    """下载消息里的文件或图片。"""
    r = _session().get(
        f"{BASE}/im/v1/messages/{message_id}/resources/{file_key}",
        params={"type": kind},
        headers={"Authorization": f"Bearer {tenant_token()}"},
        timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"下载失败 {r.status_code}: {r.text[:200]}")
    return r.content


def _handle_file(kb, chat_id, msg):
    import store

    try:
        content = json.loads(msg.content or "{}")
        if msg.message_type == "file":
            key = content.get("file_key")
            name = content.get("file_name") or f"{msg.message_id}.bin"
            kind = "file"
        elif msg.message_type == "image":
            key = content.get("image_key")
            name = f"{msg.message_id}.jpg"
            kind = "image"
        else:
            send_text(chat_id, f"暂不支持 {msg.message_type} 类型，发文件和图片可以。")
            return

        if not key:
            send_text(chat_id, "这条消息里没有找到文件标识。")
            return

        data = download_resource(msg.message_id, key, kind)
        path = store.save_file(kb, name, data)
        store.git_commit(kb, path, f"file: {path.name}")
        send_text(chat_id, f"✅ 文件已存入 002-references/files\n{path.name}\n{len(data):,} 字节")
    except Exception as exc:  # noqa: BLE001
        send_text(chat_id, f"⚠️ 文件存入失败：{exc}"[:2000])


def _handle(kb, chat_id, text):
    import inbox   # 延迟导入，避免循环

    print(f"[feishu] 收到 {chat_id}: {text[:40]}", flush=True)
    try:
        rel_dir, body = inbox.split_prefix(text)
        urls = inbox.URL_RE.findall(body)

        if urls:
            send_text(chat_id, f"收到 {len(urls)} 个链接，开始抓取…")
            send_text(chat_id, inbox.handle_urls(urls, kb))
            return

        if body:
            folder = rel_dir or "001-inbox"
            path = inbox.save_text(kb, body, folder, kind=folder.split("/")[-1])
            send_text(chat_id, f"已存入 {folder}\n{path.name}")
    except Exception as exc:  # noqa: BLE001
        try:
            send_text(chat_id, f"⚠️ 处理失败：{exc}"[:3000])
        except Exception:  # noqa: BLE001
            pass


def start(kb):
    """阻塞式启动长连接；断线自动重连。"""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    def on_message(data: P2ImMessageReceiveV1) -> None:
        msg = data.event.message
        mid = msg.message_id
        with _seen_lock:
            if mid in _seen:
                return
            _seen.add(mid)
            if len(_seen) > 2000:
                _seen.clear()

        if msg.message_type != "text":
            threading.Thread(target=_handle_file, args=(kb, msg.chat_id, msg), daemon=True).start()
            return

        text = _clean(json.loads(msg.content).get("text", ""))
        if not text:
            return
        threading.Thread(target=_handle, args=(kb, msg.chat_id, text), daemon=True).start()

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    app_id, app_secret = _credentials()

    # 先补收关机期间的消息，再接实时长连接
    try:
        backfill(kb)
    except Exception as exc:  # noqa: BLE001
        print(f"[feishu] 补收跳过：{exc}", flush=True)

    while True:
        try:
            client = lark.ws.Client(
                app_id, app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.WARNING,
            )
            print("[feishu] 长连接启动中…", flush=True)
            client.start()
        except Exception as exc:  # noqa: BLE001
            print(f"[feishu] 连接断开：{exc}，30 秒后重连", flush=True)
        time.sleep(30)
