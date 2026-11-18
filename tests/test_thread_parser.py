"""thread_parser 离线页面解析测试。

覆盖：引用块与正文分离、跨页引用、折叠块/分页尾部/嵌套损坏/编码异常、
重复抓取去重、局部重试的恢复范围、页面改版前后的输出对比。"""
from pathlib import Path

from src.forumweave import thread_parser as tp

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "pages"


def parse(name, **extra):
    rec = {"id": name, "page_file": f"pages/{name}",
           "html_bytes": (PAGES / name).read_bytes()}
    rec.update(extra)
    return tp.process(rec)


def strip_pos(posts):
    return [{k: v for k, v in p.items() if k != "pos"} for p in posts]


# ---------------------------------------------------------- 引用块 vs 正文

def test_quote_separated_from_body_no_extra_post():
    out = parse("thread_p1.html")
    posts = out["posts"]
    # 5 个真实帖子；2 楼的引用块不得升格为第 6 个帖子
    assert len(posts) == 5
    p2 = posts[1]
    assert p2["body"] == "支持，先把引用和正文拆开。"
    assert "楼主开工" not in p2["body"]
    assert len(p2["quotes"]) == 1
    q = p2["quotes"][0]
    assert q["author"] == "张三"
    assert "楼主开工" in q["text"]
    assert q["ref"] == {"post": "t1-p1"}
    # 引用块同时提供父回复线索
    assert p2["parent_hint"] == {"post": "t1-p1", "via": "quote"}


def test_parent_hint_from_reply_to_resolves_floor():
    posts = parse("thread_p1.html")["posts"]
    p3 = posts[2]
    # “回复 #2”解析为楼层线索，并解析成本页 post_id
    assert p3["parent_hint"] == {"floor": 2, "post": "t1-p2"}
    assert p3["body"] == "同意二楼，另外分页尾部别算成帖子。"
    assert p3["author"] == {"name": "王五", "uid": "u03"}


def test_cross_page_quote_and_reply_clues():
    out = parse("thread_p2.html")
    assert len(out["posts"]) == 3
    p6 = out["posts"][0]
    # 跨页引用：引用对象在第 1 页，只保留线索，不在本页造帖
    assert p6["quotes"][0]["ref"] == {"post": "t1-p1", "page": 1}
    assert p6["parent_hint"] == {"post": "t1-p1", "page": 1, "via": "quote"}
    p7 = out["posts"][1]
    # “回复 第1页 #3”跨页，无法解析成本页 post_id，保留页+楼层线索
    assert p7["parent_hint"] == {"page": 1, "floor": 3}


# ---------------------------------------------------------- 折叠 vs 真空

def test_collapsed_not_loaded_vs_genuinely_empty():
    posts = parse("thread_p1.html")["posts"]
    p4, p5 = posts[3], posts[4]
    # 折叠块：有 collapsed/lazy 标记 → 内容未随页面下发，应局部重试
    assert p4["body"] == ""
    assert p4["empty_reason"] == "collapsed_not_loaded"
    # 正文容器完整渲染但为空白 → 确实为空
    assert p5["body"] == ""
    assert p5["empty_reason"] == "empty"


def test_pagination_tail_is_not_a_post():
    out = parse("thread_p1.html")
    assert out["page_meta"] == {"page": 1, "total_pages": 2,
                                "next": "thread_p2.html"}
    assert all("下一页" not in p["body"] for p in out["posts"])
    assert [p["floor"] for p in out["posts"]] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------- 重复抓取

def test_repeat_fetch_deterministic_no_duplicate_posts():
    a = parse("thread_p1.html")
    b = parse("thread_p1.html")
    assert a["posts"] == b["posts"]
    assert a["deduped"] == 0
    # 抓取故障把同一页内容拼接了两次：节点数量不得翻倍
    dup = parse("thread_p1_dup.html")
    assert dup["deduped"] == 5
    assert strip_pos(dup["posts"]) == strip_pos(a["posts"])


# ---------------------------------------------------------- 嵌套损坏 + 局部重试

def test_broken_nesting_healed_and_fatal_block_diagnosed():
    out = parse("thread_broken.html")
    # 可恢复的嵌套损坏被修复：作者/正文/引用各归其位
    assert len(out["posts"]) == 1
    p = out["posts"][0]
    assert p["author"] == {"name": "老钱", "uid": "u11"}
    assert p["body"] == "正文嵌套坏了 斜体没闭合"
    assert [q["text"] for q in p["quotes"]] == ["引用跨边界"]
    # 致命区块（无正文容器且无折叠标记）单独记入诊断，不拖垮整页
    assert len(out["parse_errors"]) == 1
    err = out["parse_errors"][0]
    assert err["kind"] == "missing-body"
    assert err["block"] == 1
    # 诊断指回失败区块：offset 落在该区块起始标签处
    html = out["html"]
    assert html[err["offset"]:err["offset"] + 5] == "<div "
    assert "post-b2" in html[err["offset"]:err["offset"] + 60]
    assert "post-b2" in err["excerpt"]


FIXED_B2 = ('<div class="post" id="post-b2" data-post-id="t9-p2">'
            '<span class="author" data-uid="u12">小孙</span>'
            '<div class="body">补抓到的正文</div></div>')


def test_local_retry_scope_restores_only_failed_block():
    out = parse("thread_broken.html")
    out2 = tp.process({**out, "retry": {"blocks": {"1": FIXED_B2}}})
    assert out2["retried"] == [1]
    assert len(out2["posts"]) == 2          # 节点数量恢复
    assert out2["parse_errors"] == []       # 对应诊断被清除
    assert out2["posts"][0] == out["posts"][0]   # 旧输出原样保留
    assert out2["posts"][1]["body"] == "补抓到的正文"
    assert out2["posts"][1]["pos"]["block"] == 1
    assert out2["posts"][1]["pos"]["via"] == "retry"


def test_retry_without_reparse_and_still_failing_fragment():
    out = parse("thread_broken.html")
    slim = {"posts": out["posts"], "parse_errors": out["parse_errors"],
            "page_file": "pages/thread_broken.html"}
    ok = tp.process({**slim, "retry": {"blocks": {"1": FIXED_B2}}})
    assert len(ok["posts"]) == 2 and ok["parse_errors"] == []
    # 重试片段仍然缺正文：诊断重新生成，其余区块不受影响
    bad = tp.process({**slim, "retry": {"blocks": {
        "1": '<div class="post" data-post-id="t9-p2">'
             '<span class="author">小孙</span></div>'}}})
    assert len(bad["posts"]) == 1
    assert bad["parse_errors"][0]["kind"] == "missing-body"
    assert bad["parse_errors"][0]["block"] == 1


def test_collapsed_block_retry_after_partial_load():
    out = parse("thread_p1.html")
    frag = ('<div class="post" id="post-4" data-post-id="t1-p4">'
            '<span class="author" data-uid="u04">赵六</span>'
            '<div class="body">折叠内容补抓成功</div></div>')
    out2 = tp.process({**out, "retry": {"blocks": {"3": frag}}})
    assert len(out2["posts"]) == 5          # 同键替换，不制造重复帖子
    p4 = out2["posts"][3]
    assert p4["body"] == "折叠内容补抓成功"
    assert p4["empty_reason"] is None


# ---------------------------------------------------------- 编码异常

def test_encoding_anomaly_falls_back_and_flags():
    out = parse("thread_gbk.html", declared_charset="utf-8")
    enc = out["encoding"]
    assert enc["declared"] == "utf-8"
    assert enc["used"] == "gb18030"         # 声明 utf-8 失败，回退 gb18030
    assert enc["anomaly"] is True
    assert len(out["posts"]) == 2           # 不乱码也不丢帖
    assert out["posts"][0]["author"]["name"] == "编码用户"
    assert "GBK" in out["posts"][0]["body"]


# ---------------------------------------------------------- 页面改版

def test_redesign_yields_same_logical_posts():
    v1 = parse("thread_p1.html")
    v2 = parse("thread_p1_v2.html")
    # 改版前后：节点数量与旧输出（除源位置外）一致
    assert len(v2["posts"]) == len(v1["posts"]) == 5
    assert strip_pos(v2["posts"]) == strip_pos(v1["posts"])
    assert v2["page_meta"] == v1["page_meta"]
    assert v2["parse_errors"] == v1["parse_errors"] == []


# ---------------------------------------------------------- 透传与位置

def test_non_page_record_passthrough():
    rec = tp.process({"id": "r-x", "name": "n"})
    assert rec["_stage_thread_parser"] == tp.PARSER_VERSION
    assert "posts" not in rec


def test_source_positions_preserved():
    out = parse("thread_p1.html")
    for i, p in enumerate(out["posts"]):
        assert p["pos"]["page"] == "pages/thread_p1.html"
        assert p["pos"]["block"] == i
        assert out["html"][p["pos"]["offset"]:p["pos"]["offset"] + 5] == "<div "
