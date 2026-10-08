"""Analysis policy fingerprints must be stable and exclude runtime credentials."""

from vitalis.application.analysis_policy import (
    analysis_policy_digest,
    installed_analysis_rules_digest,
)


def test_policy_digest_changes_only_with_declared_analysis_inputs():
    values = ("Asia/Shanghai", "14.0", "9.0", "2026-09a", "catalog-1", "rules-1")
    original = analysis_policy_digest(*values)
    assert len(original) == 64
    assert original == analysis_policy_digest(*values)
    for index in range(len(values)):
        changed = list(values)
        changed[index] += "-new"
        assert analysis_policy_digest(*changed) != original


def test_shipped_rule_fingerprint_is_stable_and_nonempty():
    digest = installed_analysis_rules_digest()
    assert len(digest) == 64
    assert digest == installed_analysis_rules_digest()


def test_presentation_revision_does_not_invalidate_analysis_facts(tmp_path, monkeypatch):
    from vitalis.application import analysis_policy

    roots = {}
    for package in ("vitalis", "vitalis.intelligence", "vitalis.domain", "vitalis.application", "vitalis.adapters.zepp"):
        root = tmp_path / package
        root.mkdir()
        roots[package] = root
    semantic = roots["vitalis.intelligence"] / "analyzers.py"
    semantic.write_text("rules = 1\n", encoding="utf-8")
    renderer = roots["vitalis.intelligence"] / "report_rendering.py"
    renderer.write_text("font_size = 16\n", encoding="utf-8")
    (roots["vitalis"] / "time.py").write_text("timezone = 'UTC'\n", encoding="utf-8")
    (roots["vitalis.adapters.zepp"] / "catalog.py").write_text("catalog = 1\n", encoding="utf-8")
    catalog = roots["vitalis.adapters.zepp"] / "data"
    catalog.mkdir()
    (catalog / "strength_exercises.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(analysis_policy.resources, "files", lambda package: roots[package])

    def fingerprint():
        installed_analysis_rules_digest.cache_clear()
        return installed_analysis_rules_digest()

    try:
        original = fingerprint()
        renderer.write_text("font_size = 28\n", encoding="utf-8")
        assert fingerprint() == original
        semantic.write_text("rules = 2\n", encoding="utf-8")
        assert fingerprint() != original
    finally:
        installed_analysis_rules_digest.cache_clear()
