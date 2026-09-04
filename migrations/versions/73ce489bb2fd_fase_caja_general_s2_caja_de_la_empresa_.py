"""FASE-CAJA-GENERAL-S2: caja de la empresa y categorias Korvo

Revision ID: 73ce489bb2fd
Revises: 4e9c6ae54c73
Create Date: 2026-09-03 21:29:47.127889

Dos cosas, las dos necesarias para que el libro de caja general exista.

1. `gasto` e `ingreso` pasan a ser de la EMPRESA
--------------------------------------------------------------------------
Venian de la base vieja de la constructora colgados de `usuario_id` NOT NULL.
Sobre eso, un "libro unico de caja" no es unico: el dia que Nachi tenga login,
cada uno veria su mitad y nadie la caja de Korvo. Es exactamente el problema
que FASE-AUDITORIA-S2 le arreglo a `historial`, y se arregla igual:

    empresa_id   NOT NULL, nuevo. El duenio de la fila.
    usuario_id   pasa a NULLABLE. Quien tipeo la fila sigue siendo un dato
                 util, pero deja de ser el que decide si la fila existe: al
                 borrarse la cuenta queda en NULL y el gasto sobrevive.

EL `empresa_id` NO SE AGREGA NOT NULL DE UNA

Las dos tablas estan vacias en Supabase, pero NO en la base SQLite local de
desarrollo (un gasto y un ingreso al momento de escribir esto), y un
ADD COLUMN NOT NULL sin default sobre una tabla poblada se cae. Va en tres
pasos, igual que el historial: se agrega nullable, se rellena desde el usuario
de cada fila, y recien ahi se aprieta.

El paso del medio tiene un guard en vez de un default. Si alguna fila quedara
sin empresa despues del backfill, la migracion corta con un mensaje legible en
lugar de inventarle una: meter la plata de alguien en la caja equivocada es
peor que no migrar.

2. Las siete categorias Korvo
--------------------------------------------------------------------------
`categoria` tiene CERO filas en produccion, y el alta de gasto exige una. O
sea que hoy cargar un gasto no es dificil: es imposible. La semilla es la que
destraba la pantalla, y de paso reemplaza el vocabulario de la constructora
(que nunca se cargo) por el que ya usa la hoja CAJA GENERAL del Excel.

Van a nombre del usuario mas viejo de cada empresa porque `categoria.usuario_id`
sigue siendo NOT NULL -- a esa tabla no se le toca el esquema en esta slice.
Que las vean todos los de la empresa lo resuelve `_categorias_de()` en app.py,
con un join, sin migrar nada.

La semilla es idempotente por nombre dentro de la empresa (`categoria` no tiene
UNIQUE, asi que el guard es el WHERE NOT EXISTS, igual que la semilla de
cuenta_cobro de FASE-MP-S1). Correrla dos veces no duplica nada.
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '73ce489bb2fd'
down_revision = '4e9c6ae54c73'
branch_labels = None
depends_on = None


FK_GASTO_EMPRESA = 'fk_gasto_empresa_id_empresa'
FK_INGRESO_EMPRESA = 'fk_ingreso_empresa_id_empresa'
IX_GASTO_EMPRESA = 'ix_gasto_empresa_id'
IX_INGRESO_EMPRESA = 'ix_ingreso_empresa_id'

# El vocabulario de la caja de Korvo, sacado de lo que Roman ya escribe a mano
# en la columna DIARIO del Excel: compras de mercaderia, taxes de aduana,
# publicidad, empaque, productos de prueba y ventas.
#
# "Aporte de capital (socios)" es la unica que no sale del Excel, y es la que
# hace que el saldo pueda arrancar en 0 sin mentir: la plata que los tres
# socios pusieron para la mercaderia inicial entra como un ingreso mas. No se
# trackea cuanto puso cada uno -- eso seria una tabla de socios, y no la hay.
CATEGORIAS_KORVO = (
    ('Compra de mercadería', 'gasto'),
    ('Aduana/Impuestos', 'gasto'),
    ('Publicidad', 'gasto'),
    ('Envíos y empaque', 'gasto'),
    ('Productos de prueba', 'gasto'),
    ('Venta', 'ingreso'),
    ('Aporte de capital (socios)', 'ingreso'),
)


# Dos versiones del mismo INSERT, una por esquema. Ver `sembrar_categorias`.
_SEMBRAR_VIA_USUARIO = """
    INSERT INTO categoria (nombre, tipo, usuario_id, fecha_creacion)
    SELECT :nombre, :tipo, duenio.usuario_id, :ahora
      FROM (SELECT empresa_id, MIN(id) AS usuario_id
              FROM usuario
             GROUP BY empresa_id) AS duenio
     WHERE NOT EXISTS (
             SELECT 1
               FROM categoria c
               JOIN usuario u ON u.id = c.usuario_id
              WHERE c.nombre = :nombre
                AND u.empresa_id = duenio.empresa_id)
"""

_SEMBRAR_VIA_EMPRESA = """
    INSERT INTO categoria (nombre, tipo, empresa_id, usuario_id, fecha_creacion)
    SELECT :nombre, :tipo, duenio.empresa_id, duenio.usuario_id, :ahora
      FROM (SELECT empresa_id, MIN(id) AS usuario_id
              FROM usuario
             GROUP BY empresa_id) AS duenio
     WHERE NOT EXISTS (
             SELECT 1
               FROM categoria c
              WHERE c.nombre = :nombre
                AND c.empresa_id = duenio.empresa_id)
"""


def sembrar_categorias(conn):
    """Deja las siete categorias Korvo en cada empresa que no las tenga.

    Aparte de `upgrade()` para que el test pueda correrla contra una base de
    prueba y afirmar sobre el resultado, en vez de afirmar sobre una lista
    copiada a mano que despues se despega de la migracion.

    Un INSERT por categoria, con parametros, en vez de un solo INSERT con
    VALUES: `CROSS JOIN (VALUES ...) AS c(nombre)` no existe en SQLite, y esto
    tiene que correr igual en la base local que en Supabase.

    MIRA EL ESQUEMA ANTES DE ELEGIR EL INSERT (agregado en FASE-CATEGORIA-S1).
    Esa revision le puso a `categoria` un `empresa_id` NOT NULL, y esta funcion
    tiene dos llamadores que ven esquemas distintos: `upgrade()`, que corre una
    revision ANTES y donde la columna todavia no existe, y los tests, que la
    llaman contra un `create_all()` del modelo de hoy, donde existe y no admite
    NULL. Un solo INSERT no sirve para los dos, y hardcodear el viejo dejaria
    la semilla sin empresa justo en la tabla cuyo duenio es la empresa.

    El deduplicado sigue al mismo criterio: con `empresa_id` se compara
    directo, sin el hay que pasar por `usuario` -- que es exactamente el parche
    que FASE-CATEGORIA-S1 vino a sacar.
    """
    ahora = datetime.utcnow()
    columnas = {c['name'] for c in sa.inspect(conn).get_columns('categoria')}
    sentencia = (_SEMBRAR_VIA_EMPRESA if 'empresa_id' in columnas
                 else _SEMBRAR_VIA_USUARIO)
    for nombre, tipo in CATEGORIAS_KORVO:
        conn.execute(sa.text(sentencia),
                     {'nombre': nombre, 'tipo': tipo, 'ahora': ahora})


def _backfill(conn, tabla, fase):
    """Rellena `empresa_id` desde el usuario de cada fila, o corta.

    La subconsulta correlacionada en vez de UPDATE ... FROM: la segunda es
    sintaxis de Postgres y esto tambien corre contra el SQLite local.
    """
    conn.execute(sa.text("""
        UPDATE {tabla}
           SET empresa_id = (SELECT u.empresa_id
                               FROM usuario u
                              WHERE u.id = {tabla}.usuario_id)
    """.format(tabla=tabla)))

    huerfanas = conn.execute(sa.text(
        'SELECT count(*) FROM {tabla} WHERE empresa_id IS NULL'.format(tabla=tabla)
    )).scalar()
    if huerfanas:
        raise RuntimeError(
            '%s: %d fila(s) de %s quedaron sin empresa despues del backfill '
            '(usuario_id en NULL, o apuntando a un usuario que ya no existe). '
            'La migracion no les inventa una empresa: seria meter esa plata en '
            'la caja de cualquiera. Revisalas a mano con:\n'
            '    SELECT * FROM %s WHERE empresa_id IS NULL;\n'
            'y asignales el empresa_id que corresponda antes de reintentar.'
            % (fase, huerfanas, tabla, tabla))


def upgrade():
    conn = op.get_bind()

    # 1. La columna nueva, nullable a proposito: ver el docstring del modulo.
    op.add_column('gasto', sa.Column('empresa_id', sa.Integer(), nullable=True))
    op.add_column('ingreso', sa.Column('empresa_id', sa.Integer(), nullable=True))

    # 2. Backfill + guard, una tabla a la vez.
    _backfill(conn, 'gasto', 'FASE-CAJA-GENERAL-S2')
    _backfill(conn, 'ingreso', 'FASE-CAJA-GENERAL-S2')

    # 3. Recien ahora se aprieta, con todas las filas ya rellenadas. Y en el
    #    mismo movimiento se afloja usuario_id, que es la otra mitad del
    #    cambio: la fila sobrevive a la cuenta que la cargo.
    with op.batch_alter_table('gasto', schema=None) as batch_op:
        batch_op.alter_column('empresa_id',
                              existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column('usuario_id',
                              existing_type=sa.INTEGER(), nullable=True)
        batch_op.create_index(IX_GASTO_EMPRESA, ['empresa_id'], unique=False)
        batch_op.create_foreign_key(FK_GASTO_EMPRESA, 'empresa',
                                    ['empresa_id'], ['id'])

    with op.batch_alter_table('ingreso', schema=None) as batch_op:
        batch_op.alter_column('empresa_id',
                              existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column('usuario_id',
                              existing_type=sa.INTEGER(), nullable=True)
        batch_op.create_index(IX_INGRESO_EMPRESA, ['empresa_id'], unique=False)
        batch_op.create_foreign_key(FK_INGRESO_EMPRESA, 'empresa',
                                    ['empresa_id'], ['id'])

    # 4. El vocabulario, sin el cual la pantalla de alta no se puede usar.
    sembrar_categorias(conn)


def downgrade():
    conn = op.get_bind()

    # Las categorias sembradas se van solo si nadie las uso todavia. Una que
    # ya tiene gastos colgando no se toca: borrarla dejaria esas filas sin
    # etiqueta y el downgrade no tiene por que costar datos que si son de
    # alguien.
    for nombre, _tipo in CATEGORIAS_KORVO:
        conn.execute(sa.text("""
            DELETE FROM categoria
             WHERE nombre = :nombre
               AND NOT EXISTS (SELECT 1 FROM gasto g WHERE g.categoria_id = categoria.id)
               AND NOT EXISTS (SELECT 1 FROM ingreso i WHERE i.categoria_id = categoria.id)
        """), {'nombre': nombre})

    # Volver a NOT NULL exige que no haya filas sin usuario. Las que tengan
    # usuario_id en NULL son, por definicion, las de cuentas ya eliminadas: no
    # hay a quien reasignarlas y no entran en el esquema viejo, asi que se van.
    # Es la parte del downgrade que pierde datos, y no tiene alternativa --
    # misma decision que en FASE-AUDITORIA-S2.
    conn.execute(sa.text('DELETE FROM gasto WHERE usuario_id IS NULL'))
    conn.execute(sa.text('DELETE FROM ingreso WHERE usuario_id IS NULL'))

    with op.batch_alter_table('ingreso', schema=None) as batch_op:
        batch_op.drop_constraint(FK_INGRESO_EMPRESA, type_='foreignkey')
        batch_op.drop_index(IX_INGRESO_EMPRESA)
        batch_op.alter_column('usuario_id',
                              existing_type=sa.INTEGER(), nullable=False)
        batch_op.drop_column('empresa_id')

    with op.batch_alter_table('gasto', schema=None) as batch_op:
        batch_op.drop_constraint(FK_GASTO_EMPRESA, type_='foreignkey')
        batch_op.drop_index(IX_GASTO_EMPRESA)
        batch_op.alter_column('usuario_id',
                              existing_type=sa.INTEGER(), nullable=False)
        batch_op.drop_column('empresa_id')
