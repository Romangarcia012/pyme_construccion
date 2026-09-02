"""FASE-MP-S1 cuentas de Mercado Pago y sus credenciales

Revision ID: 14291f8d0459
Revises: 92e7824727b1
Create Date: 2026-09-02 11:20:00.000000

Cinco cambios, todos aditivos. Ninguna fila existente cambia de valor: se
agregan dos columnas nullable, una tabla nueva y dos filas de cuenta_cobro por
empresa, y despues se apunta cada canal a la cuenta que le corresponde (esa
columna nacio NULL en el paso anterior, asi que tampoco se pisa nada).

  1. cuenta_cobro.id_cuenta_externa: el user_id que devuelve el OAuth de
     Mercado Pago. Nombre por simetria con canal_venta.id_tienda_externo.
  2. credencial_cuenta_cobro: la hermana de credencial_canal para las cuentas
     de cobro. Mismo cifrado Fernet; a diferencia de Tiendanube guarda tambien
     refresh_token y expira_en, porque el token de Mercado Pago vence.
  3. canal_venta.cuenta_cobro_id: a que cuenta entra la plata de cada canal.
     Nullable a proposito -- el canal 'manual' de una empresa sin cuentas
     conectadas es un caso legitimo, no un dato faltante.
  4. Semilla de las dos cuentas reales de Mercado Pago, una por persona.
  5. Cableado canal -> cuenta: tiendanube y manual cobran en la cuenta de
     Roman, mercadolibre en la de Nachi.

Sobre el punto 5: NO se toca canal_venta.activo. El canal 'mercadolibre' sigue
apagado. Que la cuenta de cobro de Nachi se conecte no significa que la
integracion de ventas de Mercado Libre exista; son dos cosas independientes y
prenderlo aca dejaria un canal activo sin credencial, que es justo lo que el
test de invariantes de FASE2-S1 prohibe.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '14291f8d0459'
down_revision = '92e7824727b1'
branch_labels = None
depends_on = None


# Las dos cuentas reales. El alias es el `nombre` de cuenta_cobro, que ya tiene
# UNIQUE (empresa_id, nombre): eso es lo que hace idempotente a la semilla.
CUENTA_ROMAN = 'Roman - Presencial y Tiendanube'
CUENTA_NACHI = 'Nachi - Mercado Libre'


def upgrade():
    with op.batch_alter_table('cuenta_cobro', schema=None) as batch_op:
        batch_op.add_column(sa.Column('id_cuenta_externa', sa.String(length=100),
                                      nullable=True))

    op.create_table(
        'credencial_cuenta_cobro',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cuenta_cobro_id', sa.Integer(), nullable=False),
        sa.Column('access_token_cifrado', sa.Text(), nullable=True),
        sa.Column('refresh_token_cifrado', sa.Text(), nullable=True),
        sa.Column('expira_en', sa.DateTime(), nullable=True),
        sa.Column('actualizado_en', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['cuenta_cobro_id'], ['cuenta_cobro.id'],
                                name='fk_credencial_cuenta_cobro_cuenta'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cuenta_cobro_id', name='uq_credencial_cuenta_cobro_cuenta'),
    )
    with op.batch_alter_table('credencial_cuenta_cobro', schema=None) as batch_op:
        batch_op.create_index('ix_credencial_cuenta_cobro_cuenta_cobro_id',
                              ['cuenta_cobro_id'], unique=False)

    with op.batch_alter_table('canal_venta', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cuenta_cobro_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_canal_venta_cuenta_cobro_id',
                              ['cuenta_cobro_id'], unique=False)
        batch_op.create_foreign_key('fk_canal_venta_cuenta_cobro',
                                    'cuenta_cobro', ['cuenta_cobro_id'], ['id'])

    conn = op.get_bind()

    # --- Semilla: las dos cuentas de Mercado Pago ---------------------------
    # Una fila por empresa y por cuenta, mismo patron que la semilla de canales
    # de FASE2-S1, con el WHERE NOT EXISTS de FASE3-S4: esto corre sobre una
    # base viva y la UNIQUE (empresa_id, nombre) no perdona un segundo intento.
    #
    # activo=true y saldo_actual=0: la cuenta existe y sirve desde el momento
    # cero, lo que falta es el token. "Sin conectar" se lee de que no haya fila
    # en credencial_cuenta_cobro, no de este flag. Es la diferencia con
    # canal_venta.activo, donde el flag SI significa "tiene credencial".
    conn.execute(sa.text("""
        INSERT INTO cuenta_cobro (empresa_id, nombre, tipo, metodo_ingesta,
                                  moneda, saldo_actual, activo, fecha_creacion)
        SELECT e.id, c.nombre, 'mercadopago', 'api', 'ARS', 0, true, now()
        FROM empresa e
        CROSS JOIN (VALUES
            (:cuenta_roman),
            (:cuenta_nachi)
        ) AS c(nombre)
        WHERE NOT EXISTS (
            SELECT 1 FROM cuenta_cobro cc
            WHERE cc.empresa_id = e.id AND cc.nombre = c.nombre
        )
    """), {'cuenta_roman': CUENTA_ROMAN, 'cuenta_nachi': CUENTA_NACHI})

    # --- Cableado: que canal cobra en que cuenta ----------------------------
    # Solo se escribe donde todavia esta NULL. Si una slice futura reasigna un
    # canal a mano, volver a correr esta migracion no se lo pisa.
    conn.execute(sa.text("""
        UPDATE canal_venta cv
        SET cuenta_cobro_id = cc.id
        FROM cuenta_cobro cc
        WHERE cc.empresa_id = cv.empresa_id
          AND cc.nombre = :cuenta
          AND cv.tipo IN ('tiendanube', 'manual')
          AND cv.cuenta_cobro_id IS NULL
    """), {'cuenta': CUENTA_ROMAN})

    conn.execute(sa.text("""
        UPDATE canal_venta cv
        SET cuenta_cobro_id = cc.id
        FROM cuenta_cobro cc
        WHERE cc.empresa_id = cv.empresa_id
          AND cc.nombre = :cuenta
          AND cv.tipo = 'mercadolibre'
          AND cv.cuenta_cobro_id IS NULL
    """), {'cuenta': CUENTA_NACHI})


def downgrade():
    conn = op.get_bind()

    # Desapuntar antes de borrar, si no la FK bloquea el DELETE.
    conn.execute(sa.text("""
        UPDATE canal_venta cv
        SET cuenta_cobro_id = NULL
        WHERE cv.cuenta_cobro_id IN (
            SELECT cc.id FROM cuenta_cobro cc WHERE cc.nombre IN (:roman, :nachi)
        )
    """), {'roman': CUENTA_ROMAN, 'nachi': CUENTA_NACHI})

    # Solo las cuentas sembradas que no llegaron a usarse. Si ya tienen
    # movimientos, pagos o liquidaciones colgando, borrarlas seria perder plata
    # registrada: se prefiere dejar la fila.
    conn.execute(sa.text("""
        DELETE FROM cuenta_cobro cc
        WHERE cc.nombre IN (:roman, :nachi)
          AND cc.tipo = 'mercadopago'
          AND NOT EXISTS (SELECT 1 FROM movimiento_cuenta m WHERE m.cuenta_id = cc.id)
          AND NOT EXISTS (SELECT 1 FROM pago p WHERE p.cuenta_cobro_id = cc.id)
          AND NOT EXISTS (SELECT 1 FROM liquidacion l WHERE l.cuenta_cobro_id = cc.id)
          AND NOT EXISTS (SELECT 1 FROM gasto g WHERE g.cuenta_pago_id = cc.id)
          AND NOT EXISTS (SELECT 1 FROM sync_log s WHERE s.cuenta_cobro_id = cc.id)
    """), {'roman': CUENTA_ROMAN, 'nachi': CUENTA_NACHI})

    with op.batch_alter_table('canal_venta', schema=None) as batch_op:
        batch_op.drop_constraint('fk_canal_venta_cuenta_cobro', type_='foreignkey')
        batch_op.drop_index('ix_canal_venta_cuenta_cobro_id')
        batch_op.drop_column('cuenta_cobro_id')

    with op.batch_alter_table('credencial_cuenta_cobro', schema=None) as batch_op:
        batch_op.drop_index('ix_credencial_cuenta_cobro_cuenta_cobro_id')
    op.drop_table('credencial_cuenta_cobro')

    with op.batch_alter_table('cuenta_cobro', schema=None) as batch_op:
        batch_op.drop_column('id_cuenta_externa')
