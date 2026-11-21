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

from unittest.mock import patch

from apps.admin_console.database.connection import db_session
from apps.admin_console.database.repositories.session_repository import SessionRepository


def _insert_running_session(db_path, session_id, pid):
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions "
            "(session_id, initial_goal, start_time, status, device_info, pid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, session_id, 1.0, "running", "{}", pid),
        )
        conn.commit()


def test_startup_cleanup_keeps_live_cross_process_session(tmp_path):
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    _insert_running_session(db_path, "live", 111)
    _insert_running_session(db_path, "dead", 222)

    with patch.object(repository, "process_is_alive", side_effect=lambda pid: pid == 111):
        assert repository.cleanup_orphans_on_startup() == 1

    assert repository.get_session_status("live") == "running"
    assert repository.get_session_status("dead") == "failed"


def test_session_list_harvest_rechecks_worker_liveness(tmp_path):
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    _insert_running_session(db_path, "external-live", 333)

    with patch.object(repository, "process_is_alive", return_value=True):
        assert repository.harvest_orphaned_sessions(["external-live"]) == 0

    assert repository.get_session_status("external-live") == "running"
