# -*- coding: utf-8 -*-
"""FASE-RESULTADOS-S2 categoria es_prueba

Revision ID: 74115c49e5c7
Revises: 52e5bd8bc955
Create Date: 2026-10-19 15:49:44.534515

ESQUEMA

`categoria.es_prueba`: BOOLEAN NOT NULL DEFAULT false. Marca las categorias
de muestras y productos de prueba, que el resultado del negocio muestra en
su propia linea, debajo de una "utilidad neta antes de pruebas". Siguen
siendo gasto y restan igual: la utilidad neta no cambia un peso.

CHECK `ck_categoria_prueba_es_gasto`: solo una categoria de naturaleza
'gasto' puede ser de prueba.

DATOS

Backfill: la categoria sembrada "Productos de prueba" (tipo gasto, naturaleza
gasto) pasa a es_prueba = true. Va por NOMBRE solo esta unica vez, al
sembrar; de aca en adelante lo que manda es la marca, y renombrar la
categoria ya no mueve su gasto de linea. En una base sin esa categoria el
UPDATE no toca nada.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '74115c49e5c7'
down_revision = '52e5bd8bc955'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('categoria', schema=None) as batch_op:
        batch_op.add_column(sa.Column('es_prueba', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.create_check_constraint('ck_categoria_prueba_es_gasto', "es_prueba = false OR naturaleza = 'gasto'")

    op.execute("""
        UPDATE categoria SET es_prueba = true
         WHERE tipo = 'gasto' AND naturaleza = 'gasto'
           AND nombre = 'Productos de prueba'
    """)


def downgrade():
    with op.batch_alter_table('categoria', schema=None) as batch_op:
        batch_op.drop_constraint('ck_categoria_prueba_es_gasto', type_='check')
        batch_op.drop_column('es_prueba')
