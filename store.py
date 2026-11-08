"""落盘：命名、去重、YAML frontmatter、原子写、git commit。"""

import hashlib
import json
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import yaml

CST = timezone(timedelta(hours=8))

# 网页抓取和视频转写可能同时提交，避免 git index.lock 冲突
_GIT_LOCK = threading.Lock()

# 这些参数只是追踪用的，去掉之后同一个链接才算同一个
TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "share_from", "ref", "ref_src", "ref_url", "s", "t",
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid",
}


def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def canonical(url):
    """去掉追踪参数和锚点，让同一篇文章只有一个指纹。"""
    p = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING]
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", urlencode(query), ""))


def url_hash(url):
    return hashlib.sha1(canonical(url).encode("utf-8")).hexdigest()[:8]


def slugify(title, maxlen=40):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", title or "")
    s = re.sub(r"\s+", "-", s.strip())
    s = s.strip(".-") or "untitled"
    return s[:maxlen].rstrip(".-")


def find_existing(kb, url):
    h = url_hash(url)
    return next((kb / "002-references" / "raw").rglob(f"*-{h}.md"), None)


def write_note(kb, meta, body):
    """写一篇 markdown，返回路径。原子写：先写 .tmp 再 replace。"""
    source = meta.get("source", "web")
    folder = kb / "002-references" / "raw" / source
    folder.mkdir(parents=True, exist_ok=True)

    date = datetime.now(CST).strftime("%Y%m%d")
    name = f"{date}-{slugify(meta.get('title'))}-{url_hash(meta['url'])}.md"
    path = folder / name

    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=1000).strip()
    content = f"---\n{fm}\n---\n\n# {meta.get('title', '未命名')}\n\n{body.strip()}\n"

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


def save_file(kb, filename, data: bytes, rel_dir="002-references/files"):
    folder = kb / rel_dir
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", filename or "unnamed")
    path = folder / safe
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


def write_simple(kb, rel_dir, title_hint, meta, body):
    """写一篇没有 URL 的 markdown（随手记、笔记、转写稿）。"""
    folder = kb / rel_dir
    folder.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    hint = slugify(title_hint or "", 30) or "note"
    path = folder / f"{stamp}-{hint}.md"

    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=1000).strip()
    content = f"---\n{fm}\n---\n\n{body.strip()}\n"

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


def save_failure(kb, url, error):
    folder = kb / "002-references" / "failed"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now(CST).strftime('%Y%m%d-%H%M%S')}-{url_hash(url)}.json"
    path = folder / name
    path.write_text(
        json.dumps({"url": url, "error": str(error)[:1000], "at": now_iso()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def git_move(kb, src, dst, message):
    """移动已入库的文件：能 git mv 就保留历史，否则直接搬。"""
    with _GIT_LOCK:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if (kb / ".git").exists():
            r = subprocess.run(
                ["git", "-C", str(kb), "mv", "--",
                 str(src.relative_to(kb)), str(dst.relative_to(kb))],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if r.returncode == 0:
                subprocess.run(
                    ["git", "-C", str(kb), "commit", "-m", message],
                    capture_output=True,
                )
                return True
        os.replace(src, dst)
        return False


def git_commit(kb, path, title):
    with _GIT_LOCK:
        if not (kb / ".git").exists():
            return False
        subprocess.run(["git", "-C", str(kb), "add", "--", str(path)],
                       capture_output=True)
        r = subprocess.run(
            ["git", "-C", str(kb), "commit", "-m", f"add: {title}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return r.returncode == 0
