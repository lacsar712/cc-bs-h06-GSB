"""后台工人：用 SKIP LOCKED 认领 pending 应变读数并写入合格/越界结论。

结论只认 rules.judge_microstrain：库里合格就写合格，越界就写越界。
认领与写结论在同一个事务里完成——要么整单判完，要么回滚仍为 pending，
中断不会留下“说法改了一半”的行。
"""

import os
import time

from db import connect_sync, ensure_schema_sync, seed_if_empty_sync
from rules import judge_microstrain

POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "1.0"))

CLAIM_SQL = """
SELECT id, microstrain
FROM strain_readings
WHERE status = 'pending'
ORDER BY id
FOR UPDATE SKIP LOCKED
LIMIT 1
"""

FINISH_SQL = """
UPDATE strain_readings
SET status = 'done', verdict = %s, reason = %s, processed_at = now()
WHERE id = %s
"""


def claim_one(conn):
    """认领一条 pending 读数；行锁由调用方的事务持有到写结论完成。"""
    return conn.execute(CLAIM_SQL).fetchone()


def finish(conn, reading_id: int, microstrain: float) -> None:
    """按规则判定并写入结论；与认领同属一个事务，不断半截。"""
    verdict, reason = judge_microstrain(microstrain)
    conn.execute(FINISH_SQL, (verdict, reason, reading_id))


def run_once(conn) -> bool:
    with conn.transaction():
        row = claim_one(conn)
        if not row:
            return False
        finish(conn, row["id"], float(row["microstrain"]))
    return True


def reconcile_finished(conn) -> int:
    """按当前规则重算已完结单的结论，清掉历史遗留的错标与半截说法。

    结论与说明永远由同一次判定成对写回；同时把中断残留的
    processing 行放回 pending 队列重判。返回修正的行数。
    """
    fixed = 0
    with conn.transaction():
        conn.execute(
            "UPDATE strain_readings SET status = 'pending' "
            "WHERE status = 'processing'"
        )
        rows = conn.execute(
            "SELECT id, microstrain, verdict, reason "
            "FROM strain_readings WHERE status = 'done'"
        ).fetchall()
        for row in rows:
            verdict, reason = judge_microstrain(float(row["microstrain"]))
            if row["verdict"] != verdict or row["reason"] != reason:
                conn.execute(
                    "UPDATE strain_readings SET verdict = %s, reason = %s "
                    "WHERE id = %s",
                    (verdict, reason, row["id"]),
                )
                fixed += 1
    return fixed


def main() -> None:
    with connect_sync() as conn:
        ensure_schema_sync(conn)
        seed_if_empty_sync(conn)
        conn.commit()
        fixed = reconcile_finished(conn)
        if fixed:
            print(f"worker: reconciled {fixed} finished reading(s)", flush=True)

    while True:
        try:
            with connect_sync() as conn:
                processed = run_once(conn)
        except Exception as exc:
            print(f"worker error: {exc}", flush=True)
            processed = False
        if not processed:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
