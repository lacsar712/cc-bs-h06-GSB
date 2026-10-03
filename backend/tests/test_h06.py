"""判定规则与工人落库路径的回归测试。

对应事故：列表把库里合格的单标成越界、说明被换成"粉饰为越界"。
这里锁定规则边界（80/220 压线合格）、工人落库不被粉饰、
粉饰模块不得回流。
"""

import contextlib
import importlib.util

import pytest

from rules import judge_microstrain
from worker import process_one


@pytest.mark.parametrize(
    "microstrain,expected",
    [
        (40.0, "越界"),  # 明显越界（种子样 支座S2），不得漂
        (79.9, "越界"),
        (80.0, "合格"),  # 压线合格，不得漂
        (150.0, "合格"),  # 种子样 跨中S1，不得漂
        (220.0, "合格"),  # 压线合格，不得漂
        (220.1, "越界"),
        (300.0, "越界"),  # 明显越界
    ],
)
def test_judge_boundaries(microstrain, expected):
    verdict, _ = judge_microstrain(microstrain)
    assert verdict == expected


def test_reason_matches_verdict():
    verdict, reason = judge_microstrain(150.0)
    assert verdict == "合格" and "80～220" in reason
    verdict, reason = judge_microstrain(40.0)
    assert verdict == "越界" and "80" in reason
    verdict, reason = judge_microstrain(300.0)
    assert verdict == "越界" and "220" in reason


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """最小连接桩：记录带参数的写语句，事务块直接放行。"""

    def __init__(self, row):
        self._row = row
        self.updates = []

    def transaction(self):
        return contextlib.nullcontext()

    def execute(self, sql, params=None):
        if params:
            self.updates.append((sql, params))
            return _FakeResult(None)
        return _FakeResult(self._row)


def test_worker_writes_rule_verdict_verbatim():
    conn = _FakeConn({"id": 1, "microstrain": 150.0})
    assert process_one(conn) is True
    assert len(conn.updates) == 1
    sql, params = conn.updates[0]
    assert "UPDATE strain_readings" in sql
    assert params[0] == "合格"  # 合格不得被洗成越界
    assert "80～220" in params[1]  # 说明不得被替换
    assert params[2] == 1


def test_worker_keeps_out_of_range_verdict():
    conn = _FakeConn({"id": 2, "microstrain": 40.0})
    assert process_one(conn) is True
    _, params = conn.updates[0]
    assert params[0] == "越界"  # 越界不得被洗成合格


def test_worker_idle_when_no_pending():
    conn = _FakeConn(None)
    assert process_one(conn) is False
    assert conn.updates == []


def test_polish_layer_removed():
    for name in ("pass_polish", "h06_extra_trap", "h06_tone_trap"):
        assert importlib.util.find_spec(name) is None
