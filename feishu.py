"""飞书通道：长连接收消息，复用同一套抓取与落盘逻辑。

和 Telegram 的区别只在"怎么收、怎么回"，抓取和入库走的是同一批函数。
长连接不需要公网 IP，国内直连，不用挂代理。
"""

import json
import os
import re
import threading
import time

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
            threading.Thread(
                target=send_text, args=(msg.chat_id, "目前只支持文字和链接，文件请走 Telegram。"),
                daemon=True,
            ).start()
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
