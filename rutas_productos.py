# -*- coding: utf-8 -*-
"""Listado de stock (FASE-STOCK-S1).

Solo lectura. Hasta ahora `producto.stock` solo se veia entrando a Supabase:
lo escribia el resync de Tiendanube (FASE3-S3) y desde esta slice tambien lo
descuenta la venta presencial (`rutas_ventas.py`), pero no habia pantalla.

Lo que NO hace y es a proposito: no se puede editar el stock desde aca. La
fuente de verdad sigue siendo Tiendanube y cada resync pisa el numero, asi que
una edicion manual duraria hasta la proxima corrida y seria mentira mientras
tanto. Tampoco hay alertas de stock bajo.

El blueprint se registra en app.py; ninguna ruta existente se toca.
"""

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from models import CanalVenta, MapeoProductoCanal, Producto

productos_bp = Blueprint('productos', __name__, url_prefix='/productos')

# Como se muestra cada canal de origen. El fallback es el nombre de la fila,
# asi un canal futuro no aparece en blanco.
ETIQUETA_CANAL = {
    'tiendanube': 'Tiendanube',
    'mercadolibre': 'Mercado Libre',
    'manual': 'Manual',
}


def _canales_por_producto(empresa_id):
    """producto_id -> lista de canales de los que vino ese producto.

    Un solo par de queries en vez de uno por fila: el catalogo de una PyME de
    corralon entra holgado en memoria y la alternativa serian N+1 viajes a
    Supabase por cada carga de la pantalla.

    Un producto puede estar mapeado a mas de un canal (el mismo SKU publicado
    en Tiendanube y en Mercado Libre), por eso el valor es una lista.
    """
    canales = {
        canal.id: ETIQUETA_CANAL.get(canal.tipo, canal.nombre)
        for canal in CanalVenta.query.filter_by(empresa_id=empresa_id).all()
    }

    # El filtro por canal (y no por producto) es lo que mantiene el listado
    # dentro de la empresa: los canales ya vienen filtrados por empresa_id.
    por_producto = {}
    if canales:
        mapeos = (MapeoProductoCanal.query
                  .filter(MapeoProductoCanal.canal_id.in_(list(canales)))
                  .all())
        for mapeo in mapeos:
            etiqueta = canales.get(mapeo.canal_id)
            if not etiqueta:
                continue
            vistos = por_producto.setdefault(mapeo.producto_id, [])
            if etiqueta not in vistos:
                vistos.append(etiqueta)

    return por_producto


@productos_bp.route('/listar')
@login_required
def listar_stock():
    """Que hay y cuanto queda, por producto."""
    productos = (Producto.query
                 .filter_by(empresa_id=current_user.empresa_id)
                 .order_by(Producto.nombre, Producto.sku)
                 .all())

    canales = _canales_por_producto(current_user.empresa_id)

    filas = []
    for producto in productos:
        filas.append({
            'sku': producto.sku,
            'nombre': producto.nombre,
            # None y 0 no son lo mismo y la plantilla los distingue: None es
            # "este producto no lleva control de stock", 0 es "no queda
            # ninguno". La vista pasa el None crudo y decide alla.
            'stock': producto.stock,
            'canales': ', '.join(canales.get(producto.id, [])),
            'activo': producto.activo,
        })

    return render_template('productos_listar.html', filas=filas)
