from app.storage import DriveUsage, summarize_storage


def test_summarize_finite_quotas_and_unknown_accounts():
    summary = summarize_storage(
        [
            DriveUsage("one@example.com", used_bytes=25, limit_bytes=100),
            DriveUsage("two@example.com", used_bytes=50, limit_bytes=200),
            DriveUsage("unlimited@example.com", used_bytes=500),
            DriveUsage("unavailable@example.com", error="offline"),
        ]
    )

    assert summary["connected_accounts"] == 4
    assert summary["known_accounts"] == 3
    assert summary["finite_quota_accounts"] == 2
    assert summary["used_bytes"] == 575
    assert summary["finite_used_bytes"] == 75
    assert summary["capacity_bytes"] == 300
    assert summary["available_bytes"] == 225
    assert summary["used_percent"] == 25
    assert summary["capacity_is_complete"] is False


def test_summary_handles_no_known_capacity():
    summary = summarize_storage([DriveUsage("one@example.com", error="offline")])

    assert summary["capacity_bytes"] == 0
    assert summary["used_percent"] == 0