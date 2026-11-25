"""App names resolve to an installed package without the LLM when unambiguous."""

from third_party.mobile_use.tools.mobile.launch_app import match_package_by_name

PKGS = {
    "com.shopee.vn",
    "com.grabtaxi.passenger",
    "xyz.be.customer",
    "xyz.be.cake",
    "com.google.android.youtube",
    "com.google.android.apps.youtube.music",
    "com.bachhoaxanh",
}


def test_unique_segment_match():
    assert match_package_by_name("Shopee", PKGS) == "com.shopee.vn"


def test_unique_substring_match():
    assert match_package_by_name("Grab", PKGS) == "com.grabtaxi.passenger"
    assert match_package_by_name("Bach hoa xanh", PKGS) == "com.bachhoaxanh"


def test_ambiguous_or_short_names_go_to_llm():
    assert match_package_by_name("YouTube", PKGS) is None  # two packages
    assert match_package_by_name("Be", PKGS) is None  # too short
    assert match_package_by_name("Lazada", PKGS) is None  # not installed
