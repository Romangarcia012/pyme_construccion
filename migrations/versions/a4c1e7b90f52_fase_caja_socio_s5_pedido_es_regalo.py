"""FASE-CAJA-SOCIO-S5 pedido.es_regalo

Revision ID: a4c1e7b90f52
Revises: b7d3e0c19a45
Create Date: 2026-09-05 00:00:00.000000

Una columna nueva y UN backfill acotado.

  1. pedido.es_regalo: este pedido es un regalo, no una venta. NOT NULL con
     server_default false, porque "no es un regalo" es la respuesta de casi
     todas las filas y no es un dato que falte. El server_default va en la
     columna y no solo en el modelo: las filas que ya existen las tiene que
     completar la BASE, y el default de Python solo corre en los INSERT que
     pasan por SQLAlchemy.

  2. Backfill: el pedido "Sorteo" queda marcado.

POR QUE EL BACKFILL MATCHEA LA NOTA, UNA SOLA VEZ

Porque no hay otro rastro. El regalo se cargo como venta manual de $1 por
unidad para que el stock se descontara, y lo unico que lo distingue de una
venta chica es que alguien escribio "Sorteo" en la nota. El id (62 en
Supabase) no sirve: los ids no coinciden entre las bases, asi que una
migracion por id marcaria el pedido equivocado en cualquier otra.

La diferencia con usar la nota EN EL REPORTE -- que es lo que esta slice vino
a evitar -- es que esto corre una vez, sobre las filas que existen hoy y que
se miraron una por una antes de escribir esto: al momento de generar la
migracion la base productiva tiene 6 pedidos y "Sorteo" aparece en la nota de
exactamente uno (se verifico contra Supabase, no se asumio). De aca en
adelante el dato vive en la columna y nadie vuelve a leer texto libre para
decidir si algo es facturacion.

El filtro es exacto (`nota = 'Sorteo'`) y no un LIKE: un LIKE '%sorteo%'
agarraria "vendido antes del sorteo" y volveria invisible una venta real.
Si en otra base no hay ninguna fila que matchee, el UPDATE no toca nada y la
migracion pasa igual -- que es el resultado correcto: ahi no hubo ningun
regalo que marcar.

QUE NO HACE EL BACKFILL

No toca el stock, ni los items, ni el total, ni el pago del pedido. La
mercaderia salio del deposito de verdad y su costo se pago de verdad -- ya
esta cargado como gasto ("Sorteo", capital). Lo unico que cambia es que deja
de contarse como plata que entro.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a4c1e7b90f52'
down_revision = 'b7d3e0c19a45'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('pedido', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('es_regalo', sa.Boolean(), nullable=False,
                      server_default=sa.text('false')))

    op.execute("UPDATE pedido SET es_regalo = true WHERE nota = 'Sorteo'")


def downgrade():
    # La columna se va entera y con ella la unica marca de que ese pedido no
    # fue una venta. Bajar esta migracion hace que el regalo vuelva a contar
    # como facturacion, y el unico rastro que queda para rearmarlo es la nota
    # -- que es texto libre y puede haber cambiado.
    with op.batch_alter_table('pedido', schema=None) as batch_op:
        batch_op.drop_column('es_regalo')
