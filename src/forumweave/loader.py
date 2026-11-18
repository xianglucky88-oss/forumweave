"""单一职责：从本地 fixtures 读取原始记录（离线摄取入口，不联网）。

记录带 ``page_file`` 时，把引用的离线页面以原始字节挂到 ``html_bytes``
（保留字节是为了让 thread_parser 处理编码异常）；路径必须留在 fixtures
目录内，防止 ``../`` 逃逸。"""
import json
from .config import FIXTURES

def load():
    with open(FIXTURES, encoding="utf-8") as f:
        records = json.load(f)
    base = FIXTURES.parent.resolve()
    for rec in records:
        page_file = rec.get("page_file")
        if not page_file:
            continue
        path = (base / page_file).resolve()
        if not path.is_relative_to(base):
            raise ValueError(f"page_file escapes fixtures dir: {page_file}")
        rec["html_bytes"] = path.read_bytes()
    return records
