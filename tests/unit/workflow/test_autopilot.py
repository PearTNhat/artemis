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

"""Record -> verified replay of an app workflow on a fake, app-agnostic screen machine."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from artemis.agents.flash.workflow_hook import FlashWorkflowHook
from artemis.workflow.autopilot import Autopilot
from artemis.workflow.recorder import RawScreen, RecordedStep, detect_slots, record_run
from artemis.workflow.selector import select_workflow
from artemis.workflow.store import Workflow, WorkflowStore

PKG = "com.example.shop"


def _n(rid, l, t, r, b, *, cls="android.widget.TextView", text="", clickable=False, **kw):
    node = {
        "package": kw.pop("package", PKG),
        "class": cls,
        "resource-id": f"{PKG}:id/{rid}" if rid else "",
        "bounds": f"[{l},{t}][{r},{b}]",
        "clickable": "true" if clickable else "false",
        "visible-to-user": "true",
        "text": text,
    }
    node.update({k: ("true" if v is True else v) for k, v in kw.items()})
    return node


def _chrome():
    return [
        _n("toolbar", 0, 0, 1000, 80, cls="android.view.ViewGroup"),
        _n("tab_1", 0, 920, 250, 1000, clickable=True),
        _n("tab_2", 250, 920, 500, 1000, clickable=True),
        _n("tab_3", 500, 920, 750, 1000, clickable=True),
    ]


def home():
    return _chrome() + [
        _n("home_title", 20, 10, 400, 70, text="Home"),
        _n("search_bar", 20, 100, 980, 170, cls="android.widget.EditText", text="Search", clickable=True),
        _n("banner", 20, 200, 980, 500, cls="android.widget.ImageView"),
    ]


def search(typed=""):
    return _chrome() + [
        _n("back_btn", 0, 10, 80, 70, cls="android.widget.ImageButton", clickable=True),
        _n("search_input", 100, 10, 980, 70, cls="android.widget.EditText", text=typed, clickable=True,
           editable=True, focused=True),
        _n("recent_header", 20, 100, 500, 150, text="Recent"),
        _n("clear_recent", 800, 100, 980, 150, text="Clear", clickable=True),
    ]


def results(item):
    return _chrome() + [
        _n("results_header", 20, 10, 600, 70, text="Results"),
        _n("filter_btn", 850, 10, 980, 70, cls="android.widget.ImageButton", clickable=True),
        _n("result_list", 0, 100, 1000, 900, cls="androidx.recyclerview.widget.RecyclerView", scrollable=True),
        _n("item_title", 20, 120, 980, 300, text=f"{item} special", clickable=True),
        _n("item_title", 20, 320, 980, 500, text="Something else", clickable=True),
    ]


def detail(item):
    return _chrome() + [
        _n("detail_title", 20, 10, 980, 70, text=item),
        _n("detail_image", 20, 100, 980, 500, cls="android.widget.ImageView"),
        _n("qty", 20, 520, 300, 600, text="1"),
        _n("add_btn", 20, 820, 980, 900, cls="android.widget.Button", text="Add to basket", clickable=True),
    ]


def cart():
    return _chrome() + [
        _n("cart_title", 20, 10, 980, 70, text="Basket"),
        _n("cart_line", 20, 100, 980, 200, text="1 item"),
        _n("cart_total", 20, 600, 980, 700, text="Total"),
        _n("place_order", 20, 820, 980, 900, cls="android.widget.Button", text="Place order", clickable=True),
    ]


def other_app():
    return [_n(f"x{i}", 0, i * 100, 1000, i * 100 + 90, package="com.other.app") for i in range(8)]


def raw(nodes):
    return RawScreen(nodes, 1000, 1000)


def step(tool, args, pre, post, *, point=None, bounds=None, choice=False):
    action = {"action": tool, "args": args}
    if point:
        action["normalized_coordinates"] = list(point)
    if bounds:
        action["target_bounds"] = list(bounds)
    return RecordedStep(tool=tool, args=args, action=action, pre=raw(pre), post=raw(post), after_user_choice=choice)


GOAL = "Search for pho bo and add it to the basket, stop before ordering"


def recorded_run():
    return [
        step("click", {"target": 2}, home(), search(), point=(500, 135)),
        step("input_text", {"target": 2, "text": "pho bo"}, search(), search("pho bo"), point=(540, 40)),
        step("press_key", {"key": "ENTER"}, search("pho bo"), results("pho bo")),
        step("click", {"target": 4}, results("pho bo"), detail("pho bo special"), point=(500, 210), choice=True),
        step("click", {"target": 4}, detail("pho bo special"), cart(), point=(500, 860)),
        step("click", {"target": 4}, cart(), cart(), point=(500, 860)),
    ]


class FakeApp:
    """Screen machine reacting to the autopilot's actions like an app would."""

    def __init__(self, screen, *, enter_leads_to=None):
        self.screen = screen
        self.typed = ""
        self.enter_leads_to = enter_leads_to
        self.actions: list[tuple[str, dict]] = []

    def current(self):
        return raw(self.screen)

    async def refresh(self):
        return self.current()

    async def act(self, tool, args, element):
        self.actions.append((tool, args))
        rid = (element or {}).get("resource-id", "").split("/")[-1]
        if tool == "click" and rid == "search_bar":
            self.screen = search()
        elif tool == "input_text":
            self.typed = args["text"]
            self.screen = search(self.typed)
        elif tool == "press_key" and args["key"] == "ENTER":
            self.screen = self.enter_leads_to() if self.enter_leads_to else results(self.typed)
        elif tool == "click" and rid == "add_btn":
            self.screen = cart()
        elif tool == "click" and rid == "item_title":
            self.screen = detail(element.get("text", ""))
        else:
            return False, "unexpected action"
        return True, "ok"


def build_wf():
    steps = recorded_run()
    wf = record_run(steps, goal=GOAL, workflow_id="shop__1")
    assert wf is not None
    return wf


def build_confirmed_wf():
    """Two independent successful runs so nav steps in a commitment workflow are active."""
    wf = build_wf()
    record_run(recorded_run(), goal=GOAL, workflow=wf, slot_values={"slot1": "pho bo"})
    return wf


def test_recorder_classifies_steps_by_ui_pattern():
    wf = build_wf()
    assert wf.package == PKG
    assert wf.slots == {"slot1": "pho bo"}
    kinds = [(e.tool, e.kind) for e in wf.edges]
    assert kinds == [
        ("click", "nav"),            # open the search field
        ("input_text", "slot"),      # type the task value
        ("press_key", "nav"),        # run the search
        ("click", "judgement"),      # pick a result after asking the user
        ("click", "nav"),            # fixed action button on the detail screen
        ("", "commitment_stop"),     # never replayed
    ]
    nav = wf.edges[0]
    assert nav.target.resource_id.endswith("search_bar")
    assert nav.dst is not None and nav.anchors
    assert wf.edges[1].slot == "slot1"


def test_detect_slots_only_keeps_text_from_goal():
    steps = [
        step("input_text", {"text": "pho bo"}, search(), search("pho bo")),
        step("input_text", {"text": "secret note"}, search(), search("secret note")),
    ]
    assert detect_slots(steps, GOAL) == {"slot1": "pho bo"}


def test_result_pick_without_user_choice_is_a_slot_pick():
    steps = recorded_run()
    steps[3].after_user_choice = False
    wf = record_run(steps, goal=GOAL)
    pick = wf.edges[3]
    assert pick.kind == "slot" and pick.target.slot == "slot1"


def test_replay_with_new_slot_value_stops_at_the_choice():
    wf = build_confirmed_wf()
    app = FakeApp(home())
    rep = asyncio.run(Autopilot(wf, {"slot1": "bun cha"}, poll_interval_s=0).run(app))
    assert [a[0] for a in app.actions] == ["click", "input_text", "press_key"]
    assert app.actions[1][1]["text"] == "bun cha"
    assert rep.steps == 3
    assert "choice" in rep.stop_reason
    assert app.screen == results("bun cha")
    assert all(e.ok == 1 for e in wf.edges[:3])


def test_replay_stops_when_destination_differs():
    wf = build_confirmed_wf()
    app = FakeApp(home(), enter_leads_to=other_screen_same_app)
    rep = asyncio.run(Autopilot(wf, {"slot1": "bun cha"}, poll_interval_s=0).run(app))
    assert rep.steps == 2
    assert rep.acted == 3  # the unverified action still counts: the AI must re-observe
    assert "NOT the recorded screen" in rep.notice("shop__1")
    assert "not the recorded next screen" in rep.stop_reason
    enter = wf.edges[2]
    assert enter.fail == 1 and enter.consec_fail == 1


def other_screen_same_app():
    return _chrome() + [
        _n("error_title", 20, 10, 980, 70, text="Oops"),
        _n("error_body", 20, 100, 980, 400, text="Try again later"),
        _n("retry", 20, 820, 980, 900, cls="android.widget.Button", text="Retry", clickable=True),
    ]


def test_edge_disabled_after_two_consecutive_failures():
    wf = build_confirmed_wf()
    for _ in range(2):
        app = FakeApp(home(), enter_leads_to=other_screen_same_app)
        asyncio.run(Autopilot(wf, {"slot1": "bun cha"}, poll_interval_s=0).run(app))
    assert wf.edges[2].state == "disabled"


def test_missing_slot_value_hands_over_before_typing():
    wf = build_confirmed_wf()
    app = FakeApp(home())
    rep = asyncio.run(Autopilot(wf, {}, poll_interval_s=0).run(app))
    assert rep.steps == 1
    assert "slot1" in rep.stop_reason


def test_never_passes_a_commitment_stop():
    wf = build_confirmed_wf()
    app = FakeApp(detail("bun cha special"))
    rep = asyncio.run(Autopilot(wf, {"slot1": "bun cha"}, poll_interval_s=0).run(app))
    assert rep.steps == 1  # add to basket
    assert "commitment" in rep.stop_reason
    assert app.screen == cart()


def test_nav_target_showing_commitment_wording_is_refused():
    wf = build_wf()
    screen = detail("x")
    screen[-1] = dict(screen[-1], text="Place order")
    app = FakeApp(screen)
    rep = asyncio.run(Autopilot(wf, {"slot1": "x"}, poll_interval_s=0).run(app))
    assert rep.steps == 0 and app.actions == []


def test_other_app_does_nothing():
    wf = build_wf()
    app = FakeApp(other_app())
    rep = asyncio.run(Autopilot(wf, {"slot1": "x"}, poll_interval_s=0).run(app))
    assert rep.steps == 0 and app.actions == []
    assert "foreground app" in rep.stop_reason


def test_merge_does_not_duplicate_edges():
    wf = build_wf()
    n_edges = len(wf.edges)
    record_run(recorded_run(), goal=GOAL, workflow=wf, slot_values={"slot1": "pho bo"})
    assert len(wf.edges) == n_edges


def test_store_roundtrip(tmp_path):
    store = WorkflowStore(tmp_path)
    wf = build_wf()
    store.save(wf)
    store.save(wf)  # second save keeps a .bak
    loaded = store.list(PKG)
    assert len(loaded) == 1
    again = loaded[0]
    assert again.to_json()["edges"] == wf.to_json()["edges"]
    assert {k: [f.tokens for f in n.fps] for k, n in again.nodes.items()} == {
        k: [f.tokens for f in n.fps] for k, n in wf.nodes.items()
    }
    assert list(tmp_path.glob("*/*.bak"))


def test_selector_drops_values_not_in_the_goal():
    wf = build_wf()

    async def ask(_prompt):
        return '{"workflow": 1, "slots": {"slot1": "invented dish"}}'

    picked, values = asyncio.run(select_workflow("Search for bun cha and add it", [wf], ask))
    assert picked is wf and values == {}


def test_selector_accepts_value_copied_from_goal():
    wf = build_wf()

    async def ask(_prompt):
        return 'sure: {"workflow": 1, "slots": {"slot1": "bun cha"}}'

    picked, values = asyncio.run(select_workflow("Search for bun cha and add it", [wf], ask))
    assert picked is wf and values == {"slot1": "bun cha"}


def test_selector_same_goal_needs_no_llm():
    wf = build_wf()

    async def ask(_prompt):  # pragma: no cover - must not be called
        raise AssertionError("LLM called")

    picked, values = asyncio.run(select_workflow(GOAL, [wf], ask))
    assert picked is wf and values == wf.slots


def test_hook_drops_pre_screen_of_unobserved_burst_action(tmp_path):
    ctx = SimpleNamespace(last_raw_observation={"elements": home(), "width": 1000, "height": 1000, "seq": 7})
    hook = FlashWorkflowHook(SimpleNamespace(ctx=ctx, goal=GOAL), "record", str(tmp_path))
    res = SimpleNamespace(status="success", metadata={}, text_summary="ok")
    pre = hook.snapshot()
    hook.after_action("click", {"target": 1}, {}, res, pre)  # skip_observe: seq unchanged
    pre2 = hook.snapshot()
    hook.after_action("click", {"target": 2}, {}, res, pre2)
    assert hook.steps[0].pre is not None and hook.steps[0].post is None
    assert hook.steps[1].pre is None


def test_hook_finish_records_only_completed_runs(tmp_path):
    runner = SimpleNamespace(ctx=SimpleNamespace(), goal=GOAL)
    hook = FlashWorkflowHook(runner, "record", str(tmp_path))
    hook.steps = recorded_run()
    asyncio.run(hook.finish({"status": "failed"}))
    assert WorkflowStore(tmp_path).list(PKG) == []
    asyncio.run(hook.finish({"status": "completed"}))
    saved = WorkflowStore(tmp_path).list(PKG)
    assert len(saved) == 1 and saved[0].runs == 1


@pytest.mark.parametrize("mode", ["off", "bogus"])
def test_hook_off_records_nothing(tmp_path, mode, monkeypatch):
    monkeypatch.delenv("ARTEMIS_WORKFLOW_MODE", raising=False)
    from artemis.agents.flash.workflow_hook import resolve_mode

    m, _, _ = resolve_mode({"mode": mode})
    assert m == "off"


def test_floating_button_over_a_list_is_not_a_list_item():
    from artemis.workflow.fingerprint import normalize
    from artemis.workflow.targets import is_list_item

    screen = results("pho bo") + [
        _n("fab", 400, 700, 600, 800, cls="android.widget.ImageButton", text="Add", clickable=True)
    ]
    norm = normalize(screen, width=1000, height=1000)
    fab = next(n for n in norm if n["resource-id"].endswith("fab"))
    item = next(n for n in norm if n["text"] == "pho bo special")
    assert fab["in_scrollable"]  # geometry alone says "inside the list"
    assert not is_list_item(fab, norm)
    assert is_list_item(item, norm)


def test_keyboard_key_is_not_occluded_by_the_app_window_listed_after_it():
    from artemis.workflow.fingerprint import normalize
    from artemis.workflow.targets import TargetDescriptor, resolve

    ime = "com.example.keyboard"
    screen = [
        _n("", 0, 600, 1000, 1000, cls="android.widget.FrameLayout", package=ime),
        {**_n("", 840, 850, 990, 920, cls="android.widget.FrameLayout", text="Search", clickable=True, package=ime),
         "resource-id": f"{ime}:id/key_action"},
    ] + search("pho bo") + [_n("search_root", 0, 0, 1000, 1000, cls="android.widget.FrameLayout")]
    norm = normalize(screen, width=1000, height=1000)
    node, status = resolve(TargetDescriptor(resource_id=f"{ime}:id/key_action"), norm)
    assert status == "ok" and node is not None


def _keyboard(pkg="com.example.keyboard", shifted=False):
    keys = [
        {**_n("", 10 + i * 95, 700 + (i % 3) * 80, 95 + i * 95, 770 + (i % 3) * 80,
              cls="android.widget.FrameLayout", text=("Q" if shifted else "q") + str(i), clickable=True,
              package=pkg), "resource-id": f"{pkg}:id/key_{i}"}
        for i in range(10)
    ]
    return [_n("", 0, 680, 1000, 1000, cls="android.widget.FrameLayout", package=pkg)] + keys


def test_keyboard_does_not_change_the_app_fingerprint():
    from artemis.workflow.fingerprint import app_fingerprint, normalize

    root = [_n("root", 0, 0, 1000, 1000, cls="android.widget.FrameLayout")]
    bare = app_fingerprint(normalize(root + search(), width=1000, height=1000))
    with_kb = app_fingerprint(normalize(root + search() + _keyboard(), width=1000, height=1000))
    assert bare.tokens == with_kb.tokens and bare.package == with_kb.package == PKG


def test_app_target_under_the_keyboard_is_occluded():
    from artemis.workflow.fingerprint import normalize
    from artemis.workflow.targets import TargetDescriptor, resolve

    screen = detail("x") + _keyboard()  # keyboard over the bottom button
    norm = normalize(screen, width=1000, height=1000)
    node, status = resolve(TargetDescriptor(resource_id=f"{PKG}:id/add_btn"), norm)
    assert status == "occluded"


def test_slot_pick_ignores_the_field_holding_the_typed_value():
    from artemis.workflow.fingerprint import normalize
    from artemis.workflow.targets import TargetDescriptor, resolve

    screen = results("bun cha")[:-1] + [  # one listed entry (several would be a choice)
        _n("search_input", 100, 10, 800, 70, cls="android.widget.EditText", text="bun cha", editable=True)
    ]
    norm = normalize(screen, width=1000, height=1000)
    node, status = resolve(TargetDescriptor(slot="slot1"), norm, slot_value="bun cha")
    assert status == "ok" and node["text"] == "bun cha special"


def test_notice_reminds_to_scroll_on_scrollable_screens():
    from artemis.workflow.autopilot import AutopilotReport

    rep = AutopilotReport(ran=["click -> ok"], acted=1, stop_reason="no recorded step from this screen")
    assert "scroll to check" in rep.notice("wf", scrollable=True)
    assert "scroll" not in rep.notice("wf", scrollable=False)


def test_layout_fingerprint_ignores_how_many_results_a_list_shows():
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.matcher import weighted_jaccard

    def rows(n):
        return results("x")[:-2] + [
            _n("", 20, 120 + i * 120, 980, 220 + i * 120, cls="android.widget.LinearLayout", clickable=True)
            for i in range(n)
        ]

    a = app_fingerprint(normalize(rows(2), width=1000, height=1000))
    b = app_fingerprint(normalize(rows(5), width=1000, height=1000))
    assert weighted_jaccard(a, b) == 1.0


def test_autopilot_never_restarts_the_workflow_in_the_same_run():
    wf = build_confirmed_wf()
    ap = Autopilot(wf, {"slot1": "bun cha"}, poll_interval_s=0)
    app = FakeApp(home())
    asyncio.run(ap.run(app))  # click, type, search -> stops at the choice
    app.screen = home()  # the AI finished and the app came back to the start screen
    rep = asyncio.run(ap.run(app))
    assert rep.acted == 0 and "already replayed" in rep.stop_reason
    assert rep.revisit_node  # the hook warns the model once, before it redoes the steps


def test_slot_pick_from_a_list_of_several_entries_is_a_choice():
    """'Reykjavik' typed; results 'Reykjavik, Canada' and 'Reykjavík, Iceland':
    the accent hides one match from a plain substring test, and even one match
    among several listed entries is a choice for the AI / user."""
    from artemis.workflow.fingerprint import normalize
    from artemis.workflow.targets import TargetDescriptor, resolve

    screen = results("x")[:-2] + [
        _n("city_name", 20, 120, 980, 200, text="Reykjavik, MB, Canada", clickable=True),
        _n("city_name", 20, 220, 980, 300, text="Reykjavík, Iceland", clickable=True),
    ]
    norm = normalize(screen, width=1000, height=1000)
    _, status = resolve(TargetDescriptor(slot="slot1"), norm, slot_value="Reykjavik")
    assert status == "ambiguous_slot"  # accent-insensitive: both match

    screen[-1] = dict(screen[-1], text="Akureyri, Iceland")
    norm = normalize(screen, width=1000, height=1000)
    _, status = resolve(TargetDescriptor(slot="slot1"), norm, slot_value="Reykjavik")
    assert status == "list_choice"


def test_single_listed_entry_is_picked():
    from artemis.workflow.fingerprint import normalize
    from artemis.workflow.targets import TargetDescriptor, resolve

    screen = results("x")[:-2] + [_n("city_name", 20, 120, 980, 200, text="Nairobi, Kenya", clickable=True)]
    node, status = resolve(TargetDescriptor(slot="slot1"), normalize(screen, width=1000, height=1000), slot_value="nairobi")
    assert status == "ok" and node["text"] == "Nairobi, Kenya"


def _long_detail(offset):
    """Detail screen whose 'add' button sits further down a scrolling page."""
    base = _chrome() + [
        _n("detail_title", 20, 10, 980, 70, text="x"),
        _n("page", 0, 80, 1000, 900, cls="android.widget.ScrollView", scrollable=True),
        _n("qty", 20, 100, 300, 160, text=f"row {offset}"),
        _n("desc", 20, 200, 980, 400, text=f"description part {offset}"),
    ]
    if offset >= 1:
        base.append(_n("add_btn", 20, 700, 980, 780, cls="android.widget.Button", text="Add to basket", clickable=True))
    return base


class ScrollApp(FakeApp):
    def __init__(self, pages):
        super().__init__(_long_detail(0))
        self.offset = 0
        self.pages = pages

    async def act(self, tool, args, element):
        if tool == "swipe":
            self.actions.append((tool, args))
            if self.offset + 1 < self.pages:
                self.offset += 1
                self.screen = _long_detail(self.offset) if self.offset < 5 else _long_detail(0)
            return True, "ok"
        return await super().act(tool, args, element)


def _wf_with_detail_step():
    wf = build_confirmed_wf()
    # re-record the add step on the scrolled-down page (confirmed across 2 runs)
    steps = [step("click", {"target": 9}, _long_detail(1), cart(), point=(500, 740))]
    record_run(steps, goal=GOAL, workflow=wf, slot_values={"slot1": "pho bo"})
    record_run(steps, goal=GOAL, workflow=wf, slot_values={"slot1": "pho bo"})
    return wf


def test_autopilot_scrolls_to_a_target_recorded_below_the_fold():
    wf = _wf_with_detail_step()
    app = ScrollApp(pages=3)
    rep = asyncio.run(Autopilot(wf, {"slot1": "x"}, poll_interval_s=0).run(app))
    tool, args = app.actions[0]
    x1, y1, x2, y2 = args["coordinates"]
    assert tool == "swipe" and y1 > y2  # scrolls the content up
    assert 80 <= y2 < y1 <= 900  # inside the scrolling area [80, 900]
    assert ("click", {"target": 1}) in app.actions and app.screen == cart()


def test_autopilot_stops_scrolling_at_the_end_of_the_list():
    wf = _wf_with_detail_step()
    app = ScrollApp(pages=1)  # nothing below: the swipe changes nothing
    rep = asyncio.run(Autopilot(wf, {"slot1": "x"}, poll_interval_s=0).run(app))
    assert [a[0] for a in app.actions] == ["swipe"]
    assert "end of the list" in rep.stop_reason


def test_reaching_the_recorded_end_screen_says_the_task_is_done():
    steps = recorded_run()[:5]  # ends on the cart screen (no commitment step)
    steps[3].after_user_choice = False
    wf = record_run(steps, goal=GOAL)
    assert wf.end_nodes
    app = FakeApp(home())
    rep = asyncio.run(Autopilot(wf, {"slot1": "bun cha"}, poll_interval_s=0).run(app))
    # results has 2 entries -> the pick is a choice for the AI; finish the pick by hand
    assert "list_choice" in rep.stop_reason or "choice" in rep.stop_reason
    app.screen = detail("bun cha special")
    ap = Autopilot(wf, {"slot1": "bun cha"}, poll_interval_s=0)
    ap._done = {wf.edges[0].id, wf.edges[1].id, wf.edges[2].id}
    rep = asyncio.run(ap.run(app))
    assert rep.finished and app.screen == cart()
    text = rep.notice("wf")
    assert "report_task_status" in text and "Do NOT repeat" in text


def test_merged_edge_order_does_not_block_a_step_that_never_ran():
    """Merges append edges out of path order: a later-recorded step must not
    mark an earlier-listed, never-run step as already done."""
    wf = build_confirmed_wf()
    enter = wf.edges[2]
    wf.edges.remove(enter)
    wf.edges.append(enter)  # e.g. learned in a later run, listed last
    app = FakeApp(home())
    rep = asyncio.run(Autopilot(wf, {"slot1": "bun cha"}, poll_interval_s=0).run(app))
    assert [a[0] for a in app.actions] == ["click", "input_text", "press_key"]


def _form(first="", last=""):
    return _chrome() + [
        _n("title", 20, 10, 980, 70, text="Create contact"),
        _n("first", 20, 120, 980, 180, cls="android.widget.EditText", text=first, editable=True, clickable=True),
        _n("last", 20, 220, 980, 280, cls="android.widget.EditText", text=last, editable=True, clickable=True),
        _n("save", 800, 10, 980, 70, cls="android.widget.Button", text="Save", clickable=True),
    ]


FORM_GOAL = "Create a contact with first name Minh and last name Tran"


def test_focus_tap_on_a_field_is_not_a_step_and_corrections_keep_the_last_typing():
    steps = [
        step("click", {}, _form(), _form(), point=(500, 250)),  # focus "last": same screen
        step("input_text", {"text": "Minh", "target": 2}, _form(), _form(last="Minh"), point=(500, 250)),  # wrong field
        step("input_text", {"text": "Tran", "target": 2}, _form(last="Minh"), _form(last="Tran"), point=(500, 250)),
        step("input_text", {"text": "Minh", "target": 1}, _form(last="Tran"), _form("Minh", "Tran"), point=(500, 150)),
    ]
    wf = record_run(steps, goal=FORM_GOAL)
    inputs = [(e.slot, e.target.resource_id.split("/")[-1]) for e in wf.edges if e.tool == "input_text"]
    assert ("slot1", "last") not in inputs  # the mistaken "Minh" into the last-name field is dropped
    assert ("slot1", "first") in inputs and ("slot2", "last") in inputs
    assert not any(e.kind == "judgement" for e in wf.edges)  # the focus tap left no "choice"


def test_typing_into_the_wrong_field_is_not_verified():
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.store import Edge
    from artemis.workflow.targets import TargetDescriptor

    wf = build_wf()
    edge = Edge(id="x", src="n1", dst="n1", kind="slot", tool="input_text", slot="slot1",
                target=TargetDescriptor(resource_id=f"{PKG}:id/first"))
    ap = Autopilot(wf, {"slot1": "Minh"})
    wrong = normalize(_form(last="Minh"), width=1000, height=1000)
    right = normalize(_form(first="Minh"), width=1000, height=1000)
    assert not ap._verified(edge, wrong, app_fingerprint(wrong), "Minh")
    assert ap._verified(edge, right, app_fingerprint(right), "Minh")


def test_fields_without_ids_are_named_by_their_hint():
    from artemis.workflow.fingerprint import normalize
    from artemis.workflow.targets import descriptor_for, resolve

    def form(shift):
        return _chrome() + [
            {**_n("", 20, 120 + shift, 980, 180 + shift, cls="android.widget.EditText", editable=True), "hint": "First name"},
            {**_n("", 20, 220 + shift, 980, 280 + shift, cls="android.widget.EditText", editable=True), "hint": "Last name"},
        ]

    rec = normalize(form(0), width=1000, height=1000)
    desc = descriptor_for(next(n for n in rec if n.get("hint") == "Last name"))
    assert desc.hint == "Last name" and not desc.resource_id
    scrolled = normalize(form(300), width=1000, height=1000)  # the form scrolled: grid cells moved
    node, status = resolve(desc, scrolled)
    assert status == "ok" and node["hint"] == "Last name"


def test_autopilot_hides_the_keyboard_covering_its_target():
    wf = build_confirmed_wf()  # has nav "Add to basket" on the detail screen
    focused = {**_n("note", 20, 520, 980, 600, cls="android.widget.EditText", editable=True), "focused": "true"}
    with_kb = detail("x") + [focused] + _keyboard()

    class KbApp(FakeApp):
        async def act(self, tool, args, element):
            if tool == "press_key" and args.get("key") == "BACK":
                self.actions.append((tool, args))
                self.screen = detail("x") + [focused]
                return True, "ok"
            return await super().act(tool, args, element)

    app = KbApp(with_kb)
    rep = asyncio.run(Autopilot(wf, {"slot1": "x"}, poll_interval_s=0).run(app))
    assert app.actions[0] == ("press_key", {"key": "BACK"})
    assert ("click", {"target": 1}) in app.actions and app.screen == cart()


def test_tap_with_no_layout_change_is_not_a_choice():
    steps = recorded_run()[:4]
    steps.insert(1, step("click", {}, search(), search(), point=(890, 125)))  # e.g. "show more" in place
    wf = record_run(steps, goal=GOAL)
    assert [e.kind for e in wf.edges if e.src == wf.edges[1].src].count("judgement") == 0


def test_form_fields_are_filled_in_recorded_order():
    steps = [
        step("input_text", {"text": "Minh", "target": 1}, _form(), _form("Minh"), point=(500, 150)),
        step("input_text", {"text": "Tran", "target": 2}, _form("Minh"), _form("Minh", "Tran"), point=(500, 250)),
        step("click", {"target": 4}, _form("Minh", "Tran"), cart(), point=(890, 40)),
    ]
    wf = record_run(steps, goal=FORM_GOAL)

    class FormApp(FakeApp):
        async def act(self, tool, args, element):
            self.actions.append((tool, args))
            rid = (element or {}).get("resource-id", "").split("/")[-1]
            if tool == "input_text":
                f, l = getattr(self, "f", ""), getattr(self, "l", "")
                if rid == "first":
                    self.f = args["text"]
                elif rid == "last":
                    self.l = args["text"]
                self.screen = _form(getattr(self, "f", ""), getattr(self, "l", ""))
                return True, "ok"
            if tool == "click" and rid == "save":
                self.screen = cart()
                return True, "ok"
            return False, "unexpected"

    app = FormApp(_form())
    rep = asyncio.run(Autopilot(wf, {"slot1": "Lan", "slot2": "Pham"}, poll_interval_s=0).run(app))
    assert [(t, a.get("text")) for t, a in app.actions] == [("input_text", "Lan"), ("input_text", "Pham"), ("click", None)]
    assert app.screen == cart()


def test_node_keeps_its_entry_state_and_skips_duplicate_layouts():
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.store import MAX_FPS_PER_NODE, Node

    fp = lambda nodes: app_fingerprint(normalize(nodes, width=1000, height=1000))  # noqa: E731
    entry = fp(home())
    node = Node("n1", [entry])
    for _ in range(3):
        node.add_fp(fp(search()))  # same layout three times -> stored once
    assert len(node.fps) == 2
    for i in range(MAX_FPS_PER_NODE + 3):
        node.add_fp(fp(detail("x") + [_n(f"extra_{i}", 20, 650, 980, 700, text="extra")]))
    assert node.fps[0].tokens == entry.tokens and len(node.fps) == MAX_FPS_PER_NODE


def test_record_mode_merges_a_repeated_task_instead_of_a_new_file(tmp_path):
    goal2 = GOAL.replace("pho bo", "bun cha")

    def run(goal, value):
        steps = recorded_run()
        for st in steps:
            if st.tool == "input_text":
                st.args["text"] = value
            for scr in (st.pre, st.post):  # the screens show what was typed in this run
                if scr is not None:
                    scr.elements = [{**n, "text": n.get("text", "").replace("pho bo", value)} for n in scr.elements]
        return steps

    async def ask(_prompt):
        return '{"workflow": 1, "slots": {"slot1": "bun cha"}}'

    first = FlashWorkflowHook(SimpleNamespace(ctx=SimpleNamespace(), goal=GOAL), "record", str(tmp_path))
    first.steps = run(GOAL, "pho bo")
    asyncio.run(first.finish({"status": "completed"}))

    second = FlashWorkflowHook(SimpleNamespace(ctx=SimpleNamespace(), goal=goal2), "record", str(tmp_path))
    second._ask_text = ask
    second.steps = run(goal2, "bun cha")
    asyncio.run(second.finish({"status": "completed"}))

    saved = WorkflowStore(tmp_path).list(PKG)
    assert len(saved) == 1 and saved[0].runs == 2 and saved[0].ok_runs == 2


def _kb_scroll_app(kb_top):
    """Scrolling form with a focused field and a keyboard from ``kb_top`` down;
    fails the test if a swipe ever starts or ends on the keyboard."""
    focused = {**_n("note", 20, 100, 980, 160, cls="android.widget.EditText", editable=True), "focused": "true"}

    def kb():
        return [_n("", 0, kb_top, 1000, 1000, cls="android.widget.FrameLayout", package="com.example.keyboard")] + [
            {**_n("", 10 + i * 95, kb_top + 20, 95 + i * 95, kb_top + 90, text=f"k{i}", clickable=True,
                  package="com.example.keyboard"), "resource-id": f"com.example.keyboard:id/key_{i}"}
            for i in range(10)
        ]

    class App(ScrollApp):
        def __init__(self):
            super().__init__(pages=3)
            self.kb = True
            self.screen = _long_detail(0) + [focused] + kb()

        async def act(self, tool, args, element):
            if tool == "swipe":
                x1, y1, x2, y2 = args["coordinates"]
                if self.kb and max(y1, y2) >= kb_top:
                    raise AssertionError(f"swipe {args['coordinates']} touches the keyboard at y>={kb_top}")
            if tool == "press_key" and args.get("key") == "BACK":
                self.actions.append((tool, args))
                self.kb = False
                self.screen = _long_detail(self.offset) + [focused]
                return True, "ok"
            res = await super().act(tool, args, element)
            if tool == "swipe":
                self.screen = self.screen + [focused] + (kb() if self.kb else [])
            return res

    return App()


def test_scroll_search_swipes_above_the_keyboard_without_hiding_it():
    """Regression: a swipe started on the keyboard glide-typed "Hu" into a field
    ("Lan" -> "Lan Hu Hu Hu"). With room above the keyboard, swipe there."""
    wf = _wf_with_detail_step()
    app = _kb_scroll_app(kb_top=680)
    rep = asyncio.run(Autopilot(wf, {"slot1": "x"}, poll_interval_s=0).run(app))
    assert app.actions[0][0] == "swipe"  # scrolled above the keyboard, not BACK first
    # the found button then sat under the keyboard: hidden only then, before the tap
    assert app.actions[1:] == [("press_key", {"key": "BACK"}), ("click", {"target": 1})]
    assert app.screen == cart(), rep.stop_reason


def test_keyboard_over_most_of_the_list_is_hidden_before_scrolling(monkeypatch):
    import artemis.workflow.autopilot as ap_mod

    wf = _wf_with_detail_step()
    app = _kb_scroll_app(kb_top=680)
    real = ap_mod._scroll_swipe
    # no safe band while the keyboard is up (e.g. a short list above a tall keyboard)
    monkeypatch.setattr(ap_mod, "_scroll_swipe", lambda norm: real(norm) if not app.kb else None)
    rep = asyncio.run(Autopilot(wf, {"slot1": "x"}, poll_interval_s=0).run(app))
    assert app.actions[0] == ("press_key", {"key": "BACK"})
    assert app.screen == cart(), rep.stop_reason


def test_swipe_band_stops_above_another_window():
    from artemis.workflow.autopilot import _scroll_swipe
    from artemis.workflow.fingerprint import normalize

    norm = normalize(_long_detail(0) + _keyboard(), width=1000, height=1000)
    x1, y1, x2, y2 = _scroll_swipe(norm)
    assert y1 < 680  # the keyboard window starts at y=680


def test_submit_waits_for_every_field_of_its_form_even_scrolled_away():
    """Save recorded after first+last name were typed needs both, even when the
    first-name field has scrolled out of view by then; values typed on another
    screen do not count."""
    steps = [
        step("input_text", {"text": "Minh", "target": 1}, _form(), _form("Minh"), point=(500, 150)),
        step("input_text", {"text": "Tran", "target": 2}, _form("Minh"), _form("Minh", "Tran"), point=(500, 250)),
        # by the time of Save the first-name field is no longer shown
        step("click", {"target": 4}, [n for n in _form("Minh", "Tran") if "first" not in n["resource-id"]],
             cart(), point=(890, 40)),
    ]
    wf = record_run(steps, goal=FORM_GOAL)
    save = next(e for e in wf.edges if e.target and e.target.resource_id.endswith("save"))
    assert set(save.typed.split(",")) == {"slot1", "slot2"}
