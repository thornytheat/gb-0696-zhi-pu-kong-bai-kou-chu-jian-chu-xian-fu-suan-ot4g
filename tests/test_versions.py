"""标样版本化与样品绑定：补齐标样只接管待复核样品。"""
import os
from decimal import Decimal
from app import store
from app.pipeline import run, sample_text
from app.modules.m3_curve.rules import standards_digest
from app.exporter import evidence_csv
from app.errors import BusinessError
import pytest

SUPPLEMENT = "C6,standard,Pb,1301,100,6,1,ug/L,B01\n"


@pytest.fixture(autouse=True)
def fresh_store():
    from app.config import DATA_DIR
    for suffix in ("", "-wal", "-shm"):
        path = DATA_DIR / ("runs.sqlite" + suffix)
        if path.exists():
            path.unlink()
    yield


def _save(text, parent=None):
    parent = store.get(parent["id"]) if parent else None
    parent_id = parent["id"] if parent else None
    ident = store.digest_for(text, parent_id)[:16]
    return store.save(text, run(text, parent=parent, run_id=ident), parent_id)


def _keys(result, count=None):
    keys = [
        "|".join(str(row[field]) for field in ("batch", "analyte", "sample_id"))
        for row in result["final"]
    ]
    return keys[:count] if count else keys


@pytest.fixture
def first():
    return _save(sample_text())


@pytest.fixture
def reviewed(first):
    return store.review(first["id"], first["revision"], "通过", _keys(first["result"], 10))


def test_curve_version_on_every_sample(first):
    assert first["result"]["curves"]["B01|Pb"]["version"] == 1
    rows = first["result"]["final"]
    assert {row["curve_id"] for row in rows} == {"B01|Pb|v1"}
    assert {row["standard_version"] for row in rows} == {1}
    assert all(row["standards_digest"] for row in rows)


def test_supplement_only_takes_pending_samples(reviewed, first):
    child = _save(sample_text() + SUPPLEMENT, parent=first)
    assert child["result"]["curves"]["B01|Pb"]["version"] == 2
    frozen = [r for r in child["result"]["final"] if r.get("frozen")]
    pending = [r for r in child["result"]["final"] if not r.get("frozen")]
    assert len(frozen) == 10 and len(pending) == 41
    assert {r["standard_version"] for r in frozen} == {1}
    assert {r["standard_version"] for r in pending} == {2}
    before = {r["sample_id"]: r["concentration"] for r in reviewed["result"]["final"]}
    after = {r["sample_id"]: r["concentration"] for r in child["result"]["final"]}
    for row in frozen:
        assert after[row["sample_id"]] == before[row["sample_id"]]
        assert row["curve_id"] == "B01|Pb|v1"
    assert child["result"]["review_summary"]["pending"] == 41


def test_new_version_changes_pending_concentration(reviewed, first):
    child = _save(sample_text() + SUPPLEMENT, parent=first)
    row = next(r for r in child["result"]["final"] if r["sample_id"] == "S011")
    # 新标样插入 (6,13.01) 后斜率与截距变化，待复核样品随新标曲重算。
    assert row["curve_id"] == "B01|Pb|v2"
    assert row["concentration"] != "2.0"


def test_replay_same_revision_keeps_one_version(first):
    child = _save(sample_text() + SUPPLEMENT, parent=first)
    replay = _save(sample_text() + SUPPLEMENT, parent=child)
    assert replay["result"]["curves"]["B01|Pb"]["version"] == 2
    assert replay["result"]["curve_events"][0]["status"] == "unchanged"
    assert [c["curve_id"] for c in replay["result"]["curve_registry"]] == ["B01|Pb|v1", "B01|Pb|v2"]
    assert _save(sample_text() + SUPPLEMENT, parent=first)["id"] == child["id"]


def test_failed_curve_update_retains_old_version(first):
    lines = sample_text().splitlines()
    header, data = lines[0], [l for l in lines[1:] if l.strip() and not l.startswith("C")]
    broken = "\n".join([
        header,
        "C0,standard,Pb,100,100,0,1,ug/L,B01",
        "C1,standard,Pb,300,100,1,1,ug/L,B01",
    ] + data) + "\n"
    child = _save(broken, parent=first)
    assert child["result"]["curves"]["B01|Pb"]["version"] == 1
    event = child["result"]["curve_events"][0]
    assert event["status"] == "update_failed" and event["retained_version"] == 1
    assert child["result"]["blocked"] is False
    assert {r["curve_id"] for r in child["result"]["final"]} == {"B01|Pb|v1"}


def test_failed_update_without_prior_curve_blocks():
    lines = sample_text().splitlines()
    header, data = lines[0], [l for l in lines[1:] if l.strip() and not l.startswith("C")]
    broken = "\n".join([
        header,
        "C0,standard,Pb,100,100,0,1,ug/L,B01",
        "C1,standard,Pb,300,100,1,1,ug/L,B01",
    ] + data) + "\n"
    result = run(broken)
    assert result["curve_events"][0]["status"] == "update_failed"
    assert result["curve_events"][0]["retained_version"] is None
    assert result["blocked"] is True


def test_standards_digest_ignores_order():
    digest = standards_digest([(Decimal(1), Decimal(2)), (Decimal(2), Decimal(4))])
    reordered = standards_digest([(Decimal(2), Decimal(4)), (Decimal(1), Decimal(2))])
    assert digest == reordered
    assert digest != standards_digest([(Decimal(1), Decimal(2))])


def test_review_rejects_sample_outside_batch(first):
    with pytest.raises(BusinessError):
        store.review(first["id"], first["revision"], "通过", ["B01|Pb|NOPE"])


def test_review_requires_current_revision(first):
    with pytest.raises(BusinessError, match="版本已更新"):
        store.review(first["id"], first["revision"] + 1, "通过", _keys(first["result"], 2))


def test_partial_review_keeps_others_pending(first):
    reviewed = store.review(first["id"], first["revision"], "退回", _keys(first["result"], 3))
    assert len(reviewed["sample_reviews"]) == 3
    assert reviewed["result"]["review_summary"]["pending"] == 48


def test_export_contains_curve_binding_and_decisions(reviewed):
    text = evidence_csv(reviewed)
    assert "curve_id" in text and "standards_digest" in text
    assert "B01|Pb|v1" in text
    assert '"curve_events"' in text
    frozen_line = next(l for l in text.splitlines() if "B01|Pb|v1" in l and "通过" in l)
    assert "通过" in frozen_line


def test_parent_must_share_analyte_batch(first):
    other = sample_text().replace(",B01", ",B99")
    with pytest.raises(BusinessError, match="分析批"):
        _save(other + "\nC6,standard,Pb,1300,100,6,1,ug/L,B99\n", parent=first)


def test_standards_only_supplement_reuses_parent_rows(reviewed, first):
    header, standards = sample_text().splitlines()[0], [
        line for line in sample_text().splitlines()[1:] if line.startswith("C")
    ]
    text = "\n".join([header] + standards + [SUPPLEMENT.strip()]) + "\n"
    child = _save(text, parent=first)
    assert len(child["result"]["final"]) == 51
    assert child["result"]["curves"]["B01|Pb"]["version"] == 2
    frozen = [row for row in child["result"]["final"] if row.get("frozen")]
    assert len(frozen) == 10
    assert {row["curve_id"] for row in frozen} == {"B01|Pb|v1"}


def test_chained_supplements_keep_history(reviewed, first):
    child = _save(sample_text() + SUPPLEMENT, parent=first)
    more = SUPPLEMENT + "C7,standard,Pb,1500,100,7,1,ug/L,B01\n"
    grandchild = _save(sample_text() + more, parent=child)
    assert grandchild["result"]["curves"]["B01|Pb"]["version"] == 3
    assert [c["curve_id"] for c in grandchild["result"]["curve_registry"]] == [
        "B01|Pb|v1", "B01|Pb|v2", "B01|Pb|v3",
    ]
    assert {row["curve_id"] for row in grandchild["result"]["final"] if row.get("frozen")} == {
        "B01|Pb|v1"
    }


def test_append_only_standard_file_merges_parent_standards(first):
    header = sample_text().splitlines()[0]
    text = "\n".join([header, SUPPLEMENT.strip()]) + "\n"
    child = _save(text, parent=first)
    curve = child["result"]["curves"]["B01|Pb"]
    assert curve["version"] == 2 and curve["standard_points"] == 7
    assert child["result"]["balance"]["input"] == 1
    assert child["result"]["review_summary"]["pending"] == 51


def test_all_samples_reviewed_leaves_nothing_pending(first):
    reviewed = store.review(first["id"], first["revision"], "通过")
    child = _save(sample_text() + SUPPLEMENT, parent=reviewed)
    assert child["result"]["review_summary"]["pending"] == 0
    assert child["result"]["review_summary"]["frozen"] == 51
    assert child["result"]["blocked"] is False
    assert {row["curve_id"] for row in child["result"]["final"]} == {"B01|Pb|v1"}
    assert {row["decision"] for row in child["result"]["final"]} == {"通过"}
