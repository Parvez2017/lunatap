/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

export interface AppReference {
  name: string;
  icon: string;
  /** iOS bundle identifier, e.g. com.apple.Preferences. */
  pkg?: string;
  category?: string;
}

export type SuggestionCategory =
  | 'all'
  | 'flash'
  | 'pro'
  | 'cross_app'
  | 'monitor';

export interface SmartSuggestion {
  id: string;
  title: string;
  description: string;
  goal: string;
  profile: 'flash' | 'pro';
  category: 'flash' | 'pro' | 'cross_app' | 'monitor';
  tag: string;
  apps: AppReference[];
  requiredPackages?: string[];
  matchMode?: 'any' | 'all';
  priority?: number;
}

/**
 * Recognized iOS app registry, keyed by bundle identifier.
 * Mirrors APP_REGISTRY in apps/admin_console/services/task_preset_catalog.py.
 */
export const APP_REGISTRY: Record<string, AppReference> = {
  'com.apple.Preferences': { name: 'Settings', icon: 'settings', pkg: 'com.apple.Preferences', category: 'system' },
  'com.apple.mobilesafari': { name: 'Safari', icon: 'public', pkg: 'com.apple.mobilesafari', category: 'browser' },
  'com.apple.Maps': { name: 'Maps', icon: 'explore', pkg: 'com.apple.Maps', category: 'navigation' },
  'com.apple.MobileSMS': { name: 'Messages', icon: 'chat', pkg: 'com.apple.MobileSMS', category: 'communication' },
  'com.apple.mobilecal': { name: 'Calendar', icon: 'calendar_month', pkg: 'com.apple.mobilecal', category: 'productivity' },
  'com.apple.mobilenotes': { name: 'Notes', icon: 'sticky_note_2', pkg: 'com.apple.mobilenotes', category: 'productivity' },
  'com.apple.reminders': { name: 'Reminders', icon: 'checklist', pkg: 'com.apple.reminders', category: 'productivity' },
  'com.apple.mobiletimer': { name: 'Clock', icon: 'timer', pkg: 'com.apple.mobiletimer', category: 'utility' },
  'com.apple.mobileslideshow': { name: 'Photos', icon: 'photo_library', pkg: 'com.apple.mobileslideshow', category: 'media' },
  'com.apple.MobileAddressBook': { name: 'Contacts', icon: 'contacts', pkg: 'com.apple.MobileAddressBook', category: 'productivity' },
  'com.apple.mobilemail': { name: 'Mail', icon: 'mail', pkg: 'com.apple.mobilemail', category: 'productivity' },
  'com.apple.Health': { name: 'Health', icon: 'favorite', pkg: 'com.apple.Health', category: 'lifestyle' },
  'com.apple.weather': { name: 'Weather', icon: 'partly_cloudy_day', pkg: 'com.apple.weather', category: 'utility' },
  'com.apple.calculator': { name: 'Calculator', icon: 'calculate', pkg: 'com.apple.calculator', category: 'utility' },
  'com.apple.AppStore': { name: 'App Store', icon: 'storefront', pkg: 'com.apple.AppStore', category: 'tools' },
  'com.google.ios.youtube': { name: 'YouTube', icon: 'smart_display', pkg: 'com.google.ios.youtube', category: 'entertainment' },
  'com.google.Gmail': { name: 'Gmail', icon: 'mail', pkg: 'com.google.Gmail', category: 'productivity' },
  'com.google.chrome.ios': { name: 'Chrome', icon: 'public', pkg: 'com.google.chrome.ios', category: 'browser' },
  'com.google.Maps': { name: 'Google Maps', icon: 'explore', pkg: 'com.google.Maps', category: 'navigation' },
  'com.tencent.xin': { name: 'WeChat', icon: 'forum', pkg: 'com.tencent.xin', category: 'social' }
};

/**
 * Curated iOS task presets (mirrors PRESET_TASK_CATALOG on the backend).
 */
export const SMART_TASK_LIBRARY: SmartSuggestion[] = [
  {
    id: 'settings_about',
    title: 'iOS Version Check',
    description: 'Read the iOS version from Settings',
    goal: 'Open Settings, go to General > About, and report the iOS version.',
    profile: 'flash',
    category: 'flash',
    tag: 'Settings',
    apps: [{ name: 'Settings', icon: 'settings', pkg: 'com.apple.Preferences' }],
    requiredPackages: ['com.apple.Preferences'],
    priority: 95
  },
  {
    id: 'settings_display',
    title: 'Display Settings',
    description: 'Check appearance and text size in Display & Brightness',
    goal: 'Open Settings > Display & Brightness, report whether Light or Dark appearance is selected, and report the Text Size setting.',
    profile: 'flash',
    category: 'flash',
    tag: 'Settings',
    apps: [{ name: 'Settings', icon: 'settings', pkg: 'com.apple.Preferences' }],
    requiredPackages: ['com.apple.Preferences'],
    priority: 90
  },
  {
    id: 'safari_research',
    title: 'Safari Search',
    description: 'Search the web in Safari and read the top result',
    goal: 'Open Safari, search for the latest news about multimodal mobile AI agents, and summarize the top result.',
    profile: 'flash',
    category: 'flash',
    tag: 'Safari',
    apps: [{ name: 'Safari', icon: 'public', pkg: 'com.apple.mobilesafari' }],
    requiredPackages: ['com.apple.mobilesafari'],
    priority: 88
  },
  {
    id: 'maps_coffee',
    title: 'Maps Coffee Finder',
    description: 'Find a well-rated coffee shop in Apple Maps',
    goal: 'Open Maps, search for coffee shops nearby, and report the name and rating of the top result.',
    profile: 'flash',
    category: 'flash',
    tag: 'Maps',
    apps: [{ name: 'Maps', icon: 'explore', pkg: 'com.apple.Maps' }],
    requiredPackages: ['com.apple.Maps'],
    priority: 86
  },
  {
    id: 'clock_timer',
    title: 'Focus Timer',
    description: 'Start a 25-minute timer in Clock',
    goal: 'Open Clock, switch to the Timers tab, set 25 minutes, and start the timer.',
    profile: 'flash',
    category: 'flash',
    tag: 'Clock',
    apps: [{ name: 'Clock', icon: 'timer', pkg: 'com.apple.mobiletimer' }],
    requiredPackages: ['com.apple.mobiletimer'],
    priority: 84
  },
  {
    id: 'notes_create',
    title: 'Quick Note',
    description: 'Create a note in Notes',
    goal: 'Open Notes, create a new note titled "Lunatap test", add the line "Created by an AI agent", and go back to the notes list.',
    profile: 'flash',
    category: 'flash',
    tag: 'Notes',
    apps: [{ name: 'Notes', icon: 'sticky_note_2', pkg: 'com.apple.mobilenotes' }],
    requiredPackages: ['com.apple.mobilenotes'],
    priority: 82
  },
  {
    id: 'reminders_add',
    title: 'Add a Reminder',
    description: 'Add a reminder to the default list',
    goal: 'Open Reminders and add a reminder "Review test results" to the default list.',
    profile: 'flash',
    category: 'flash',
    tag: 'Reminders',
    apps: [{ name: 'Reminders', icon: 'checklist', pkg: 'com.apple.reminders' }],
    requiredPackages: ['com.apple.reminders'],
    priority: 80
  },
  {
    id: 'photos_recent',
    title: 'Recent Photo',
    description: 'Open the most recent photo',
    goal: 'Open Photos, open the most recent item in the library, and describe what it shows.',
    profile: 'flash',
    category: 'flash',
    tag: 'Photos',
    apps: [{ name: 'Photos', icon: 'photo_library', pkg: 'com.apple.mobileslideshow' }],
    requiredPackages: ['com.apple.mobileslideshow'],
    priority: 78
  },
  {
    id: 'youtube_lofi',
    title: 'YouTube Search',
    description: 'Search YouTube and play a video',
    goal: 'Open YouTube, search for "lofi hip hop radio", and open the top result.',
    profile: 'flash',
    category: 'flash',
    tag: 'YouTube',
    apps: [{ name: 'YouTube', icon: 'smart_display', pkg: 'com.google.ios.youtube' }],
    requiredPackages: ['com.google.ios.youtube'],
    priority: 76
  },
  {
    id: 'gmail_receipts',
    title: 'Gmail Receipts',
    description: 'Find recent order or delivery emails',
    goal: 'Open Gmail and search for recent order or delivery confirmation emails; report the newest one.',
    profile: 'flash',
    category: 'flash',
    tag: 'Gmail',
    apps: [{ name: 'Gmail', icon: 'mail', pkg: 'com.google.Gmail' }],
    requiredPackages: ['com.google.Gmail'],
    priority: 74
  },
  {
    id: 'wechat_browse',
    title: 'WeChat Chats',
    description: 'Read the latest chats in WeChat',
    goal: 'Open WeChat and list the three most recent chats.',
    profile: 'flash',
    category: 'flash',
    tag: 'WeChat',
    apps: [{ name: 'WeChat', icon: 'forum', pkg: 'com.tencent.xin' }],
    requiredPackages: ['com.tencent.xin'],
    priority: 70
  },
  {
    id: 'pro_settings_qa',
    title: 'Settings Smoke Test',
    description: 'Open each main Settings section and check it loads',
    goal: 'In Settings, open Wi-Fi, Bluetooth, General, Accessibility, Privacy & Security, and Battery in turn; verify each screen loads without errors, return to the main list each time, and summarize the results.',
    profile: 'pro',
    category: 'pro',
    tag: 'Settings QA',
    apps: [{ name: 'Settings', icon: 'settings', pkg: 'com.apple.Preferences' }],
    requiredPackages: ['com.apple.Preferences'],
    priority: 92
  },
  {
    id: 'pro_research_notes',
    title: 'Research to Notes',
    description: 'Research in Safari and save a comparison in Notes',
    goal: 'Use Safari to compare the top 3 noise-cancelling headphones by price and battery life, then save a short comparison table as a new note in Notes.',
    profile: 'pro',
    category: 'cross_app',
    tag: 'Safari + Notes',
    apps: [
      { name: 'Safari', icon: 'public', pkg: 'com.apple.mobilesafari' },
      { name: 'Notes', icon: 'sticky_note_2', pkg: 'com.apple.mobilenotes' }
    ],
    requiredPackages: ['com.apple.mobilesafari', 'com.apple.mobilenotes'],
    matchMode: 'all',
    priority: 89
  },
  {
    id: 'pro_maps_messages',
    title: 'Commute ETA Message',
    description: 'Check a route in Maps and draft an ETA in Messages',
    goal: 'Open Maps, get driving directions to the nearest airport and note the travel time, then open Messages and draft (do not send) a message with the ETA.',
    profile: 'pro',
    category: 'cross_app',
    tag: 'Maps + Messages',
    apps: [
      { name: 'Maps', icon: 'explore', pkg: 'com.apple.Maps' },
      { name: 'Messages', icon: 'chat', pkg: 'com.apple.MobileSMS' }
    ],
    requiredPackages: ['com.apple.Maps', 'com.apple.MobileSMS'],
    matchMode: 'all',
    priority: 87
  },
  {
    id: 'pro_mail_calendar',
    title: 'Email to Calendar',
    description: 'Turn the latest invitation email into a calendar event',
    goal: 'Open Mail, find the most recent email with an event or meeting date, then open Calendar and create an event with its title, date and location.',
    profile: 'pro',
    category: 'cross_app',
    tag: 'Mail + Calendar',
    apps: [
      { name: 'Mail', icon: 'mail', pkg: 'com.apple.mobilemail' },
      { name: 'Calendar', icon: 'calendar_month', pkg: 'com.apple.mobilecal' }
    ],
    requiredPackages: ['com.apple.mobilemail', 'com.apple.mobilecal'],
    matchMode: 'all',
    priority: 85
  },
  {
    id: 'monitor_weather',
    title: 'Weather Watch',
    description: 'Check the temperature periodically',
    goal: '[Loop:continuous] Open Weather every 10 minutes for the next hour, record the current temperature for the first city each time, and report the readings.',
    profile: 'pro',
    category: 'monitor',
    tag: 'Weather',
    apps: [{ name: 'Weather', icon: 'partly_cloudy_day', pkg: 'com.apple.weather' }],
    requiredPackages: ['com.apple.weather'],
    priority: 60
  }
];
