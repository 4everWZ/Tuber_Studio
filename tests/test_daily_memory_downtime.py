from tuber_studio.daily_memory import DailyMemoryService


def test_downtime_requires_a_real_heartbeat_gap_and_recalls_a_fact(store):
    service = DailyMemoryService(store)
    char = store.character()
    now = 1700000000.0
    store.set("system_last_active_at", now - 7 * 86400)  # legacy model activity is not a heartbeat

    def start(at):
        return service.check_and_record_downtime(char["id"], char["version"], current_time=at)

    assert start(now) is None
    assert store.get("system_runtime_heartbeat_at") == now
    service.update_active_timestamp(now + 2 * 86400)
    assert start(now + 2 * 86400 + 60) is None  # online and idle until the last heartbeat
    resume = now + 5 * 86400 + 60
    event = start(resume)
    assert event["days"] == 3
    assert event["anecdote"] == "本次启动距上次工作台运行记录约 3 天。"
    assert not service.context(char, "章鱼今天吃什么")
    assert service.context(char, "怎么才开机")[0]["text"] == event["anecdote"]
    assert start(resume + 10) is None
