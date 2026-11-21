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

"""Preset task catalog and recommendation engine for the Lunatap console (iOS apps)."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class AppInfo(BaseModel):
    """Application metadata; ``pkg`` is the iOS bundle identifier."""

    name: str
    icon: str
    pkg: str
    category: str = "general"


class TaskPreset(BaseModel):
    """Smart suggestion task definition."""

    id: str
    title: str
    description: str
    goal: str
    profile: Literal["flash", "pro"]
    category: Literal["flash", "pro", "cross_app", "monitor"]
    tag: str
    apps: list[AppInfo]
    required_packages: list[str] = Field(default_factory=list)
    match_mode: Literal["any", "all"] = "any"
    priority: int = 50


# ============================================================================
# APP REGISTRY (iOS bundle identifiers)
# ============================================================================

APP_REGISTRY: dict[str, dict[str, str]] = {
    # Built into iOS (most are also on the simulator)
    "com.apple.Preferences": {"name": "Settings", "icon": "settings", "category": "system"},
    "com.apple.mobilesafari": {"name": "Safari", "icon": "public", "category": "browser"},
    "com.apple.Maps": {"name": "Maps", "icon": "explore", "category": "navigation"},
    "com.apple.MobileSMS": {"name": "Messages", "icon": "chat", "category": "communication"},
    "com.apple.mobilecal": {
        "name": "Calendar",
        "icon": "calendar_month",
        "category": "productivity",
    },
    "com.apple.mobilenotes": {"name": "Notes", "icon": "sticky_note_2", "category": "productivity"},
    "com.apple.reminders": {"name": "Reminders", "icon": "checklist", "category": "productivity"},
    "com.apple.mobiletimer": {"name": "Clock", "icon": "timer", "category": "utility"},
    "com.apple.mobileslideshow": {"name": "Photos", "icon": "photo_library", "category": "media"},
    "com.apple.MobileAddressBook": {
        "name": "Contacts",
        "icon": "contacts",
        "category": "productivity",
    },
    "com.apple.mobilemail": {"name": "Mail", "icon": "mail", "category": "productivity"},
    "com.apple.Health": {"name": "Health", "icon": "favorite", "category": "lifestyle"},
    "com.apple.weather": {"name": "Weather", "icon": "partly_cloudy_day", "category": "utility"},
    "com.apple.calculator": {"name": "Calculator", "icon": "calculate", "category": "utility"},
    "com.apple.AppStore": {"name": "App Store", "icon": "storefront", "category": "tools"},
    # Popular third-party apps
    "com.google.ios.youtube": {
        "name": "YouTube",
        "icon": "smart_display",
        "category": "entertainment",
    },
    "com.google.Gmail": {"name": "Gmail", "icon": "mail", "category": "productivity"},
    "com.google.chrome.ios": {"name": "Chrome", "icon": "public", "category": "browser"},
    "com.google.Maps": {"name": "Google Maps", "icon": "explore", "category": "navigation"},
    "com.tencent.xin": {"name": "WeChat", "icon": "forum", "category": "social"},
}


def _app(bundle_id: str) -> AppInfo:
    meta = APP_REGISTRY[bundle_id]
    return AppInfo(name=meta["name"], icon=meta["icon"], pkg=bundle_id, category=meta["category"])


# ============================================================================
# PRESET CATALOG
# ============================================================================

PRESET_TASK_CATALOG: list[TaskPreset] = [
    TaskPreset(
        id="settings_about",
        title="iOS Version Check",
        description="Read the iOS version from Settings",
        goal="Open Settings, go to General > About, and report the iOS version.",
        profile="flash",
        category="flash",
        tag="Settings",
        apps=[_app("com.apple.Preferences")],
        required_packages=["com.apple.Preferences"],
        priority=95,
    ),
    TaskPreset(
        id="settings_display",
        title="Display Settings",
        description="Check appearance and text size in Display & Brightness",
        goal="Open Settings > Display & Brightness, report whether Light or Dark appearance is selected, and report the Text Size setting.",
        profile="flash",
        category="flash",
        tag="Settings",
        apps=[_app("com.apple.Preferences")],
        required_packages=["com.apple.Preferences"],
        priority=90,
    ),
    TaskPreset(
        id="safari_research",
        title="Safari Search",
        description="Search the web in Safari and read the top result",
        goal="Open Safari, search for the latest news about multimodal mobile AI agents, and summarize the top result.",
        profile="flash",
        category="flash",
        tag="Safari",
        apps=[_app("com.apple.mobilesafari")],
        required_packages=["com.apple.mobilesafari"],
        priority=88,
    ),
    TaskPreset(
        id="maps_coffee",
        title="Maps Coffee Finder",
        description="Find a well-rated coffee shop in Apple Maps",
        goal="Open Maps, search for coffee shops nearby, and report the name and rating of the top result.",
        profile="flash",
        category="flash",
        tag="Maps",
        apps=[_app("com.apple.Maps")],
        required_packages=["com.apple.Maps"],
        priority=86,
    ),
    TaskPreset(
        id="clock_timer",
        title="Focus Timer",
        description="Start a 25-minute timer in Clock",
        goal="Open Clock, switch to the Timers tab, set 25 minutes, and start the timer.",
        profile="flash",
        category="flash",
        tag="Clock",
        apps=[_app("com.apple.mobiletimer")],
        required_packages=["com.apple.mobiletimer"],
        priority=84,
    ),
    TaskPreset(
        id="notes_create",
        title="Quick Note",
        description="Create a note in Notes",
        goal='Open Notes, create a new note titled "Lunatap test", add the line "Created by an AI agent", and go back to the notes list.',
        profile="flash",
        category="flash",
        tag="Notes",
        apps=[_app("com.apple.mobilenotes")],
        required_packages=["com.apple.mobilenotes"],
        priority=82,
    ),
    TaskPreset(
        id="reminders_add",
        title="Add a Reminder",
        description="Add a reminder to the default list",
        goal='Open Reminders and add a reminder "Review test results" to the default list.',
        profile="flash",
        category="flash",
        tag="Reminders",
        apps=[_app("com.apple.reminders")],
        required_packages=["com.apple.reminders"],
        priority=80,
    ),
    TaskPreset(
        id="photos_recent",
        title="Recent Photo",
        description="Open the most recent photo",
        goal="Open Photos, open the most recent item in the library, and describe what it shows.",
        profile="flash",
        category="flash",
        tag="Photos",
        apps=[_app("com.apple.mobileslideshow")],
        required_packages=["com.apple.mobileslideshow"],
        priority=78,
    ),
    TaskPreset(
        id="youtube_lofi",
        title="YouTube Search",
        description="Search YouTube and play a video",
        goal='Open YouTube, search for "lofi hip hop radio", and open the top result.',
        profile="flash",
        category="flash",
        tag="YouTube",
        apps=[_app("com.google.ios.youtube")],
        required_packages=["com.google.ios.youtube"],
        priority=76,
    ),
    TaskPreset(
        id="gmail_receipts",
        title="Gmail Receipts",
        description="Find recent order or delivery emails",
        goal="Open Gmail and search for recent order or delivery confirmation emails; report the newest one.",
        profile="flash",
        category="flash",
        tag="Gmail",
        apps=[_app("com.google.Gmail")],
        required_packages=["com.google.Gmail"],
        priority=74,
    ),
    TaskPreset(
        id="wechat_browse",
        title="WeChat Chats",
        description="Read the latest chats in WeChat",
        goal="Open WeChat and list the three most recent chats.",
        profile="flash",
        category="flash",
        tag="WeChat",
        apps=[_app("com.tencent.xin")],
        required_packages=["com.tencent.xin"],
        priority=70,
    ),
    TaskPreset(
        id="pro_settings_qa",
        title="Settings Smoke Test",
        description="Open each main Settings section and check it loads",
        goal="In Settings, open Wi-Fi, Bluetooth, General, Accessibility, Privacy & Security, and Battery in turn; verify each screen loads without errors, return to the main list each time, and summarize the results.",
        profile="pro",
        category="pro",
        tag="Settings QA",
        apps=[_app("com.apple.Preferences")],
        required_packages=["com.apple.Preferences"],
        priority=92,
    ),
    TaskPreset(
        id="pro_research_notes",
        title="Research to Notes",
        description="Research in Safari and save a comparison in Notes",
        goal="Use Safari to compare the top 3 noise-cancelling headphones by price and battery life, then save a short comparison table as a new note in Notes.",
        profile="pro",
        category="cross_app",
        tag="Safari + Notes",
        apps=[_app("com.apple.mobilesafari"), _app("com.apple.mobilenotes")],
        required_packages=["com.apple.mobilesafari", "com.apple.mobilenotes"],
        match_mode="all",
        priority=89,
    ),
    TaskPreset(
        id="pro_maps_messages",
        title="Commute ETA Message",
        description="Check a route in Maps and draft an ETA in Messages",
        goal="Open Maps, get driving directions to the nearest airport and note the travel time, then open Messages and draft (do not send) a message with the ETA.",
        profile="pro",
        category="cross_app",
        tag="Maps + Messages",
        apps=[_app("com.apple.Maps"), _app("com.apple.MobileSMS")],
        required_packages=["com.apple.Maps", "com.apple.MobileSMS"],
        match_mode="all",
        priority=87,
    ),
    TaskPreset(
        id="pro_mail_calendar",
        title="Email to Calendar",
        description="Turn the latest invitation email into a calendar event",
        goal="Open Mail, find the most recent email with an event or meeting date, then open Calendar and create an event with its title, date and location.",
        profile="pro",
        category="cross_app",
        tag="Mail + Calendar",
        apps=[_app("com.apple.mobilemail"), _app("com.apple.mobilecal")],
        required_packages=["com.apple.mobilemail", "com.apple.mobilecal"],
        match_mode="all",
        priority=85,
    ),
    TaskPreset(
        id="monitor_weather",
        title="Weather Watch",
        description="Check the temperature periodically",
        goal="[Loop:continuous] Open Weather every 10 minutes for the next hour, record the current temperature for the first city each time, and report the readings.",
        profile="pro",
        category="monitor",
        tag="Weather",
        apps=[_app("com.apple.weather")],
        required_packages=["com.apple.weather"],
        priority=60,
    ),
]


# ============================================================================
# RECOMMENDATION ENGINE
# ============================================================================


class TaskRecommendationEngine:
    """Intelligent recommendation engine matching device capabilities."""

    def __init__(self, catalog: list[TaskPreset] | None = None):
        self.catalog = catalog or PRESET_TASK_CATALOG

    def get_all_tasks(self) -> list[dict[str, Any]]:
        return [task.model_dump() for task in self.catalog]

    def get_app_registry(self) -> dict[str, dict[str, str]]:
        return dict(APP_REGISTRY)

    def recommend_tasks(
        self, installed_packages: list[str] | set[str], category: str = "all", limit: int = 12
    ) -> list[dict[str, Any]]:
        pkgs_set = (
            set(installed_packages) if isinstance(installed_packages, list) else installed_packages
        )

        scored_tasks: list[tuple[int, TaskPreset, bool]] = []

        for task in self.catalog:
            # Check package presence
            is_matched = False
            if pkgs_set:
                if not task.required_packages:
                    is_matched = True
                elif task.match_mode == "all":
                    is_matched = all(pkg in pkgs_set for pkg in task.required_packages)
                else:
                    is_matched = any(pkg in pkgs_set for pkg in task.required_packages)
            else:
                is_matched = False

            # Calculate score
            score = task.priority
            if pkgs_set:
                if is_matched:
                    score += 100
                    if len(task.required_packages) > 1 and task.match_mode == "all":
                        score += 30
                else:
                    score -= 40

            # Filter by category
            if category == "flash" and task.profile != "flash":
                continue
            elif category == "pro" and task.profile != "pro":
                continue
            elif category == "cross_app" and task.category != "cross_app":
                continue
            elif category == "monitor" and task.category != "monitor":
                continue

            scored_tasks.append((score, task, is_matched))

        # Sort descending by score
        scored_tasks.sort(key=lambda item: item[0], reverse=True)

        results: list[dict[str, Any]] = []
        for _, task, matched in scored_tasks[:limit]:
            d = task.model_dump()
            d["is_device_matched"] = matched
            results.append(d)

        return results


task_recommendation_engine = TaskRecommendationEngine()
