"""FASE-AUDITORIA-S3: quien disparo el sync

Una sola columna: sync_log.usuario_id, FK a usuario, NULLABLE.

NULL no es un dato faltante -- es la respuesta "lo disparo el cron". Las dos
puertas automaticas (el endpoint con token y scripts/sync_periodico.py) corren
sin sesion y sin request: no hay ninguna persona a la que atribuirles la
corrida. Solo el boton manual tiene un usuario identificado en el instante del
clic, y es el unico que escribe algo aca.

Nullable tambien por el motivo de siempre en este repo: borrar una cuenta no
puede borrar la bitacora. Sin cascade, SQLAlchemy anula la FK y la fila de
sync_log sobrevive al usuario.

Aditiva y sin backfill: las corridas viejas quedan en NULL, que es lo correcto
-- de esas no se sabe quien las disparo, y de las automaticas no habia nadie.

Revision ID: abfaa185ebe2
Revises: 3e59146576ed
Create Date: 2026-09-04 00:50:13.212654

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'abfaa185ebe2'
down_revision = '3e59146576ed'
branch_labels = None
depends_on = None

# La autogeneracion la dejaba en None y Postgres le inventaba un nombre. El
# downgrade con None es directamente un error en PG (no sabe que constraint
# borrar), y el resto de las migraciones de este repo ya nombran sus FKs.
FK_SYNC_LOG_USUARIO = 'fk_sync_log_usuario_id'


def upgrade():
    with op.batch_alter_table('sync_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('usuario_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_sync_log_usuario_id'),
                              ['usuario_id'], unique=False)
        batch_op.create_foreign_key(FK_SYNC_LOG_USUARIO, 'usuario',
                                    ['usuario_id'], ['id'])


def downgrade():
    with op.batch_alter_table('sync_log', schema=None) as batch_op:
        batch_op.drop_constraint(FK_SYNC_LOG_USUARIO, type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_sync_log_usuario_id'))
        batch_op.drop_column('usuario_id')
