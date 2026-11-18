import sqlite3, json, hashlib
from datetime import datetime, timezone
from app.config import DATA_DIR, RULE_VERSION
from app.errors import BusinessError


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "runs.sqlite", timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT '待复核', created TEXT NOT NULL)")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
    if "parent_id" not in columns:
        db.execute("ALTER TABLE runs ADD COLUMN parent_id TEXT")
    db.execute("CREATE TABLE IF NOT EXISTS sample_reviews (run_id TEXT NOT NULL, sample_key TEXT NOT NULL, decision TEXT NOT NULL, revision INTEGER NOT NULL, curve_id TEXT, decided_at TEXT NOT NULL, PRIMARY KEY(run_id,sample_key))")
    return db


def digest_for(text, parent_id=None):
    return hashlib.sha256((RULE_VERSION + "\n" + (parent_id or "") + "\n" + text).encode()).hexdigest()


def _sample_key(row):
    return "|".join(str(row.get(field, "")) for field in ("batch", "analyte", "sample_id"))


def _validate_parent(parent_id, result):
    """拒绝把不同分析批的补齐记录挂到当前批次下。"""
    parent = get(parent_id)
    parent_batches = {
        curve.split("|", 1)[0]
        for curve in (parent["result"].get("curves") or {})
    }
    intake = (result.get("reports") or [{}])[0].get("rows") or []
    standards = [row for row in intake if row.get("kind") == "standard"]
    child_batches = {row.get("batch") for row in (standards or intake) if row.get("batch")}
    if parent_batches and child_batches and not (parent_batches & child_batches):
        raise BusinessError("PARENT_BATCH", "上级批次与本次标样不属于同一分析批")
    return parent


def _inherit_reviews(db, ident, parent):
    """新批次继承父批次已复核样品的结论与当时标曲归属。"""
    for review in parent.get("sample_reviews") or []:
        db.execute(
            "INSERT OR REPLACE INTO sample_reviews(run_id,sample_key,decision,revision,curve_id,decided_at) VALUES(?,?,?,?,?,?)",
            (ident, review["sample_key"], review["decision"], 0,
             review.get("curve_id"), review["decided_at"]),
        )


def save(text, result, parent_id=None):
    parent = _validate_parent(parent_id, result) if parent_id else None
    digest = digest_for(text, parent_id)
    ident = digest[:16]
    if parent_id == ident:
        raise BusinessError("PARENT", "批次不能以自身为上级")
    with connect() as db:
        created = db.execute(
            "INSERT OR IGNORE INTO runs(id,digest,result,revision,decision,created,parent_id) VALUES(?,?,?,0,'待复核',?,?)",
            (ident, digest,
             json.dumps(dict(result, rule_version=RULE_VERSION, parent_id=parent_id), ensure_ascii=False),
             datetime.now(timezone.utc).isoformat(), parent_id),
        ).rowcount
        if created and parent_id:
            _inherit_reviews(db, ident, parent)
    return get(ident)


def get(ident):
    with connect() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "批次不存在", 404)
        item = dict(row)
        item["result"] = json.loads(item["result"])
        item["rule_version"] = item["result"].get("rule_version", RULE_VERSION)
        item["parent_id"] = item.get("parent_id") or item["result"].get("parent_id")
        item["sample_reviews"] = [
            dict(r) for r in db.execute(
                "SELECT sample_key,decision,revision,curve_id,decided_at FROM sample_reviews WHERE run_id=? ORDER BY sample_key",
                (ident,),
            )
        ]
    decisions = {r["sample_key"]: r for r in item["sample_reviews"]}
    item["result"]["sample_decisions"] = decisions
    for row in item["result"].get("final") or []:
        record = decisions.get(_sample_key(row))
        if record:
            row["decision"] = record["decision"]
            row["reviewed_with"] = record.get("curve_id") or row.get("curve_id")
    finals = item["result"].get("final") or []
    item["result"]["review_summary"] = {
        "pending": sum(1 for row in finals if row.get("decision") == "待复核"),
        "reviewed": sum(1 for row in finals if row.get("decision") in {"通过", "退回"}),
        "frozen": sum(1 for row in finals if row.get("frozen")),
        "total": len(finals),
    }
    return item


def history():
    with connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT id,revision,decision,created,parent_id FROM runs ORDER BY created DESC LIMIT 40"
        )]


def _final_rows(item):
    return item["result"].get("final") or []


def _pending(item):
    decided = item["result"].get("sample_decisions") or {}
    return [
        _sample_key(row) for row in _final_rows(item)
        if _sample_key(row) not in decided
    ]


def _row_for(item, key):
    return next((row for row in _final_rows(item) if _sample_key(row) == key), None)


def review(ident, revision, decision, samples=None):
    """复核整个批次或指定样品；已复核样品只会被显式指定时再次更新。"""
    if not isinstance(decision, str) or decision not in {"通过", "退回"}:
        raise BusinessError("DECISION", "复核决定无效")
    current = get(ident)
    if decision == "通过" and current["result"].get("blocked"):
        raise BusinessError("BLOCKED", "存在阻断项，请先修正数据", 409)
    pending = _pending(current)
    targets = pending
    if samples is not None:
        if not isinstance(samples, list) or not all(isinstance(x, str) for x in samples):
            raise BusinessError("SAMPLES", "样品范围须为编号列表")
        available = set(pending) | {
            review["sample_key"] for review in current.get("sample_reviews") or []
        }
        unknown = [x for x in samples if x not in available]
        if unknown:
            raise BusinessError("SAMPLES", "样品不在可复核范围", 409)
        targets = list(dict.fromkeys(samples))
        if not targets:
            raise BusinessError("SAMPLES", "没有待复核样品", 409)
    with connect() as db:
        changed = db.execute(
            "UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?",
            (decision if samples is None else current["decision"], ident, revision),
        ).rowcount
        if not changed:
            raise BusinessError("STALE", "版本已更新，请刷新", 409)
        now = datetime.now(timezone.utc).isoformat()
        for key in targets:
            row = _row_for(current, key)
            if row is None:
                continue
            db.execute(
                "INSERT OR REPLACE INTO sample_reviews(run_id,sample_key,decision,revision,curve_id,decided_at) VALUES(?,?,?,?,?,?)",
                (ident, key, decision, revision + 1, row.get("curve_id"), now),
            )
    return get(ident)
