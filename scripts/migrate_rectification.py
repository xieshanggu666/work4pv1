"""为已存在的数据库补齐“碳排放整改工单”所需的三张表。

用法：python scripts/migrate_rectification.py（可重复执行）

新表（建表由 SQLAlchemy 元数据完成，存在则跳过）：
- rectification_orders：整改工单（开单/提交/审核/驳回/关闭状态机，幂等键唯一）
- rectification_evidences：企业分轮次提交的整改证据
- rectification_audit_logs：整改监管审计（开单/提交/通过/驳回/关闭/越权拒绝）

整改工单不产生配额流水，因此不向统一账本事件链投影，也无需回填事件；
审核通过后自动触发的企业+年度对账复用既有 ledger_reconciliations 链路。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import Base, engine  # noqa: E402
import app.models  # noqa: F401,E402  确保整改三表注册到元数据

EXPECTED_TABLES = {
    "rectification_orders",
    "rectification_evidences",
    "rectification_audit_logs",
}


def main():
    from sqlalchemy import inspect

    created_before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)
    created_after = set(inspect(engine).get_table_names())
    new_tables = sorted(EXPECTED_TABLES & (created_after - created_before))

    if not new_tables:
        print("无需迁移：碳排放整改工单表均已存在")
    else:
        print(f"新建表：{', '.join(new_tables)}")


if __name__ == "__main__":
    main()
