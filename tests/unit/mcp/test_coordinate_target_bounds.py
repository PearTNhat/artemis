"""A copied element bounding box is accepted as a point target (its center)."""

import pytest

from artemis.agents.validator.tool_declarations import normalize_coordinate_target


@pytest.mark.parametrize(
    "raw, expected",
    [
        ([101, 81, 384, 97], [242, 89]),
        ((0, 0, 1000, 1000), [500, 500]),
        (["101", "81", "384", "97"], [242, 89]),
        ("[101, 81, 384, 97]", [242, 89]),
        ("101, 81, 384, 97", [242, 89]),
        ([100.4, 80.6, 384, 97], [242, 89]),
    ],
)
def test_bounding_box_becomes_its_center(raw, expected):
    assert normalize_coordinate_target(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        [384, 81, 101, 97],  # left > right
        [101, 97, 384, 81],  # top > bottom
        [101, 81, 1384, 97],  # outside the 0-1000 grid (pixels)
        [-5, 81, 384, 97],
        ["a", 81, 384, 97],
    ],
)
def test_malformed_box_is_left_unchanged(raw):
    assert normalize_coordinate_target(raw) == raw


def test_points_and_indices_unchanged():
    assert normalize_coordinate_target([500, 280]) == [500, 280]
    assert normalize_coordinate_target("7") == 7
    assert normalize_coordinate_target(3) == 3
