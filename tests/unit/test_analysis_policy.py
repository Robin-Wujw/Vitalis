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
