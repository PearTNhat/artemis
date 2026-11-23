"""Compact list: layers on top hide the screen below, cut-off strips, OCR gap filling."""

from artemis.utils.visualization import (
    find_overlay_hidden,
    format_minimal_list_with_elements,
    gap_fill_ocr,
    verified_overlay_hidden,
)

W, H = 1000, 2000


def node(bounds, text="", clickable=False, cls="android.widget.TextView"):
    l, t, r, b = bounds
    return {
        "bounds": f"[{l},{t}][{r},{b}]",
        "text": text,
        "clickable": "true" if clickable else "false",
        "class": cls,
        "package": "com.example",
    }


# Screen below (root, two rows and a pinned bottom button), then a sheet on top.
SCREEN_BELOW = [
    node((0, 100, 1000, 2000), clickable=True, cls="android.view.ViewGroup"),
    node((40, 300, 600, 380), "Restaurant"),
    node((40, 700, 600, 780), "Subtotal"),
    node((100, 1700, 900, 1800), "Place Order", clickable=True, cls="android.widget.Button"),
]
SHEET = [
    node((0, 600, 1000, 1900), clickable=True, cls="android.widget.LinearLayout"),
    node((40, 700, 600, 780), "Delivery instructions"),
    node((40, 800, 600, 880), "Meet at lobby"),
    node((400, 1880, 520, 1896), clickable=True, cls="android.view.View"),  # strip cut by a pinned bar
]


def test_sheet_hides_the_screen_below():
    nodes = SCREEN_BELOW + SHEET
    hidden, layers = find_overlay_hidden(nodes, W, H)
    assert layers == [(0, 600, 1000, 1900)]
    # Root container is not inside the sheet; the rows are above it (not hidden).
    assert {2, 3} <= hidden  # "Subtotal" and "Place Order" lie under the sheet
    assert 0 not in hidden and 1 not in hidden  # root (encloses the sheet) and the row above it


def _ocr(text, l, t, r, b, score=0.95):
    return {"text": text, "score": score,
            "position": [{"x": l, "y": t}, {"x": r, "y": t}, {"x": r, "y": b}, {"x": l, "y": b}]}


SHEET_SCREEN = [
    node((0, 100, 1000, 2000), clickable=True, cls="android.view.ViewGroup"),
    node((40, 700, 600, 780), "Subtotal"),
    node((40, 800, 600, 880), "Delivery fee"),
    node((100, 1700, 900, 1800), "Place Order", clickable=True, cls="android.widget.Button"),
] + SHEET[:3]
# What the pixels show: only the sheet's own text.
SHEET_PIXELS = [_ocr("Delivery instructions", 45, 705, 590, 775), _ocr("Meet at lobby", 45, 805, 590, 875)]


def test_ocr_confirms_what_the_sheet_covers():
    hidden = verified_overlay_hidden(SHEET_SCREEN, SHEET_PIXELS, W, H)
    assert hidden == {1, 2, 3}


def test_readable_candidates_are_not_hidden():
    # The tree order suggests a layer, but the pixels still show the rows below it.
    pixels = SHEET_PIXELS + [_ocr("Subtotal", 45, 705, 300, 775), _ocr("Delivery fee", 45, 805, 300, 875),
                             _ocr("Place Order", 300, 1720, 700, 1780)]
    assert verified_overlay_hidden(SHEET_SCREEN, pixels, W, H) == set()
    assert verified_overlay_hidden(SHEET_SCREEN, [], W, H) == set()  # no OCR, no hiding


def test_compact_list_drops_covered_elements_and_says_so():
    hidden = verified_overlay_hidden(SHEET_SCREEN, SHEET_PIXELS, W, H)
    text, elements, _ = format_minimal_list_with_elements(SHEET_SCREEN, W, H, compact=True, hidden=hidden)
    assert "Place Order" not in text and "Subtotal" not in text
    assert '"Meet at lobby"' in text
    assert "(a layer on top covers the screen below: 3 elements under it are not listed)" in text
    assert all(el["text"] != "Place Order" for el in elements)


def test_normal_screen_is_untouched():
    # Pre-order: the container comes before its own children, so nothing is "below" it.
    nodes = [
        node((0, 100, 1000, 2000), clickable=True, cls="android.view.ViewGroup"),
        node((40, 300, 600, 380), "Wi-Fi"),
        node((40, 400, 600, 480), "Bluetooth"),
        node((40, 500, 600, 580), "Display"),
    ]
    hidden, layers = find_overlay_hidden(nodes, W, H)
    assert not hidden and not layers
    text, _, _ = format_minimal_list_with_elements(nodes, W, H, compact=True)
    assert "layer on top" not in text and '"Bluetooth"' in text


def test_full_list_format_is_unchanged():
    text, _, _ = format_minimal_list_with_elements(SCREEN_BELOW + SHEET, W, H, compact=False, hidden={3})
    assert "Place Order" in text and "layer on top" not in text


def test_cut_off_strip_is_flagged_not_dropped():
    text, elements, _ = format_minimal_list_with_elements(SHEET, W, H, compact=True)
    strip = [el for el in elements if el.get("cut_off")]
    assert len(strip) == 1
    assert "(cut off: only a thin strip is visible; scroll to see it whole)" in text


def test_gap_fill_adds_only_text_outside_listed_elements():
    elements = [
        {"index": 1, "text": "Meet at lobby", "bounds": [40, 800, 600, 880], "center": [320, 840]},
        {"index": 2, "text": "Other", "bounds": [40, 900, 600, 980], "center": [320, 940]},
    ]
    results = [
        _ocr("Meet at lobby", 45, 810, 400, 870),  # already listed
        _ocr("Confirm address", 330, 1900, 670, 1950),  # pinned button missing from the tree
        _ocr("12:30", 20, 10, 120, 50),  # status bar band
        _ocr("x", 700, 1000, 720, 1020),  # too short
        _ocr("blurry", 700, 1100, 800, 1150, score=0.2),  # low confidence
    ]
    lines, added = gap_fill_ocr(results, elements, W, H)
    assert lines == ['[3] ocr "Confirm address" 330,950,670,975']
    assert added[0]["index"] == 3 and added[0]["center"] == [500, 1925] and added[0]["ocr_virtual"]


def test_asterisk_marks_the_label_required_and_disappears():
    nodes = [
        node((40, 700, 500, 760), "Delivery instructions"),
        node((510, 705, 530, 755), "*"),
        node((40, 800, 500, 860), "Notes*"),
        node((40, 900, 500, 960), "Cutlery"),
        node((900, 100, 920, 140), "*"),  # a lone star far from any label stays as text
    ]
    text, elements, _ = format_minimal_list_with_elements(nodes, W, H, compact=True)
    assert '"Delivery instructions" 40,350,500,380 (required)' in text
    assert '"Notes*" 40,400,500,430 (required)' in text
    assert '"Cutlery" 40,450,500,480' in text and "Cutlery\" 40,450,500,480 (required)" not in text
    assert text.count('"*"') == 1
    full, _, _ = format_minimal_list_with_elements(nodes, W, H, compact=False)
    assert "(required)" not in full
