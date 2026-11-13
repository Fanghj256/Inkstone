"""知识库维护命令。

  python manage.py drop <路径|URL> [--reason "为什么不要"]   删除一篇 raw 原文，并留审计记录
  python manage.py check                                     体检：raw 改动、失效链接、frontmatter
  python manage.py rebuild-html <文件夹|--all>                重压视频的 note.html（派生文件）
  python manage.py install-hooks                             让 raw/ 只读钩子生效

删除 raw 不要直接在编辑器里删：这个命令会 git rm、记一笔 dropped.md、再提交，
以后想找回来还能从 git 历史里捞。
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import store
import tg

RAW = Path("002-references") / "raw"
DROP_LOG = Path("002-references") / "dropped.md"
FRONTMATTER_KEYS = ("url", "title", "source", "fetched_at", "chars")


def kb_root(value=None):
    if value:
        return Path(value).expanduser().resolve()
    raw = os.environ.get("KB_ROOT", "").strip()
    if not raw:
        sys.exit("`.env` 里没有 KB_ROOT；或者用 --kb 指定知识库目录。")
    return Path(raw).expanduser().resolve()


def git(kb, *args):
    return subprocess.run(
        ["git", "-C", str(kb), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _frontmatter_title(text):
    if not text.startswith("---"):
        return ""
    block = text.split("---", 2)[1]
    m = re.search(r"^title:\s*(.+)$", block, re.M)
    return m.group(1).strip().strip("'\"") if m else ""


def title_of(kb, path, rel):
    if path.exists():
        return _frontmatter_title(path.read_text(encoding="utf-8", errors="replace")[:1000])
    shown = git(kb, "show", f"HEAD:{rel.as_posix()}")
    if shown.returncode == 0:
        return _frontmatter_title(shown.stdout[:2000])
    return path.stem


def resolve(kb, target):
    if target.startswith(("http://", "https://")):
        found = store.find_existing(kb, target)
        if not found:
            sys.exit(f"库里没有这个链接：{target}")
        return found

    path = Path(target)
    if not path.is_absolute():
        path = kb / path
    return path.resolve()


def append_drop_log(kb, rel, title, reason):
    log = kb / DROP_LOG
    if not log.exists():
        store.atomic_write_text(
            log,
            "# 已移除的原文\n\n"
            "> 这里只记录“删了什么、为什么”。原文还在 git 历史里，需要时用\n"
            "> `git log -- 002-references/raw/<文件>` 找回来。\n\n"
            "| 日期 | 文件 | 标题 | 原因 |\n|---|---|---|---|\n",
        )

    clean = lambda s: str(s or "").replace("|", "\\|").replace("\n", " ")
    line = f"| {datetime.now().strftime('%Y-%m-%d')} | `{rel.as_posix()}` | {clean(title)} | {clean(reason)} |\n"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return log


def cmd_drop(args):
    kb = kb_root(args.kb)
    path = resolve(kb, args.target)
    try:
        rel = path.relative_to(kb)
    except ValueError:
        sys.exit("只能删除知识库目录里的文件。")
    if rel.parts[:2] != RAW.parts:
        sys.exit(f"只能删除 {RAW.as_posix()}/ 下的原文；其他目录请直接用 git。")

    title = title_of(kb, path, rel)
    if path.exists():
        r = git(kb, "rm", "--quiet", "--", rel.as_posix())
    else:
        # 文件已经从磁盘删掉了：把删除动作补进暂存区
        r = git(kb, "rm", "--cached", "--quiet", "--ignore-unmatch", "--", rel.as_posix())
        git(kb, "add", "-A", "--", rel.as_posix())
    if r.returncode != 0:
        sys.exit(f"git rm 失败：{(r.stderr or r.stdout).strip()}")

    log = append_drop_log(kb, rel, title, args.reason)
    git(kb, "add", "--", log.relative_to(kb).as_posix())

    c = git(kb, "commit", "--no-verify", "-m", f"drop: {title}")
    if c.returncode != 0:
        print((c.stderr or c.stdout).strip())
        sys.exit("提交失败；删除和日志已经暂存，修好后手动 commit。")

    print(f"已移除：{rel.as_posix()}")
    print(f"已记录：{log.relative_to(kb).as_posix()}")

    if os.environ.get("AUTO_PUSH", "1") != "0" and store._has_remote(kb):
        store._push(kb)
        print("已推送到远端（失败会在下一次提交时补）。")


def _parse_ts(value):
    parts = [int(x) for x in value.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def _transcript_paragraphs(path):
    rows = []
    for block in path.read_text(encoding="utf-8", errors="replace").split("\n\n"):
        m = re.match(r"\[((?:\d{2}:)?\d{2}:\d{2})\]\s*(.*)", block.strip(), re.S)
        if m:
            rows.append({"start": float(_parse_ts(m.group(1))), "text": m.group(2).strip()})
    return rows


def _rebuild_one(kb, folder):
    import video

    meta_path = folder / "meta.json"
    transcript = folder / "transcript.txt"
    if not meta_path.exists() or not transcript.exists():
        print(f"跳过（缺 meta.json 或 transcript.txt）：{folder.name}")
        return False

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    paragraphs = _transcript_paragraphs(transcript)
    if not paragraphs:
        print(f"跳过（transcript.txt 解析不出段落）：{folder.name}")
        return False

    meta["_folder"] = folder
    html = video.build_html(meta, paragraphs, meta.get("frame_list") or [])
    store.atomic_write_text(folder / "note.html", html)

    git(kb, "add", "--", (folder / "note.html").relative_to(kb).as_posix())
    c = git(kb, "commit", "--no-verify", "-m", f"chore: 重压 {meta.get('title', folder.name)} 的 note.html")
    if c.returncode != 0:
        print((c.stderr or c.stdout).strip())
        return False

    size = (folder / "note.html").stat().st_size
    print(f"已重压：{folder.name} -> {size / 1024 / 1024:.2f} MB")
    return True


def cmd_rebuild_html(args):
    kb = kb_root(args.kb)
    root = kb / RAW / "video"
    if not root.is_dir():
        sys.exit(f"没有视频目录：{root}")

    if args.all:
        folders = sorted(p for p in root.iterdir() if p.is_dir())
    elif args.target:
        candidates = [p for p in root.iterdir() if p.is_dir() and args.target in p.name]
        if len(candidates) != 1:
            sys.exit(f"匹配到 {len(candidates)} 个视频文件夹，请写得更具体。")
        folders = candidates
    else:
        sys.exit("给一个视频文件夹名，或者用 --all。")

    push = os.environ.get("AUTO_PUSH", "1") != "0" and store._has_remote(kb)
    changed = sum(_rebuild_one(kb, folder) for folder in folders)
    if changed and push:
        store._push(kb)
    print(f"完成：{changed}/{len(folders)} 个")


def cmd_check(args):
    kb = kb_root(args.kb)
    problems = 0

    print("== raw 暂存区 ==")
    changed = git(kb, "diff", "--cached", "--name-status", "--diff-filter=MDR",
                  "--", RAW.as_posix())
    if changed.stdout.strip():
        print(changed.stdout.strip())
        print("raw/ 里只允许新增；修改或删除请用 `python manage.py drop`。")
        problems += 1
    else:
        print("干净")

    worktree = git(kb, "diff", "--name-status", "--diff-filter=MDR", "--", RAW.as_posix())
    if worktree.stdout.strip():
        print("\n== raw 工作区（未暂存）==")
        print(worktree.stdout.strip())
        print("有意删除：用 `python manage.py drop <路径>` 收尾；误删：`git checkout -- <路径>`。")
        problems += 1

    print("\n== 失效的相对链接 ==")
    link_re = re.compile(r"\]\(([^)\s]+)\)")
    for md in kb.rglob("*.md"):
        if ".git" in md.parts or md.is_relative_to(kb / RAW):
            continue          # raw 里是抓来的 README，链接指向原作者仓库，不算库内失效
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in link_re.finditer(text):
            target = m.group(1).split("#")[0]
            if not target or target.startswith(("http://", "https://", "mailto:", "data:")):
                continue
            if not (md.parent / target).resolve().exists():
                print(f"{md.relative_to(kb)} -> {target}")
                problems += 1

    print("\n== raw frontmatter ==")
    bad = 0
    for md in (kb / RAW).rglob("*.md"):
        if md.name == "README.md":
            continue
        head = md.read_text(encoding="utf-8", errors="replace")[:800]
        if not head.startswith("---"):
            print(f"缺 frontmatter：{md.relative_to(kb)}")
            bad += 1
            continue
        block = head.split("---", 2)[1]
        missing = [k for k in FRONTMATTER_KEYS if not re.search(rf"^{k}:", block, re.M)]
        if missing:
            print(f"缺 {','.join(missing)}：{md.relative_to(kb)}")
            bad += 1
    print(f"{bad} 个文件有问题" if bad else "干净")
    problems += bad

    print(f"\n共 {len(list((kb / RAW).rglob('*.md')))} 篇 raw 文档，{problems} 个待处理项。")
    return 1 if problems else 0


def cmd_install_hooks(args):
    kb = kb_root(args.kb)
    hook = kb / ".githooks" / "pre-commit"
    if not hook.exists():
        sys.exit(f"找不到 {hook}；先把这个文件放进知识库。")
    r = git(kb, "config", "core.hooksPath", ".githooks")
    if r.returncode != 0:
        sys.exit((r.stderr or r.stdout).strip())
    print("已启用 .githooks/pre-commit：raw/ 只允许新增。")
    print("要临时绕过：ALLOW_RAW_EDIT=1 git commit ...")


def main():
    parser = argparse.ArgumentParser(description="知识库维护命令")
    parser.add_argument("--kb", help="知识库根目录（默认读 .env 的 KB_ROOT）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("drop", help="删除一篇 raw 原文并记录")
    p.add_argument("target", help="知识库内路径，或抓取时的 URL")
    p.add_argument("--reason", default="手动清理", help="删除原因")
    p.set_defaults(func=cmd_drop)

    p = sub.add_parser("check", help="体检 raw 改动、失效链接、frontmatter")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("rebuild-html", help="重压视频的 note.html（只改这个派生文件）")
    p.add_argument("target", nargs="?", help="视频文件夹名的一部分")
    p.add_argument("--all", action="store_true", help="重压全部视频")
    p.set_defaults(func=cmd_rebuild_html)

    p = sub.add_parser("install-hooks", help="启用 raw 只读 pre-commit 钩子")
    p.set_defaults(func=cmd_install_hooks)

    args = parser.parse_args()
    tg.load_env()
    raise SystemExit(args.func(args) or 0)


if __name__ == "__main__":
    main()
