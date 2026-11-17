"""单一职责：对记录做单一职责变换：author_id。（作为流水线阶段）"""
def process(record):
    rec = dict(record)
    rec.setdefault("_stage_author_id", "ok")
    return rec
