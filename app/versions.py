"""标样版本与样品绑定的共享辅助。

一条运行结果里的关键结构：
- curve_registry: 每个 (batch, analyte, version) 的标曲版本历史，永不覆盖。
- curve_events: 本次运行对标曲做出的动作（首版/新版本/无变化/更新失败保留旧版）。
- curves: 本次运行实际用于计算的标曲（可能是新版本，也可能是保留的旧版本）。
- sample_decisions: 样品级复核结论，键为 batch|analyte|sample_id。
"""
from decimal import Decimal


def curve_ref(batch, analyte, version):
    return f"{batch}|{analyte}|v{version}"


def sample_key(row):
    return f'{row.get("batch", "")}|{row.get("analyte", "")}|{row.get("sample_id", "")}'


def result_of(parent):
    """接受一次运行记录（含 result）或直接的结果字典。"""
    if not isinstance(parent, dict):
        return {}
    if isinstance(parent.get("result"), dict):
        return parent["result"]
    return parent


def parent_id_of(parent):
    return parent.get("id") if isinstance(parent, dict) else None


def as_curve(raw):
    if not raw:
        return None
    curve = dict(raw)
    for field in ("slope", "intercept", "r2", "max_response"):
        if curve.get(field) is not None and not isinstance(curve[field], Decimal):
            curve[field] = Decimal(str(curve[field]))
    curve["version"] = int(curve.get("version") or 1)
    curve["standard_version"] = curve["version"]
    return curve


def parent_curve_map(parent):
    curves = result_of(parent).get("curves") or {}
    return {tuple(k.split("|", 1)): as_curve(v) for k, v in curves.items()}


def prior_registry(parent):
    return [dict(x) for x in (result_of(parent).get("curve_registry") or [])]


def parent_final(parent):
    return list(result_of(parent).get("final") or [])


def parent_normalized(parent):
    result = result_of(parent)
    if result.get("normalized"):
        return list(result["normalized"])
    reports = result.get("reports") or []
    if len(reports) > 1:
        return list(reports[1].get("rows") or [])
    return []


def parent_decisions(parent):
    return dict(result_of(parent).get("sample_decisions") or {})


def decision_of(entry):
    """兼容旧记录：决策可能是字符串或含 decision 的对象。"""
    if isinstance(entry, dict):
        return entry.get("decision")
    return entry


def frozen_rows(parent):
    """父批次中已有复核结论的样品行：这些行按当时标曲冻结。"""
    decisions = parent_decisions(parent)
    rows = {}
    for row in parent_final(parent):
        key = sample_key(row)
        if decision_of(decisions.get(key)) in {"通过", "退回"}:
            rows[key] = row
    return rows


_DECIMAL_FIELDS = ("response", "level_value", "dilution_value")


def _restore(row, inherited_from):
    """把父批次的标准化行还原成计算阶段需要的 Decimal 行。"""
    x = dict(row)
    for field in _DECIMAL_FIELDS:
        if x.get(field) is not None:
            x[field] = Decimal(str(x[field]))
    x["inherited_from"] = inherited_from
    # 负行号标记“非本次输入”，避免与本次 CSV 行号冲突，也不计入行数对账。
    x["line"] = -abs(int(x.get("line") or 0))
    return x


def inherit_parent_rows(ctx):
    """补齐文件只提交部分行时，从父批次取回同批的其余行。

    标样按 sample_id 合并：本次提交的同编号标样覆盖上级记录，未提交的
    上级标样继续构成本批标曲点。空白、样品、控制样仅在本次未提交时补回。
    已复核样品仍由冻结逻辑排除。
    """
    parent = ctx.get("parent")
    if not parent:
        return []
    parent_rows = parent_normalized(parent)
    if not parent_rows:
        return []
    batches = {row["batch"] for row in ctx["normalized"] if row.get("batch")}
    present = {}
    for row in ctx["normalized"]:
        present.setdefault((row["batch"], row["analyte"]), set()).add(row["kind"])
    touched_standards = {
        key for key, kinds in present.items() if "standard" in kinds
    }
    submitted_ids = {}
    parent_ids = {}
    for row in ctx["normalized"]:
        if row.get("kind") == "standard":
            submitted_ids.setdefault((row["batch"], row["analyte"]), set()).add(row["sample_id"])
    for row in parent_rows:
        if row.get("kind") == "standard":
            parent_ids.setdefault((row["batch"], row["analyte"]), set()).add(row["sample_id"])
    # 提交的标样编号与上级全部不重叠 → 视为“只追加新标样”，与上级标样合并；
    # 有任何编号重叠 → 视为该检测项的完整标样集合（可能修改了既有值），不合并。
    append_only = {
        key for key in touched_standards
        if not (submitted_ids.get(key, set()) & parent_ids.get(key, set()))
    }
    seen = {
        (row.get("batch"), row.get("analyte"), row.get("kind"), row.get("sample_id"))
        for row in ctx["normalized"]
    }
    added = []
    for row in parent_rows:
        key = (row.get("batch"), row.get("analyte"))
        if key[0] not in batches:
            continue
        kind = row.get("kind")
        if kind == "standard":
            if key not in append_only:
                continue
        elif kind in {"blank", "sample", "control"}:
            if kind in present.get(key, set()):
                continue
        else:
            continue
        identity = (row.get("batch"), row.get("analyte"), kind, row.get("sample_id"))
        if identity in seen:
            continue
        added.append(_restore(row, parent_id_of(parent)))
        seen.add(identity)
    ctx["normalized"].extend(added)
    ctx.setdefault("reports", [])
    if len(ctx["reports"]) > 1:
        # 后续阶段与再下一级补齐都按“本批次完整行集”看待。
        ctx["reports"][1].setdefault("rows", []).extend(added)
    ctx["inherited_rows"] = [
        {"line": row.get("line"), "kind": row.get("kind"), "id": row.get("sample_id")}
        for row in added
    ]
    return added
