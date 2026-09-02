"""FASE3-S4 canal manual y ventas presenciales

Revision ID: 92e7824727b1
Revises: 4a3c449fc7b6
Create Date: 2026-09-01 21:46:04.773055

Cuatro cambios, todos para que una venta de mostrador entre en el mismo modelo
que las de los canales externos sin tener que inventarle datos que no existen:

  1. canal_venta gana una fila tipo='manual' por empresa, activa desde el
     arranque (no necesita OAuth ni credencial, a diferencia de las otras dos).
  2. pedido.id_externo y pago.id_externo pasan a aceptar NULL. Una venta que no
     vino de ninguna API no tiene id externo, y ponerle uno inventado
     ('MANUAL-17') seria mentirle a la UNIQUE que dedupica lo que si viene de
     afuera. En Postgres los NULL no colisionan entre si, asi que
     (canal_id, id_externo) y (procesador, id_externo) siguen sirviendo para
     los canales externos, donde el id sigue siendo obligatorio por codigo.
  3. pago.comision pasa a aceptar NULL, que ahora significa "todavia no se sabe
     cuanto se llevo el procesador". Antes solo se podia decir 0, que es otra
     cosa: 0 es la comision real de un cobro en efectivo.
  4. pedido.nota: texto libre para quien carga la venta a mano.

Ninguna fila existente cambia de valor: las tres alteraciones solo aflojan
restricciones, y la semilla inserta filas nuevas.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '92e7824727b1'
down_revision = '4a3c449fc7b6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('pedido', schema=None) as batch_op:
        batch_op.add_column(sa.Column('nota', sa.Text(), nullable=True))
        batch_op.alter_column('id_externo',
                              existing_type=sa.String(length=100),
                              nullable=True)

    with op.batch_alter_table('pago', schema=None) as batch_op:
        batch_op.alter_column('id_externo',
                              existing_type=sa.String(length=100),
                              nullable=True)
        batch_op.alter_column('comision',
                              existing_type=sa.Numeric(precision=14, scale=2),
                              nullable=True)

    # --- Semilla: el canal de la carga manual -------------------------------
    # Mismo patron que la semilla de FASE2-S1, con dos diferencias: activo=true
    # (no hay nada que conectar) y el WHERE NOT EXISTS, porque esta migracion
    # corre sobre una base que ya tiene canales y la UNIQUE
    # (empresa_id, tipo) no perdona un segundo intento.
    conn = op.get_bind()
    conn.execute(sa.text("""
        INSERT INTO canal_venta (empresa_id, tipo, nombre, activo, fecha_creacion)
        SELECT e.id, 'manual', 'Venta manual / presencial', true, now()
        FROM empresa e
        WHERE NOT EXISTS (
            SELECT 1 FROM canal_venta c
            WHERE c.empresa_id = e.id AND c.tipo = 'manual'
        )
    """))


def downgrade():
    conn = op.get_bind()
    # Solo los canales manuales que no llegaron a usarse. Si hay ventas
    # cargadas colgando de uno, borrarlo dejaria pedidos huerfanos: se prefiere
    # dejar la fila y que el downgrade falle mas abajo, al reponer los NOT NULL.
    conn.execute(sa.text("""
        DELETE FROM canal_venta c
        WHERE c.tipo = 'manual'
          AND NOT EXISTS (SELECT 1 FROM pedido p WHERE p.canal_id = c.id)
          AND NOT EXISTS (SELECT 1 FROM pago g WHERE g.canal_id = c.id)
    """))

    with op.batch_alter_table('pago', schema=None) as batch_op:
        batch_op.alter_column('comision',
                              existing_type=sa.Numeric(precision=14, scale=2),
                              nullable=False)
        batch_op.alter_column('id_externo',
                              existing_type=sa.String(length=100),
                              nullable=False)

    with op.batch_alter_table('pedido', schema=None) as batch_op:
        batch_op.alter_column('id_externo',
                              existing_type=sa.String(length=100),
                              nullable=False)
        batch_op.drop_column('nota')
