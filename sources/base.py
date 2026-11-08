"""共享工具：HTTP 头、墙特征、真浏览器采集。

所有抓取器只依赖这里的两个东西：`HEADERS` 和 `browser()`。
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

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


def browser(url, body, wait=3000, space_prefix="kb"):
    """用 ego-browser 打开真实 Chromium，执行 body 里的 JS，把结果读回来。

    `body` 是一段给页面用的 JavaScript，必须把结果赋给 `data`：
        data = await page.evaluate(() => ({ ... }))
    返回值是那个对象的 dict。
    """
    space = f"{space_prefix}-{abs(hash(url)) % 10**8}"

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
