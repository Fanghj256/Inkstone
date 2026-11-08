"""Hugging Face Space：页面本体是 iframe，正文取 README + API 元信息。"""

import re
from urllib.parse import urlparse

import requests

from .base import HEADERS


def match(url):
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":")[0]
    return host == "huggingface.co" and parsed.path.startswith("/spaces/")


def fetch(url):
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 3 or parts[0] != "spaces":
        raise ValueError("这个 Hugging Face 链接不是 Space")
    owner, name = parts[1], parts[2]
    repo = f"{owner}/{name}"

    meta_resp = requests.get(f"https://huggingface.co/api/spaces/{repo}", headers=HEADERS, timeout=30)
    meta_resp.raise_for_status()
    meta = meta_resp.json()

    readme_resp = requests.get(
        f"https://huggingface.co/spaces/{repo}/raw/main/README.md", headers=HEADERS, timeout=30,
    )
    body = readme_resp.text.strip() if readme_resp.ok else ""

    # README 顶部的 frontmatter 是给 Space 页面配色的，正文从第一个标题开始
    body = re.sub(r"^---\r?\n.*?\r?\n---\r?\n", "", body, count=1, flags=re.S).strip()
    if not body:
        body = "（这个 Space 没有 README）"

    card = meta.get("cardData") or {}
    title = card.get("title") or repo
    lines = [
        f"> {card.get('short_description') or '（没有简介）'}",
        "",
        f"- 页面：https://huggingface.co/spaces/{repo}",
        f"- 运行地址：{meta.get('host') or '未知'}",
        f"- SDK：{meta.get('sdk') or '未知'}　点赞：{meta.get('likes', 0)}",
        f"- 最近更新：{(meta.get('lastModified') or '')[:10] or '未知'}",
    ]
    models = meta.get("models") or []
    datasets = meta.get("datasets") or []
    if models:
        lines.append(f"- 相关模型：{', '.join(models)}")
    if datasets:
        lines.append(f"- 相关数据集：{', '.join(datasets)}")
    lines += ["", "## README", "", body]

    return {
        "title": f"[HF Space] {title}",
        "author": owner,
        "text": "\n".join(lines),
        "extra": {
            "likes": meta.get("likes"),
            "sdk": meta.get("sdk"),
            "license": card.get("license") or "",
            "space_host": meta.get("host") or "",
            "last_modified": (meta.get("lastModified") or "")[:10],
            "extractor": "hf-api+readme",
        },
    }
