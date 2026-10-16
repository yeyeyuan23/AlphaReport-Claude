from unittest.mock import MagicMock, patch


def test_feishu_schedule_uses_one_shared_job_in_explicit_timezone():
    from reportagent.services import scheduler
    cfg = {"enabled": True, "mode": "feishu", "timezone": "Asia/Shanghai", "schedule_cron": "08:00"}
    fake = MagicMock()
    with patch.object(scheduler, "get_config", return_value=cfg), patch.object(scheduler, "_scheduler", None), patch.object(
        scheduler, "AsyncIOScheduler", return_value=fake
    ):
        scheduler.start_scheduler()
    assert fake.add_job.call_count == 1
    args = fake.add_job.call_args
    assert args.kwargs["id"] == "feishu_daily_report"
    assert str(args.kwargs["trigger"].timezone) == "Asia/Shanghai"
