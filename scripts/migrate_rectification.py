"""为已存在的数据库补齐“碳排放整改工单”链路所需的表与列。

用法：python scripts/migrate_rectification.py（可重复执行）

新表（建表由 SQLAlchemy 元数据完成，存在则跳过）：
- carbon_rectification_orders：整改工单（open/submitted/approved/closed 状态机，幂等键唯一）
- carbon_rectification_evidences：企业整改证据材料（方案/整改报告/佐证）
- recon_discrepancy_resolutions：对账差异处置回写（resolved/waived，差异稳定指纹唯一）
- carbon_rectification_audit_logs：整改监管审计（含越权拒绝留痕）

新列：
- mrv_reports：rectification_notes_json（履约报告整改附注，只追加不改写排放快照）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.database import Base, engine  # noqa: E402
import app.models  # noqa: F401,E402  确保全部表（含整改四表）注册到元数据

EXPECTED_TABLES = {
    "carbon_rectification_orders",
    "carbon_rectification_evidences",
    "recon_discrepancy_resolutions",
    "carbon_rectification_audit_logs",
}


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def main():
    inspector = inspect(engine)
    tables_before = set(inspector.get_table_names())
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    tables_after = set(inspector.get_table_names())
    new_tables = sorted(EXPECTED_TABLES & (tables_after - tables_before))

    statements: list[str] = []
    if "mrv_reports" in inspector.get_table_names() and not _has_column(
        inspector, "mrv_reports", "rectification_notes_json"
    ):
        statements.append(
            "ALTER TABLE mrv_reports ADD COLUMN rectification_notes_json TEXT NOT NULL DEFAULT '[]'"
        )

    if statements:
        with engine.begin() as conn:
            for stmt in statements:
                print(f"执行：{stmt}")
                conn.execute(text(stmt))

    if not statements and not new_tables:
        print("无需迁移：碳排放整改链路表与列均已存在")
    else:
        if new_tables:
            print(f"新建表：{', '.join(new_tables)}")
        if statements:
            print(f"迁移完成：{len(statements)} 项列变更")


if __name__ == "__main__":
    main()
