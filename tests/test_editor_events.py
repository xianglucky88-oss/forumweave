"""editor 事件身份/顺序归并 + version 消费与崩溃恢复测试。"""
import sqlite3

import pytest

from src.forumweave import editor, version


# ---- 交错序列夹具 -------------------------------------------------------
# 主体 p-1：快速连续编辑（含匿名）→ 相同正文再提交 → 重复消息 → 迟到撤销；
# 主体 p-2：同刻冲突（时间相同、rev 缺失/冲突），检验暂缓判断标记。
def _interleaved_records():
    return [
        # p-1: 快速连续编辑 A -> B -> C（间隔 10s，归并为一次编辑会话，含匿名）
        {"id": "p-1", "kind": "edit", "event_id": "e1", "ts": "2026-11-10T10:00:00Z",
         "user_id": "alice", "content": "A"},
        {"id": "p-1", "kind": "edit", "event_id": "e2", "ts": "2026-11-10T10:00:10Z",
         "user_id": "", "content": "AB"},  # 匿名编辑
        {"id": "p-1", "kind": "edit", "event_id": "e3", "ts": "2026-11-10T10:00:20Z",
         "user_id": "alice", "content": "ABC"},
        # 相同正文再提交：与当前有效正文相同 => no_op（时间在归并窗口外，独立成事件）
        {"id": "p-1", "kind": "edit", "event_id": "e4", "ts": "2026-11-10T10:01:40Z",
         "user_id": "alice", "content": "ABC"},
        # 重复消息：与 e3 同 event_id（投递重发）
        {"id": "p-1", "kind": "edit", "event_id": "e3", "ts": "2026-11-10T10:00:20Z",
         "user_id": "alice", "content": "ABC"},
        # 撤销之后的新编辑（先于撤销到达）
        {"id": "p-1", "kind": "edit", "event_id": "e6", "ts": "2026-11-10T10:10:00Z",
         "user_id": "carol", "content": "ABCDE"},
        # 迟到撤销：最后到达（到达序号最大），但逻辑时间在 e4 之后、e6 之前
        {"id": "p-1", "kind": "undo", "event_id": "e5", "ts": "2026-11-10T10:02:00Z",
         "user_id": "bob"},
    ]


def _p2_records():
    return [
        {"id": "p-2", "kind": "edit", "event_id": "x1", "ts": "2026-11-11T08:00:00Z",
         "user_id": "u", "content": "one"},
        # 时间完全相同且 rev 冲突/缺失：暂缓判断
        {"id": "p-2", "kind": "edit", "event_id": "x2", "ts": "2026-11-11T08:00:00Z",
         "user_id": "u", "content": "two"},
    ]


def _stabilized(records):
    # 到达序号是事件的固有摄入属性：打乱列表顺序不改变每个事件的 seq，
    # 因此迟到判定与重放结果只取决于事件内容本身。
    indexed = [dict(r, _arrival_seq=i) for i, r in enumerate(records)]
    return editor.stabilize_records(indexed)


# ---- editor：身份、归并、顺序 -------------------------------------------

def test_legacy_record_passthrough():
    # 旧格式记录（只有 id/src/name/value）走 naive 兼容路径，不报错
    rec = {"id": "r-old", "src": "alpha", "name": "Old", "value": 3}
    out = editor.process(rec)
    assert out["_stage_editor"] == "naive"
    assert out["id"] == "r-old"
    assert editor.stabilize_records([rec]) == []


def test_interleaved_sequence_event_count_and_sources():
    stable = _stabilized(_interleaved_records())
    p1 = [e for e in stable if e["subject_id"] == "p-1"]

    # e1/e2/e3 归并为 1 个稳定编辑；e4 独立（no_op）；e3 重复被去重；
    # e5 撤销独立；e6 独立 => 4 个稳定事件
    assert [e["event_id"] for e in p1] == ["e1", "e4", "e5", "e6"]

    merged = p1[0]
    assert merged["coalesced"] is True
    assert merged["members"] == ["e1", "e2", "e3"]
    assert merged["body"] == "ABC"
    # 操作者证据：匿名成员被保留并标记
    assert merged["anonymous"] is True
    assert merged["actor_evidence"]["actors"] == ["alice", None, "alice"]
    # 差异随稳定事件交给 version 消费
    assert merged["diff"] and merged["body_before"] is None

    # 相同正文再提交 => no_op，保留事件但不产生正文变化
    noop = next(e for e in p1 if e["event_id"] == "e4")
    assert noop["no_op"] is True
    assert noop["diff"] == []

    # 重复消息：重复副本不出现在稳定事件里，去重信息可追溯
    assert all("e3" != e["event_id"] or e["coalesced"] for e in p1)
    assert len([e for e in p1 if e["event_id"] == "e3"]) == 0


def test_late_undo_is_placed_by_logical_time():
    stable = _stabilized(_interleaved_records())
    p1 = [e for e in stable if e["subject_id"] == "p-1"]
    undo = next(e for e in p1 if e["kind"] == "undo")
    # 撤销在输入里最后到达，但逻辑时间 10:02 晚于 e4(10:01:40)、早于 e6(10:10)
    assert undo["event_id"] == "e5"
    assert undo["late"] is True
    order = [e["event_id"] for e in p1]
    assert order.index("e4") < order.index("e5") < order.index("e6")
    # 撤销把正文回滚到合并编辑之前（None），并带差异证据
    assert undo["body"] is None
    assert undo["body_before"] == "ABC"
    assert undo["diff"]


def test_equal_timestamp_conflict_is_deferred_but_deterministic():
    stable = _stabilized(_p2_records())
    p2 = [e for e in stable if e["subject_id"] == "p-2"]
    assert len(p2) == 2
    assert all(e["order_deferred"] for e in p2)  # 同刻且 rev 缺失：暂缓判断
    # 兜底总顺序确定：event_id 字典序
    assert [e["event_id"] for e in p2] == ["x1", "x2"]
    # 打乱到达顺序重放（保留每个事件固有的 _arrival_seq），稳定事件身份不变
    shuffled = [dict(r, _arrival_seq=i) for i, r in
                reversed(list(enumerate(_p2_records())))]
    again = editor.stabilize_records(shuffled)
    assert [e["event_id"] for e in again] == [e["event_id"] for e in stable]


def test_replay_does_not_change_final_version():
    records = [dict(r, _arrival_seq=i) for i, r in
               enumerate(_interleaved_records() + _p2_records())]
    view1 = version.build_all_subjects(editor.stabilize_records(records))
    # 任意打乱输入顺序、重复重放；事件自身的 _arrival_seq 不随排列变化
    import random
    rng = random.Random(7)
    shuffled = records[:]
    rng.shuffle(shuffled)
    view2 = version.build_all_subjects(editor.stabilize_records(shuffled))
    view3 = version.build_all_subjects(editor.stabilize_records(records))
    assert view1 == view2 == view3

    p1 = view1["p-1"]
    # 版本只来自产生正文变化的事件：合并编辑、撤销、e6（e4 no_op、重复被跳过）
    assert [v["event_id"] for v in p1["versions"]] == ["e1", "e5", "e6"]
    assert p1["final_body"] == "ABCDE"
    skipped_reasons = {s["event_id"]: s["reason"] for s in p1["skipped"]}
    assert skipped_reasons.get("e4") == "no_op"
    assert skipped_reasons.get("e3") == "duplicate"
    # 版本携带操作者证据与差异
    first = p1["versions"][0]
    assert first["actor"] == "alice"
    assert first["actor_evidence"]["actors"] == ["alice", None, "alice"]
    assert first["diff"]


def test_anonymous_edits_keep_actor_evidence():
    stable = _stabilized(_interleaved_records())
    anon = [e for e in stable if e["anonymous"]]
    assert anon
    # 匿名事件 actor 归一为 None，但原始操作者证据完整保留在归并事件里
    e2_member = next(e for e in stable if e["event_id"] == "e1")
    nested = e2_member["actor_evidence"]["actor_evidence"]
    assert nested[1]["raw"] == ""          # 匿名成员的原始 actor 证据
    assert e2_member["actor_evidence"]["actors"] == ["alice", None, "alice"]
    assert all("src" in item for item in nested)  # 每条证据都带来源


# ---- version 阶段入口与旧格式兼容 ---------------------------------------

def test_version_process_handles_event_and_legacy():
    legacy = {"id": "r-1", "src": "x", "name": "n", "value": 1}
    assert version.process(legacy)["_stage_version"] == "naive"

    events = editor.stabilize_records(_interleaved_records())
    out = version.process(events[0])
    assert out["_stage_version"] == "versioned"
    assert out["_version_view"]["subject_id"] == "p-1"


# ---- 崩溃恢复：事件已入库、版本未确认时中断 -----------------------------

def _fresh_con():
    con = sqlite3.connect(":memory:")
    version.ensure_tables(con)
    return con


def test_crash_between_ingest_and_confirm_no_second_edit():
    con = _fresh_con()
    stable = editor.stabilize_records(_interleaved_records())

    # 第一段：事件入库
    version.ingest_events(con, stable)
    n_ledger = con.execute("SELECT COUNT(*) FROM edit_event_ledger").fetchone()[0]
    assert n_ledger == len(stable)
    assert con.execute("SELECT COUNT(*) FROM edit_versions").fetchone()[0] == 0

    # 中断点：事件已是 pending，但版本尚未确认
    crashed = version.confirm_versions(con, subject_id="p-1", crash_after_ingest=True)
    assert crashed["crashed"] is True
    assert con.execute("SELECT COUNT(*) FROM edit_versions").fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM edit_event_ledger WHERE status='pending'").fetchone()[0] == n_ledger

    # 恢复：重放 pending，得到与一次成功完全相同的版本
    recovered = version.recover_pending(con, subject_id="p-1")
    assert recovered["crashed"] is False
    assert [v["event_id"] for v in recovered["versions"]] == ["e1", "e5", "e6"]
    assert recovered["final_body"] == "ABCDE"

    rows = con.execute(
        "SELECT version_no, event_id FROM edit_versions ORDER BY version_no").fetchall()
    assert [r[1] for r in rows] == ["e1", "e5", "e6"]

    # 再次恢复/重复确认：不产生第二次编辑，版本数量与正文不变
    again = version.recover_pending(con, subject_id="p-1")
    assert again["versions"] == []
    assert con.execute("SELECT COUNT(*) FROM edit_versions").fetchone()[0] == 3
    assert con.execute(
        "SELECT COUNT(*) FROM edit_event_ledger WHERE status='confirmed'").fetchone()[0] == n_ledger


def test_ingest_is_idempotent_on_duplicate_delivery():
    con = _fresh_con()
    stable = editor.stabilize_records(_interleaved_records())
    version.ingest_events(con, stable)
    # 整批重投（消息系统 at-least-once）
    version.ingest_events(con, stable)
    assert con.execute("SELECT COUNT(*) FROM edit_event_ledger").fetchone()[0] == len(stable)


def test_recovery_matches_one_shot_confirmation():
    records = [dict(r, _arrival_seq=i) for i, r in
               enumerate(_interleaved_records() + _p2_records())]
    expected = version.build_all_subjects(editor.stabilize_records(records))

    con = _fresh_con()
    version.ingest_events(con, editor.stabilize_records(records))
    version.confirm_versions(con, crash_after_ingest=True)  # 模拟中断
    version.recover_pending(con)
    got = version.build_all_subjects(editor.stabilize_records(records))
    # 恢复后的版本链与纯函数一次重放一致
    for sid, view in expected.items():
        db_rows = con.execute(
            "SELECT data FROM edit_versions WHERE subject_id=? ORDER BY version_no", (sid,)).fetchall()
        import json
        db_versions = [json.loads(r[0]) for r in db_rows]
        assert [v["event_id"] for v in db_versions] == [v["event_id"] for v in view["versions"]]
    assert got == expected
