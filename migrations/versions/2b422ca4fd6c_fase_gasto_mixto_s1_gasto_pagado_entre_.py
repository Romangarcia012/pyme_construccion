"""FASE-GASTO-MIXTO-S1 gasto pagado entre varias cuentas

Revision ID: 2b422ca4fd6c
Revises: a4c1e7b90f52
Create Date: 2026-09-09 14:18:25.117922

Un cambio aditivo y SIN backfill: aparece un tercer origen de fondo ('mixto')
y la tabla donde vive su reparto.

QUE VIENE A ARREGLAR

Un gasto pagado mitad de la cuenta de Roman y mitad de la de Nachi es UN pago
real, y hasta hoy habia que cargarlo como DOS gastos separados -- lo unico que
el esquema permitia, porque `origen_fondo` era 'facturacion' (una sola cuenta)
o 'capital' (ninguna). Eso duplicaba el gasto en Caja General: aparecia dos
veces donde en la realidad hubo uno, y reconciliar contra el comprobante
pedia acordarse de que esas dos filas eran la misma cosa.

LOS TRES CAMBIOS

  1. ck_gasto_origen_fondo_vocabulario: se le suma 'mixto'. Como en S3, el
     CHECK no es redundante con el @validates del modelo -- las migraciones y
     los scripts escriben SQL crudo, que no pasa por el modelo.
  2. ck_gasto_origen_fondo_cuenta: 'mixto' entra del lado de los que NO llevan
     `cuenta_pago_id`, junto con 'capital'. Un gasto que salio de dos cuentas
     no tiene UNA cuenta que poner ahi, y elegir cualquiera de las dos diria
     que el gasto entero salio de esa -- exactamente el numero que esta slice
     viene a corregir. El reparto vive en la tabla nueva.
  3. gasto_origen_mixto: una fila por cuenta, con cuanto salio de cada una.

POR QUE UNA TABLA Y NO DOS COLUMNAS

Porque las cuentas de cobro son datos, no estructura. Hoy son dos (ids 40 y
41, Roman y Nachi); un par de columnas `monto_roman`/`monto_nachi` congelaria
en el esquema una cantidad que vive en `cuenta_cobro`, y sumar una tercera
cuenta pediria otra migracion.

EL CHECK DE LA TABLA NUEVA ES `monto >= 0`, NO `> 0`

Una parte en CERO es un dato, no un hueco: dice "esta cuenta no puso nada EN
ESTE gasto", que es distinto de que nadie haya dicho como se repartio. Con
`> 0` esa fila no se podria guardar y el cero se volveria indistinguible del
silencio, que es justo la confusion que S3 evito con `origen_fondo` NULL.
Negativo si se prohibe: una cuenta no puede aportar plata en contra.

LO QUE NO CUIDA LA BASE

Que las partes sumen exactamente `gasto.monto`. Es una regla ENTRE FILAS y un
CHECK solo ve la fila que se esta escribiendo; cuidarla aca pediria un
trigger. Se valida en la ruta (`_origen_del_form`), en Decimal, mismo criterio
de simplicidad que el resto del proyecto.

POR QUE NO HAY BACKFILL

Porque no hay nada que rellenar. Al momento de generar esta migracion `gasto`
tiene 14 filas en la base productiva -- 4 con origen 'facturacion' (las 4 con
su `cuenta_pago_id` cargado) y 10 con 'capital' (las 10 sin cuenta), ninguna
con origen NULL; se verifico contra Supabase, no se asumio. Las 14 siguen
cumpliendo los dos CHECK nuevos, que son estrictamente MAS anchos que los que
reemplazan: nada de lo que hoy es valido deja de serlo. Ningun gasto existente
se convierte en mixto, y ninguno tiene por que hacerlo: los que se cargaron
como dos filas separadas son una decision del pasado que esta slice no
reescribe sola.

EL DELETE EN CASCADA NO ES DECORACION

La FK a `gasto` va con ondelete='CASCADE' porque una fila de reparto no
significa nada sin su gasto. Sin eso, Postgres RECHAZARIA el DELETE de
cualquier gasto mixto y /gasto/eliminar quedaria roto justo para las filas que
esta slice agrega.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2b422ca4fd6c'
down_revision = 'a4c1e7b90f52'
branch_labels = None
depends_on = None


# Los dos CHECK de `gasto`, antes y despues. Van como constantes porque el
# upgrade escribe los NUEVOS y el downgrade tiene que volver a escribir los
# VIEJOS exactos: dejarlos como literales sueltos en las dos funciones es como
# se desincronizan.
VOCABULARIO_VIEJO = "origen_fondo IS NULL OR origen_fondo IN ('facturacion', 'capital')"
VOCABULARIO_NUEVO = ("origen_fondo IS NULL"
                     " OR origen_fondo IN ('facturacion', 'capital', 'mixto')")

CUENTA_VIEJO = ("origen_fondo IS NULL"
                " OR (origen_fondo = 'facturacion' AND cuenta_pago_id IS NOT NULL)"
                " OR (origen_fondo = 'capital' AND cuenta_pago_id IS NULL)")
CUENTA_NUEVO = ("origen_fondo IS NULL"
                " OR (origen_fondo = 'facturacion' AND cuenta_pago_id IS NOT NULL)"
                " OR (origen_fondo IN ('capital', 'mixto')"
                " AND cuenta_pago_id IS NULL)")


def upgrade():
    # Los CHECK PRIMERO y la tabla despues: mientras el vocabulario no acepte
    # 'mixto' no puede existir un gasto mixto, y sin gasto mixto la tabla del
    # reparto no tiene a quien apuntar. En el otro orden queda una ventana --
    # corta, pero real -- con una tabla que nada puede llenar.
    #
    # Un CHECK no se "modifica": se tira y se escribe de nuevo. Las 14 filas
    # que hay hoy pasan el nuevo sin tocarlas (es mas ancho que el viejo).
    with op.batch_alter_table('gasto', schema=None) as batch_op:
        batch_op.drop_constraint('ck_gasto_origen_fondo_vocabulario',
                                 type_='check')
        batch_op.create_check_constraint(
            'ck_gasto_origen_fondo_vocabulario', VOCABULARIO_NUEVO)
        batch_op.drop_constraint('ck_gasto_origen_fondo_cuenta', type_='check')
        batch_op.create_check_constraint(
            'ck_gasto_origen_fondo_cuenta', CUENTA_NUEVO)

    op.create_table(
        'gasto_origen_mixto',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('gasto_id', sa.Integer(), nullable=False),
        sa.Column('cuenta_cobro_id', sa.Integer(), nullable=False),
        sa.Column('monto', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.CheckConstraint('monto >= 0',
                           name='ck_gasto_origen_mixto_monto_no_negativo'),
        sa.ForeignKeyConstraint(['cuenta_cobro_id'], ['cuenta_cobro.id'], ),
        sa.ForeignKeyConstraint(['gasto_id'], ['gasto.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('gasto_origen_mixto', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_gasto_origen_mixto_gasto_id'),
                              ['gasto_id'], unique=False)


def downgrade():
    # Al reves y con una consecuencia que conviene decir en voz alta: bajar
    # esta migracion PIERDE el reparto de los gastos mixtos, porque no hay
    # ningun otro lugar donde ese dato viva. Y no puede bajar mientras exista
    # un gasto con origen_fondo='mixto': el CHECK viejo lo rechazaria, y esa
    # es la senal correcta -- primero hay que decidir que se hace con esos
    # gastos, no dejar que la migracion lo decida por su cuenta.
    with op.batch_alter_table('gasto_origen_mixto', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_gasto_origen_mixto_gasto_id'))

    op.drop_table('gasto_origen_mixto')

    with op.batch_alter_table('gasto', schema=None) as batch_op:
        batch_op.drop_constraint('ck_gasto_origen_fondo_cuenta', type_='check')
        batch_op.create_check_constraint(
            'ck_gasto_origen_fondo_cuenta', CUENTA_VIEJO)
        batch_op.drop_constraint('ck_gasto_origen_fondo_vocabulario',
                                 type_='check')
        batch_op.create_check_constraint(
            'ck_gasto_origen_fondo_vocabulario', VOCABULARIO_VIEJO)
