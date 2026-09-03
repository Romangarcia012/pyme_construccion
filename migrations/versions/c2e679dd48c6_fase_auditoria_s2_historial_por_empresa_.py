"""FASE-AUDITORIA-S2: historial por empresa, valor viejo/nuevo, usuario nullable

Revision ID: c2e679dd48c6
Revises: 09440fc6b1f2
Create Date: 2026-09-03 19:08:50.746351

Tres cambios sobre `historial`:

  empresa_id      NOT NULL, nuevo. El historial pasa a ser de la EMPRESA. Sin
                  esto, el dia que Nachi tenga login, cada uno seguiria viendo
                  solo lo suyo.
  valor_anterior  Text, nullable. Los completa el hook de auditoria.py; las
  valor_nuevo     once llamadas manuales los dejan en NULL, como venian.
  usuario_id      pasa a NULLABLE. Para que el historial sobreviva a la cuenta
                  que lo genero, y para dejar lugar a las acciones del sistema
                  que una slice futura va a registrar.

EL `empresa_id` NO SE AGREGA NOT NULL DE UNA

La tabla tiene filas (3 en produccion al momento de escribir esto) y Postgres
rechaza un ADD COLUMN NOT NULL sin default sobre una tabla poblada. Va en tres
pasos: se agrega nullable, se rellena desde el usuario de cada fila, y recien
ahi se aprieta a NOT NULL.

El paso del medio tiene un guard en vez de un default. Si alguna fila quedara
sin empresa despues del backfill, la migracion corta con un mensaje legible en
lugar de inventarle una: adjudicarle a una empresa equivocada las acciones de
alguien es exactamente el error que esta tabla existe para no cometer.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c2e679dd48c6'
down_revision = '09440fc6b1f2'
branch_labels = None
depends_on = None

FK_EMPRESA = 'fk_historial_empresa_id_empresa'


def upgrade():
    # 1. Las columnas nuevas. `empresa_id` nace nullable a proposito: ver el
    #    docstring del modulo.
    op.add_column('historial', sa.Column('empresa_id', sa.Integer(), nullable=True))
    op.add_column('historial', sa.Column('valor_anterior', sa.Text(), nullable=True))
    op.add_column('historial', sa.Column('valor_nuevo', sa.Text(), nullable=True))

    # 2. Backfill: la empresa de cada fila es la del usuario que la genero.
    op.execute("""
        UPDATE historial
           SET empresa_id = usuario.empresa_id
          FROM usuario
         WHERE usuario.id = historial.usuario_id
    """)

    huerfanas = op.get_bind().execute(
        sa.text('SELECT count(*) FROM historial WHERE empresa_id IS NULL')
    ).scalar()
    if huerfanas:
        raise RuntimeError(
            'FASE-AUDITORIA-S2: %d fila(s) de historial quedaron sin empresa '
            'despues del backfill (usuario_id apunta a un usuario que ya no '
            'existe). La migracion no les inventa una empresa. Revisalas a '
            'mano con:\n'
            '    SELECT * FROM historial WHERE empresa_id IS NULL;\n'
            'y asignales el empresa_id que corresponda antes de reintentar.'
            % huerfanas
        )

    # 3. Recien ahora se aprieta, con todas las filas ya rellenadas.
    op.alter_column('historial', 'empresa_id',
                    existing_type=sa.Integer(), nullable=False)
    op.create_index(op.f('ix_historial_empresa_id'), 'historial', ['empresa_id'],
                    unique=False)
    op.create_foreign_key(FK_EMPRESA, 'historial', 'empresa', ['empresa_id'], ['id'])

    # 4. Y usuario_id se afloja.
    op.alter_column('historial', 'usuario_id',
                    existing_type=sa.INTEGER(), nullable=True)


def downgrade():
    # Volver a NOT NULL exige que no haya filas sin usuario. Las que tengan
    # usuario_id en NULL son, por definicion, las de cuentas ya eliminadas (o
    # del sistema): no hay a quien reasignarlas y no entran en el esquema
    # viejo, asi que se van. Es la parte del downgrade que pierde datos, y no
    # tiene alternativa: el esquema al que se vuelve no las admite.
    op.execute('DELETE FROM historial WHERE usuario_id IS NULL')
    op.alter_column('historial', 'usuario_id',
                    existing_type=sa.INTEGER(), nullable=False)

    op.drop_constraint(FK_EMPRESA, 'historial', type_='foreignkey')
    op.drop_index(op.f('ix_historial_empresa_id'), table_name='historial')
    op.drop_column('historial', 'valor_nuevo')
    op.drop_column('historial', 'valor_anterior')
    op.drop_column('historial', 'empresa_id')
