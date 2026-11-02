"""scope product identity to its Miaoshou account"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261103_1007"
down_revision: str | None = "20261103_1006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    constraints = [
        item for item in inspector.get_unique_constraints("products")
        if item.get("column_names") == ["warehouse_key"]
    ]
    constraint_names = {item.get("name") for item in constraints if item.get("name")}
    indexes = [
        item for item in inspector.get_indexes("products")
        if item.get("unique")
        and item.get("column_names") == ["warehouse_key"]
        and item.get("name") not in constraint_names
    ]
    # Remove explicit standalone unique indexes before rebuilding an inline
    # SQLite constraint. Indexes attached to named PostgreSQL constraints are
    # excluded above and are removed together with their owning constraint.
    for index in indexes:
        if index.get("name"):
            op.drop_index(index["name"], table_name="products")

    if connection.dialect.name == "sqlite":
        unnamed = [item for item in constraints if not item.get("name")]
        named = [item for item in constraints if item.get("name")]
        if unnamed:
            if len(unnamed) > 1:
                raise RuntimeError("Expected at most one unnamed global product-key constraint")
            with op.batch_alter_table(
                "products",
                naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
            ) as batch_op:
                batch_op.drop_constraint(
                    "uq_products_warehouse_key", type_="unique"
                )
        for constraint in named:
            op.drop_constraint(constraint["name"], "products", type_="unique")
    else:
        for constraint in constraints:
            if not constraint.get("name"):
                raise RuntimeError(
                    "Could not identify the global products.warehouse_key unique constraint"
                )
            op.drop_constraint(constraint["name"], "products", type_="unique")


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            "SELECT warehouse_key FROM products "
            "GROUP BY warehouse_key HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot restore global warehouse_key uniqueness while multiple accounts "
            "share a product key; resolve the duplicate rows before downgrading."
        )

    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(
            "products",
            naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
        ) as batch_op:
            batch_op.create_unique_constraint(
                "uq_products_warehouse_key", ["warehouse_key"]
            )
        return

    op.create_unique_constraint(
        "uq_products_warehouse_key", "products", ["warehouse_key"]
    )
