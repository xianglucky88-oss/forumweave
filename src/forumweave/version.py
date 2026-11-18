"""单一职责：消费 editor 整理好的稳定编辑事件，重放出确定性版本链（version 阶段）。

职责边界：
* 不重新判断事件身份与顺序——那是 ``editor`` 的职责；本模块只消费稳定事件上的
  ``diff``（正文差异）与 ``actor`` / ``actor_evidence`` / ``src``（操作者证据）。
* ``build_versions`` 是纯函数：同一组稳定事件任意次、任意顺序重放，版本链与
  最终正文完全一致（``no_op`` / 重复事件不产生新版本）。
* ``process`` 是流水线阶段入口：标准事件直接产出版本视图；旧格式（naive）记录
  原样透传，保持兼容。

崩溃恢复（事件已入库但版本尚未确认）：
* ``ingest_events`` 把事件写入 ledger（幂等 upsert，重复投递不产生第二条）；
* ``confirm_versions`` 在同一事务内把待处理事件重放成版本并标记 confirmed；
* 若两步之间中断，重跑 ``recover_pending`` 只会把仍是 pending 的事件再重放一次，
  版本号、最终正文与一次成功完全相同——恢复不产生第二次编辑。
"""
from __future__ import annotations

import json

from . import editor as _editor

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS edit_event_ledger(
    event_id   TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    data       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (event_id, subject_id)
)
"""
VERSION_DDL = """
CREATE TABLE IF NOT EXISTS edit_versions(
    subject_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    event_id   TEXT NOT NULL,
    data       TEXT NOT NULL,
    PRIMARY KEY (subject_id, version_no)
)
"""


# ---- 纯函数：稳定事件 → 版本链 ------------------------------------------

def build_versions(stable_events, subject_id=None):
    """按稳定事件重放出版本链。

    返回 ``{"subject_id", "versions", "final_body", "skipped"}``：
    * ``versions`` 为每个产生正文变化的事件建一个版本，携带差异与操作者证据；
    * 撤销（kind=undo）同样按重放结果建版本（含 undo_of 证据）；
    * ``no_op``、重复事件（duplicate_of 非空）只计入 skipped，不建版本。
    """
    events = [e for e in stable_events if isinstance(e, dict)
              and e.get("payload_version") == _editor.EVENT_PAYLOAD_VERSION]
    if subject_id is not None:
        events = [e for e in events if e.get("subject_id") == subject_id]
    # 防御性定序：正常情况下 editor 已排好，这里保证重放不依赖输入排列。
    events = sorted(events, key=_editor._sort_key)

    versions = []
    skipped = []
    current_body = None
    for ev in events:
        # editor 的去重台账：重复消息不进入版本链，仅计入 skipped。
        for dup in ev.get("duplicates") or []:
            skipped.append({"event_id": dup["event_id"],
                            "reason": "duplicate",
                            "duplicate_of": dup["duplicate_of"]})
        if ev.get("kind") == "duplicates":
            continue
        if ev.get("no_op"):
            skipped.append({"event_id": ev["event_id"], "reason": "no_op"})
            continue
        current_body = ev.get("body")
        versions.append({
            "subject_id": ev.get("subject_id"),
            "version_no": len(versions) + 1,
            "event_id": ev["event_id"],
            "kind": ev["kind"],
            "ts": ev["ts"],
            "rev": ev["rev"],
            "body": ev.get("body"),
            "body_before": ev.get("body_before"),
            "diff": ev.get("diff"),
            "actor": ev.get("actor"),
            "anonymous": ev.get("anonymous"),
            "actor_evidence": ev.get("actor_evidence"),
            "src": ev.get("src"),
            "undo_of": ev.get("undo_of"),
            "late": ev.get("late"),
            "order_deferred": ev.get("order_deferred"),
            "coalesced": ev.get("coalesced"),
            "members": ev.get("members"),
            "payload_version": _editor.EVENT_PAYLOAD_VERSION,
        })

    return {
        "subject_id": subject_id if subject_id is not None
        else (events[0]["subject_id"] if events else None),
        "versions": versions,
        "final_body": current_body,
        "skipped": skipped,
    }


def build_all_subjects(stable_events):
    """对全部主体分别重放，返回 subject_id -> 版本视图。"""
    subjects = sorted({e.get("subject_id") for e in stable_events
                       if isinstance(e, dict) and e.get("payload_version") == _editor.EVENT_PAYLOAD_VERSION})
    return {sid: build_versions(stable_events, subject_id=sid) for sid in subjects}


def process(record):
    """流水线阶段入口。

    标准编辑事件：附上该事件单独重放得到的版本视图（editor 已做归并/定序）。
    旧格式记录：无编辑证据可消费，原样透传并打阶段标记。
    """
    rec = dict(record)
    if rec.get("payload_version") != _editor.EVENT_PAYLOAD_VERSION:
        rec.setdefault("_stage_version", "naive")
        return rec
    view = build_versions([rec], subject_id=rec.get("subject_id"))
    rec["_stage_version"] = "versioned"
    rec["_version_view"] = view
    return rec


# ---- 持久化 ledger：两段式确认 + 崩溃恢复 -------------------------------

def ensure_tables(con):
    con.execute(LEDGER_DDL)
    con.execute(VERSION_DDL)


def ingest_events(con, stable_events, commit=True):
    """事件入库（幂等）。重复 event_id 不产生第二条 ledger 记录。"""
    ensure_tables(con)
    for ev in stable_events:
        con.execute(
            "INSERT INTO edit_event_ledger(event_id, subject_id, data, status) "
            "VALUES(?,?,?, 'pending') "
            "ON CONFLICT(event_id, subject_id) DO NOTHING",
            (ev["event_id"], ev.get("subject_id"),
             json.dumps(ev, ensure_ascii=False, sort_keys=True)),
        )
    if commit:
        con.commit()


def pending_events(con, subject_id=None):
    ensure_tables(con)
    if subject_id is None:
        rows = con.execute(
            "SELECT data FROM edit_event_ledger WHERE status='pending'").fetchall()
    else:
        rows = con.execute(
            "SELECT data FROM edit_event_ledger WHERE status='pending' AND subject_id=?",
            (subject_id,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def _mark_confirmed(con, event_ids):
    if not event_ids:
        return
    con.executemany(
        "UPDATE edit_event_ledger SET status='confirmed' WHERE event_id=?",
        [(eid,) for eid in event_ids],
    )


def _write_versions(con, view):
    for ver in view["versions"]:
        con.execute(
            "INSERT OR IGNORE INTO edit_versions(subject_id, version_no, event_id, data) "
            "VALUES(?,?,?,?)",
            (view["subject_id"], ver["version_no"], ver["event_id"],
             json.dumps(ver, ensure_ascii=False, sort_keys=True)),
        )


def confirm_versions(con, subject_id=None, commit=True, crash_after_ingest=False):
    """把 pending 事件重放为版本并确认。

    整个动作在一个事务内：重放（纯函数，确定）→ 幂等写版本表 → 标记 confirmed。
    ``crash_after_ingest`` 仅用于测试：模拟“事件已入库、版本尚未确认”的中断点，
    此时事务回滚，ledger 中事件保持 pending。
    """
    ensure_tables(con)
    pending = pending_events(con, subject_id=subject_id)
    if crash_after_ingest:
        # 显式回滚：模拟进程在确认前被杀死，不能留下半套版本。
        con.rollback()
        return {"subject_id": subject_id, "versions": [], "final_body": None,
                "skipped": [], "recovered": False, "crashed": True}
    if not pending:
        return {"subject_id": subject_id, "versions": [], "final_body": None,
                "skipped": [], "recovered": False, "crashed": False}

    # ledger 里存的就是 editor 产出的稳定事件；version 只消费、不重新定序。
    stable = pending
    if subject_id is None:
        views = build_all_subjects(stable)
    else:
        views = {subject_id: build_versions(stable, subject_id=subject_id)}

    for view in views.values():
        _write_versions(con, view)
    _mark_confirmed(con, [ev["event_id"] for ev in pending])
    if commit:
        con.commit()

    if subject_id is not None:
        view = views.get(subject_id) or build_versions([], subject_id=subject_id)
        return {**view, "recovered": True, "crashed": False}
    return {"subjects": views, "recovered": True, "crashed": False}


def recover_pending(con, subject_id=None, commit=True):
    """崩溃恢复入口：重放所有仍 pending 的事件。

    幂等：版本表对 (subject_id, version_no) 去重、ledger 确认按 event_id 更新，
    因此中断后重复恢复不会产生第二次编辑或重复版本。
    """
    return confirm_versions(con, subject_id=subject_id, commit=commit)
