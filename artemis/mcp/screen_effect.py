"""Did the agent's own actions change the screen? Shared by every agent (Flash, Pro).

The action server sits under both agents, so it is the one place that sees
everything an agent is shown and everything it does:

* every screen handed to an agent (``observe_screen`` for Flash, ``take_screenshot``
  for Pro's Operator and safety net) becomes the *last seen* screen;
* every device action dispatched since then marks the screen as *acted on*.

``wait_for_screen_change(after="my_action")`` uses that to avoid the two costly
mistakes of a wait that takes a fresh baseline:

* the action already changed the screen before the wait started (a tap that
  navigated, a page that finished loading): the wait would time out waiting for
  a change that already happened. It now returns once the screen has settled.
* the action had no visible effect (a rejected move, a disabled submit): the
  wait would burn its whole timeout. It now returns at once with a
  ``no_effect`` result so the agent re-reads the screen instead of retrying.

``after="external"`` (the other party must act: an opponent's move, a chat
reply) keeps the classic behaviour: wait for a change from now on.

Verdicts are three-state. ``unchanged`` needs a pixel comparison (status bar,
clocks and focused-input carets masked) AND, when both element lists exist, an
unchanged element list in the region. Without a screenshot to compare the
verdict is ``uncertain`` and the classic wait runs: on canvas-drawn screens the
element list alone cannot see a change.
"""

from __future__ import annotations

import asyncio
import base64
import io
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

#: A capture returns ``(image_bytes, elements, width, height)``.
Capture = Callable[[], Awaitable[tuple[bytes | None, list[dict[str, Any]] | None, int, int]]]


@dataclass
class Frame:
    image: bytes | None
    elements: list[dict[str, Any]] | None
    width: int
    height: int


class ScreenEffectTracker:
    """Remembers the last screen shown to the agent and whether it acted since."""

    #: Delays (s) before each look when judging the effect: a tap's effect
    #: (animation, page slide-in, short network round trip) gets ~1 s to show.
    JUDGE_DELAYS_S: tuple[float, ...] = (0.3, 0.7)
    #: After a change is seen, look again until two looks match.
    SETTLE_POLL_S: float = 0.3
    SETTLE_MAX_LOOKS: int = 6

    def __init__(self) -> None:
        self._seen_image: bytes | None = None
        self._seen_elements: list[dict[str, Any]] | None = None
        self.acted_since_seen: bool = False

    # --- Bookkeeping (called by the action server) ---------------------------------

    def note_seen(self, image: bytes | None, elements: list[dict[str, Any]] | None = None) -> None:
        """A screen was handed to the agent: it is the new baseline."""
        if not image:
            return
        self._seen_image = image
        self._seen_elements = list(elements) if elements else None
        self.acted_since_seen = False

    def note_seen_b64(self, image_b64: str | None) -> None:
        if not image_b64:
            return
        try:
            self.note_seen(base64.b64decode(image_b64))
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.debug(f"Could not record the screenshot as seen: {err}")

    def note_action(self) -> None:
        """A device action was dispatched since the last seen screen."""
        self.acted_since_seen = True

    @property
    def has_baseline(self) -> bool:
        return self._seen_image is not None

    # --- Judgement -----------------------------------------------------------------

    def _changed(self, frame: Frame, region: list[int] | None) -> bool | None:
        """``True``/``False`` against the baseline, ``None`` when it cannot tell."""
        from artemis.utils.screen_diff import compare_screen_pixels, compare_xml_hierarchy

        if not (frame.image and self._seen_image):
            return None
        try:
            base = _masked(self._seen_image, self._seen_elements, frame.width, frame.height)
            curr = _masked(frame.image, frame.elements, frame.width, frame.height)
            pixels_changed, _ = compare_screen_pixels(
                base, curr, region=region, width=frame.width, height=frame.height
            )
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.debug(f"Screen-effect pixel comparison failed: {err}")
            return None
        if pixels_changed:
            return True
        if self._seen_elements and frame.elements:
            xml_changed, _ = compare_xml_hierarchy(
                self._seen_elements, frame.elements, region=region, width=frame.width, height=frame.height
            )
            if xml_changed:
                return True
        return False

    async def judge(self, capture: Capture, region: list[int] | None) -> tuple[str, Frame | None]:
        """``("changed" | "unchanged" | "uncertain", latest frame)``."""
        if not self.has_baseline:
            return "uncertain", None
        frame: Frame | None = None
        verdict = "uncertain"
        for delay in self.JUDGE_DELAYS_S:
            await asyncio.sleep(delay)
            frame = await _capture_frame(capture)
            if frame is None:
                return "uncertain", None
            changed = self._changed(frame, region)
            if changed is None:
                return "uncertain", frame
            if changed:
                return "changed", frame
            verdict = "unchanged"
        return verdict, frame

    async def settle(self, capture: Capture, frame: Frame, region: list[int] | None) -> tuple[Frame, bool]:
        """Looks again until two consecutive looks match in ``region``."""
        from artemis.utils.screen_diff import compare_screen_pixels

        if not frame.image:
            return frame, False
        prev = frame
        for _ in range(self.SETTLE_MAX_LOOKS):
            await asyncio.sleep(self.SETTLE_POLL_S)
            curr = await _capture_frame(capture)
            if curr is None or not curr.image:
                return prev, False
            try:
                a = _masked(prev.image, prev.elements, curr.width, curr.height)
                b = _masked(curr.image, curr.elements, curr.width, curr.height)
                moved, _ = compare_screen_pixels(a, b, region=region, width=curr.width, height=curr.height)
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.debug(f"Screen-effect settle comparison failed: {err}")
                return curr, False
            prev = curr
            if not moved:
                return curr, True
        return prev, False


def _masked(image: bytes | None, elements, width: int, height: int):
    """Noise-masked image at device size (element bounds are device pixels)."""
    from PIL import Image

    from artemis.utils.screen_diff import mask_noise

    img = Image.open(io.BytesIO(image))
    if img.size != (width, height):
        img = img.resize((width, height))
    return mask_noise(img, elements, width, height)


async def _capture_frame(capture: Capture) -> Frame | None:
    try:
        image, elements, width, height = await capture()
    except Exception as err:  # pylint: disable=broad-exception-caught
        logger.debug(f"Screen-effect capture failed: {err}")
        return None
    return Frame(image=image, elements=elements, width=width, height=height)


NO_EFFECT_MARKER = "[NO_EFFECT]"


async def pre_wait_result(
    tracker: ScreenEffectTracker,
    capture: Capture,
    *,
    after: str,
    region: list[int] | None,
):
    """The result that answers a ``wait_for_screen_change`` without waiting, or ``None``.

    ``None`` means: run the classic wait.
    """
    from artemis.mcp.action_types import ActionResult

    if after != "my_action" or not tracker.has_baseline:
        return None
    verdict, frame = await tracker.judge(capture, region)
    if verdict == "changed" and frame is not None:
        _, settled = await tracker.settle(capture, frame, region)
        message = (
            "Screen already changed since the screen you last saw and has settled; no further wait needed."
            if settled
            else "Screen already changed since the screen you last saw but was still moving at the last look "
            "(loading or animating); check the screen and wait again if needed."
        )
        return ActionResult.success("wait_for_screen_change", message)
    if verdict == "unchanged" and tracker.acted_since_seen:
        return ActionResult.success(
            "wait_for_screen_change",
            f"{NO_EFFECT_MARKER} No visible change in the wait region since the screen you last saw "
            "(checked for ~1s); wait skipped. Your previous action(s) may have had no effect: inspect "
            "the screen (disabled control, unselected required option, validation notice, content "
            "below the fold, illegal move) before repeating them.",
            no_effect=True,
        )
    # Unchanged with nothing done since (a genuine wait for loading), or uncertain.
    return None
