from statistics import median
from app.report import report, quarantine
from app.versions import curve_ref, frozen_rows, sample_key


def corrected(response, curve, blank):
    return (response - curve["intercept"]) / curve["slope"] - blank


def _binding(row, curve):
    return {
        "curve_id": curve_ref(row["batch"], row["analyte"], curve["version"]),
        "standard_version": curve["version"],
        "standards_digest": curve.get("standards_digest"),
        "standard_points": curve.get("standard_points"),
        "curve_slope": curve["slope"],
        "curve_intercept": curve["intercept"],
        "curve_r2": curve["r2"],
        "curve_max_response": curve["max_response"],
    }


def apply(ctx):
    frozen = frozen_rows(ctx.get("parent"))
    blanks = {}
    for r in ctx["normalized"]:
        key = (r["batch"], r["analyte"])
        c = ctx["curves"].get(key)
        if c and r["kind"] == "blank":
            blanks.setdefault(key, []).append(
                (r["response"] - c["intercept"]) / c["slope"]
            )
    ctx["blanks"] = {k: median(v) for k, v in blanks.items()}
    out = []
    skipped = 0
    for r in ctx["normalized"]:
        if r["kind"] not in {"sample", "control"}:
            continue
        if sample_key(r) in frozen:
            # 已复核样品按当时标曲冻结，不参与本次重算。
            skipped += 1
            continue
        key = (r["batch"], r["analyte"])
        curve = ctx["curves"].get(key)
        if not curve:
            quarantine(ctx, r, "CURVE_MISSING")
            continue
        if key not in ctx["blanks"]:
            quarantine(ctx, r, "BLANK_MISSING")
            continue
        x = dict(r)
        x["net"] = corrected(r["response"], curve, ctx["blanks"][key])
        x["blank_value"] = ctx["blanks"][key]
        x.update(_binding(r, curve))
        out.append(x)
    ctx["corrected"] = out
    ctx["frozen"] = {
        key: dict(row) for key, row in frozen.items()
        if row.get("kind") in {"sample", "control"}
    }
    return report(out, 样品=len(out), 空白组=len(blanks), 冻结=skipped)
