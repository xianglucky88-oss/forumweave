"""单一职责：编辑事件的身份整理、顺序确定与归并（editor 阶段）。

本模块只做“事件层”的整理，不产出最终版本对象：

* ``process(record)`` —— 流水线阶段入口，把单条摄入记录规范化为标准编辑事件；
  对不含编辑标记的旧格式记录做兼容透传（``_stage_editor = "naive"``）。
* ``stabilize(events)`` —— 对一批（可能交错、迟到、重复的）事件做稳定化：
  去重 → 分组 → 定序 → 快速连续编辑归并 → 撤销解释 → 正文差异计算，
  输出带稳定身份与可解释顺序的稳定事件。

顺序规则（时间相同而版本信息冲突时的处理，显式写在实现里）：
  排序键 = (事件时间, 版本号 rev, 到达序号 seq, 事件身份 event_id)。
  当时间相同且 rev 缺失或互相冲突、无法据此判定先后时，事件被打
  ``order_deferred=True`` 标记——表示“暂缓判断”，但仍由确定性兜底键
  （到达序号、event_id）给出一个总顺序，保证任意重放得到完全相同的结果。
  迟到事件（到达顺序与逻辑顺序不一致）会被打 ``late=True``，按逻辑时间归位，
  而不是按到达时间插入。

归并语义：
  * 同一主体、时间间隔 <= ``COALESCE_WINDOW`` 的相邻非撤销编辑归并为一次
    稳定编辑会话（连续快速编辑 = 一次有意义的正文变化）；
  * 与当前有效正文相同的编辑记为 ``no_op``（相同正文再提交不产生新版本）；
  * ``kind="undo"`` 的事件解释为撤销，不参与编辑归并；迟到撤销按逻辑时间归位；
  * 匿名编辑保留全部操作者证据并打 ``anonymous=True``；
  * 重复消息（event_id 或正文指纹相同）只保留首次到达者，其余打 ``duplicate_of``。

事件差异（``diff``）与操作者证据（``actor`` / ``actor_evidence`` / ``src``）
原样挂在稳定事件上，交给 ``version`` 阶段消费；重放只依赖稳定事件本身，
与消息到达顺序无关。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from difflib import SequenceMatcher

# ---- 公开常量 -----------------------------------------------------------

# 连续快速编辑的归并窗口（秒）：窗口内相邻编辑视为同一次编辑会话。
COALESCE_WINDOW = 60.0

# 事件载荷结构版本；旧格式记录没有编辑标记，走 naive 兼容路径。
EVENT_PAYLOAD_VERSION = 2

# 标准事件上始终存在的字段，供下游（version / store）与测试依赖。
STABLE_FIELDS = (
    "event_id", "subject_id", "kind", "edit_flag", "ts", "ts_epoch",
    "rev", "actor", "anonymous", "actor_evidence", "src", "seq",
    "body", "body_before", "diff", "no_op", "duplicate_of", "duplicates",
    "coalesced", "members", "late", "order_deferred", "undo_of",
    "fingerprint", "payload_version",
)

# 字段别名：摄入侧命名可能不同，统一映射到标准名。
_ALIASES = {
    "subject_id": ("subject_id", "thread_id", "post_id", "target_id", "id"),
    "event_id": ("event_id", "edit_id", "eventid", "message_id", "seq_id"),
    "body": ("body", "text", "content", "payload"),
    "ts": ("ts", "time", "timestamp", "edited_at", "event_ts"),
    "actor": ("actor", "operator", "editor", "user", "user_id", "author"),
    "rev": ("rev", "revision", "ver", "version", "seq_no"),
    "src": ("src", "source", "origin"),
    "kind": ("kind", "type", "op"),
    "edit_flag": ("edit_flag", "edited", "is_edit", "was_edited"),
}

_UNDO_MARKERS = frozenset({"undo", "revert", "rollback"})
_ANON_MARKERS = frozenset({"", "anonymous", "anon", "unknown", "none", "null"})


# ---- 基础工具 -----------------------------------------------------------

def parse_ts(value):
    """把多种时间表示统一为带 UTC 时区的 ``datetime``；无法解析时返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # 兜底：纯数字字符串按 epoch 处理。
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt):
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _pick(rec, key):
    for name in _ALIASES[key]:
        if name in rec and rec[name] is not None:
            return rec[name]
    return None


def _as_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_anonymous(actor):
    if actor is None:
        return True
    return str(actor).strip().lower() in _ANON_MARKERS


def fingerprint(subject_id, ts_iso, actor, body):
    """正文指纹：event_id 缺失时的稳定身份，也是重复消息的判定依据之一。"""
    material = json.dumps(
        [subject_id, ts_iso, "" if actor is None else str(actor), "" if body is None else str(body)],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def body_diff(before, after):
    """行级正文差异；before/after 均为 None 时返回空列表。

    返回 JSON 可序列化的操作码列表：``[op, 旧行起点, 旧行终点, 新行起点, 新行终点]``，
    供 version 阶段直接消费、随版本证据落库。
    """
    if before is None and after is None:
        return []
    old_lines = [] if before is None else str(before).splitlines()
    new_lines = [] if after is None else str(after).splitlines()
    return [list(op) for op in SequenceMatcher(a=old_lines, b=new_lines).get_opcodes()]


def _kind_of(rec, kind_raw, edit_raw):
    if isinstance(kind_raw, str) and kind_raw.strip().lower() in _UNDO_MARKERS:
        return "undo"
    if isinstance(edit_raw, str) and edit_raw.strip().lower() in _UNDO_MARKERS:
        return "undo"
    return "edit"


# ---- 单条记录规范化（流水线阶段） ---------------------------------------

def normalize_event(record, seq=0):
    """把一条摄入记录整理成标准编辑事件（不做跨记录判断）。"""
    rec = dict(record)
    subject_id = _pick(rec, "subject_id")
    event_id = _pick(rec, "event_id")
    body = _pick(rec, "body")
    body = None if body is None else str(body)
    dt = parse_ts(_pick(rec, "ts"))
    ts_iso = _iso(dt)
    actor_raw = _pick(rec, "actor")
    actor = None if _is_anonymous(actor_raw) else str(actor_raw).strip()
    kind = _kind_of(rec, _pick(rec, "kind"), _pick(rec, "edit_flag"))
    edit_raw = _pick(rec, "edit_flag")
    edit_flag = bool(edit_raw) if not isinstance(edit_raw, str) else edit_raw.strip().lower() in _UNDO_MARKERS or bool(edit_raw.strip())
    fp = fingerprint(subject_id, ts_iso, actor, body)

    event = {
        "event_id": str(event_id) if event_id is not None else f"fp-{fp}",
        "subject_id": str(subject_id) if subject_id is not None else None,
        "kind": kind,
        "edit_flag": edit_flag,
        "ts": ts_iso,
        "ts_epoch": dt.timestamp() if dt is not None else None,
        "rev": _as_int(_pick(rec, "rev")),
        "actor": actor,
        "anonymous": _is_anonymous(actor_raw),
        "actor_evidence": {
            "raw": None if actor_raw is None else str(actor_raw),
            "src": str(_pick(rec, "src") or "").strip().lower(),
            "raw_record_keys": sorted(rec.keys()),
        },
        "src": str(_pick(rec, "src") or "").strip().lower(),
        "seq": int(seq),
        "body": body,
        "undo_target": _as_int(rec.get("undo_target", rec.get("target_rev"))),
        "fingerprint": fp,
        "payload_version": EVENT_PAYLOAD_VERSION,
    }
    return event


# 编辑事件专属信号：只有出现这些字段之一，才认为是编辑事件而非旧格式记录。
# 注意 ts/src/id 是通用字段，旧格式记录也有，不能单独作为事件判据。
_EVENT_SIGNALS = (
    frozenset(_ALIASES["edit_flag"]) | frozenset(_ALIASES["body"])
    | frozenset(_ALIASES["kind"]) | frozenset(_ALIASES["event_id"])
    | frozenset(_ALIASES["actor"])
)


def is_legacy(record):
    """旧格式记录：缺少任何编辑事件专属信号（仅含 id/src/name/value/ts 等通用字段）。"""
    if not isinstance(record, dict):
        return True
    if record.get("_stage_editor") == "naive":
        return True
    return not any(k in record for k in _EVENT_SIGNALS)


def process(record):
    """流水线阶段入口：单条记录 → 标准编辑事件；旧格式记录兼容透传。"""
    rec = dict(record)
    if is_legacy(rec):
        # 兼容：旧记录不进入事件归并，保持朴素阶段行为，下游可识别。
        rec.setdefault("_stage_editor", "naive")
        return rec
    event = normalize_event(rec, seq=int(rec.get("_arrival_seq", 0) or 0))
    event["_stage_editor"] = "event"
    return event


# ---- 批量稳定化：身份、顺序、归并 ---------------------------------------

def _sort_key(ev):
    # 时间缺失的事件排到最后；rev 缺失用 +inf 兜底但会被标 order_deferred。
    ts = ev["ts_epoch"]
    return (
        ts is None,
        ts if ts is not None else 0.0,
        ev["rev"] if ev["rev"] is not None else 1 << 62,
        ev["seq"],
        ev["event_id"],
    )


def _duplicate_index(events):
    """返回每个事件实例（按 seq 区分）-> 首次到达事件的 event_id。

    同 event_id 的多条记录里只有第一条是 keeper，其余才是重复副本；
    指纹（同主体/时间/操作者/正文）命中也判重。
    """
    dup_of = {}
    seen_id: dict[tuple, str] = {}
    seen_fp: dict[tuple, str] = {}
    for ev in sorted(events, key=lambda e: e["seq"]):
        instance = (ev.get("subject_id"), ev["event_id"], ev["seq"])
        key_id = (ev.get("subject_id"), ev["event_id"])
        key_fp = (ev.get("subject_id"), ev["fingerprint"])
        keeper = seen_id.get(key_id) or seen_fp.get(key_fp)
        if keeper is None:
            seen_id[key_id] = ev["event_id"]
            seen_fp.setdefault(key_fp, ev["event_id"])
        else:
            dup_of[instance] = keeper
    return dup_of


def _blank_stable(ev):
    stable = {field: None for field in STABLE_FIELDS}
    stable.update({
        "event_id": ev["event_id"],
        "subject_id": ev["subject_id"],
        "kind": ev["kind"],
        "edit_flag": ev["edit_flag"],
        "ts": ev["ts"],
        "ts_epoch": ev["ts_epoch"],
        "rev": ev["rev"],
        "actor": ev["actor"],
        "anonymous": ev["anonymous"],
        "actor_evidence": ev["actor_evidence"],
        "src": ev["src"],
        "seq": ev["seq"],
        "body": ev["body"],
        "no_op": False,
        "coalesced": False,
        "members": [ev["event_id"]],
        "late": False,
        "order_deferred": False,
        "fingerprint": ev["fingerprint"],
        "payload_version": EVENT_PAYLOAD_VERSION,
    })
    return stable


def _merge_cluster(cluster):
    """把一个快速连续编辑簇归并成单个稳定编辑事件，保留全部成员证据。"""
    head, tail = cluster[0], cluster[-1]
    merged = _blank_stable(head)
    merged.update({
        "event_id": head["event_id"],
        "ts": tail["ts"],
        "ts_epoch": tail["ts_epoch"],
        "rev": tail["rev"],
        # 归并事件以簇内最后一次编辑的正文为结果正文。
        "body": tail["body"],
        "actor": tail["actor"],
        "anonymous": any(ev["anonymous"] for ev in cluster),
        "actor_evidence": {
            "raw": tail["actor_evidence"]["raw"],
            "src": tail["actor_evidence"]["src"],
            "raw_record_keys": tail["actor_evidence"]["raw_record_keys"],
            "actors": [ev["actor"] for ev in cluster],
            "actor_evidence": [ev["actor_evidence"] for ev in cluster],
        },
        "src": tail["src"],
        "coalesced": True,
        "members": [ev["event_id"] for ev in cluster],
        "late": any(ev["_late"] for ev in cluster),
        "order_deferred": any(ev["_deferred"] for ev in cluster),
        "duplicate_of": next((ev["event_id"] for ev in cluster if ev.get("_dup_of")), None),
        "fingerprint": tail["fingerprint"],
    })
    return merged


def stabilize(events):
    """把一批规范化事件整理为稳定事件列表。

    纯函数：输出只由输入事件内容决定，与调用次数、输入排列顺序无关
    （到达顺序由事件自身的 ``seq`` 字段承载）。
    """
    # 仅消费标准事件；旧格式（naive）记录原样排除，由兼容路径处理。
    events = [e for e in events if isinstance(e, dict) and e.get("payload_version") == EVENT_PAYLOAD_VERSION]
    if not events:
        return []

    dup_of = _duplicate_index(events)

    # 重复消息不参与归并/重放，但保留在分组里以保证到达排名（late 判定）不失真；
    # 其身份通过 duplicate_of 可追溯。
    grouped: dict[str, list[dict]] = {}
    for ev in events:
        ev = dict(ev)
        ev["_dup_key"] = (ev.get("subject_id"), ev["event_id"], ev["seq"])
        ev["_dup_of"] = dup_of.get(ev["_dup_key"])
        grouped.setdefault(ev["subject_id"] or "", []).append(ev)

    out: list[dict] = []
    # 去重台账：subject_id -> [{"event_id", "duplicate_of"}]，交给 version 消费。
    duplicates_by_subject: dict[str, list[dict]] = {}
    for subject_id in sorted(grouped):
        group = grouped[subject_id]
        ordered = sorted(group, key=_sort_key)
        duplicates_by_subject[subject_id] = [
            {"event_id": ev["event_id"], "duplicate_of": ev["_dup_of"]}
            for ev in ordered if ev.get("_dup_of")
        ]

        # 顺序注解只在“有效事件”（非重复副本）之间比较，重复消息不参与排名。
        # late = 事件固有的到达序号(seq)与其逻辑序号不一致；seq 是事件自身属性，
        # 不随输入列表排列变化，因此该判定在任意重放下保持确定。
        effective = [ev for ev in ordered if not ev.get("_dup_of")]
        arrival_rank = {id(ev): rank for rank, ev in
                        enumerate(sorted(effective, key=lambda e: e["seq"]))}
        for logical_rank, ev in enumerate(effective):
            ev["_late"] = arrival_rank[id(ev)] != logical_rank
            ev["_deferred"] = ev["ts_epoch"] is None
        for prev, cur in zip(effective, effective[1:]):
            # 同刻且无法用版本信息区分先后（rev 相同，含都为 None 的情况）。
            if prev["ts_epoch"] is not None and cur["ts_epoch"] is not None and \
                    prev["ts_epoch"] == cur["ts_epoch"] and \
                    (prev["rev"] is None or prev["rev"] == cur["rev"]):
                prev["_deferred"] = cur["_deferred"] = True
        for ev in group:
            ev.setdefault("_late", False)
            ev.setdefault("_deferred", False)

        # 快速连续编辑归并：同主体、相邻、均为 edit、时间间隔在窗口内。
        # 重复消息不参与归并与重放（仅在去重统计中可追溯），直接跳过。
        clusters: list[list[dict]] = []
        for ev in ordered:
            if ev.get("_dup_of"):
                continue
            if ev["kind"] != "edit" or not clusters or clusters[-1][-1]["kind"] != "edit":
                clusters.append([ev])
                continue
            last = clusters[-1][-1]
            gap = None
            if ev["ts_epoch"] is not None and last["ts_epoch"] is not None:
                gap = ev["ts_epoch"] - last["ts_epoch"]
            # gap 必须严格为正：同刻事件属于“时间相同而版本信息冲突”，
            # 保持独立并由 order_deferred 标注，不做归并。
            if gap is not None and 0 < gap <= COALESCE_WINDOW:
                clusters[-1].append(ev)
            else:
                clusters.append([ev])

        # 顺序重放：解释撤销、计算正文差异、标记 no_op。撤销事件打断编辑簇，
        # 因此撤销本身不进入归并。
        # version_stack：每次有效编辑之后的正文，version_stack[n-1] 即第 n 版正文；
        # current_body 始终是当前有效正文（初始 None）。
        current_body = None
        version_stack: list = []
        for cluster in clusters:
            ev0 = cluster[0]
            if ev0["kind"] == "undo":
                ev = ev0
                stable = _blank_stable(ev)
                target = ev.get("undo_target")
                if target is not None and version_stack:
                    # 回到指定版本：取第 target 版正文，越界收敛到最近边界。
                    idx = max(0, min(target - 1, len(version_stack) - 1))
                    new_body = version_stack[idx]
                elif version_stack:
                    # 普通撤销：丢弃最近一个版本，回到它之前的正文。
                    new_body = version_stack.pop()
                    new_body = version_stack[-1] if version_stack else None
                else:
                    new_body = current_body
                stable.update({
                    "body_before": current_body,
                    "body": new_body,
                    "diff": body_diff(current_body, new_body),
                    "no_op": new_body == current_body,
                    "late": ev["_late"],
                    "order_deferred": ev["_deferred"],
                    "duplicate_of": ev["_dup_of"],
                    "undo_of": target,
                })
                current_body = new_body
                out.append(stable)
                continue

            if len(cluster) > 1:
                stable = _merge_cluster(cluster)
                before = current_body
            else:
                ev = cluster[0]
                stable = _blank_stable(ev)
                stable.update({
                    "late": ev["_late"],
                    "order_deferred": ev["_deferred"],
                    "duplicate_of": ev["_dup_of"],
                })
                before = current_body

            same = stable["body"] == current_body
            stable["body_before"] = before
            # 正文未变化（no_op）时差异归一为空，避免把“相同正文再提交”描述成变化。
            stable["diff"] = [] if same else body_diff(before, stable["body"])
            stable["no_op"] = bool(same)
            if not same:
                current_body = stable["body"]
                version_stack.append(current_body)
            out.append(stable)

    # 全局保持 (主体, 逻辑顺序) 的确定性输出。
    out.sort(key=lambda s: (s["subject_id"] or "", _sort_key(s)))
    # 去重台账挂在每个主体的首个稳定事件上；主体事件全被去重时单独补一条占位，
    # 保证 version 侧仍能报告 skipped。
    seen_subjects = {s["subject_id"] for s in out}
    for subject_id, dups in duplicates_by_subject.items():
        if not dups:
            continue
        if subject_id in seen_subjects:
            for st in out:
                if st["subject_id"] == subject_id:
                    st["duplicates"] = dups
                    break
        else:
            placeholder = {field: None for field in STABLE_FIELDS}
            placeholder.update({
                "subject_id": subject_id, "kind": "duplicates",
                "no_op": True, "duplicates": dups, "members": [],
                "payload_version": EVENT_PAYLOAD_VERSION,
            })
            out.append(placeholder)
    out.sort(key=lambda s: (s["subject_id"] or "", _sort_key(s)))
    return out


def stabilize_records(records):
    """便利入口：摄入记录（可含旧格式）→ 稳定事件；旧格式记录被跳过。

    到达序号优先取记录自带的 ``_arrival_seq``（摄入顺序是事件的固有属性），
    缺失时退化为列表位置，保证重放不依赖列表排列。
    """
    events = []
    for idx, rec in enumerate(records):
        if is_legacy(rec):
            continue
        seq = int(rec.get("_arrival_seq", idx) or 0)
        events.append(normalize_event(rec, seq=seq))
    return stabilize(events)
