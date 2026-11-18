import csv, io, json


def _curve_fields(row):
    return {
        "curve_id": row.get("curve_id"),
        "standard_version": row.get("standard_version"),
        "standards_digest": row.get("standards_digest"),
        "standard_points": row.get("standard_points"),
        "reviewed_with": row.get("reviewed_with"),
    }


def evidence_csv(run):
    out = io.StringIO()
    writer = csv.writer(out)
    result = run["result"]
    decisions = result.get("sample_decisions") or {}
    writer.writerow([
        "run_id", "sha256", "rule_version", "kind", "line", "reason",
        "batch", "analyte", "sample_id", "curve_id", "standard_version",
        "standards_digest", "qualifier", "status", "decision", "evidence",
    ])
    writer.writerow([
        run["id"], run["digest"], run["rule_version"], "manifest", "", "",
        "", "", "", "", "", "", "", "",
        "",
        json.dumps({
            "balance": result.get("balance"),
            "conclusion": "阻断" if result.get("blocked") else "可复核",
            "decision": run["decision"],
            "revision": run["revision"],
            "parent_id": run.get("parent_id"),
            "curve_events": result.get("curve_events", []),
        }, ensure_ascii=False),
    ])
    for entry in result.get("curve_registry", []) or []:
        writer.writerow([
            run["id"], run["digest"], run["rule_version"], "curve", "", "",
            entry.get("batch"), entry.get("analyte"), "", entry.get("curve_id"),
            entry.get("version"), entry.get("standards_digest"), "", "", "",
            json.dumps(entry, ensure_ascii=False),
        ])
    for row in result.get("final", []):
        key = "|".join(str(row.get(field, "")) for field in ("batch", "analyte", "sample_id"))
        decision = row.get("decision") or (decisions.get(key) or {}).get("decision") or "待复核"
        writer.writerow([
            run["id"], run["digest"], run["rule_version"], "result", row.get("line", ""), "",
            row.get("batch"), row.get("analyte"), row.get("sample_id"),
            row.get("curve_id"), row.get("standard_version"), row.get("standards_digest"),
            row.get("qualifier"), row.get("status"), decision,
            json.dumps(row, ensure_ascii=False),
        ])
    for row in result.get("quarantine", []):
        writer.writerow([
            run["id"], run["digest"], run["rule_version"], "quarantine",
            row.get("line", ""), row["reason"], "", "", row.get("id", ""),
            "", "", "", "", "", "", json.dumps(row, ensure_ascii=False),
        ])
    return "﻿" + out.getvalue()
