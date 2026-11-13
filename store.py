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


def atomic_write_text(path, content):
    """先写 .tmp 再 replace；Windows 下跨盘/占用也不会留下半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


def dump_frontmatter(meta):
    return yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=1000).strip()


def md_document(meta, body, heading=None):
    """frontmatter + 可选标题 + 正文。所有 Markdown 落盘只走这一个格式。"""
    front = dump_frontmatter(meta)
    parts = [f"---\n{front}\n---", ""]
    if heading:
        parts += [f"# {heading}", ""]
    parts.append(body.strip())
    return "\n".join(parts) + "\n"


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

    atomic_write_text(path, md_document(meta, body, heading=meta.get("title", "未命名")))
    return path


def save_file(kb, filename, data: bytes, rel_dir="002-references/files"):
    folder = kb / rel_dir
    folder.mkdir(parents=True, exist_ok=True)
    # 只取文件名本身，挡掉 `..\..\x` 这类上传名
    safe = Path(filename or "").name
    safe = re.sub(r'[\\/:*?"<>|]', "_", safe).strip()
    if safe in ("", ".", ".."):
        safe = "unnamed"
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

    atomic_write_text(path, md_document(meta, body))
    return path


def save_failure(kb, url, error):
    folder = kb / "002-references" / "failed"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now(CST).strftime('%Y%m%d-%H%M%S')}-{url_hash(url)}.json"
    path = folder / name
    atomic_write_text(
        path,
        json.dumps({"url": url, "error": str(error)[:1000], "at": now_iso()},
                   ensure_ascii=False, indent=2),
    )
    return path


def git_move(kb, src, dst, message):
    """移动已入库的文件：能 git mv 就保留历史，否则直接搬。"""
    moved = False
    committed = False
    with _GIT_LOCK:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if (kb / ".git").exists():
            r = subprocess.run(
                ["git", "-C", str(kb), "mv", "--",
                 str(src.relative_to(kb)), str(dst.relative_to(kb))],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if r.returncode == 0:
                moved = True
                c = subprocess.run(
                    ["git", "-C", str(kb), "commit", "-m", message],
                    capture_output=True,
                )
                committed = c.returncode == 0
        if not moved:
            os.replace(src, dst)

    # 和 git_commit 一样，移动也要补一次 push，否则远端要等下一次入库才更新
    if committed and os.environ.get("AUTO_PUSH", "1") != "0" and _has_remote(kb):
        _push(kb)
    return committed


def _has_remote(kb):
    r = subprocess.run(["git", "-C", str(kb), "remote"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return bool(r.stdout.strip())


def _push(kb):
    """推远端，失败不影响本地；没网时下一次提交会补上。"""
    try:
        subprocess.run(["git", "-C", str(kb), "push", "--quiet"],
                       capture_output=True, timeout=90)
    except Exception:  # noqa: BLE001
        pass


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
        ok = r.returncode == 0

    # 配了远端就顺手备份；AUTO_PUSH=0 可以关掉
    if ok and os.environ.get("AUTO_PUSH", "1") != "0" and _has_remote(kb):
        _push(kb)
    return ok
