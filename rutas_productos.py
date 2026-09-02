# -*- coding: utf-8 -*-
"""Pantallas de producto: listado de stock (FASE-STOCK-S1) y resumen de
vendido por canal y color (FASE-REPORTES-S1).

Las dos son de solo lectura. Hasta ahora `producto.stock` solo se veia entrando a Supabase:
lo escribia el resync de Tiendanube (FASE3-S3) y desde esta slice tambien lo
descuenta la venta presencial (`rutas_ventas.py`), pero no habia pantalla.

Lo que NO hace y es a proposito: no se puede editar el stock desde aca. La
fuente de verdad sigue siendo Tiendanube y cada resync pisa el numero, asi que
una edicion manual duraria hasta la proxima corrida y seria mentira mientras
tanto. Tampoco hay alertas de stock bajo.

El blueprint se registra en app.py; ninguna ruta existente se toca.
"""

from collections import OrderedDict

from flask import Blueprint, render_template
from flask_login import current_user, login_required
from sqlalchemy import func

from models import (
    CanalVenta,
    MapeoProductoCanal,
    Pedido,
    PedidoItem,
    Producto,
    db,
)

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


# --------------------------------------------------------------------------
# FASE-REPORTES-S1: vendido por canal y color
# --------------------------------------------------------------------------
# Reemplaza la hoja INVENTARIO que Roman arma a mano en Excel: por cada
# producto, sus variantes de color con lo que queda y lo que se vendio,
# abierto por canal. Solo lectura y solo agregacion: no escribe nada, no
# edita, no alerta. Una tabla, como la planilla que reemplaza.
#
# Lo que la pantalla NO muestra y en el Excel esta: "stock inicial". No existe
# como dato en el sistema y ponerlo obligaria a inventarlo o a pedir una carga
# manual. Quien lo necesite lo reconstruye sumando Stock actual + Vendido.

# Los estados que NO cuentan como venta. En la base real de hoy el unico valor
# de pedido.estado que existe es 'open' (Tiendanube manda su status tal cual:
# 'open' | 'closed' | 'cancelled'; las ventas de mostrador nacen 'completado').
# O sea: hoy este filtro no descarta ninguna fila. Esta puesto igual para el
# dia que aparezca la primera cancelacion, que va a entrar sola sin que haya
# que acordarse de tocar el reporte. Se compara en minusculas y se incluyen
# las dos grafias del ingles porque la de Tiendanube es 'cancelled'.
ESTADOS_NO_VENDIDOS = ('cancelled', 'canceled', 'cancelado', 'anulado')

# Como se llama el renglon de los items que no se pudieron atar a un producto
# (pedido_item.producto_id NULL, contemplado desde FASE3-S2). No se descartan:
# si se cayeran del reporte, el total dejaria de coincidir con lo que de verdad
# salio, y la diferencia seria invisible.
ETIQUETA_SIN_IDENTIFICAR = 'Sin identificar'


def _partir_nombre(nombre):
    """'Tarjetero de Aluminio (Negro)' -> ('Tarjetero de Aluminio', 'Negro').

    Es exactamente el formato que arma el sync de Tiendanube al bajar cada
    variante (`_nombre_de_variante` en ingestor_tiendanube.py), asi que no es
    adivinar: es leer lo que este mismo sistema escribio. Un nombre sin
    parentesis final vuelve entero y sin etiqueta.
    """
    if nombre and nombre.endswith(')') and ' (' in nombre:
        base, _, etiqueta = nombre.rpartition(' (')
        base = base.strip()
        if base:
            return base, etiqueta[:-1].strip()
    return nombre, None


def _clave_de_grupo(producto, mapeos_por_producto):
    """Que variantes van juntas bajo un mismo encabezado.

    Dos colores del mismo producto son dos filas distintas de `producto`
    (FASE2-S1) pero comparten el id del producto padre en el canal. Ese id es
    lo que las junta: por texto no se puede, justamente porque los nombres
    difieren.

    Un producto sin mapeo (nunca vino de un canal) es su propio grupo.
    """
    mapeos = mapeos_por_producto.get(producto.id)
    if mapeos:
        primero = mapeos[0]
        return ('externo', primero.canal_id, primero.id_producto_externo)
    return ('producto', producto.id)


def _mapeos_por_producto(ids_producto):
    """producto_id -> sus mapeos, en orden estable (canal, id).

    El orden importa: de el sale cual mapeo define el grupo, y dos cargas de
    la pantalla tienen que agrupar igual.
    """
    por_producto = {}
    if not ids_producto:
        return por_producto

    mapeos = (MapeoProductoCanal.query
              .filter(MapeoProductoCanal.producto_id.in_(list(ids_producto)))
              .order_by(MapeoProductoCanal.canal_id, MapeoProductoCanal.id)
              .all())
    for mapeo in mapeos:
        por_producto.setdefault(mapeo.producto_id, []).append(mapeo)
    return por_producto


def _vendido_por_producto_y_canal(empresa_id):
    """(producto_id, canal_id) -> unidades vendidas. producto_id None incluido.

    Una sola consulta agregada en vez de traer los items: lo que la pantalla
    muestra son sumas, y sumarlas del lado de la base evita arrastrar todo el
    historial de pedidos a memoria cada vez que alguien entra.
    """
    filas = (db.session.query(PedidoItem.producto_id,
                              Pedido.canal_id,
                              func.sum(PedidoItem.cantidad))
             .join(Pedido, Pedido.id == PedidoItem.pedido_id)
             .filter(Pedido.empresa_id == empresa_id)
             .filter(func.lower(Pedido.estado).notin_(ESTADOS_NO_VENDIDOS))
             .group_by(PedidoItem.producto_id, Pedido.canal_id)
             .all())

    return {(producto_id, canal_id): int(cantidad or 0)
            for producto_id, canal_id, cantidad in filas}


def _sumar_stock(acumulado, stock):
    """Suma de stocks donde None es 'no se lleva la cuenta', no cero.

    Si ninguna variante del grupo lleva control, el total del grupo tambien es
    None: mostrar 0 ahi seria afirmar que no queda nada, que es lo contrario.
    """
    if stock is None:
        return acumulado
    return stock if acumulado is None else acumulado + stock


@productos_bp.route('/resumen')
@login_required
def resumen_ventas():
    """Vendido por canal y color, con el stock que queda al lado."""
    empresa_id = current_user.empresa_id

    # Las columnas de venta: TODOS los canales de la empresa, incluso los que
    # no vendieron nada. Un canal en cero es informacion ("por ahi no sale
    # nada"), no una columna vacia que convenga esconder.
    canales = (CanalVenta.query
               .filter_by(empresa_id=empresa_id)
               .order_by(CanalVenta.id)
               .all())
    columnas = [canal.nombre for canal in canales]
    ids_canal = [canal.id for canal in canales]

    productos = (Producto.query
                 .filter_by(empresa_id=empresa_id)
                 .order_by(Producto.nombre, Producto.sku)
                 .all())

    mapeos = _mapeos_por_producto([producto.id for producto in productos])
    vendido = _vendido_por_producto_y_canal(empresa_id)

    # Los productos ya vienen ordenados por nombre, asi que el orden en que se
    # crean los grupos es el orden en que se muestran.
    grupos = OrderedDict()
    for producto in productos:
        base, etiqueta = _partir_nombre(producto.nombre)
        clave = _clave_de_grupo(producto, mapeos)
        grupo = grupos.setdefault(clave, {'bases': [], 'variantes': []})
        grupo['bases'].append(base)
        grupo['variantes'].append((producto, etiqueta))

    salida = []
    for grupo in grupos.values():
        # El encabezado es el nombre base compartido. Si las variantes no
        # coinciden en el (nombres cargados a mano, un canal que no use el
        # formato "Base (Variante)"), no se fuerza: se muestra el nombre
        # completo de la primera y listo. Fabricar un nombre comun a partir de
        # textos que no lo tienen es peor que repetir uno.
        compartido = len(set(grupo['bases'])) == 1 and bool(grupo['bases'][0])
        nombre_grupo = grupo['bases'][0] if compartido else grupo['variantes'][0][0].nombre

        filas = []
        total_por_canal = [0] * len(ids_canal)
        total_stock = None
        total_vendido = 0

        for producto, etiqueta in grupo['variantes']:
            por_canal = [vendido.get((producto.id, canal_id), 0)
                         for canal_id in ids_canal]
            vendido_fila = sum(por_canal)

            filas.append({
                'sku': producto.sku,
                # Sin etiqueta de variante (un producto sin colores) la fila se
                # nombra con el nombre completo: nunca queda en blanco.
                'variante': etiqueta if (compartido and etiqueta) else producto.nombre,
                'stock': producto.stock,
                'por_canal': por_canal,
                'vendido': vendido_fila,
                'activo': producto.activo,
            })

            total_por_canal = [acum + unidades for acum, unidades
                               in zip(total_por_canal, por_canal)]
            total_stock = _sumar_stock(total_stock, producto.stock)
            total_vendido += vendido_fila

        salida.append({
            'nombre': nombre_grupo,
            'filas': filas,
            'total': {
                'stock': total_stock,
                'por_canal': total_por_canal,
                'vendido': total_vendido,
            },
        })

    # Los items que ningun mapeo pudo atar a un producto. Van al final, en su
    # propia tabla y sin columna de stock: no se sabe de que producto son, asi
    # que no hay stock que mostrar. Si se cayeran, el total del reporte dejaria
    # de coincidir con lo que de verdad salio.
    sin_identificar_por_canal = [vendido.get((None, canal_id), 0)
                                 for canal_id in ids_canal]
    sin_identificar = None
    if sum(sin_identificar_por_canal):
        sin_identificar = {
            'etiqueta': ETIQUETA_SIN_IDENTIFICAR,
            'por_canal': sin_identificar_por_canal,
            'vendido': sum(sin_identificar_por_canal),
        }

    return render_template('productos_resumen.html',
                           columnas=columnas,
                           grupos=salida,
                           sin_identificar=sin_identificar)
