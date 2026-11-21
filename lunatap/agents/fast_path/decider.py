# Copyright 2026 The Lunatap Authors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Decide an Operator turn by text match or Clef, or hand it to the LLM."""

from __future__ import annotations

import base64
import io
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from PIL import Image

from lunatap.agents.fast_path import questions
from lunatap.agents.fast_path.text_match import match_step
from lunatap.config.settings import settings
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

FAST_PATH_ENV = "LUNATAP_FAST_PATH"
CLEF_MODEL_ENV = "LUNATAP_CLEF_MODEL"
DEFAULT_CLEF_MODEL = "clef-flash"
# Clef must be at least this sure that the step needs a tap.
MIN_MOVE_PROBABILITY = 0.5
# After this many fast turns in a row the LLM takes one turn, so it can
# notice anything the fast path cannot. Code settles the plan ledger for
# tap steps, so the run can be longer than one or two taps.
MAX_CONSECUTIVE_FAST_TURNS = 5
CLEF_TIMEOUT_SECONDS = 8.0
_MAX_IMAGE_SIDE = 1568


def fast_path_enabled() -> bool:
    """On unless ``LUNATAP_FAST_PATH`` is 0/false/no/off."""
    return os.environ.get(FAST_PATH_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def _clef_credentials() -> tuple[str, str] | None:
    account = settings.CLOUDFLARE_ACCOUNT_ID or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    token = settings.CLOUDFLARE_API_TOKEN
    token_value = token.get_secret_value() if token else os.environ.get("CLOUDFLARE_API_TOKEN")
    if account and token_value:
        return account, token_value
    return None


@dataclass(frozen=True)
class FastDecision:
    """One tap the fast path is confident about."""

    index: int  # 1-based position in the indexed element list
    source: str  # "text" or "clef"
    reason: str
    seconds: float


def _screenshot_jpeg_b64(screenshot_b64: str) -> str:
    raw = base64.b64decode(screenshot_b64)
    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGB")
        image.thumbnail((_MAX_IMAGE_SIDE, _MAX_IMAGE_SIDE))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class FastPath:
    """Per-task fast-path state: the last fast tap and the run of fast turns."""

    def __init__(self, client: httpx.AsyncClient | None = None, *, harness: bool = False) -> None:
        self._client = client
        # In harness mode an IDE agent is the judge: every call is deliberate,
        # so the env switch, the repeat guard and the fast-run cap do not apply.
        self._harness = harness
        # Plan steps that already got their one fast tap in this task.
        self._tapped_steps: set[str] = set()
        self._consecutive = 0

    def note_llm_turn(self) -> None:
        self._consecutive = 0

    async def decide(
        self,
        step: str | None,
        elements: list[dict[str, Any]],
        screenshot_b64: str | None,
        width: int,
        height: int,
    ) -> FastDecision | None:
        """A confident tap for ``step``, or ``None`` to let the LLM decide."""
        decision, _ = await self.judge(step, elements, screenshot_b64, width, height)
        return decision

    async def judge(
        self,
        step: str | None,
        elements: list[dict[str, Any]],
        screenshot_b64: str | None,
        width: int,
        height: int,
    ) -> tuple[FastDecision | None, str]:
        """``decide`` plus the reason, for callers that show it to a person or agent."""
        if not self._harness and not fast_path_enabled():
            return None, "the fast path is switched off"
        if not step or not elements:
            return None, "no step or no elements on screen"
        if not self._harness and self._consecutive >= MAX_CONSECUTIVE_FAST_TURNS:
            return None, f"the LLM takes a turn after {MAX_CONSECUTIVE_FAST_TURNS} fast ones"
        started = time.perf_counter()

        matched = match_step(step, elements, width, height)
        if matched is not None:
            if self._repeats(step, matched):
                return None, "this step already got its one fast tap"
            return self._accept(
                step,
                elements,
                matched,
                "text",
                f"its text '{matched.get('text')}' names the step",
                started,
            ), "text match"

        if not screenshot_b64:
            return None, "no element's text names the step and there is no screenshot for Clef"
        answers, failure = await self._ask_clef(step, elements, screenshot_b64, width, height)
        if answers is None:
            return None, f"no element's text names the step and {failure}"
        move = answers.get("move") or {}
        move_choice = move.get("choice")
        move_probability = (move.get("probabilities") or {}).get(move_choice, 0.0)
        if move_choice != questions.MOVE_TAP or move_probability < MIN_MOVE_PROBABILITY:
            reason = f"Clef says '{move_choice}' ({move_probability:.2f})"
            logger.info(
                f"Fast path: {reason} for step '{step}'; the Operator LLM decides this turn."
            )
            return None, reason
        index = questions.option_index((answers.get("target") or {}).get("choice", ""))
        element = next(
            (e for pos, e in enumerate(elements, 1) if e.get("index", pos) == index), None
        )
        if element is None:
            return None, "Clef chose an element that is not on the list"
        if self._repeats(step, element):
            return None, "this step already got its one fast tap"
        return self._accept(
            step,
            elements,
            element,
            "clef",
            f"Clef chose it ({move_probability:.2f} sure the step needs a tap)",
            started,
        ), f"Clef chose it ({move_probability:.2f})"

    def _repeats(self, step: str, element: dict[str, Any]) -> bool:
        """Each plan step gets one fast tap per task.

        If that tap did not finish the step, the same step coming back means
        the fast path misread the screen or the plan; the LLM looks next.
        """
        return not self._harness and step in self._tapped_steps

    def _accept(
        self,
        step: str,
        elements: list[dict[str, Any]],
        element: dict[str, Any],
        source: str,
        why: str,
        started: float,
    ) -> FastDecision:
        position = next(pos for pos, e in enumerate(elements, 1) if e is element)
        self._tapped_steps.add(step)
        self._consecutive += 1
        decision = FastDecision(
            index=position,
            source=source,
            reason=f"Tap [{position}] '{element.get('text')}' for step '{step}': {why}.",
            seconds=time.perf_counter() - started,
        )
        logger.info(f"Fast path ({source}, {decision.seconds:.2f}s): {decision.reason}")
        return decision

    async def _ask_clef(
        self,
        step: str,
        elements: list[dict[str, Any]],
        screenshot_b64: str,
        width: int,
        height: int,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Clef's answers, or ``(None, why)``."""
        credentials = _clef_credentials()
        if credentials is None:
            return None, "Clef is not configured (CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN)"
        account, token = credentials
        model = os.environ.get(CLEF_MODEL_ENV, DEFAULT_CLEF_MODEL)
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"
        )
        try:
            body = questions.build_request(
                step,
                questions.element_options(elements, width, height),
                _screenshot_jpeg_b64(screenshot_b64),
            )
        except (OSError, ValueError) as exc:
            logger.debug(f"Fast path: screenshot not usable for Clef: {exc}")
            return None, "the screenshot could not be prepared for Clef"
        try:
            client = self._client or httpx.AsyncClient(timeout=CLEF_TIMEOUT_SECONDS)
            try:
                response = await client.post(
                    url, json=body, headers={"Authorization": f"Bearer {token}"}
                )
            finally:
                if self._client is None:
                    await client.aclose()
            response.raise_for_status()
            return response.json()["result"]["answers"], None
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.warning(f"Fast path: Clef request failed ({exc}); using the Operator LLM.")
            return None, f"the Clef request failed ({exc})"
