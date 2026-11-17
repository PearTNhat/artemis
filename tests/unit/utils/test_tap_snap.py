"""Tests for snapping a model-estimated tap onto the element it names."""

from artemis.utils.tap_snap import snap_to_named_element

W, H = 1280, 2772

# Grab restaurant screen from trace 594a6455 (step 7): the model aimed at the
# Basket bar with [500, 887], which landed ~120 px above it.
BASKET = {"text": "Basket • 1 Item", "bounds": [80, 2490, 1200, 2670]}
PRICE = {"text": "58.500₫", "bounds": [930, 2540, 1170, 2620]}
GRABMORE = {"text": "GrabMore", "bounds": [430, 2190, 840, 2330]}
DISH = {"text": "Phá lấu bò thập cẩm 500gr", "bounds": [690, 2040, 1180, 2160]}
ELEMENTS = [DISH, GRABMORE, BASKET, PRICE]


def test_near_miss_snaps_to_center_of_named_element():
    snap = snap_to_named_element([500, 887], "green Basket 1 Item button", ELEMENTS, W, H)
    assert snap is not None
    assert snap.element_text == "Basket • 1 Item"
    assert snap.point == [500, round(2580 * 1000 / H)]


def test_point_inside_named_element_is_centered():
    snap = snap_to_named_element([150, 940], "Basket button", ELEMENTS, W, H)
    assert snap is not None
    assert snap.point == [500, round(2580 * 1000 / H)]


def test_far_point_is_not_snapped():
    assert snap_to_named_element([500, 300], "Basket button", ELEMENTS, W, H) is None


def test_relational_description_is_not_snapped_to_label():
    desc = "plus button for Phá lấu bò thập cẩm"
    assert snap_to_named_element([920, 760], desc, ELEMENTS, W, H) is None


def test_control_word_not_in_element_text_is_not_snapped():
    desc = "nút thêm món Phá lấu"
    assert snap_to_named_element([920, 760], desc, ELEMENTS, W, H) is None


def test_long_label_sharing_one_word_is_not_snapped():
    assert snap_to_named_element([700, 750], "phá lấu", ELEMENTS, W, H) is None


def test_unrelated_description_is_not_snapped():
    assert snap_to_named_element([500, 887], "video thumbnail", ELEMENTS, W, H) is None


def test_diacritics_are_ignored():
    elements = [{"text": "Giỏ hàng", "bounds": [80, 2490, 1200, 2670]}]
    snap = snap_to_named_element([500, 887], "gio hang", elements, W, H)
    assert snap is not None and snap.point[1] == round(2580 * 1000 / H)


def test_string_bounds_and_missing_inputs():
    elements = [{"text": "Basket", "bounds": "[80,2490][1200,2670]"}]
    assert snap_to_named_element([500, 887], "Basket", elements, W, H) is not None
    assert snap_to_named_element([500, 887], None, elements, W, H) is None
    assert snap_to_named_element([500, 887], "Basket", [], W, H) is None
