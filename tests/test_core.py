"""纯函数与落盘测试：不联网、不碰真实知识库。"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import manage  # noqa: E402
import store  # noqa: E402

try:
    import channels  # noqa: E402
except Exception:  # 没装视频依赖时跳过通道测试
    channels = None


class StoreTest(unittest.TestCase):
    def test_canonical_strips_tracking_and_fragment(self):
        url = "HTTPS://Example.COM/a?utm_source=x&id=7&spm=abc#part"
        self.assertEqual(store.canonical(url), "https://example.com/a?id=7")

    def test_url_hash_is_stable(self):
        a = "https://example.com/a?utm_source=x"
        b = "https://example.com/a"
        self.assertEqual(store.url_hash(a), store.url_hash(b))

    def test_slugify_removes_windows_chars(self):
        self.assertEqual(store.slugify('a/b:c*?"<>|d'), "a-b-c-d")

    def test_md_document_has_frontmatter_and_heading(self):
        text = store.md_document({"title": "T", "chars": 1}, "body", heading="T")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("\n# T\n", text)
        self.assertTrue(text.endswith("body\n"))

    def test_write_note_uses_source_dir_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp)
            meta = {
                "url": "https://example.com/a",
                "title": "Hello",
                "source": "web",
                "chars": 4,
            }
            path = store.write_note(kb, meta, "body")
            self.assertEqual(path.parent, kb / "002-references" / "raw" / "web")
            self.assertTrue(path.name.endswith(f"-{store.url_hash(meta['url'])}.md"))
            self.assertIn("title: Hello", path.read_text(encoding="utf-8"))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_save_file_blocks_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp)
            path = store.save_file(kb, r"..\..\evil.md", b"x")
            self.assertEqual(path.parent, kb / "002-references" / "files")
            self.assertEqual(path.name, "evil.md")


@unittest.skipIf(channels is None, "视频依赖未安装")
class ChannelTest(unittest.TestCase):
    class Fake:
        name = "fake"

        def __init__(self):
            self.messages = []

        def send_text(self, chat_id, text):
            self.messages.append(text)

        def send_file(self, chat_id, path, caption=""):
            self.messages.append(f"FILE:{Path(path).name}")

    def test_split_prefix(self):
        self.assertEqual(channels.split_prefix("/note 你好")[0], "003-notes")
        self.assertEqual(channels.split_prefix("普通文字")[0], None)

    def test_video_receipt_points_to_learn(self):
        text = channels.video_receipt(
            {"title": "T", "source": "video", "duration_hms": "01:00",
             "chars": 12, "frames": 3, "video_id": "BV1"},
            Path("raw/video/20260101-T-abc"),
        )
        self.assertIn("/learn BV1", text)
        self.assertIn("截图 3 张", text)

    def test_handle_text_saves_to_inbox_and_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp)
            ch = self.Fake()
            channels.handle_text(kb, ch, 1, "随手记一句")
            self.assertEqual(len(list((kb / "001-inbox").glob("*.md"))), 1)

            channels.handle_text(kb, ch, 1, "/note 一条笔记")
            self.assertEqual(len(list((kb / "003-notes").glob("*.md"))), 1)
            self.assertTrue(any("已存入 003-notes" in m for m in ch.messages))


class ManageTest(unittest.TestCase):
    def _repo(self, tmp):
        kb = Path(tmp)
        subprocess.run(["git", "init", "-q", str(kb)], check=True)
        subprocess.run(["git", "-C", str(kb), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(kb), "config", "user.name", "t"], check=True)
        raw = kb / "002-references" / "raw" / "web"
        raw.mkdir(parents=True)
        doc = raw / "20260101-x-abcdef12.md"
        doc.write_text(
            "---\nurl: https://example.com/x\ntitle: 测试文章\nsource: web\n"
            "fetched_at: '2026-01-01T00:00:00+08:00'\nchars: 10\n---\n\n正文\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(kb), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(kb), "commit", "-qm", "init"], check=True)
        return kb, doc

    def test_drop_removes_and_logs(self):
        os.environ["AUTO_PUSH"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            kb, doc = self._repo(tmp)
            args = type("A", (), {
                "kb": str(kb),
                "target": str(doc),
                "reason": "不想要了",
            })()
            manage.cmd_drop(args)
            self.assertFalse(doc.exists())
            log = (kb / "002-references" / "dropped.md").read_text(encoding="utf-8")
            self.assertIn("测试文章", log)
            self.assertIn("不想要了", log)
            subject = subprocess.run(
                ["git", "-C", str(kb), "log", "-1", "--format=%s"],
                capture_output=True, text=True, encoding="utf-8",
            ).stdout.strip()
            self.assertEqual(subject, "drop: 测试文章")

    def test_drop_already_deleted_file(self):
        os.environ["AUTO_PUSH"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            kb, doc = self._repo(tmp)
            doc.unlink()
            args = type("A", (), {
                "kb": str(kb),
                "target": str(doc),
                "reason": "VSCode 里删过了",
            })()
            manage.cmd_drop(args)
            log = (kb / "002-references" / "dropped.md").read_text(encoding="utf-8")
            self.assertIn("测试文章", log)
            subject = subprocess.run(
                ["git", "-C", str(kb), "log", "-1", "--format=%s"],
                capture_output=True, text=True, encoding="utf-8",
            ).stdout.strip()
            self.assertEqual(subject, "drop: 测试文章")


if __name__ == "__main__":
    unittest.main()
