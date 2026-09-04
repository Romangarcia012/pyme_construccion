"""FASE-DEVOLUCIONES-S2: la devolucion dice que producto y cuantas unidades

Revision ID: 4b0c77a8c3cd
Revises: f79bfd4cca5b
Create Date: 2026-09-04 14:30:57.351895

Un cambio de esquema, aditivo, y SIN backfill.

  1. devolucion.pedido_item_id: la linea del pedido que volvio. FK a
     pedido_item, indexada, nullable.
  2. devolucion.cantidad: cuantas unidades volvieron. Integer, nullable.
  3. CHECK que ata las dos: o estan las dos o no esta ninguna, y si estan,
     cantidad tiene que ser mayor a cero.

QUE PROBLEMA RESUELVE

`devolucion` existe desde FASE2-S1 (4a3c449fc7b6) apuntando solo a `pedido`, y
guardando plata: monto, comision_devuelta, motivo, estado. Con eso se puede
decir "de este pedido volvieron $12.000", pero no QUE volvio ni CUANTAS
unidades -- que es exactamente el dato que hace falta para sumarle al stock.
Sin estas dos columnas la tabla no puede sostener ninguna devolucion de
mercaderia, y de hecho nadie la escribia: cero rutas, cero syncs.

POR QUE DOS COLUMNAS Y NO UNA TABLA devolucion_item

Porque `devolucion` es append-only por diseno: cada cambio de estado entra como
fila nueva encadenada por evento_previo_id, y cada fila describe el estado
COMPLETO del evento. Una tabla hija obliga a elegir entre copiar los items en
cada cambio de estado (tres revisiones de un contracargo = tres juegos
identicos, sin forma de saber cual vale) o colgarlos solo de la primera fila
(rompiendo el invariante). Con las columnas adentro de la fila no hay que
elegir.

Se pierde poder agrupar "estas dos lineas volvieron en el mismo acto": una
devolucion de dos productos son dos cadenas que comparten pedido_id y
fecha_evento. Nada de lo que hoy existe consume ese agrupamiento. Y como las
columnas son nullable, el dia que haga falta se agrega la tabla hija sin tener
que migrar ni una fila de estas.

POR QUE NULLABLE Y NO NOT NULL

`cantidad` NOT NULL a secas dejaria fuera del esquema al contracargo, que es un
evento de PLATA y no de mercaderia: al comprador le devolvieron el dinero y se
quedo con el producto. Ahi no hay ninguna cantidad que poner, y obligar a una
seria obligar a inventarla. El CHECK hace el trabajo que NOT NULL no puede
hacer solo: DENTRO de una devolucion de mercaderia la cantidad es obligatoria y
positiva; fuera de ese caso no hay nada que contar. Una fila con item y sin
cantidad (una devolucion que no dice cuanto volvio) o con cantidad y sin item
(una cantidad de nada) no entra.

Mismo criterio de CHECK que ck_gasto_origen_fondo_cuenta (f79bfd4cca5b) y que
ck_cuenta_cobro_socio_vocabulario (4e9c6ae54c73), y por la misma razon: el
formulario no es el unico camino de escritura. Las migraciones y los scripts
escriben SQL crudo, que no pasa por el modelo.

POR QUE NO HAY BACKFILL

Porque no hay nada que rellenar: al momento de generar esta migracion
`devolucion` tiene CERO filas en la base productiva (se verifico contra
Supabase, no se asumio: 0 devoluciones, 2 pedidos, 2 pedido_item). Nunca se
escribio una. Sin filas no hay ninguna que pueda violar el CHECK, asi que va
junto con las columnas y no en un paso posterior.

LO QUE ESTA MIGRACION NO ARREGLA

`devolucion.id_externo` sigue sin UNIQUE ni hash_dedup, a diferencia de `pago`
(uq_pago_procesador_id_externo) y `movimiento_cuenta` (hash_dedup). Hoy no
molesta porque la unica escritura es la pantalla de carga manual, que no usa
id_externo. Pero el dia que exista un sync de devoluciones va a hacer falta, y
con estas columnas encima el riesgo sube: reimportar el mismo refund no
duplicaria solo una fila, sumaria el stock dos veces. Queda anotado aca porque
es donde se va a mirar cuando se escriba ese sync.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4b0c77a8c3cd'
down_revision = 'f79bfd4cca5b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('devolucion', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pedido_item_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('cantidad', sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_devolucion_pedido_item_id'), ['pedido_item_id'], unique=False)
        # La FK va con nombre explicito. El autogenerate la deja en None, que
        # en Postgres se resuelve solo al crearla (queda
        # devolucion_pedido_item_id_fkey) pero deja el downgrade sin forma de
        # nombrarla para borrarla: `drop_constraint(None)` no es valido.
        batch_op.create_foreign_key(
            'fk_devolucion_pedido_item', 'pedido_item', ['pedido_item_id'], ['id'])
        batch_op.create_check_constraint(
            'ck_devolucion_item_y_cantidad',
            '(pedido_item_id IS NULL AND cantidad IS NULL)'
            ' OR (pedido_item_id IS NOT NULL AND cantidad IS NOT NULL AND cantidad > 0)')


def downgrade():
    # Las dos columnas se van enteras, y con ellas el unico lugar donde vivia
    # QUE volvio y CUANTO. Bajar esta migracion deja las devoluciones que se
    # hayan cargado convertidas en lo que eran antes de S2: un monto sin
    # mercaderia. El stock que ya se sumo NO se deshace -- vive en
    # producto.stock, que esta migracion no toca -- asi que despues de un
    # downgrade el inventario queda bien y la explicacion de por que se movio,
    # perdida.
    with op.batch_alter_table('devolucion', schema=None) as batch_op:
        batch_op.drop_constraint('ck_devolucion_item_y_cantidad', type_='check')
        batch_op.drop_constraint('fk_devolucion_pedido_item', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_devolucion_pedido_item_id'))
        batch_op.drop_column('cantidad')
        batch_op.drop_column('pedido_item_id')
