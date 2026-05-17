from datetime import datetime, timedelta, timezone


def test_db_health_does_not_warn_when_wal_checkpoint_has_no_wal_file(monkeypatch):
    import infra.db_health as db_health

    monkeypatch.setattr(
        db_health,
        "_file_system_facts",
        lambda: {
            "exists": True,
            "size_mb": 1024.0,
            "wal_exists": False,
            "wal_size_mb": 0,
            "shm_exists": False,
            "shm_size_mb": 0,
            "free_space_gb": 16.0,
        },
    )
    monkeypatch.setattr(
        db_health,
        "_sqlite_facts",
        lambda timeout=15: {
            "open_success": True,
            "open_error": "",
            "journal_mode": "wal",
            "table_count": 3,
            "total_rows": 100,
            "tables": {"ctrader_deals": 100},
        },
    )
    saved = {}
    monkeypatch.setattr(db_health, "_save_health_state", lambda report: saved.update(report))

    report = db_health.run_full_health_check(include_integrity=False)

    assert report["overall_status"] == "healthy"
    assert "WAL mode reported but no -wal file exists" not in report["issues"]


def test_auth_health_refreshes_stale_token_before_warning(monkeypatch):
    import infra.auth_health as auth_health

    stale = (datetime.now(timezone.utc) - timedelta(hours=31)).strftime("%Y-%m-%d %H:%M:%S")
    state = {
        "access_token": "present",
        "refresh_token": "present",
        "last_refresh_utc": stale,
        "refresh_count": 126,
        "consecutive_failures": 0,
        "saved_utc": stale,
    }
    calls = []

    class FakeTokenManager:
        def try_refresh(self):
            calls.append("refresh")
            return "new-token"

    monkeypatch.setattr(auth_health, "_load_token_state", lambda: state)
    monkeypatch.setattr(auth_health, "_get_token_manager", lambda: FakeTokenManager())

    result = auth_health.refresh_stale_token_if_needed(max_age_hours=24)

    assert result["attempted"] is True
    assert result["refreshed"] is True
    assert calls == ["refresh"]


def test_auth_health_does_not_refresh_fresh_token(monkeypatch):
    import infra.auth_health as auth_health

    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    state = {
        "access_token": "present",
        "refresh_token": "present",
        "last_refresh_utc": fresh,
        "refresh_count": 126,
        "consecutive_failures": 0,
        "saved_utc": fresh,
    }
    calls = []

    class FakeTokenManager:
        def try_refresh(self):
            calls.append("refresh")
            return "new-token"

    monkeypatch.setattr(auth_health, "_load_token_state", lambda: state)
    monkeypatch.setattr(auth_health, "_get_token_manager", lambda: FakeTokenManager())

    result = auth_health.refresh_stale_token_if_needed(max_age_hours=24)

    assert result["attempted"] is False
    assert result["refreshed"] is False
    assert calls == []
