from app.report import report
from app.versions import parent_decisions


def ready(rows, errors):
    return bool(rows) and not errors and not any(
        r["status"] in {"OVER_RANGE", "CONTROL_FAIL"} for r in rows
    )


def _decorate(row):
    x = dict(row)
    x.setdefault("decision", "待复核")
    return x


def _merged(ctx, fresh):
    """未复核样品用本次标曲版本计算；已复核样品保持当时标曲与结论。"""
    frozen = ctx.get("frozen") or {}
    decisions = parent_decisions(ctx.get("parent"))
    merged = []
    for row in fresh:
        x = _decorate(row)
        # 本次到达计算阶段的样品都尚未复核，决策留待复核页面写入。
        x["decision"] = "待复核"
        merged.append(x)
    for key, row in frozen.items():
        x = _decorate(row)
        record = decisions.get(key)
        x["decision"] = record.get("decision") if isinstance(record, dict) else "通过"
        x["frozen"] = True
        merged.append(x)
    return merged


def apply(ctx):
    fresh = ctx["quality"]
    merged = _merged(ctx, fresh)
    ctx["final"] = merged
    events = ctx.get("curve_events", [])
    ctx["curve_superseded"] = bool([
        e for e in events if e.get("status") in {"new_version", "update_failed"}
    ])
    # 更新失败但保留了旧版标曲的检测项仍可用旧版继续出结果，不阻断；
    # 没有旧版可保留的失败才会让新结果失去依据，必须阻断。
    retained = {
        (e.get("batch"), e.get("analyte")) for e in events
        if e.get("status") == "update_failed" and e.get("retained_version")
    }
    blocking = [
        e for e in ctx.get("curve_errors", [])
        if (e.get("batch"), e.get("analyte")) not in retained
    ]
    ctx["retained_on_failure"] = [
        e for e in events if e.get("status") == "update_failed" and e.get("retained_version")
    ]
    if fresh:
        ctx["blocked"] = not ready(fresh, blocking)
    else:
        # 全部样品已复核并冻结：没有新的待复核样品，不重新阻断历史结论。
        ctx["blocked"] = bool(blocking)
    # 从上级批次取回的行不是本次输入，不计入本次行数对账。
    inherited_lines = {row.get("line") for row in ctx.get("inherited_rows", [])}
    excluded = {r["line"] for r in ctx["quarantine"] if r["line"] not in inherited_lines}
    accepted = ctx["input_count"] - len(excluded) - ctx["blank_rows"]
    ctx["balance"] = {
        "input": ctx["input_count"],
        "accepted": accepted,
        "quarantined": len(excluded),
        "blank": ctx["blank_rows"],
    }
    return report(
        merged,
        结论="阻断" if ctx["blocked"] else "可复核",
        样品=len(merged),
        待复核=sum(1 for r in merged if r.get("decision") == "待复核"),
        冻结=sum(1 for r in merged if r.get("frozen")),
        隔离=len(excluded),
    )
