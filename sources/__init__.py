"""按域名分派提取器。

每个模块只暴露两个函数：`match(url)` 和 `fetch(url)`。
新增一个源 = 新建一个文件 + 在下面的 `_SOURCES` 里加一行，不用改这里。

设计原则只有一条：**入库的必须是一手原文**，任何环节都不许模型重写正文。
浏览器路径统一走 ego-browser，它用的是本机真实的 Chromium 和登录态。
"""

from . import arxiv, github, huggingface, wechat, weibo, web, x, xiaohongshu, zhihu
from .base import WALL_MARKERS

# 顺序即优先级；没有 match 的 web 作为兜底，不参与匹配
_SOURCES = (x, zhihu, weibo, wechat, github, huggingface, arxiv, xiaohongshu)


def _name(module):
    return module.__name__.rsplit(".", 1)[-1]


def route(url):
    for module in _SOURCES:
        if module.match(url):
            return _name(module)
    return "web"


def _module_for(url):
    for module in _SOURCES:
        if module.match(url):
            return module
    return web


def fetch(url):
    """统一入口：返回 {title, author, text, source, extra}。"""
    module = _module_for(url)
    kind = _name(module)
    data = module.fetch(url)

    text = data["text"]

    # 抓到了但其实是墙：宁可判失败，也不要把页脚和法律声明存成"文章"
    if kind in ("web", "wechat", "xiaohongshu", "weibo"):
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
