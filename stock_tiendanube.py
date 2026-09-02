# -*- coding: utf-8 -*-
"""Empuje de stock a Tiendanube despues de una venta presencial (FASE-STOCK-S1).

Contexto: el resync de FASE3-S3 sigue siendo la fuente de verdad del catalogo y
pisa `producto.stock` en cada corrida. Esto no lo cambia. Lo que agrega es el
descuento puntual del mostrador ENTRE dos sincronizaciones: si alguien compra
tres martillos en el local, la tienda online no puede seguir ofreciendo esos
tres hasta la proxima corrida del backfill.

Por eso el push manda el stock ABSOLUTO ya descontado y no un delta: si esta
llamada se repite (un reintento, un doble submit que el navegador reenvia) el
resultado del lado de Tiendanube es el mismo. Y si el numero quedara desfasado
igual, el proximo resync lo corrige -- este modulo no es la ultima palabra
sobre el stock, es un parche entre corridas.

A diferencia del backfill de FASE3-S2, esto corre SINCRONICO dentro del request
de la venta: son una o dos lineas, no cientos de paginas, y un thread aparte
solo agregaria complejidad para ahorrar un par de segundos.

REGLA QUE NO SE NEGOCIA: nada de lo que pase aca puede voltear la venta. La
venta ya ocurrio fisicamente y ya esta commiteada antes de que se llame a este
modulo. Todo error se junta y se devuelve para avisarlo; ninguno se propaga.

PERMISOS: la unica escritura que hace es el PUT de stock de una variante. El
scope "Edit Products" que habilita eso tambien alcanzaria para tocar precios,
pedidos o clientes; no se usa para nada de eso.
"""

import sys
from datetime import datetime

from integracion_tiendanube import ErrorTiendanube, actualizar_stock_variante
from models import CanalVenta, MapeoProductoCanal, Producto, SyncLog, db

# El canal contra el que se empuja. Es el unico: Mercado Libre todavia no
# existe como integracion y el canal 'manual' no tiene API.
TIPO_TIENDANUBE = 'tiendanube'

# Como quedan marcadas las filas de sync_log de esta slice. Se separan de las
# del backfill ('backfill') para que un fallo de stock no se confunda con una
# corrida de sincronizacion rota.
ENTIDAD = 'stock_push'
OPERACION = 'venta_manual'


def _log(mensaje):
    sys.stderr.write('[stock-tiendanube] %s\n' % mensaje)
    sys.stderr.flush()


def canal_tiendanube(empresa_id):
    """El canal de Tiendanube conectado de la empresa, o None.

    Un canal inactivo es un canal desconectado: no hay a donde empujar y
    tampoco es un error que haya que reportar.
    """
    return CanalVenta.query.filter_by(
        empresa_id=empresa_id, tipo=TIPO_TIENDANUBE, activo=True).first()


def _mapeo_de(canal_id, producto_id):
    """El mapeo del producto hacia el canal, o None si no vino de ahi.

    `mapeo_producto_canal` no tiene columna `activo`: lo que se prende y se
    apaga es el canal. Un mapeo cuyo canal esta activo es un mapeo activo.
    """
    return MapeoProductoCanal.query.filter_by(
        canal_id=canal_id, producto_id=producto_id).first()


def _registrar_error(canal_id, mensaje, arranque):
    """Deja el rastro del fallo en sync_log.

    Una fila por linea que no se pudo empujar: el flash se lo lleva el viento
    en cuanto Roman recarga la pagina, esto queda.

    Tiene su propio try porque es lo ultimo que puede fallar de una venta que
    ya esta guardada: si ni el registro del error se puede escribir, se avisa
    por el log del servidor y se sigue.
    """
    try:
        db.session.add(SyncLog(
            canal_id=canal_id,
            entidad=ENTIDAD,
            operacion=OPERACION,
            estado='error',
            registros_leidos=1,
            registros_error=1,
            mensaje_error=str(mensaje)[:2000],
            fecha_inicio=arranque,
            fecha_fin=datetime.utcnow(),
        ))
        db.session.commit()
    except Exception as fallo:  # noqa: BLE001
        db.session.rollback()
        _log('no se pudo registrar el fallo de stock del canal %s: %r' % (canal_id, fallo))


def empujar_stock(empresa_id, producto_ids, token_de_canal=None):
    """Sube a Tiendanube el stock actual de cada producto de `producto_ids`.

    Se le pasan los ids y no los objetos a proposito: esto corre DESPUES del
    commit de la venta, asi que el stock se relee de la base y es el que quedo
    guardado, no el que alguien calculo en memoria antes.

    Devuelve la lista de nombres de producto que no se pudieron actualizar,
    para que la ruta arme el aviso. Lista vacia = no hay nada que avisar, sea
    porque todo salio bien o porque no habia nada que empujar.

    `token_de_canal` existe para los tests: por defecto usa el mismo lector de
    credenciales que el backfill.
    """
    if not producto_ids:
        return []

    canal = canal_tiendanube(empresa_id)
    if canal is None:
        # Tienda no conectada: no hay push que intentar y no es un error.
        return []

    arranque = datetime.utcnow()

    # Que productos de la venta tienen realmente algo que empujar. Se resuelve
    # ANTES de pedir el token: si ninguna linea vino de Tiendanube, una
    # credencial rota no tiene por que generar un aviso.
    objetivos = []
    for producto_id in producto_ids:
        producto = db.session.get(Producto, producto_id)
        if producto is None or producto.stock is None:
            # stock NULL = producto sin control de stock. No se descuento nada
            # localmente, asi que tampoco hay nada que informarle a la tienda.
            continue
        mapeo = _mapeo_de(canal.id, producto_id)
        if mapeo is None:
            # El producto no vino de Tiendanube (carga a mano, otro canal
            # futuro). Esperado, no es un fallo.
            continue
        objetivos.append((producto, mapeo))

    if not objetivos:
        return []

    if token_de_canal is None:
        token_de_canal = _token_del_canal

    try:
        token = token_de_canal(canal)
    except Exception as exc:  # noqa: BLE001 - sin token no se empuja, pero la venta vive
        _log('sin credencial para empujar stock del canal %s: %r' % (canal.id, exc))
        fallidos = []
        for producto, _ in objetivos:
            _registrar_error(
                canal.id,
                '%s (SKU %s): no se pudo leer la credencial de Tiendanube: %s'
                % (producto.nombre, producto.sku, exc),
                arranque)
            fallidos.append(producto.nombre)
        return fallidos

    fallidos = []
    for producto, mapeo in objetivos:
        # Un fallo por linea no puede frenar a las demas: cada producto es una
        # llamada independiente y el resto del stock igual conviene corregirlo.
        try:
            if not mapeo.id_variante_externo:
                # El endpoint de escritura direcciona la VARIANTE, no el
                # producto. Sin ese id no hay a donde escribir. En la practica
                # no deberia pasar (Tiendanube manda siempre al menos una
                # variante), pero si pasa hay que avisarlo, no ignorarlo.
                raise ErrorTiendanube(
                    'El producto no tiene identificada su variante en Tiendanube.',
                    detalle='mapeo %s sin id_variante_externo' % mapeo.id)

            actualizar_stock_variante(
                canal.id_tienda_externo,
                mapeo.id_producto_externo,
                mapeo.id_variante_externo,
                producto.stock,
                token,
            )
        except ErrorTiendanube as exc:
            _log('stock de %s no empujado: %s (%s)' % (producto.sku, exc, exc.detalle))
            _registrar_error(canal.id, '%s (SKU %s): %s' % (
                producto.nombre, producto.sku, exc.detalle or exc), arranque)
            fallidos.append(producto.nombre)
        except Exception as exc:  # noqa: BLE001 - lo inesperado tampoco voltea la venta
            _log('stock de %s no empujado por un error inesperado: %r' % (producto.sku, exc))
            _registrar_error(canal.id, '%s (SKU %s): %r' % (
                producto.nombre, producto.sku, exc), arranque)
            fallidos.append(producto.nombre)

    return fallidos


def _token_del_canal(canal):
    """El access_token descifrado del canal.

    Import diferido y reutilizado del backfill: la lectura de la credencial es
    exactamente la misma operacion, y tener dos copias de esa logica seria
    tener dos lugares donde arreglar el dia que cambie el cifrado.
    """
    from sync_tiendanube import _token_de
    return _token_de(canal)
