"""按域名分派提取器：X / GitHub / 知乎 / 通用网页。

设计原则只有一条：**入库的必须是一手原文**，任何环节都不许模型重写正文。
浏览器路径统一走 ego-browser，它用的是你本机真实的 Chromium 和登录态。
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import trafilatura

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}


def route(url):
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    if host.endswith("x.com") or host.endswith("twitter.com"):
        return "x"
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host.endswith("zhihu.com"):
        return "zhihu"
    if host.endswith("xiaohongshu.com") or host.endswith("xhslink.com"):
        return "xiaohongshu"
    if host.endswith("mp.weixin.qq.com"):
        return "wechat"
    if host == "huggingface.co" and urlparse(url).path.startswith("/spaces/"):
        return "huggingface"
    return "web"


# 这些是"抓到了但其实是墙"的特征。宁可判失败，也不要往库里塞垃圾。
WALL_MARKERS = (
    "当前笔记暂时无法浏览",
    "请打开小红书App",
    "环境异常",
    "完成验证后即可继续访问",
    "访问过于频繁",
    "登录后查看",
    "你访问的页面不见了",
    "该内容已被发布者删除",
)


# --------------------------------------------------------------------------
# 真浏览器采集（ego-browser）
# --------------------------------------------------------------------------

_JS = r'''const { writeFile } = await import("node:fs/promises");
const task = await taskSpace(__SPACE__);
const page = task.page("p1");
let data;
try {
  await page.goto(__URL__, { timeout: 60000 });
  await page.waitForTimeout(__WAIT__);
__BODY__
} catch (e) {
  data = { error: String((e && e.message) || e) };
}
await writeFile(__OUT__, JSON.stringify(data), "utf8");
try { await task.finish({ keep: [] }); } catch (e) {}
console.log("done");
'''

_X_BODY = r'''
  data = await page.evaluate(() => {
    const pick = (sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ""; };
    const isNum = (s) => /^[\d,.\s]+$/.test(s);

    const article = pick('[data-testid="twitterArticleReadView"]');
    const tweets = Array.from(document.querySelectorAll('[data-testid="tweetText"]'))
      .map((el) => el.innerText.trim()).filter(Boolean);

    let body = article || tweets.join("\n\n---\n\n");

    // 标题优先取页面标题，正文容器里的第一个 h1 往往只是小节标题
    let title = (document.title || "")
      .replace(/^X 上的 [^：]*：/, "")
      .replace(/^.*? on X: /, "")
      .replace(/ \/ X$/, "")
      .replace(/^[“"「]/, "").replace(/[”"」]$/, "")
      .trim();

    // 文章头部会混进互动数（19 / 15 / 60 / 8,488），连同重复的标题行一起剥掉
    const lines = body.split("\n");
    while (lines.length) {
      const head = lines[0].trim();
      if (!head || isNum(head) || (title && head === title)) { lines.shift(); continue; }
      break;
    }
    body = lines.join("\n").trim();

    if (!title) title = body.split("\n")[0] || "";
    if (title.length > 80) title = title.slice(0, 80).trim() + "…";

    const nameBlock = pick('[data-testid="User-Name"]');
    return {
      title: title,
      author: nameBlock.split("\n").filter(Boolean)[1] || "",
      text: body,
      kind: article ? "article" : "tweet",
      url: location.href,
    };
  });
'''

_ZHIHU_BODY = r'''
  // 知乎默认只显示回答的前几段，先点开所有"阅读全文"
  await page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll("button, .ContentItem-expandButton"));
    nodes.filter((el) => /展开阅读全文|阅读全文|显示全部/.test(el.innerText || ""))
      .slice(0, 6)
      .forEach((el) => { try { el.click(); } catch (e) {} });
  });
  await page.waitForTimeout(1500);

  await page.evaluate(() => new Promise((resolve) => {
    let n = 0;
    const timer = setInterval(() => {
      window.scrollBy(0, window.innerHeight * 0.9);
      if (++n >= 8) { clearInterval(timer); resolve(true); }
    }, 500);
  }));
  await page.waitForTimeout(1000);
  data = await page.evaluate(() => {
    const pick = (sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ""; };
    return {
      title: pick("h1.Post-Title") || pick(".QuestionHeader-title") || document.title,
      author: pick(".AuthorInfo-name") || pick(".UserLink-link") || "",
      text: pick(".Post-RichText") || pick(".RichText.ztext") || pick("article") || "",
      kind: "answer",
      url: location.href,
    };
  });
'''

_WEB_BODY = r'''
  await page.evaluate(() => new Promise((resolve) => {
    let n = 0;
    const timer = setInterval(() => {
      window.scrollBy(0, window.innerHeight);
      if (++n >= 5) { clearInterval(timer); resolve(true); }
    }, 400);
  }));
  data = await page.evaluate(() => {
    const pick = (sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ""; };
    return {
      title: (document.title || "").trim(),
      author: pick('meta[name="author"]') ? "" : "",
      text: pick("article") || pick("main") || document.body.innerText.trim(),
      kind: "page",
      url: location.href,
    };
  });
'''

_XHS_BODY = r'''
  await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll("button, span, div"));
    btns.filter((el) => (el.innerText || "").trim() === "展开")
      .slice(0, 3)
      .forEach((el) => { try { el.click(); } catch (e) {} });
  });
  await page.waitForTimeout(1200);
  data = await page.evaluate(() => {
    const pick = (sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ""; };
    const desc = pick("#detail-desc") || pick(".note-text") || pick(".desc");
    return {
      title: pick("#detail-title") || document.title,
      author: pick(".author-container .username") || pick(".author-wrapper .name") || "",
      text: desc,
      kind: "note",
      url: location.href,
    };
  });
'''

_BODIES = {"x": _X_BODY, "zhihu": _ZHIHU_BODY, "web": _WEB_BODY, "xiaohongshu": _XHS_BODY}


def _browser(url, kind, wait=3000):
    body = _BODIES.get(kind, _WEB_BODY)
    space = f"kb-{kind}-{abs(hash(url)) % 10**8}"

    # mkstemp 会返回一个打开的文件描述符，Windows 下不关掉会导致后面删不掉
    fd, out_name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    out = Path(out_name)

    js = (
        _JS.replace("__SPACE__", json.dumps(space))
        .replace("__URL__", json.dumps(url))
        .replace("__WAIT__", str(wait))
        .replace("__BODY__", body)
        .replace("__OUT__", json.dumps(out.as_posix()))
    )

    fd, script_name = tempfile.mkstemp(suffix=".mjs")
    os.close(fd)
    script = Path(script_name)
    script.write_text(js, encoding="utf-8")

    try:
        proc = subprocess.run(
            ["ego-browser", "nodejs", "-e", f"await import('file:///{script.as_posix()}')"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ego-browser 退出码 {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}")
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("ego-browser 没有产出结果文件")
        data = json.loads(out.read_text(encoding="utf-8"))
    finally:
        for p in (out, script):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    if data.get("error"):
        raise RuntimeError(f"页面抓取失败：{data['error']}")
    return data


# --------------------------------------------------------------------------
# GitHub（走 API，比爬页面稳得多）
# --------------------------------------------------------------------------

def _gh(args):
    proc = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh api 失败：{(proc.stderr or '').strip()[:300]}")
    return proc.stdout


def _github(url):
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
            "extra": {"repo": f"{owner}/{repo}", "state": item.get("state")},
        }

    info = json.loads(_gh([f"repos/{owner}/{repo}"]))
    readme = ""
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
        },
    }


# --------------------------------------------------------------------------
# Hugging Face Space（页面本体是 iframe，正文取 README + API 元信息）
# --------------------------------------------------------------------------

def _hf_space(url):
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 3 or parts[0] != "spaces":
        raise ValueError("这个 Hugging Face 链接不是 Space")
    owner, name = parts[1], parts[2]
    repo = f"{owner}/{name}"

    meta_resp = requests.get(f"https://huggingface.co/api/spaces/{repo}", timeout=30)
    meta_resp.raise_for_status()
    meta = meta_resp.json()

    readme_resp = requests.get(
        f"https://huggingface.co/spaces/{repo}/raw/main/README.md", timeout=30,
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
        },
    }


# --------------------------------------------------------------------------
# 通用网页（trafilatura 抽取正文；失败再退回真浏览器）
# --------------------------------------------------------------------------

def _web(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.content.decode(resp.apparent_encoding or "utf-8", errors="replace")

    text = trafilatura.extract(
        html, url=url, output_format="markdown",
        include_links=True, include_tables=True, favor_recall=True,
    )
    meta = trafilatura.extract_metadata(html, default_url=url)
    author = (meta.author if meta and meta.author else "") or ""

    # 公众号的作者是公众号名，trafilatura 抓不到，从页面里补
    if not author and "mp.weixin.qq.com" in url:
        m = re.search(r'id="js_name"[^>]*>\s*([^<]+)', html)
        if m:
            author = m.group(1).strip()

    if not text or len(text) < 200:
        data = _browser(url, "web", wait=2500)
        if not data.get("text"):
            raise RuntimeError("正文抽取为空，可能被反爬拦住了")
        return {
            "title": (meta.title if meta and meta.title else data.get("title")) or url,
            "author": author,
            "text": data["text"],
            "extra": {"extractor": "browser-fallback"},
        }

    title = (meta.title if meta and meta.title else "") or url
    return {
        "title": title,
        "author": author,
        "text": text,
        "extra": {"extractor": "trafilatura"},
    }


# --------------------------------------------------------------------------

def fetch(url):
    """统一入口：返回 {title, author, text, source, extra}。"""
    kind = route(url)

    if kind == "github":
        data = _github(url)
        data.setdefault("extra", {})["extractor"] = "gh-api"

    elif kind == "huggingface":
        data = _hf_space(url)
        data.setdefault("extra", {})["extractor"] = "hf-api+readme"

    elif kind == "xiaohongshu":
        data = _browser(url, "xiaohongshu", wait=4000)
        text = (data.get("text") or "").strip()
        if len(text) < 30:
            raise RuntimeError(
                "小红书没抓到正文：多半是没登录，或笔记已失效（xsec_token 会过期）"
            )
        data.setdefault("extra", {})["extractor"] = "ego-browser/note"

    elif kind in ("x", "zhihu"):
        data = _browser(url, kind, wait=4000 if kind == "x" else 3000)
        if not data.get("text") or len(data["text"]) < 30:
            raise RuntimeError("没抓到正文，可能没登录或链接已失效")
        data.setdefault("extra", {})["extractor"] = f"ego-browser/{data.get('kind', kind)}"

    else:
        data = _web(url)

    text = data["text"]

    # 抓到了但其实是墙：宁可判失败，也不要把页脚和法律声明存成"文章"
    if kind in ("web", "wechat", "xiaohongshu"):
        head = text[:2000]
        for marker in WALL_MARKERS:
            if marker in head:
                raise RuntimeError(f"页面被拦截（出现「{marker}」），不是正文")

    return {
        "title": (data.get("title") or url).strip(),
        "author": (data.get("author") or "").strip(),
        "text": text,
        "source": kind,
        "extra": data.get("extra") or {},
    }
