import pytest

from app.services import awg_profile


def test_generated_profiles_pass_validation():
    # the generator is random → check in bulk that the constraints always hold
    for _ in range(200):
        p = awg_profile.generate_profile()
        awg_profile.validate_profile(p)  # must not raise
        assert p["s1"] + 56 != p["s2"]
        headers = [p["h1"], p["h2"], p["h3"], p["h4"]]
        assert len(set(headers)) == 4
        assert all(h > 4 for h in headers)
        assert p["jmin"] < p["jmax"] <= 1280


def test_validate_rejects_s1_plus_56_equals_s2():
    p = awg_profile.generate_profile()
    p["s2"] = p["s1"] + 56
    with pytest.raises(ValueError):
        awg_profile.validate_profile(p)


def test_validate_rejects_duplicate_headers():
    p = awg_profile.generate_profile()
    p["h2"] = p["h1"]
    with pytest.raises(ValueError):
        awg_profile.validate_profile(p)


def test_profile_from_node_none_when_unset(node):
    # a vanilla WG node without awg_* → no profile
    assert awg_profile.profile_from_node(node) is None
