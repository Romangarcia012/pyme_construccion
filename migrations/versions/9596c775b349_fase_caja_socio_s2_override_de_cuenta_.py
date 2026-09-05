"""FASE-CAJA-SOCIO-S2 override de cuenta por pedido

Revision ID: 9596c775b349
Revises: 418ecc984913
Create Date: 2026-09-04 20:52:56.454019

Un solo cambio y aditivo: `pedido.cuenta_cobro_override_id`, nullable, FK a
cuenta_cobro. No hay backfill y no lo tiene que haber -- NULL es el valor
correcto para las miles de filas que ya existen y para todas las que se carguen
despues. NULL significa "la cuenta que le toca por canal", que es la regla que
siguio rigiendo hasta hoy y sigue rigiendo manana.

QUE VIENE A ARREGLAR

Tres ventas manuales cargadas con montos agregados entraron todas por el canal
manual, que siempre atribuye a la cuenta de Roman. Dos estan bien; la tercera
("ventas Meli", $84.627,70) en la realidad es plata de Nachi. Sin esta columna
la unica forma de corregirlo seria cambiarle el canal al pedido -- mintiendo
sobre por donde vino la venta -- o cambiarle la cuenta al canal manual, que
moveria de socio TODAS las ventas presenciales de una sola vez.

POR QUE NO SE TOCA canal_venta.cuenta_cobro_id

Porque la regla general no cambia: cada canal sigue cobrando donde cobra y la
venta manual sigue cayendo por defecto en la cuenta de Roman. Esto es la
excepcion, y una excepcion se guarda en la fila que la tiene, no reescribiendo
la regla para todos.

SOBRE EL NOMBRE DE LA FK

Alembic la autogenero sin nombre (`None`), que en Postgres sirve para crearla
pero revienta al bajarla: no hay constraint que se llame None. Va nombrada a
mano con la convencion de la base (fk_<tabla>_<columna>_<tabla_apuntada>) para
que el downgrade sea reversible de verdad y no solo en el papel.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9596c775b349'
down_revision = '418ecc984913'
branch_labels = None
depends_on = None


NOMBRE_FK = 'fk_pedido_cuenta_cobro_override_id_cuenta_cobro'


def upgrade():
    with op.batch_alter_table('pedido', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('cuenta_cobro_override_id', sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_pedido_cuenta_cobro_override_id'),
            ['cuenta_cobro_override_id'], unique=False)
        batch_op.create_foreign_key(
            NOMBRE_FK, 'cuenta_cobro', ['cuenta_cobro_override_id'], ['id'])


def downgrade():
    # Se va la columna entera y con ella las correcciones cargadas. Es la unica
    # copia de ese dato: bajar esta migracion vuelve a atribuir cada pedido por
    # su canal, que es exactamente el estado anterior.
    with op.batch_alter_table('pedido', schema=None) as batch_op:
        batch_op.drop_constraint(NOMBRE_FK, type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_pedido_cuenta_cobro_override_id'))
        batch_op.drop_column('cuenta_cobro_override_id')
