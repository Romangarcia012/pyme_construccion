"""FASE-CAJA-GENERAL-S3 origen del fondo del gasto

Revision ID: f79bfd4cca5b
Revises: abfaa185ebe2
Create Date: 2026-09-04 01:49:35.698491

Un cambio de esquema, aditivo, y SIN backfill.

  1. gasto.origen_fondo: con que plata se pago, con vocabulario fijo
     ('facturacion' | 'capital'). Nullable: NULL es "todavia no se dijo", no
     un tercer origen.
  2. CHECK del vocabulario, mismo criterio que ck_cuenta_cobro_socio_vocabulario
     (4e9c6ae54c73): el formulario no es el unico camino de escritura -- las
     migraciones y los scripts escriben SQL crudo, que no pasa por el modelo
     ni por su @validates.
  3. CHECK que ata origen_fondo con cuenta_pago_id, que es la regla que le da
     sentido al campo:
       - 'facturacion' SIN cuenta no contesta la pregunta que vino a
         contestar: la plata salio de la facturacion de Roman o de la de
         Nachi, y sin la cuenta el reporte de caja no sabe a quien restarle.
       - 'capital' CON cuenta afirma algo falso -- que esa plata salio de lo
         que facturo ese socio -- y le restaria de menos al saldo real.
     El caso NULL queda deliberadamente fuera: cuenta_pago_id existe desde
     FASE2-S1 como campo suelto, y una fila vieja podria tenerlo cargado sin
     que nadie haya dicho de que bolsillo salio. Apretarlo aca obligaria a
     inventar ese dato o a borrar el que ya hay.

POR QUE NO HAY BACKFILL

Porque no hay nada que rellenar: al momento de generar esta migracion `gasto`
tiene CERO filas en la base productiva (se verifico contra Supabase, no se
asumio). El histórico del Excel todavia no se cargo. Si se hubiera cargado
antes de correr esto, las filas existentes quedarian igual en NULL a
proposito: de que bolsillo salio cada una es un dato que nadie registro en su
momento, y ponerle 'facturacion' a todo por defecto seria inventarlo -- con el
agravante de que el saldo real de los socios saldria mintiendo y con cara de
estar bien. El reporte cuenta esas filas aparte, como "sin clasificar", que es
la unica forma honesta de mostrar un dato que falta.

Los dos CHECK van juntos con la columna y no en un paso posterior: no hay
backfill del que tengan que protegerse, y sin filas no hay ninguna que pueda
violarlos.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f79bfd4cca5b'
down_revision = 'abfaa185ebe2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('gasto', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('origen_fondo', sa.String(length=20), nullable=True))
        batch_op.create_check_constraint(
            'ck_gasto_origen_fondo_cuenta',
            "origen_fondo IS NULL"
            " OR (origen_fondo = 'facturacion' AND cuenta_pago_id IS NOT NULL)"
            " OR (origen_fondo = 'capital' AND cuenta_pago_id IS NULL)")
        batch_op.create_check_constraint(
            'ck_gasto_origen_fondo_vocabulario',
            "origen_fondo IS NULL OR origen_fondo IN ('facturacion', 'capital')")


def downgrade():
    # La columna se va entera y con ella el unico lugar donde vivia el dato:
    # bajar esta migracion PIERDE de que plata salio cada gasto, porque no hay
    # ningun otro campo del que se pueda deducir. `cuenta_pago_id` sobrevive,
    # pero por si solo no distingue "se pago de la cuenta de Roman" de "no se
    # dijo nada" -- que es exactamente el agujero que esta slice vino a tapar.
    with op.batch_alter_table('gasto', schema=None) as batch_op:
        batch_op.drop_constraint('ck_gasto_origen_fondo_vocabulario', type_='check')
        batch_op.drop_constraint('ck_gasto_origen_fondo_cuenta', type_='check')
        batch_op.drop_column('origen_fondo')
