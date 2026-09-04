"""FASE-CAJA-SOCIO-S1 socio en cuenta_cobro

Revision ID: 4e9c6ae54c73
Revises: c2e679dd48c6
Create Date: 2026-09-03 20:48:12.859900

Un solo cambio de esquema y un backfill, los dos aditivos. Ninguna fila pierde
un dato: se agrega una columna nullable y se le pone valor a las dos que ya
existen.

  1. cuenta_cobro.socio: de que socio es la cuenta, con vocabulario fijo
     ('roman' | 'nachi'). Hasta hoy eso se deducia leyendo cuenta_cobro.nombre
     -- texto libre, sin ninguna regla -- asi que renombrar una cuenta movia
     en silencio la facturacion de un socio al otro.
  2. Backfill de las dos cuentas sembradas en FASE-MP-S1.
  3. CHECK del vocabulario. Va del lado de la base porque las cuentas de cobro
     NO se cargan por ninguna pantalla: el unico camino de alta que existe es
     una migracion con SQL crudo como esta, que no pasa por el modelo ni por
     su @validates.

SOBRE EL BACKFILL Y EL NOMBRE

El UPDATE de abajo hace exactamente lo que esta slice viene a eliminar: mira
`nombre` para decidir el socio. Es a proposito y es la ultima vez que pasa. En
este momento el nombre es el UNICO dato que liga cada fila con una persona --
no hay otro de donde sacarlo -- y despues de correr esto deja de serlo. Por eso
tampoco se filtra por id: los ids 40 y 41 son los de la base de hoy, y una base
nueva o restaurada tendria otros.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4e9c6ae54c73'
down_revision = 'c2e679dd48c6'
branch_labels = None
depends_on = None


# Los mismos literales que sembro 14291f8d0459. Se repiten aca en vez de
# importarse: una migracion tiene que seguir corriendo igual el dia que la
# anterior se borre o se squashee.
#
# Van escritos DENTRO del SQL y no como parametros ligados, que es lo que
# hacia la migracion de la que salieron. El motivo es `flask db upgrade --sql`:
# en modo offline Alembic no tiene contra que resolver los binds y los imprime
# como NULL, asi que el SQL revisable -- y el que alguien podria llegar a
# pegar a mano contra la base -- traia dos UPDATE que no tocaban una fila. Son
# dos constantes de este archivo, no entra nada de afuera.


def upgrade():
    with op.batch_alter_table('cuenta_cobro', schema=None) as batch_op:
        batch_op.add_column(sa.Column('socio', sa.String(length=20), nullable=True))

    conn = op.get_bind()

    # --- Backfill de las cuentas ya sembradas -------------------------------
    # Solo donde todavia esta NULL, mismo criterio que el cableado de
    # canal -> cuenta en FASE-MP-S1: volver a correr esto no pisa una
    # asignacion hecha despues a mano.
    conn.execute(sa.text("""
        UPDATE cuenta_cobro
        SET socio = 'roman'
        WHERE nombre = 'Roman - Presencial y Tiendanube' AND socio IS NULL
    """))

    conn.execute(sa.text("""
        UPDATE cuenta_cobro
        SET socio = 'nachi'
        WHERE nombre = 'Nachi - Mercado Libre' AND socio IS NULL
    """))

    # El CHECK va DESPUES del backfill: si alguna fila tuviera un valor que el
    # vocabulario no admite, queremos que falle aca -- con la migracion entera
    # revertida -- y no dejar la columna cargada a medias sin proteccion.
    with op.batch_alter_table('cuenta_cobro', schema=None) as batch_op:
        batch_op.create_check_constraint(
            'ck_cuenta_cobro_socio_vocabulario',
            "socio IS NULL OR socio IN ('roman', 'nachi')")


def downgrade():
    # No hay backfill que revertir: la columna se va entera y con ella el unico
    # lugar donde vivia el dato. El nombre de la cuenta queda intacto, asi que
    # volver a subir esta migracion reconstruye lo mismo.
    with op.batch_alter_table('cuenta_cobro', schema=None) as batch_op:
        batch_op.drop_constraint('ck_cuenta_cobro_socio_vocabulario', type_='check')
        batch_op.drop_column('socio')
