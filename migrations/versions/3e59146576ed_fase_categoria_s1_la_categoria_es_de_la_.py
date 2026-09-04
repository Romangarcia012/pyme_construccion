# -*- coding: utf-8 -*-
"""FASE-CATEGORIA-S1: la categoria es de la empresa, no del usuario

Revision ID: 3e59146576ed
Revises: 73ce489bb2fd
Create Date: 2026-09-04 00:23:08.979911

TERCERA VEZ EL MISMO ARREGLO

`historial` (FASE-AUDITORIA-S2) y despues `gasto`/`ingreso`
(FASE-CAJA-GENERAL-S2) venian de la base vieja de la constructora colgados de
`usuario_id` NOT NULL con cascade destructivo. `categoria` es la ultima que
quedaba asi, y la unica de las cuatro que ademas tenia dos parches encima
sosteniendola: un JOIN a `usuario` en cada consulta (`_categorias_de()` en
app.py) para que los socios se vieran las categorias entre si, y una
reasignacion a un "heredero" dentro de `eliminar_cuenta` para que borrarse la
cuenta no dejara a la empresa sin vocabulario. Los dos existian por la misma
razon: la tabla no sabia de que empresa era. Ahora lo sabe, y los dos se van.

Que importa mas aca que en las otras: `categoria` no es una fila de datos, es
la etiqueta de las demas. `gasto.categoria_id` e `ingreso.categoria_id` apuntan
a esta tabla, asi que el cascade que borraba categorias al borrar una cuenta se
llevaba puesta la etiqueta de los gastos que FASE-CAJA-GENERAL-S2 acababa de
salvar. La reasignacion tapaba ese agujero mientras hubiera un heredero.

EL ORDEN, IGUAL QUE EN LAS DOS ANTERIORES

La columna entra NULLABLE, se rellena, y recien despues se aprieta. Al reves
(NOT NULL de entrada) la migracion no puede correr contra una tabla que ya
tiene filas: es exactamente el caso de produccion, donde estan las siete
categorias Korvo sembradas por la revision anterior.

EL BACKFILL LLEGA A TODO

Hoy en Supabase: 7 categorias, todas con `usuario_id` NOT NULL apuntando a un
usuario que existe. Cero huerfanas. El guard esta igual, porque el dia que esta
migracion corra sobre otra base no tiene por que ser cierto -- y si no lo es,
lo correcto es cortar, no inventarle una empresa a una etiqueta.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3e59146576ed'
down_revision = '73ce489bb2fd'
branch_labels = None
depends_on = None


FK_CATEGORIA_EMPRESA = 'fk_categoria_empresa_id_empresa'
IX_CATEGORIA_EMPRESA = 'ix_categoria_empresa_id'


def _backfill(conn):
    """Rellena `categoria.empresa_id` desde el usuario de cada fila, o corta.

    Subconsulta correlacionada y no `UPDATE ... FROM`: la segunda es sintaxis
    de Postgres y esto tambien tiene que correr contra el SQLite local.
    """
    conn.execute(sa.text("""
        UPDATE categoria
           SET empresa_id = (SELECT u.empresa_id
                               FROM usuario u
                              WHERE u.id = categoria.usuario_id)
    """))

    huerfanas = conn.execute(sa.text(
        'SELECT count(*) FROM categoria WHERE empresa_id IS NULL')).scalar()
    if huerfanas:
        raise RuntimeError(
            'FASE-CATEGORIA-S1: %d categoria(s) quedaron sin empresa despues '
            'del backfill (usuario_id en NULL, o apuntando a un usuario que ya '
            'no existe). La migracion no les inventa una empresa: seria darle '
            'el vocabulario de una a cualquier otra, y con el las etiquetas de '
            'sus gastos. Revisalas a mano con:\n'
            '    SELECT * FROM categoria WHERE empresa_id IS NULL;\n'
            'y asignales el empresa_id que corresponda antes de reintentar.'
            % huerfanas)


def upgrade():
    conn = op.get_bind()

    # 1. La columna nueva, nullable a proposito: ver el docstring del modulo.
    op.add_column('categoria', sa.Column('empresa_id', sa.Integer(), nullable=True))

    # 2. Backfill + guard.
    _backfill(conn)

    # 3. Recien ahora se aprieta, con todas las filas ya rellenadas. Y en el
    #    mismo movimiento se afloja usuario_id, que es la otra mitad del
    #    cambio: la categoria sobrevive a la cuenta que la creo.
    with op.batch_alter_table('categoria', schema=None) as batch_op:
        batch_op.alter_column('empresa_id',
                              existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column('usuario_id',
                              existing_type=sa.INTEGER(), nullable=True)
        batch_op.create_index(IX_CATEGORIA_EMPRESA, ['empresa_id'], unique=False)
        batch_op.create_foreign_key(FK_CATEGORIA_EMPRESA, 'empresa',
                                    ['empresa_id'], ['id'])


def downgrade():
    conn = op.get_bind()

    # Volver a NOT NULL exige que no haya categorias sin usuario. Las que lo
    # tengan en NULL son, por definicion, las de cuentas ya eliminadas.
    #
    # Aca NO se hace lo que hizo el downgrade de FASE-CAJA-GENERAL-S2 (borrar
    # las filas sin usuario): una categoria no esta sola, la apuntan
    # `gasto.categoria_id`, `ingreso.categoria_id` y
    # `regla_categorizacion.categoria_id`, y borrarla dejaria esas filas sin
    # etiqueta o directamente volteria el DELETE contra la FK. Asi que primero
    # se intenta lo que hacia el esquema viejo: reasignarlas al usuario mas
    # antiguo de su propia empresa. Es el parche que esta misma revision saco
    # de `eliminar_cuenta`, puesto donde corresponde -- en el camino de vuelta.
    conn.execute(sa.text("""
        UPDATE categoria
           SET usuario_id = (SELECT MIN(u.id)
                               FROM usuario u
                              WHERE u.empresa_id = categoria.empresa_id)
         WHERE usuario_id IS NULL
    """))

    # Lo que quede sin usuario es de una empresa que ya no tiene ninguno. Se va
    # solo si nadie la esta usando; si alguien la usa, el downgrade corta antes
    # de romper datos que si son de alguien.
    conn.execute(sa.text("""
        DELETE FROM categoria
         WHERE usuario_id IS NULL
           AND NOT EXISTS (SELECT 1 FROM gasto g WHERE g.categoria_id = categoria.id)
           AND NOT EXISTS (SELECT 1 FROM ingreso i WHERE i.categoria_id = categoria.id)
           AND NOT EXISTS (SELECT 1 FROM regla_categorizacion r
                            WHERE r.categoria_id = categoria.id)
    """))

    trabadas = conn.execute(sa.text(
        'SELECT count(*) FROM categoria WHERE usuario_id IS NULL')).scalar()
    if trabadas:
        raise RuntimeError(
            'FASE-CATEGORIA-S1 (downgrade): %d categoria(s) quedaron sin '
            'usuario al que volver -- su empresa no tiene ninguno -- y estan '
            'en uso por gastos, ingresos o reglas. El esquema viejo no las '
            'admite y borrarlas dejaria esas filas sin etiqueta. Revisalas '
            'con:\n'
            '    SELECT * FROM categoria WHERE usuario_id IS NULL;\n'
            'y decidi a mano antes de reintentar.' % trabadas)

    with op.batch_alter_table('categoria', schema=None) as batch_op:
        batch_op.drop_constraint(FK_CATEGORIA_EMPRESA, type_='foreignkey')
        batch_op.drop_index(IX_CATEGORIA_EMPRESA)
        batch_op.alter_column('usuario_id',
                              existing_type=sa.INTEGER(), nullable=False)
        batch_op.drop_column('empresa_id')
