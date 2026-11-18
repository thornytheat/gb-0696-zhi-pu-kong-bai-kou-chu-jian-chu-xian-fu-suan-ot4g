import hashlib
import json
from decimal import Decimal
from app.errors import BusinessError
from app.report import report
from app.versions import (
    as_curve,
    curve_ref,
    inherit_parent_rows,
    parent_curve_map,
    parent_id_of,
    prior_registry,
)


def _canonical(value):
    value = Decimal(value)
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def standards_digest(points):
    """同一组标样（与行序无关）产生同一个内容摘要。

    摘要只由标样浓度与响应组成，因此“重试同一次标样修订”得到相同摘要，
    复算时直接复用既有版本，不会新增版本号。补齐标样会改变摘要。
    """
    items = sorted([_canonical(x), _canonical(y)] for x, y in points)
    payload = json.dumps(items, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fit(points):
    if len(points) < 3:
        raise BusinessError("CURVE_POINTS", "至少3个标曲点")
    n = Decimal(len(points))
    sx = sum(x for x, y in points)
    sy = sum(y for x, y in points)
    den = n * sum(x * x for x, y in points) - sx * sx
    if den == 0:
        raise BusinessError("CURVE_RANK", "标曲浓度不独立")
    slope = (n * sum(x * y for x, y in points) - sx * sy) / den
    if slope <= 0:
        raise BusinessError("CURVE_SLOPE", "标曲斜率无效")
    intercept = (sy - slope * sx) / n
    residual = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
    total = sum((y - sy / n) ** 2 for x, y in points)
    return {
        "slope": slope,
        "intercept": intercept,
        "r2": Decimal(1) - residual / total if total else Decimal(0),
        "max_response": max(y for x, y in points),
    }


def _registry_from_parent(parent):
    """父批次的版本历史；旧记录缺少版本清单时由当前标曲补一条。"""
    registry = prior_registry(parent)
    known = {entry.get("curve_id") for entry in registry}
    for (batch, analyte), curve in parent_curve_map(parent).items():
        ref = curve_ref(batch, analyte, curve["version"])
        if ref in known:
            continue
        registry.append({
            "batch": batch,
            "analyte": analyte,
            "version": curve["version"],
            "curve_id": ref,
            "standards_digest": curve.get("standards_digest"),
            "standard_points": curve.get("standard_points"),
            "slope": curve["slope"],
            "intercept": curve["intercept"],
            "r2": curve["r2"],
            "max_response": curve["max_response"],
            "created_run": parent_id_of(parent),
        })
    return registry


def _version(entry):
    return int(entry.get("version") or 0)


def _matching_version(registry, batch, analyte, digest):
    matches = [
        entry for entry in registry
        if entry.get("batch") == batch
        and entry.get("analyte") == analyte
        and entry.get("standards_digest") == digest
    ]
    return max(matches, key=_version) if matches else None


def _versions_for(registry, batch, analyte):
    return [
        _version(entry) for entry in registry
        if entry.get("batch") == batch and entry.get("analyte") == analyte
    ]


def apply(ctx):
    parent = ctx.get("parent")
    prior = parent_curve_map(parent)
    registry = _registry_from_parent(parent)
    inherit_parent_rows(ctx)
    groups = {}
    for r in ctx["normalized"]:
        if r["kind"] == "standard":
            groups.setdefault((r["batch"], r["analyte"]), []).append(
                (r["level_value"], r["response"])
            )
    curves = dict(prior)
    events = []
    errors = []
    for key in sorted(set(groups) | set(prior)):
        batch, analyte = key
        points = groups.get(key)
        previous = prior.get(key)
        if points is None:
            if previous:
                events.append({
                    "batch": batch,
                    "analyte": analyte,
                    "status": "inherited",
                    "from_version": previous["version"],
                    "to_version": previous["version"],
                    "standards_digest": previous.get("standards_digest"),
                    "points": previous.get("standard_points"),
                })
            continue
        digest = standards_digest(points)
        matched = _matching_version(registry, batch, analyte, digest)
        if matched:
            curve = as_curve(matched)
            curve["standard_points"] = matched.get("standard_points")
            curves[key] = curve
            status = (
                "unchanged"
                if previous and previous["version"] == curve["version"]
                else "reused"
            )
            events.append({
                "batch": batch,
                "analyte": analyte,
                "status": status,
                "from_version": previous["version"] if previous else None,
                "to_version": curve["version"],
                "standards_digest": digest,
                "points": len(points),
            })
            continue
        try:
            fitted = fit(points)
        except BusinessError as e:
            errors.append({
                "batch": batch,
                "analyte": analyte,
                "status": e.code,
                "standards_digest": digest,
                "standard_points": len(points),
            })
            if previous:
                # 曲线更新失败：保留旧版结果，不推进版本号。
                curves[key] = previous
                events.append({
                    "batch": batch,
                    "analyte": analyte,
                    "status": "update_failed",
                    "reason": e.code,
                    "from_version": previous["version"],
                    "to_version": previous["version"],
                    "retained_version": previous["version"],
                    "standards_digest": digest,
                    "points": len(points),
                })
            else:
                curves.pop(key, None)
                events.append({
                    "batch": batch,
                    "analyte": analyte,
                    "status": "update_failed",
                    "reason": e.code,
                    "from_version": None,
                    "to_version": None,
                    "retained_version": None,
                    "standards_digest": digest,
                    "points": len(points),
                })
            continue
        version = max(_versions_for(registry, batch, analyte) + [
            previous["version"] if previous else 0
        ]) + 1
        curve = dict(fitted)
        curve.update({
            "standards_digest": digest,
            "standard_points": len(points),
            "version": version,
            "standard_version": version,
            "created_run": ctx.get("run_id"),
        })
        curves[key] = curve
        registry.append({
            "batch": batch,
            "analyte": analyte,
            "version": version,
            "curve_id": curve_ref(batch, analyte, version),
            "standards_digest": digest,
            "standard_points": len(points),
            "slope": fitted["slope"],
            "intercept": fitted["intercept"],
            "r2": fitted["r2"],
            "max_response": fitted["max_response"],
            "created_run": ctx.get("run_id"),
        })
        events.append({
            "batch": batch,
            "analyte": analyte,
            "status": "first_version" if previous is None else "new_version",
            "from_version": previous["version"] if previous else None,
            "to_version": version,
            "standards_digest": digest,
            "points": len(points),
        })
    for key, curve in curves.items():
        curve["curve_id"] = curve_ref(key[0], key[1], curve["version"])
    ctx["curves"] = curves
    ctx["curve_registry"] = registry
    ctx["curve_events"] = events
    ctx["curve_errors"] = errors
    return report(
        [dict(entry, curve_id=curve_ref(entry["batch"], entry["analyte"], entry["version"]))
         for entry in registry]
        + errors,
        标曲=len(curves),
        版本=len(registry),
        失败=len(errors),
    )
