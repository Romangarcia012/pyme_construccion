# -*- coding: utf-8 -*-
"""FASE-AUDITORIA-EXCEL-S2 el combo no tiene stock propio

Revision ID: b7d3e0c19a45
Revises: 9596c775b349
Create Date: 2026-09-05 03:20:00.000000

Migracion de DATOS, no de esquema: no agrega ni saca ninguna columna. Corrige
una fila que estaba contando mercaderia dos veces.

QUE VIENE A ARREGLAR

La auditoria del Excel contra la app encontro que el "Combo 3 Tarjeteros Korvo
de Aluminio" (SKU `Combo Tarjeteros`) tenia stock = 60. Ese 60 no es
mercaderia aparte: el combo se ARMA con las mismas unidades fisicas que ya
cuentan Tarjetero Negro y Tarjetero Gris. Sumando las tres filas, esas piezas
figuraban dos veces.

POR QUE NULL Y NO UN NUMERO -- NI SIQUIERA CERO

Porque el vocabulario ya existe en el codigo y dice exactamente esto:

    NULL  = "nadie lleva la cuenta de este producto"
    0     = "no queda ninguno"

Los tres lugares que leen el campo estan de acuerdo y tratan NULL como "no
aplica", no como "cero": `rutas_ventas._descontar_stock` lo saltea al vender,
`stock_tiendanube.empujar_stock` (linea 129) no le informa nada a la tienda, y
`rutas_devoluciones` no le suma nada al devolver.

Un 0 aca afirmaria algo falso -- que no se puede armar ningun combo -- cuando
en realidad hay 170 negros y 93 grises con los que armarlo. El combo no tiene
stock propio: tiene el de sus partes.

LO QUE ESTA MIGRACION NO PUEDE GARANTIZAR

Esto dura hasta el proximo sync de Tiendanube. `sync_tiendanube.
_upsert_producto_y_mapeo` pisa `producto.stock` en CADA corrida con lo que
diga la tienda, NULL incluido, y es a proposito: la fuente de verdad del stock
es Tiendanube, no esta base.

O sea que el arreglo permanente no es SQL, es apagarle el control de stock al
combo EN EL PANEL DE TIENDANUBE. Cuando eso pase, TN va a mandar stock null,
el sync va a escribir NULL solo, y las dos puntas van a decir lo mismo sin
pelearse. Hasta entonces esta migracion deja la base bien y el proximo sync la
vuelve a romper.

POR QUE EL UPDATE VA POR SKU Y NO POR ID

El id 44 es de la base de Korvo y de nadie mas. El SKU es el que Tiendanube le
puso al producto y es el que usa el propio sync para encontrar la fila
(`Producto.query.filter_by(empresa_id=..., sku=...)`). En una base donde ese
producto no exista, el UPDATE no toca nada y la migracion pasa igual.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'b7d3e0c19a45'
down_revision = '9596c775b349'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        UPDATE producto
           SET stock = NULL
         WHERE sku = 'Combo Tarjeteros'
           AND stock IS NOT NULL
    """)


def downgrade():
    """No se restaura el 60.

    Bajar esta migracion no puede querer decir "volve a contar las mismas
    piezas dos veces". El 60 era el numero equivocado; reescribirlo seria
    reintroducir el duplicado a proposito. El downgrade existe para que la
    cadena sea reversible, y no hace nada.
    """
    pass
