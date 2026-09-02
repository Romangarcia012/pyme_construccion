"""elimina la columna huerfana cuenta_cobro.identificador_externo

Revision ID: 5d10e8d6bd8c
Revises: 14291f8d0459
Create Date: 2026-09-02 12:05:00.000000

La columna nacio con el modelo de FASE2-S1 (migracion 4a3c449fc7b6) como
"identificador de la cuenta del lado del procesador", pero nunca se le escribio
ni se le leyo nada: no la toca ninguna ruta, ningun sync, ningun template y
ningun test. Quedo sin semantica definida en ningun lado.

FASE-MP-S1 agrego `id_cuenta_externa` para guardar el user_id que devuelve el
OAuth de Mercado Pago, que es exactamente el proposito que esta columna decia
tener. Tener las dos es una invitacion a que la proxima slice escriba en la que
no corresponde y despues nadie sepa cual de las dos es la buena.

SOBRE EL BORRADO DE DATOS: no hay ninguno. Antes de escribir esta migracion se
verifico contra Supabase que las 2 filas de cuenta_cobro tienen
identificador_externo NULL, asi que el DROP no pierde informacion. El downgrade
repone la columna, pero vacia -- no hay nada que restaurar, y no podria haberlo:
una columna sin escritores no acumula datos.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5d10e8d6bd8c'
down_revision = '14291f8d0459'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cuenta_cobro', schema=None) as batch_op:
        batch_op.drop_column('identificador_externo')


def downgrade():
    with op.batch_alter_table('cuenta_cobro', schema=None) as batch_op:
        batch_op.add_column(sa.Column('identificador_externo', sa.String(length=100),
                                      nullable=True))
