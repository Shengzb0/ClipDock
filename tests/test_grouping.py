import time

from clipdock.store import Item
from clipdock.ui import burst_groups, burst_label, day_label


def item(i, ts):
    return Item(id=i, kind="text", preview=str(i), length=1, source="", created_at=ts, last_copied=ts,
                favorite=False, project_id=None, use_count=0)


def at(day_offset, hh, mm, base=None):
    base = base or time.time()
    t = time.localtime(base - day_offset * 86400)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hh, mm, 0, 0, 0, -1))


def test_burst_groups_split_on_gap():
    now = at(0, 15, 0)
    times = [at(0, 14, 10, now), at(0, 14, 8, now), at(0, 14, 2, now),   # 间隔 2、6 分钟
             at(0, 13, 0, now), at(1, 23, 58, now)]
    items = [item(i, t) for i, t in enumerate(times)]
    groups = burst_groups(items, gap_seconds=5 * 60)
    assert [[x.id for x in g] for g in groups] == [[0, 1], [2], [3], [4]]
    groups = burst_groups(items, gap_seconds=10 * 60)
    assert [[x.id for x in g] for g in groups] == [[0, 1, 2], [3], [4]]
    assert burst_groups([], 300) == []


def test_burst_labels():
    now = at(0, 15, 0)
    same_day = [item(0, at(0, 14, 10, now)), item(1, at(0, 14, 2, now))]
    assert burst_label(same_day, now) == "今天 14:02 – 14:10 · 2 条"
    assert burst_label([item(0, at(0, 9, 5, now))], now) == "今天 09:05 · 1 条"
    across = [item(0, at(0, 0, 3, now)), item(1, at(1, 23, 58, now))]
    assert burst_label(across, now) == "昨天 23:58 – 今天 00:03 · 2 条"
    assert day_label(at(1, 10, 0, now), now) == "昨天"
    t = time.localtime(now - 5 * 86400)
    assert day_label(at(5, 10, 0, now), now) in (f"{t.tm_mon}月{t.tm_mday}日", f"{t.tm_year}年{t.tm_mon}月{t.tm_mday}日")
