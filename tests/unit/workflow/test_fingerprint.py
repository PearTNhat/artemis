# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for artemis.workflow.fingerprint (Mục 2.1–2.2)."""

from artemis.workflow.fingerprint import (
    SYSTEMUI_PACKAGE,
    foreground_package,
    normalize,
    size_bucket,
    tokenize,
)


def _node(
    *,
    cls: str = "android.widget.TextView",
    rid: str = "",
    pkg: str = "com.example.app",
    bounds: str = "[0,0][500,200]",
    clickable: str = "false",
    scrollable: str = "false",
    focused: str = "false",
    checkable: str = "false",
    enabled: str = "true",
    visible: str = "true",
    text: str = "",
    children: list | None = None,
) -> dict:
    item = {
        "class": cls,
        "resource-id": rid,
        "package": pkg,
        "bounds": bounds,
        "clickable": clickable,
        "scrollable": scrollable,
        "focused": focused,
        "checkable": checkable,
        "enabled": enabled,
        "visible-to-user": visible,
        "text": text,
    }
    if children is not None:
        item["children"] = children
    return item


def test_normalize_flattens_nested_and_scales_bounds_to_1000():
    nested = [
        _node(
            cls="android.widget.FrameLayout",
            bounds="[0,0][1080,2400]",
            children=[
                _node(
                    cls="android.widget.Button",
                    rid="com.example.app:id/go",
                    bounds="[270,1200][810,1440]",
                    clickable="true",
                    text="Go",
                )
            ],
        )
    ]
    norm = normalize(nested, width=1080, height=2400)
    assert len(norm) == 2
    assert norm[0]["bounds"] == [0, 0, 1000, 1000]
    assert norm[1]["bounds"] == [250, 500, 750, 600]
    assert norm[1]["clickable"] is True
    assert norm[1]["resource-id"] == "com.example.app:id/go"


def test_normalize_handles_parsed_bounds_fallback_and_empty():
    assert normalize(None) == []
    assert normalize([]) == []
    raw = [
        {
            "class": "android.view.View",
            "package": "com.example.app",
            "parsed_bounds": {"left": 0, "top": 0, "right": 360, "bottom": 640},
        }
    ]
    norm = normalize(raw, width=720, height=1280)
    assert norm[0]["bounds"] == [0, 0, 500, 500]
    assert norm[0]["enabled"] is True
    assert norm[0]["visible-to-user"] is True


def test_tokenize_filters_systemui_invisible_and_tiny_nodes():
    norm = normalize(
        [
            _node(pkg=SYSTEMUI_PACKAGE, bounds="[0,0][1000,50]", text="12:00"),
            _node(pkg="com.example.app", bounds="[0,100][500,300]", visible="false"),
            # 20 x 20 = 400 < 1000 (0.1% of 1000x1000)
            _node(pkg="com.example.app", bounds="[10,100][30,120]", clickable="true"),
            # Valid node: 200 x 100 = 20000 >= 1000
            _node(
                pkg="com.example.app",
                rid="com.example.app:id/search",
                bounds="[100,100][300,200]",
                clickable="true",
                text="Pho",
            ),
        ],
        width=1000,
        height=1000,
    )
    fp = tokenize(norm)
    assert fp.package == "com.example.app"
    assert fp.node_count == 1
    assert sum(fp.tokens.values()) == 1
    assert fp.app_token_count == 1


def test_tokenize_excludes_dynamic_text_unless_static_text_provided():
    screen_pho = normalize(
        [
            _node(rid="com.example.app:id/title", bounds="[50,100][950,200]", text="Phở bò"),
            _node(rid="com.example.app:id/cta", bounds="[50,800][950,900]", clickable="true", text="Search"),
        ],
        width=1000,
        height=1000,
    )
    screen_tea = normalize(
        [
            _node(rid="com.example.app:id/title", bounds="[50,100][950,200]", text="Trà sữa"),
            _node(rid="com.example.app:id/cta", bounds="[50,800][950,900]", clickable="true", text="Search"),
        ],
        width=1000,
        height=1000,
    )
    fp_pho = tokenize(screen_pho)
    fp_tea = tokenize(screen_tea)
    assert fp_pho.tokens == fp_tea.tokens

    fp_static = tokenize(screen_pho, static_texts={"Search"})
    assert any(tok[0] == "text" for tok in fp_static.tokens)
    assert sum(v for k, v in fp_static.tokens.items() if k[0] == "text") == 1


def test_tokenize_deduplicates_children_inside_scrollable_container():
    # Flat pre-order dump: RecyclerView followed by 4 identical-type item cards
    flat_many = normalize(
        [
            _node(
                cls="androidx.recyclerview.widget.RecyclerView",
                rid="com.example.app:id/list",
                bounds="[0,100][1000,900]",
                scrollable="true",
            ),
            _node(cls="android.widget.TextView", rid="com.example.app:id/item", bounds="[50,120][950,220]", clickable="true", text="Item 1"),
            _node(cls="android.widget.TextView", rid="com.example.app:id/item", bounds="[50,240][950,340]", clickable="true", text="Item 2"),
            _node(cls="android.widget.TextView", rid="com.example.app:id/item", bounds="[50,360][950,460]", clickable="true", text="Item 3"),
            _node(cls="android.widget.TextView", rid="com.example.app:id/item", bounds="[50,480][950,580]", clickable="true", text="Item 4"),
        ],
        width=1000,
        height=1000,
    )
    flat_one = normalize(
        [
            _node(
                cls="androidx.recyclerview.widget.RecyclerView",
                rid="com.example.app:id/list",
                bounds="[0,100][1000,900]",
                scrollable="true",
            ),
            _node(cls="android.widget.TextView", rid="com.example.app:id/item", bounds="[50,120][950,220]", clickable="true", text="Other"),
        ],
        width=1000,
        height=1000,
    )
    fp_many = tokenize(flat_many)
    fp_one = tokenize(flat_one)
    assert fp_many.tokens == fp_one.tokens
    child_tok = ("scroll_child", "android.widget.TextView", "com.example.app:id/item", "clickable")
    assert fp_many.tokens[child_tok] == 1


def test_foreground_package_prefers_full_app_over_keyboard_overlay_and_size_buckets():
    nodes = normalize(
        [
            _node(pkg="com.example.app", bounds="[0,0][1000,600]"),
            _node(pkg="com.google.android.inputmethod.latin", bounds="[0,600][300,700]", clickable="true"),
            _node(pkg="com.google.android.inputmethod.latin", bounds="[300,600][600,700]", clickable="true"),
            _node(pkg="com.google.android.inputmethod.latin", bounds="[600,600][900,700]", clickable="true"),
        ],
        width=1000,
        height=1000,
    )
    assert foreground_package(nodes) == "com.example.app"
    assert [size_bucket(v) for v in (20, 80, 200, 500, 850)] == [0, 1, 2, 3, 4]
