"""Telegram Bot API 的最小封装。"""

import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent


def load_env(path=None):
    env_file = Path(path) if path else ROOT / ".env"
    if not env_file.exists():
        sys.exit(f"缺少 {env_file}。先复制 .env.example 再填。")

    for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    # requests、gh 这些第三方工具只认标准的 HTTP(S)_PROXY，不认我们自己起的 PROXY。
    # 不导出的话，Telegram 走代理，抓正文却直连，被墙的站就会超时。
    proxy = os.environ.get("PROXY", "").strip()
    if proxy:
        os.environ.setdefault("HTTP_PROXY", proxy)
        os.environ.setdefault("HTTPS_PROXY", proxy)


def proxies():
    p = os.environ.get("PROXY", "").strip()
    return {"http": p, "https": p} if p else None


def api(method, http_timeout=40, **params):
    """调 Bot API。params 里的 timeout 是长轮询等待，不是 HTTP 超时。"""
    token = os.environ["TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        resp = requests.post(url, json=params, proxies=proxies(), timeout=http_timeout)
    except requests.exceptions.ProxyError as exc:
        # 代理还没起来是常态（开机自启时尤其常见），交给上层重试，不要直接退出
        raise ConnectionError(f"连不上代理 {os.environ.get('PROXY')}") from exc
    except requests.exceptions.Timeout as exc:
        raise ConnectionError("请求超时：代理不通或网络抖动") from exc
    except requests.exceptions.RequestException as exc:
        raise ConnectionError(f"网络错误：{exc}") from exc

    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"{method} 失败：{body.get('error_code')} {body.get('description')}")
    return body["result"]


def send(text, chat_id=None):
    """给 .env 里配置的 CHAT_ID 发消息；超长自动截断。"""
    cid = chat_id or os.environ.get("CHAT_ID")
    if not cid:
        return None
    return api("sendMessage", chat_id=int(cid), text=text[:4000])


def send_document(path, chat_id=None, caption=None):
    """发文件（视频笔记的 note.html / transcript.md）。"""
    cid = chat_id or os.environ.get("CHAT_ID")
    if not cid:
        return None
    token = os.environ["TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    path = Path(path)
    with path.open("rb") as handle:
        resp = requests.post(
            url,
            data={"chat_id": int(cid), "caption": (caption or "")[:1024]},
            files={"document": (path.name, handle)},
            proxies=proxies(),
            timeout=300,
        )
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(
            f"sendDocument 失败：{body.get('error_code')} {body.get('description')}"
        )
    return body["result"]
