import os

from src.forumweave import pipeline, store
from src.forumweave.config import DB_PATH


def _fresh_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


def test_pipeline_runs_and_idempotent():
    _fresh_db()
    a = pipeline.run()
    b = pipeline.run()   # 重跑不增行（幂等 upsert）
    assert a["fed"] == 10
    assert b["rows"] == a["rows"] == 8   # r-003 与 page-t1-p1 的重复抓取被去重


def test_store_roundtrip():
    con = store.connect()
    try:
        assert store.count(con) >= 0
    finally:
        con.close()


def test_page_records_carry_parsed_posts_through_pipeline():
    _fresh_db()
    pipeline.run()
    con = store.connect()
    try:
        data = dict(store.all_records(con))
    finally:
        con.close()
    # 正文/作者线索/父回复提示/源页面位置沿流水线落库
    p1 = data["page-t1-p1"]
    assert len(p1["posts"]) == 5
    assert p1["parse_errors"] == []
    p2 = p1["posts"][1]
    assert p2["body"] == "支持，先把引用和正文拆开。"
    assert p2["quotes"][0]["ref"] == {"post": "t1-p1"}
    assert p2["parent_hint"] == {"post": "t1-p1", "via": "quote"}
    assert p2["pos"]["page"] == "pages/thread_p1.html"
    # 同一页面重复抓取：同一主键幂等覆盖，不制造重复帖子
    assert len(data["page-t1-p1"]["posts"]) == 5
    # 失败区块的诊断随记录落库，并能指回失败区块
    broken = data["page-t9-broken"]
    assert len(broken["posts"]) == 1
    err = broken["parse_errors"][0]
    assert err["kind"] == "missing-body"
    assert broken["html"][err["offset"]:].startswith(
        '<div class="post" id="post-b2"')
