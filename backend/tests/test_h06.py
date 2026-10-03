"""H06 回归：结论忠于规则与库值——不粉饰、不洗白、不断半截、复核只读。"""

import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _install_stub_deps_if_missing():
    """本地没装三方依赖时补最小替身；容器内有真依赖则原样使用。"""

    def _mod(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    try:
        import psycopg  # noqa: F401
    except ImportError:
        rows_mod = _mod("psycopg.rows", dict_row=object)
        _mod("psycopg", rows=rows_mod)
    try:
        import psycopg_pool  # noqa: F401
    except ImportError:
        _mod("psycopg_pool", AsyncConnectionPool=object)

    try:
        import sanic  # noqa: F401
    except ImportError:

        class _StubSanicApp:
            def __init__(self, name):
                self.name = name
                self.ctx = types.SimpleNamespace()

            def before_server_start(self, fn):
                return fn

            def after_server_stop(self, fn):
                return fn

            def get(self, _path):
                return lambda fn: fn

            def post(self, _path):
                return lambda fn: fn

        class _StubResponse:
            def __init__(self, data, status=200):
                self.data = data
                self.status = status
                self.body = json.dumps(data, ensure_ascii=False).encode()

        response_mod = _mod(
            "sanic.response",
            json=lambda data, status=200: _StubResponse(data, status),
        )
        _mod("sanic", Sanic=_StubSanicApp, response=response_mod)

    try:
        import jwt  # noqa: F401
    except ImportError:

        class _InvalidTokenError(Exception):
            pass

        def _encode(payload, _secret, algorithm=None):
            return json.dumps(payload, ensure_ascii=False)

        def _decode(token, _secret, algorithms=None):
            try:
                return json.loads(token)
            except ValueError as exc:
                raise _InvalidTokenError(str(exc))

        _mod(
            "jwt",
            encode=_encode,
            decode=_decode,
            InvalidTokenError=_InvalidTokenError,
        )

    try:
        import passlib  # noqa: F401
    except ImportError:

        class _StubCryptContext:
            def __init__(self, **_kwargs):
                pass

            def hash(self, password):
                return "stub:" + password

            def verify(self, password, hashed):
                return hashed == "stub:" + password

        context_mod = _mod("passlib.context", CryptContext=_StubCryptContext)
        _mod("passlib", context=context_mod)


_install_stub_deps_if_missing()

import worker  # noqa: E402
from api import app as api_module  # noqa: E402
from rules import judge_microstrain  # noqa: E402


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeTransaction:
    """正常退出才把暂存语句落库；异常则全部丢弃。"""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, _exc, _tb):
        conn = self._conn
        if exc_type is None:
            conn.writes.extend(conn.staged)
            conn.commits += 1
        else:
            conn.rollbacks += 1
        conn.staged = []
        return False


class _FakeConn:
    """最小同步连接替身：SELECT 返回预置行，写语句先暂存待提交。"""

    def __init__(self, rows=(), fail_on=None):
        self._rows = list(rows)
        self.fail_on = fail_on
        self.staged = []
        self.writes = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("connection lost mid-update")
        if sql.lstrip().upper().startswith("SELECT"):
            return _FakeResult(self._rows)
        self.staged.append((sql, params))
        return _FakeResult([])

    def transaction(self):
        return _FakeTransaction(self)


def _run_and_get_finish_params(row):
    conn = _FakeConn(rows=[row])
    assert worker.run_once(conn) is True
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert len(conn.writes) == 1
    sql, params = conn.writes[0]
    assert sql == worker.FINISH_SQL  # 结论/说明/状态同一条 UPDATE 落库
    return params


def test_rules_boundary_and_clear_samples_do_not_drift():
    for value in (80, 220, 150):
        verdict, reason = judge_microstrain(value)
        assert verdict == "合格"
        assert "范围内" in reason
    for value, keyword in ((79.9, "下限"), (40, "下限"), (220.1, "上限"), (300, "上限")):
        verdict, reason = judge_microstrain(value)
        assert verdict == "越界"
        assert keyword in reason


def test_worker_writes_rule_truth_both_directions():
    verdict, reason, rid = _run_and_get_finish_params({"id": 1, "microstrain": 150.0})
    assert (verdict, rid) == ("合格", 1)
    assert "范围内" in reason

    verdict, reason, rid = _run_and_get_finish_params({"id": 2, "microstrain": 40.0})
    assert (verdict, rid) == ("越界", 2)
    assert "下限" in reason


def test_worker_boundary_samples_do_not_drift():
    samples = ((80.0, "合格"), (220.0, "合格"), (79.9, "越界"), (220.1, "越界"))
    for value, expected in samples:
        verdict, _reason, _rid = _run_and_get_finish_params(
            {"id": 9, "microstrain": value}
        )
        assert verdict == expected


def test_interrupted_update_leaves_no_half_written_row():
    conn = _FakeConn(rows=[{"id": 3, "microstrain": 150.0}], fail_on="UPDATE")
    try:
        worker.run_once(conn)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the interrupted update to raise")
    assert conn.rollbacks == 1
    assert conn.writes == []  # 半截更新不得落库，行仍是 pending


def test_empty_queue_writes_nothing():
    conn = _FakeConn()
    assert worker.run_once(conn) is False
    assert conn.writes == []


def test_reconcile_repairs_polished_and_stuck_rows_only():
    rows = [
        # 被粉饰过的合格单：150με 必须回到合格
        {"id": 1, "microstrain": 150.0, "verdict": "越界", "reason": "粉饰为越界"},
        # 本来就对的越界单：一个字也不许动
        {
            "id": 2,
            "microstrain": 40.0,
            "verdict": "越界",
            "reason": "微应变低于 80 με 设计下限",
        },
        # 半截话：结论对、说明错，说明要成对改回
        {"id": 3, "microstrain": 220.0, "verdict": "合格", "reason": "粉饰为越界"},
    ]
    conn = _FakeConn(rows=rows)
    fixed = worker.reconcile_finished(conn)
    assert fixed == 2
    updates = {
        params[2]: params
        for sql, params in conn.writes
        if sql.lstrip().upper().startswith("UPDATE") and params and len(params) == 3
    }
    assert set(updates) == {1, 3}  # 2 号行不得被洗
    assert updates[1][0] == "合格" and "范围内" in updates[1][1]
    assert updates[3][0] == "合格" and "范围内" in updates[3][1]
    # 中断残留的 processing 行被放回 pending 队列重判
    assert any("processing" in sql for sql, _ in conn.writes)


class _AsyncCtx:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *_exc):
        return False


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))

    async def fetchone(self):
        return self._row


class _FakeAsyncConn:
    def __init__(self, row):
        self.cursor_obj = _FakeCursor(row)
        self.committed = False

    def cursor(self):
        return _AsyncCtx(self.cursor_obj)

    async def commit(self):
        self.committed = True


class _FakeAsyncPool:
    def __init__(self, row):
        self.conn = _FakeAsyncConn(row)

    def connection(self):
        return _AsyncCtx(self.conn)


def _make_token(username, role):
    return api_module.jwt.encode(
        {"sub": username, "role": role}, api_module.SECRET, algorithm="HS256"
    )


def _request(token=None, body=None, pool=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    app = types.SimpleNamespace(ctx=types.SimpleNamespace(pool=pool))
    return types.SimpleNamespace(headers=headers, json=body or {}, app=app)


def test_reviewer_cannot_write():
    resp = asyncio.run(
        api_module.create_reading(
            _request(
                _make_token("reviewer", "reader"),
                {"span_code": "跨中S9", "microstrain": 150},
            )
        )
    )
    assert resp.status == 403


def test_anonymous_cannot_write():
    resp = asyncio.run(
        api_module.create_reading(
            _request(body={"span_code": "跨中S9", "microstrain": 150})
        )
    )
    assert resp.status == 401


def test_writer_can_write_and_row_enters_pending_queue():
    row = {
        "id": 11,
        "span_code": "跨中S3",
        "microstrain": 80.0,
        "verdict": None,
        "reason": None,
        "status": "pending",
        "created_by": "surveyor",
        "created_at": None,
        "processed_at": None,
    }
    pool = _FakeAsyncPool(row)
    resp = asyncio.run(
        api_module.create_reading(
            _request(
                _make_token("surveyor", "writer"),
                {"span_code": "跨中S3", "microstrain": 80},
                pool,
            )
        )
    )
    assert resp.status == 201
    assert pool.conn.committed
    sql, params = pool.conn.cursor_obj.executed[0]
    assert "'pending'" in sql
    assert params == ("跨中S3", 80.0, "surveyor")
