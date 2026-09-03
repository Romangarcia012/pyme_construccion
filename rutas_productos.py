# -*- coding: utf-8 -*-
"""Pantallas de producto: listado de stock (FASE-STOCK-S1), carga del costo
(FASE-REPORTES-S3-COSTO) y resumen de vendido por canal y color
(FASE-REPORTES-S1).

Hasta ahora `producto.stock` solo se veia entrando a Supabase: lo escribia el
resync de Tiendanube (FASE3-S3) y tambien lo descuenta la venta presencial
(`rutas_ventas.py`), pero no habia pantalla.

Lo que NO se puede editar desde aca, y es a proposito: el stock. La fuente de
verdad sigue siendo Tiendanube y cada resync pisa el numero, asi que una
edicion manual duraria hasta la proxima corrida y seria mentira mientras tanto.
Tampoco hay alertas de stock bajo.

Lo que SI se edita, desde FASE-REPORTES-S3-COSTO, es `costo_unitario`. Es el
caso opuesto al del stock: Tiendanube no lo sabe y ningun sync lo pisa (ver
`ingestor_tiendanube._normalizar_producto`, que lo deja NULL siempre), asi que
el unico modo de que exista es que alguien lo escriba. Sin el, el margen de
cada venta es NULL y no hay reporte de rentabilidad posible.

El blueprint se registra en app.py; ninguna ruta existente se toca.
"""

from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ingestor_tiendanube import a_decimal
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


# --------------------------------------------------------------------------
# FASE-REPORTES-S3-COSTO: la sugerencia de Tiendanube
# --------------------------------------------------------------------------
# Tiendanube manda un `cost` por linea dentro del payload de detalle de cada
# pedido (products[].cost). Ya esta guardado en `pedido.raw_payload` desde
# FASE-REPORTES-S3-FIX2: no hace falta llamar al catalogo ni pedir ningun scope
# nuevo, alcanza con leer lo que la base ya tiene.
#
# Ese numero NO es el costo real y por eso solo se muestra, nunca se guarda ni
# autocompleta. Verificado contra el Tarjetero Negro: Tiendanube dice 3230.81 y
# el costo de verdad que Roman tiene anotado es 3994.18. La diferencia es que
# el `cost` del panel de Tiendanube cubre compra + flete y se queda sin los
# impuestos ni el empaque. Sirve de piso y de recordatorio; el numero lo pone
# Roman.

# Cuantos pedidos se leen hacia atras para armar la sugerencia. Cada uno trae
# su raw_payload entero, que es un JSON grande, y esto es una ayuda visual: no
# justifica arrastrar el historico completo a memoria en cada carga de la
# pantalla. Un producto que no se vendio en los ultimos 200 pedidos queda sin
# sugerencia, que es exactamente lo que corresponde -- del cost de hace un anio
# no hay nada que sugerir.
LIMITE_PEDIDOS_SUGERENCIA = 200


def _costo_sugerido_por_producto(empresa_id):
    """producto_id -> el `cost` mas reciente que mando el canal, o {}.

    Se cruza por el par (id externo del producto, id externo de la variante)
    contra MapeoProductoCanal, que es la misma identidad que usa el sync. El
    canal sale del pedido, no del payload: el mapeo es unico por canal y dos
    canales podrian repetir un id externo.

    Los pedidos se recorren del mas nuevo al mas viejo y gana el primero que
    aparece: el costo de una variante cambia con el tiempo y el ultimo es el
    unico que describe hoy.
    """
    mapeos = {}
    for mapeo in (MapeoProductoCanal.query
                  .join(CanalVenta, CanalVenta.id == MapeoProductoCanal.canal_id)
                  .filter(CanalVenta.empresa_id == empresa_id)
                  .all()):
        clave = (mapeo.canal_id, mapeo.id_producto_externo,
                 mapeo.id_variante_externo or '')
        mapeos[clave] = mapeo.producto_id

    if not mapeos:
        return {}

    pedidos = (Pedido.query
               .filter_by(empresa_id=empresa_id)
               .order_by(Pedido.fecha_pedido.desc(), Pedido.id.desc())
               .limit(LIMITE_PEDIDOS_SUGERENCIA)
               .all())

    sugeridos = {}
    for pedido in pedidos:
        payload = pedido.raw_payload
        if not isinstance(payload, dict):
            continue
        for linea in (payload.get('products') or []):
            if not isinstance(linea, dict):
                continue
            clave = (pedido.canal_id,
                     str(linea.get('product_id') or ''),
                     str(linea.get('variant_id') or ''))
            producto_id = mapeos.get(clave)
            # El `in` es lo que hace que gane el pedido mas nuevo: ya recorrido
            # de nuevo a viejo, el primero que escribe es el que queda.
            if producto_id is None or producto_id in sugeridos:
                continue
            # por_defecto=None: una linea sin `cost` (o con basura) no deja
            # sugerencia, en vez de sugerir un costo de cero.
            costo = a_decimal(linea.get('cost'), por_defecto=None)
            if costo is not None:
                sugeridos[producto_id] = costo

    return sugeridos


@productos_bp.route('/listar')
@login_required
def listar_stock():
    """Que hay, cuanto queda y cuanto cuesta, por producto."""
    productos = (Producto.query
                 .filter_by(empresa_id=current_user.empresa_id)
                 .order_by(Producto.nombre, Producto.sku)
                 .all())

    canales = _canales_por_producto(current_user.empresa_id)
    sugeridos = _costo_sugerido_por_producto(current_user.empresa_id)

    filas = []
    for producto in productos:
        filas.append({
            'sku': producto.sku,
            'nombre': producto.nombre,
            # None y 0 no son lo mismo y la plantilla los distingue: None es
            # "este producto no lleva control de stock", 0 es "no queda
            # ninguno". La vista pasa el None crudo y decide alla.
            'stock': producto.stock,
            # El costo cargado va crudo al input: NULL es un input vacio, y el
            # vacio NO se rellena con la sugerencia de Tiendanube. Que quede
            # vacio es lo que distingue "todavia no lo cargue" de "vale esto".
            'costo_unitario': producto.costo_unitario,
            'costo_tn': sugeridos.get(producto.id),
            'canales': ', '.join(canales.get(producto.id, [])),
            'activo': producto.activo,
        })

    return render_template('productos_listar.html', filas=filas)


class CostoInvalido(Exception):
    """Lo que vino en el formulario no es un costo. El mensaje se le muestra a
    quien lo estaba cargando, asi que se escribe en criollo."""


def _leer_costo(texto, nombre):
    """El texto del input -> Decimal con dos decimales, o None si vino vacio.

    El vacio es un valor con significado, no un error: borrar el input es como
    se saca un costo mal cargado y se vuelve a "no lo se". Guardar 0 en su
    lugar seria afirmar que el producto es gratis, y ese cero se congelaria en
    el snapshot de cada venta que venga despues.

    La coma se acepta como decimal porque es lo que tipea cualquiera aca; es el
    mismo criterio que usa `rutas_ventas._leer_precio`.
    """
    texto = (texto or '').strip().replace(',', '.')
    if not texto:
        return None
    try:
        costo = Decimal(texto)
    except (InvalidOperation, ValueError):
        raise CostoInvalido('El costo de "%s" no es un numero.' % nombre)
    # NaN e Infinity pasan por Decimal() sin quejarse y romperian recien contra
    # la base, con la transaccion ya a medias.
    if not costo.is_finite():
        raise CostoInvalido('El costo de "%s" no es un numero.' % nombre)
    if costo < 0:
        raise CostoInvalido('El costo de "%s" no puede ser negativo.' % nombre)
    return costo.quantize(Decimal('0.01'))


@productos_bp.route('/costos', methods=['POST'])
@login_required
def guardar_costos():
    """Guarda el costo unitario que se cargo a mano en el listado.

    Es una sola tanda para toda la pantalla: Roman abre el listado, completa
    los que sabe y aprieta una vez. Por eso tambien es todo o nada -- si una
    fila trae basura no se guarda ninguna y el formulario vuelve con el error.
    Guardar las buenas y descartar la mala dejaria la pantalla mostrando un
    exito parcial que nadie pidio.

    Lo que esta ruta NO toca: `pedido_item.costo_unitario_snapshot`. El costo
    que se guarda aca rige de aca en adelante -- lo congela cada venta nueva al
    momento de ocurrir (sync_tiendanube y rutas_ventas ya lo hacen). Las ventas
    ya guardadas con el snapshot en NULL se quedan asi: reescribirlas seria
    inventar que ese costo regia cuando se vendieron, que es justamente lo que
    el snapshot existe para evitar.
    """
    # El filtro por empresa es lo unico que impide que un SKU ajeno entre por
    # el formulario: lo que llega son cadenas que mando el cliente.
    productos = {producto.sku: producto for producto in
                 Producto.query.filter_by(empresa_id=current_user.empresa_id).all()}

    skus = request.form.getlist('sku')
    costos = request.form.getlist('costo_unitario')

    try:
        cambios = []
        for indice, sku in enumerate(skus):
            sku = (sku or '').strip()
            producto = productos.get(sku)
            if producto is None:
                # Una fila que no corresponde a un producto de la empresa se
                # ignora en silencio en vez de voltear la tanda: el listado se
                # arma desde el servidor, asi que esto solo pasa si el catalogo
                # cambio entre que se abrio la pantalla y se apreto guardar.
                continue
            texto = costos[indice] if indice < len(costos) else ''
            costo = _leer_costo(texto, producto.nombre)
            if costo != producto.costo_unitario:
                cambios.append((producto, costo))

        # Recien se escribe cuando TODAS las filas pasaron la validacion: con
        # la asignacion adentro del bucle de arriba, una fila mala mas abajo
        # dejaria las anteriores ya modificadas en la sesion.
        for producto, costo in cambios:
            producto.costo_unitario = costo
        db.session.commit()
    except CostoInvalido as error:
        db.session.rollback()
        flash(str(error), 'error')
        return redirect(url_for('productos.listar_stock'))
    except Exception:  # noqa: BLE001
        db.session.rollback()
        flash('No se pudieron guardar los costos. Revisa los datos e intenta de nuevo.',
              'error')
        return redirect(url_for('productos.listar_stock'))

    if cambios:
        flash('Se actualizo el costo de %d producto%s.'
              % (len(cambios), '' if len(cambios) == 1 else 's'), 'success')
    else:
        flash('No hubo cambios que guardar.', 'warning')
    return redirect(url_for('productos.listar_stock'))


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


def _clave_de_grupo(producto, mapeos_por_producto, con_canal=True):
    """Que variantes van juntas bajo un mismo encabezado.

    Dos colores del mismo producto son dos filas distintas de `producto`
    (FASE2-S1) pero comparten el id del producto padre en el canal. Ese id es
    lo que las junta: por texto no se puede, justamente porque los nombres
    difieren.

    Un producto sin mapeo (nunca vino de un canal) es su propio grupo.

    `con_canal=False` lo usa el reporte de margen (FASE-REPORTES-S3-MARGEN):
    ahi el canal no parte el grupo, porque un producto es una fila sola sin
    importar por donde se vendio. Aca si parte, y a proposito: en esta
    pantalla el canal es una columna, y dos productos padre distintos de dos
    canales distintos pueden repetir el id externo sin tener nada que ver.
    """
    mapeos = mapeos_por_producto.get(producto.id)
    if mapeos:
        primero = mapeos[0]
        if con_canal:
            return ('externo', primero.canal_id, primero.id_producto_externo)
        return ('externo', primero.id_producto_externo)
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


def _stock_inicial(stock, vendido):
    """Con cuanto se arranco: lo que queda mas lo que salio.

    Es la columna con la que empieza la planilla. No esta guardada en ningun
    lado -- nadie anoto el stock del dia cero -- pero se reconstruye sola: si
    quedan 93 y se vendieron 5, habia 98.

    Sin control de stock no hay de donde partir: el vendido solo no dice
    cuanto habia, asi que la fila queda sin control tambien.
    """
    if stock is None:
        return None
    return stock + vendido


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
        total_stock_inicial = None
        total_vendido = 0

        for producto, etiqueta in grupo['variantes']:
            por_canal = [vendido.get((producto.id, canal_id), 0)
                         for canal_id in ids_canal]
            vendido_fila = sum(por_canal)
            stock_inicial = _stock_inicial(producto.stock, vendido_fila)

            filas.append({
                'sku': producto.sku,
                # Sin etiqueta de variante (un producto sin colores) la fila se
                # nombra con el nombre completo: nunca queda en blanco.
                'variante': etiqueta if (compartido and etiqueta) else producto.nombre,
                'stock_inicial': stock_inicial,
                'stock': producto.stock,
                'por_canal': por_canal,
                'vendido': vendido_fila,
                'activo': producto.activo,
            })

            total_por_canal = [acum + unidades for acum, unidades
                               in zip(total_por_canal, por_canal)]
            total_stock = _sumar_stock(total_stock, producto.stock)
            # El TOTAL suma las filas que se ven, no rehace la cuenta sobre los
            # totales: si una variante queda sin control de stock, sale de las
            # dos cuentas a la vez y la columna sigue cerrando con lo de arriba.
            total_stock_inicial = _sumar_stock(total_stock_inicial, stock_inicial)
            total_vendido += vendido_fila

        salida.append({
            'nombre': nombre_grupo,
            'filas': filas,
            'total': {
                'stock_inicial': total_stock_inicial,
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
