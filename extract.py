"""兼容层：实现已拆到 `sources/` 包，一个来源一个文件。

保留这个文件是为了让 `import extract` 的老代码继续能用。
"""

from sources import fetch, route  # noqa: F401

__all__ = ["fetch", "route"]
