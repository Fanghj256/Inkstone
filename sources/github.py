"""GitHub：仓库 README、issue、PR。

走 gh CLI（已登录），比爬页面稳得多，还能顺手拿到 star、语言、License。
"""

import json
import subprocess
from urllib.parse import urlparse


def match(url):
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host == "github.com" or host.endswith(".github.com")


def _gh(args):
    proc = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh api 失败：{(proc.stderr or '').strip()[:300]}")
    return proc.stdout


def fetch(url):
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub 链接缺少 owner/repo")
    owner, repo = parts[0], parts[1]
    rest = parts[2:]

    if rest and rest[0] in ("issues", "pull") and len(rest) > 1:
        num = rest[1]
        item = json.loads(_gh([f"repos/{owner}/{repo}/issues/{num}"]))
        labels = ", ".join(l["name"] for l in item.get("labels", []))
        body = "\n".join([
            f"> {item.get('state', '')} · {item.get('user', {}).get('login', '')} · "
            f"{item.get('created_at', '')[:10]}",
            "",
            item.get("body") or "（正文为空）",
            "",
            "---",
            "",
            f"标签：{labels or '无'}",
        ])
        return {
            "title": f"[{owner}/{repo}#{num}] {item.get('title', '')}",
            "author": item.get("user", {}).get("login", ""),
            "text": body,
            "extra": {"repo": f"{owner}/{repo}", "state": item.get("state"), "extractor": "gh-api"},
        }

    info = json.loads(_gh([f"repos/{owner}/{repo}"]))
    try:
        readme = _gh([f"repos/{owner}/{repo}/readme", "-H", "Accept: application/vnd.github.raw"])
    except RuntimeError:
        readme = "（这个仓库没有 README）"

    lic = (info.get("license") or {}).get("spdx_id") or "无"
    body = "\n".join([
        f"> {info.get('description') or '（没有描述）'}",
        "",
        "## 仓库信息",
        "",
        f"- Star：{info.get('stargazers_count')}　Fork：{info.get('forks_count')}",
        f"- 主语言：{info.get('language') or '未知'}　License：{lic}",
        f"- 最近更新：{(info.get('updated_at') or '')[:10]}",
        f"- Topics：{', '.join(info.get('topics') or []) or '无'}",
        "",
        "## README",
        "",
        readme.strip(),
    ])
    return {
        "title": f"[GitHub] {owner}/{repo}",
        "author": owner,
        "text": body,
        "extra": {
            "stars": info.get("stargazers_count"),
            "language": info.get("language"),
            "license": lic,
            "extractor": "gh-api",
        },
    }
