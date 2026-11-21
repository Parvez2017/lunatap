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
#
# Modified by the Lunatap Authors in 2026 (forked from google/artemis).

"""Native iOS actuator sharing Artemis's normalized action contract."""

from lunatap.mcp.action_types import ActionCode, ActionResult
from lunatap.mcp.action_manifest import DEVICE_ACTIONS
from lunatap.mcp.actuators.device import DeviceActuator, ensure_focus_at_coords


class IosActuator(DeviceActuator):
    """Reuse controller actions while keeping Android shell conventions out of iOS."""

    def capabilities(self) -> frozenset[str]:
        return DEVICE_ACTIONS - {"erase_one_char", "focus_and_clear_text"}

    def _dims(self) -> tuple[int, int]:
        # The simulator may rotate after SDK initialization. Its most recent
        # screenshot dimensions own the coordinate space for the next action.
        return self.controller.driver.screen_size

    async def input_text(
        self, text: str, target: tuple[int, int] | None = None, clear_exist: bool = True
    ) -> ActionResult:
        # Xcode has no verified whole-field clear, but focusing a field like
        # the Safari address bar select-alls its content, so typed text still
        # replaces it. The result must not claim a clear that never happened.
        if target:
            error = await ensure_focus_at_coords(self.controller, *self._to_px(*target))
            if error:
                return ActionResult.failure("input_text", error)
        success = await self.controller.type_text(text, clear_existing=False)
        if not success:
            return ActionResult.failure("input_text", "Failed to type text on the iOS device.")
        note = (
            " (no whole-field clear on iOS; text was inserted without clearing)"
            if clear_exist
            else ""
        )
        return ActionResult.success("input_text", f"Typed '{text}'.{note}")

    async def press_key(self, key: str) -> ActionResult:
        if await self.controller.press_key(key):
            return ActionResult.success("press_key", f"Pressed key '{key}'.")
        return ActionResult.failure(
            "press_key", f"iOS key '{key}' is unsupported or failed.", code=ActionCode.UNSUPPORTED
        )
