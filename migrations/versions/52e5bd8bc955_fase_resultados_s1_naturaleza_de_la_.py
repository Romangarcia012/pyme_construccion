# -*- coding: utf-8 -*-
"""FASE-RESULTADOS-S1 naturaleza de la categoria

Revision ID: 52e5bd8bc955
Revises: 2b422ca4fd6c
Create Date: 2026-10-19 13:20:25.890690

Migracion de ESQUEMA y de DATOS. Las dos mitades van juntas porque la de datos
no significa nada sin la columna, y la columna sola deja las categorias sin
clasificar -- el resultado del negocio no tendria que restar.

ESQUEMA

`categoria.naturaleza`: 'activo' | 'gasto' | NULL (ver NATURALEZAS en
models.py). Dos CHECK: el vocabulario, y que solo la lleve una categoria de
tipo gasto.

DATOS (todo lo aprobado por Roman en la FASE 1 de esta slice)

1. Naturaleza de las categorias sembradas, por NOMBRE y para cualquier
   empresa que las tenga:

       Compra de mercadería   activo
       Aduana/Impuestos       activo  (la aduana de importacion es costo de
                                       la mercaderia, ya esta dentro de
                                       costo_unitario)
       Publicidad             gasto
       Productos de prueba    gasto   (decision de Roman: las muestras son
                                       investigacion, no stock)
       Envíos y empaque       gasto, y pasa a llamarse "Empaque y despacho"

   La categoria mixta se parte: nace "Flete de compra" (activo) para el
   flete que TRAE la mercaderia del proveedor; el empaque para despachar
   ventas se queda en la vieja, renombrada.

2. Las compras que se borraron el 2026-09-14 (historial 60-70) vuelven a
   cargarse, solo en la empresa "Korvo". Los montos salen de `historial`; la
   fecha original no quedo guardada en ningun lado y se usa:
     - 2026-07-28 (la fecha del aporte de capital que las pago) para las que
       suman exactamente ese aporte;
     - 2026-09-14 (la fecha de los aportes 11 y 12) para el pago de taxes de
       los microfonos y el envio de las pruebas.
   Todas con origen_fondo='capital', que es como se pagaron: el aporte 10
   ($2.371.037,83) es la suma exacta de los gastos de capital al 2026-09-06.

   "Taxes aduana prueba y tarj" ($246.537,67) mezclaba dos naturalezas y se
   recarga partido en dos filas:
       228.969,00 tarjeteros  -> 300 u. x 3.994,18 menos compra y flete
        17.568,67 muestras    -> el resto
   El reparto es una INFERENCIA de FASE 1 (el stock la respalda: 61 vendidos
   + 4 regalados + 235 en deposito = 300); si el Excel dice otra cosa se
   corrige editando las dos filas, no esta migracion.

3. Sorteo, opcion B: el gasto manual "Sorteo" ($15.975,16) se borra. El costo
   de lo regalado sale ahora de los pedidos `es_regalo` x su snapshot, en el
   bloque "Resultado del negocio", y dejarlo cargado lo restaria dos veces.
   Tampoco fue una salida de caja: esos tarjeteros ya estaban pagados.

4. El ingreso 13 "Piedra que se seca" ($61.500, aporte) se borra: esa piedra
   ya esta dentro del aporte 10.

Cada INSERT y cada DELETE tiene su guarda (NOT EXISTS / filtro por empresa,
descripcion y monto), asi que en una base sin esas filas -- la de los tests,
la de otra empresa -- la mitad de datos pasa sin tocar nada.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '52e5bd8bc955'
down_revision = '2b422ca4fd6c'
branch_labels = None
depends_on = None


EMPRESA = 'Korvo'

# (descripcion, monto, fecha, categoria)
COMPRAS_RECARGADAS = [
    ('Compra tarjeteros', '481355.00', '2026-07-28', 'Compra de mercadería'),
    ('Envio Tarjeteros', '487930.00', '2026-07-28', 'Flete de compra'),
    ("Taxes aduana tarjeteros (parte de 'Taxes aduana prueba y tarj')",
     '228969.00', '2026-07-28', 'Aduana/Impuestos'),
    ('Compra microfonos', '464000.00', '2026-07-28', 'Compra de mercadería'),
    ('Envio Microfonos', '294000.00', '2026-07-28', 'Flete de compra'),
    ('Pago taxes microfonos', '256417.17', '2026-09-14', 'Aduana/Impuestos'),
    ('Compra Muestras', '271740.00', '2026-07-28', 'Productos de prueba'),
    ("Taxes aduana muestras (parte de 'Taxes aduana prueba y tarj')",
     '17568.67', '2026-07-28', 'Productos de prueba'),
    ('Piedra que se seca', '61500.00', '2026-07-28', 'Productos de prueba'),
    ('Pago envio prod pruebas', '600000.00', '2026-09-14',
     'Productos de prueba'),
]

SORTEO = ('Sorteo', '15975.16', '2026-09-04', 'Publicidad')
INGRESO_PIEDRA = ('Piedra que se seca', '61500.00', '2026-09-14',
                  'Aporte de capital (socios)')


def _ejecutar(sql, **params):
    op.get_bind().execute(sa.text(sql), params)


def upgrade():
    with op.batch_alter_table('categoria', schema=None) as batch_op:
        batch_op.add_column(sa.Column('naturaleza', sa.String(length=10), nullable=True))
        batch_op.create_check_constraint('ck_categoria_naturaleza_solo_gasto', "naturaleza IS NULL OR tipo = 'gasto'")
        batch_op.create_check_constraint('ck_categoria_naturaleza_vocabulario', "naturaleza IS NULL OR naturaleza IN ('activo', 'gasto')")

    # 1. Naturaleza de las categorias sembradas.
    _ejecutar("""
        UPDATE categoria SET naturaleza = 'activo'
         WHERE tipo = 'gasto'
           AND nombre IN ('Compra de mercadería', 'Aduana/Impuestos')
    """)
    _ejecutar("""
        UPDATE categoria SET naturaleza = 'gasto'
         WHERE tipo = 'gasto'
           AND nombre IN ('Publicidad', 'Productos de prueba',
                          'Envíos y empaque')
    """)
    _ejecutar("""
        UPDATE categoria SET nombre = 'Empaque y despacho'
         WHERE tipo = 'gasto' AND nombre = 'Envíos y empaque'
    """)
    _ejecutar("""
        INSERT INTO categoria (nombre, tipo, naturaleza, empresa_id,
                               usuario_id, fecha_creacion)
        SELECT 'Flete de compra', 'gasto', 'activo', c.empresa_id,
               NULL, CURRENT_TIMESTAMP
          FROM categoria c
         WHERE c.tipo = 'gasto' AND c.nombre = 'Empaque y despacho'
           AND NOT EXISTS (SELECT 1 FROM categoria x
                            WHERE x.empresa_id = c.empresa_id
                              AND x.nombre = 'Flete de compra')
    """)

    # 2. Las compras borradas el 2026-09-14, de vuelta y ya partidas.
    for descripcion, monto, fecha, categoria in COMPRAS_RECARGADAS:
        _ejecutar("""
            INSERT INTO gasto (descripcion, monto, fecha, empresa_id,
                               usuario_id, categoria_id, fecha_creacion,
                               origen_fondo)
            SELECT :descripcion, :monto, :fecha, e.id, NULL, c.id,
                   CURRENT_TIMESTAMP, 'capital'
              FROM empresa e
              JOIN categoria c ON c.empresa_id = e.id
                              AND c.tipo = 'gasto' AND c.nombre = :categoria
             WHERE e.nombre = :empresa
               AND NOT EXISTS (SELECT 1 FROM gasto g
                                WHERE g.empresa_id = e.id
                                  AND g.descripcion = :descripcion
                                  AND g.monto = :monto)
        """, descripcion=descripcion, monto=monto, fecha=fecha,
             categoria=categoria, empresa=EMPRESA)

    # 3. Sorteo, opcion B: el costo de lo regalado sale de los pedidos.
    _ejecutar("""
        DELETE FROM gasto
         WHERE descripcion = :descripcion AND monto = :monto
           AND origen_fondo = 'capital'
           AND empresa_id IN (SELECT id FROM empresa WHERE nombre = :empresa)
    """, descripcion=SORTEO[0], monto=SORTEO[1], empresa=EMPRESA)

    # 4. La piedra ya esta dentro del aporte 10.
    _ejecutar("""
        DELETE FROM ingreso
         WHERE descripcion = :descripcion AND monto = :monto
           AND empresa_id IN (SELECT id FROM empresa WHERE nombre = :empresa)
    """, descripcion=INGRESO_PIEDRA[0], monto=INGRESO_PIEDRA[1],
         empresa=EMPRESA)


def downgrade():
    """Deshace todo, datos incluidos, en el orden inverso.

    El Sorteo y el ingreso de la piedra vuelven con sus datos originales (los
    ids nuevos los pone la secuencia). "Flete de compra" se borra solo si
    ningun gasto la usa: si alguien ya cargo un flete ahi, borrarla romperia
    la FK, y el downgrade prefiere dejarla.
    """
    _ejecutar("""
        INSERT INTO ingreso (descripcion, monto, fecha, empresa_id,
                             usuario_id, categoria_id, fecha_creacion)
        SELECT :descripcion, :monto, :fecha, e.id, NULL, c.id,
               CURRENT_TIMESTAMP
          FROM empresa e
          JOIN categoria c ON c.empresa_id = e.id
                          AND c.tipo = 'ingreso' AND c.nombre = :categoria
         WHERE e.nombre = :empresa
           AND NOT EXISTS (SELECT 1 FROM ingreso i
                            WHERE i.empresa_id = e.id
                              AND i.descripcion = :descripcion
                              AND i.monto = :monto)
    """, descripcion=INGRESO_PIEDRA[0], monto=INGRESO_PIEDRA[1],
         fecha=INGRESO_PIEDRA[2], categoria=INGRESO_PIEDRA[3],
         empresa=EMPRESA)

    _ejecutar("""
        INSERT INTO gasto (descripcion, monto, fecha, empresa_id, usuario_id,
                           categoria_id, fecha_creacion, origen_fondo)
        SELECT :descripcion, :monto, :fecha, e.id, NULL, c.id,
               CURRENT_TIMESTAMP, 'capital'
          FROM empresa e
          JOIN categoria c ON c.empresa_id = e.id
                          AND c.tipo = 'gasto' AND c.nombre = :categoria
         WHERE e.nombre = :empresa
           AND NOT EXISTS (SELECT 1 FROM gasto g
                            WHERE g.empresa_id = e.id
                              AND g.descripcion = :descripcion
                              AND g.monto = :monto)
    """, descripcion=SORTEO[0], monto=SORTEO[1], fecha=SORTEO[2],
         categoria=SORTEO[3], empresa=EMPRESA)

    for descripcion, monto, _fecha, _categoria in COMPRAS_RECARGADAS:
        _ejecutar("""
            DELETE FROM gasto
             WHERE descripcion = :descripcion AND monto = :monto
               AND origen_fondo = 'capital'
               AND empresa_id IN (SELECT id FROM empresa
                                   WHERE nombre = :empresa)
        """, descripcion=descripcion, monto=monto, empresa=EMPRESA)

    _ejecutar("""
        DELETE FROM categoria
         WHERE nombre = 'Flete de compra' AND tipo = 'gasto'
           AND NOT EXISTS (SELECT 1 FROM gasto g
                            WHERE g.categoria_id = categoria.id)
    """)
    _ejecutar("""
        UPDATE categoria SET nombre = 'Envíos y empaque'
         WHERE tipo = 'gasto' AND nombre = 'Empaque y despacho'
    """)

    with op.batch_alter_table('categoria', schema=None) as batch_op:
        batch_op.drop_constraint('ck_categoria_naturaleza_vocabulario', type_='check')
        batch_op.drop_constraint('ck_categoria_naturaleza_solo_gasto', type_='check')
        batch_op.drop_column('naturaleza')
