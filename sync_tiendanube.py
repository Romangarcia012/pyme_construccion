# -*- coding: utf-8 -*-
"""Backfill de productos y pedidos de Tiendanube a la base (FASE3-S2).

Esto es lo unico de la slice que escribe. El cliente HTTP vive en
`integracion_tiendanube.py` y el mapeo en `ingestor_tiendanube.py`; aca solo
se decide que se inserta, que se actualiza y que se cuenta.

DISPARO: manual, desde POST /integraciones/tiendanube/sincronizar. Corre en un
thread daemon con su propio app_context y su propia sesion de SQLAlchemy: la
respuesta HTTP vuelve enseguida y el worker de gunicorn no se queda esperando
a la API. No hay Celery, ni RQ, ni Redis, ni scheduler: el volumen (menos de
500 pedidos/mes) no los justifica y el automatismo es FASE3-S4.

IDEMPOTENCIA: apretar el boton dos veces no puede duplicar nada. Cada escritura
es un upsert contra la UNIQUE que ya trajo FASE2-S1:

    producto              UNIQUE (empresa_id, sku)
    mapeo_producto_canal  UNIQUE (canal_id, id_producto_externo, id_variante_externo)
    pedido                UNIQUE (canal_id, id_externo)

Por eso esta slice NO agrega migraciones: las tres restricciones que hacen
falta ya existen y estan aplicadas en Supabase.

AISLAMIENTO POR REGISTRO: cada producto y cada pedido se escribe dentro de un
SAVEPOINT propio. Si uno revienta (un dato raro, una UNIQUE inesperada), se
revierte solo ese y la corrida sigue; sin el savepoint, un IntegrityError deja
la sesion inutilizable y se pierde todo lo que ya se habia leido.
"""

import sys
import threading
from datetime import datetime, timedelta

import cripto

from ingestor_canal import ENTIDAD_PEDIDO, ENTIDAD_PRODUCTO, ErrorIngesta
from ingestor_tiendanube import desde_canal
from models import (
    CanalVenta,
    CredencialCanal,
    MapeoProductoCanal,
    Pedido,
    PedidoItem,
    Producto,
    SyncLog,
    db,
)

# Todas las filas de sync_log de una corrida llevan esta operacion. Sirve para
# distinguirlas de las que escriba el polling automatico de FASE3-S4.
OPERACION = 'backfill'

# Un sync_log en 'corriendo' mas viejo que esto quedo huerfano: el proceso se
# murio (deploy de Render, reinicio del dyno) sin poder cerrarlo. Pasado el
# plazo se lo da por perdido, si no el boton queda trabado para siempre.
TTL_CORRIENDO = timedelta(minutes=30)

# Cada cuantos registros se hace COMMIT. Ni uno por registro (serian cientos de
# ida y vuelta a Supabase) ni todo junto al final (un corte a mitad de camino
# perderia la corrida entera).
LOTE_COMMIT = 50


def _log(mensaje):
    """Detalle tecnico al log del servidor. El thread no tiene request donde
    mostrar nada, asi que esta es la unica traza que queda en vivo."""
    sys.stderr.write('[sync-tiendanube] %s\n' % mensaje)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Estado de la corrida
# ---------------------------------------------------------------------------

def _corriendo(canal_id):
    """Las filas de sync_log de este canal que quedaron en 'corriendo'."""
    return (SyncLog.query
            .filter_by(canal_id=canal_id, operacion=OPERACION, estado='corriendo')
            .order_by(SyncLog.fecha_inicio.desc())
            .all())


def sync_en_curso(canal_id):
    """La corrida viva de este canal, o None.

    Cierra de paso las que quedaron huerfanas: una fila en 'corriendo' de hace
    horas no es una corrida, es un proceso que se murio.
    """
    vivas = []
    limite = datetime.utcnow() - TTL_CORRIENDO
    for fila in _corriendo(canal_id):
        if fila.fecha_inicio and fila.fecha_inicio < limite:
            fila.estado = 'error'
            fila.mensaje_error = ('La corrida quedo sin cerrar (el proceso se '
                                  'interrumpio). Se da por perdida.')
            fila.fecha_fin = datetime.utcnow()
        else:
            vivas.append(fila)
    db.session.commit()
    return vivas[0] if vivas else None


def ultimo_sync(canal_id):
    """Resumen de la ultima corrida, para la UI.

    Devuelve None si el canal nunca se sincronizo. Una corrida son dos filas
    de sync_log (una por entidad) que comparten fecha_inicio; ese timestamp
    hace de id de corrida.
    """
    ultima = (SyncLog.query
              .filter_by(canal_id=canal_id, operacion=OPERACION)
              .order_by(SyncLog.fecha_inicio.desc(), SyncLog.id.desc())
              .first())
    if ultima is None:
        return None

    filas = (SyncLog.query
             .filter_by(canal_id=canal_id, operacion=OPERACION,
                        fecha_inicio=ultima.fecha_inicio)
             .all())
    por_entidad = {fila.entidad: fila for fila in filas}

    estados = [fila.estado for fila in filas]
    if 'corriendo' in estados:
        estado = 'corriendo'
    elif 'error' in estados:
        estado = 'error'
    elif 'parcial' in estados:
        estado = 'parcial'
    else:
        estado = 'ok'

    fines = [fila.fecha_fin for fila in filas if fila.fecha_fin]
    mensajes = [fila.mensaje_error for fila in filas if fila.mensaje_error]

    def _resumen(entidad):
        fila = por_entidad.get(entidad)
        if fila is None:
            return {'leidos': 0, 'nuevos': 0, 'actualizados': 0, 'error': 0}
        return {
            'leidos': fila.registros_leidos,
            'nuevos': fila.registros_nuevos,
            'actualizados': fila.registros_actualizados,
            'error': fila.registros_error,
        }

    return {
        'estado': estado,
        'inicio': ultima.fecha_inicio,
        'fin': max(fines) if fines else None,
        'productos': _resumen(ENTIDAD_PRODUCTO),
        'pedidos': _resumen(ENTIDAD_PEDIDO),
        'mensaje_error': mensajes[0] if mensajes else None,
    }


# ---------------------------------------------------------------------------
# Disparo
# ---------------------------------------------------------------------------

def lanzar_backfill(app_obj, canal_id):
    """Deja la corrida marcada como 'corriendo' y arranca el thread.

    Devuelve (arrancó: bool, mensaje para el usuario). El chequeo de "ya hay
    uno" es una lectura del ultimo sync_log, no un lock de base: con un solo
    usuario apretando un boton alcanza, y dos corridas simultaneas tampoco
    duplicarian filas (todo es upsert), solo gastarian rate limit al pedo.
    """
    canal = db.session.get(CanalVenta, canal_id)
    if canal is None or not canal.activo:
        return False, 'El canal de Tiendanube no está conectado.'

    if sync_en_curso(canal_id) is not None:
        return False, 'Ya hay una sincronización en curso. Esperá a que termine.'

    arranque = datetime.utcnow()
    for entidad in (ENTIDAD_PRODUCTO, ENTIDAD_PEDIDO):
        db.session.add(SyncLog(
            canal_id=canal_id, entidad=entidad, operacion=OPERACION,
            estado='corriendo', fecha_inicio=arranque,
        ))
    db.session.commit()

    hilo = threading.Thread(
        target=_correr_en_contexto, args=(app_obj, canal_id, arranque),
        name='sync-tiendanube-%s' % canal_id, daemon=True,
    )
    hilo.start()
    return True, 'Sincronización iniciada. Actualizá la página en un rato para ver el resultado.'


def _correr_en_contexto(app_obj, canal_id, arranque):
    """Cuerpo del thread. Su propio app_context => su propia db.session."""
    with app_obj.app_context():
        try:
            correr_backfill(canal_id, arranque)
        except Exception as exc:  # noqa: BLE001 - el thread no puede propagar
            _log('la corrida del canal %s se cayo: %r' % (canal_id, exc))
            _cerrar_con_error(canal_id, arranque, exc)
        finally:
            db.session.remove()


def _cerrar_con_error(canal_id, arranque, exc):
    """Deja constancia del fallo pase lo que pase.

    Si el sync se cae, lo peor seria que el sync_log quedara en 'corriendo'
    para siempre: el boton quedaria trabado y nadie sabria que fallo. Por eso
    esto tiene su propio try: ni siquiera un rollback fallido puede tapar el
    registro del error.
    """
    try:
        db.session.rollback()
        filas = (SyncLog.query
                 .filter_by(canal_id=canal_id, operacion=OPERACION, fecha_inicio=arranque)
                 .all())
        for fila in filas:
            if fila.estado == 'corriendo':
                fila.estado = 'error'
                fila.mensaje_error = str(exc)[:2000]
                fila.fecha_fin = datetime.utcnow()
                if fila.duracion_ms is None and fila.fecha_inicio:
                    fila.duracion_ms = _ms(fila.fecha_inicio)
        db.session.commit()
    except Exception as fallo:  # noqa: BLE001
        db.session.rollback()
        _log('no se pudo ni registrar el error del canal %s: %r' % (canal_id, fallo))


def _ms(desde):
    return int((datetime.utcnow() - desde).total_seconds() * 1000)


# ---------------------------------------------------------------------------
# La corrida
# ---------------------------------------------------------------------------

def correr_backfill(canal_id, arranque):
    """Trae el catalogo y despues los pedidos. Necesita app_context activo.

    El orden importa: los pedidos resuelven producto_id contra
    mapeo_producto_canal, asi que el catalogo tiene que estar cargado antes o
    las lineas quedan todas sin mapear.
    """
    canal = db.session.get(CanalVenta, canal_id)
    if canal is None:
        raise ErrorIngesta('El canal %s ya no existe.' % canal_id)

    ingestor = desde_canal(canal, _token_de(canal))

    filas = {
        fila.entidad: fila
        for fila in SyncLog.query.filter_by(
            canal_id=canal_id, operacion=OPERACION, fecha_inicio=arranque).all()
    }

    productos = _sincronizar_productos(canal, ingestor, filas.get(ENTIDAD_PRODUCTO))
    pedidos = _sincronizar_pedidos(canal, ingestor, filas.get(ENTIDAD_PEDIDO))

    canal.fecha_ultima_sync = datetime.utcnow()
    db.session.commit()

    _log('canal %s: %s productos, %s pedidos' % (canal_id, productos, pedidos))
    return {'productos': productos, 'pedidos': pedidos}


def _token_de(canal):
    """El access_token descifrado del canal."""
    credencial = (CredencialCanal.query
                  .filter_by(canal_id=canal.id, activo=True)
                  .order_by(CredencialCanal.id.desc())
                  .first())
    if credencial is None or not credencial.access_token_cifrado:
        raise ErrorIngesta(
            'El canal de Tiendanube no tiene credencial guardada. Volve a conectarlo.',
            canal='tiendanube'
        )
    try:
        return cripto.descifrar(credencial.access_token_cifrado)
    except cripto.ErrorCifrado as exc:
        raise ErrorIngesta(
            'La credencial de Tiendanube no se pudo descifrar. Volve a conectar la tienda.',
            canal='tiendanube'
        ) from exc


def _cerrar(fila, leidos, nuevos, actualizados, errores, mensaje=None):
    if fila is None:
        return
    fila.registros_leidos = leidos
    fila.registros_nuevos = nuevos
    fila.registros_actualizados = actualizados
    fila.registros_error = errores
    fila.estado = 'parcial' if errores else 'ok'
    fila.mensaje_error = mensaje[:2000] if mensaje else None
    fila.fecha_fin = datetime.utcnow()
    fila.duracion_ms = _ms(fila.fecha_inicio) if fila.fecha_inicio else None


# -- Productos --------------------------------------------------------------

def _sincronizar_productos(canal, ingestor, fila_log):
    """Catalogo -> producto + mapeo_producto_canal, a nivel SKU.

    Granularidad variante: un producto de Tiendanube con tres talles son tres
    filas de producto, porque cada talle tiene su propio precio y su propio
    costo, y el margen se calcula por SKU vendido.
    """
    crudos = ingestor.traer_productos()

    leidos = nuevos = actualizados = errores = 0

    for crudo in crudos:
        for par in ingestor.variantes_de_producto(crudo):
            leidos += 1
            try:
                with db.session.begin_nested():
                    datos = ingestor.normalizar(ENTIDAD_PRODUCTO, par)
                    _, es_nuevo = _upsert_producto_y_mapeo(canal, datos)
                if es_nuevo:
                    nuevos += 1
                else:
                    actualizados += 1
            except Exception as exc:  # noqa: BLE001 - un SKU raro no voltea el resto
                errores += 1
                _log('producto salteado en el canal %s: %r' % (canal.id, exc))

            if leidos % LOTE_COMMIT == 0:
                db.session.commit()

    db.session.commit()
    _cerrar(fila_log, leidos, nuevos, actualizados, errores)
    db.session.commit()
    return {'leidos': leidos, 'nuevos': nuevos, 'actualizados': actualizados,
            'error': errores}


def _upsert_producto_y_mapeo(canal, datos):
    """Un SKU del canal -> una fila de producto y una de mapeo.

    Lo que NO se toca al actualizar: `costo_unitario`. Tiendanube no lo sabe y
    Roman lo carga a mano; pisarlo con NULL en cada sync borraria el unico dato
    de costo que existe y dejaria todos los margenes en cero.
    """
    producto = Producto.query.filter_by(
        empresa_id=canal.empresa_id, sku=datos['sku']).first()

    es_nuevo = producto is None
    if es_nuevo:
        producto = Producto(
            empresa_id=canal.empresa_id,
            sku=datos['sku'],
            nombre=datos['nombre'],
            activo=datos['activo'],
            # costo_unitario queda NULL a proposito: lo carga Roman.
        )
        db.session.add(producto)
    else:
        producto.nombre = datos['nombre']
        producto.activo = datos['activo']

    # Precio y stock si son datos del canal: se refrescan siempre.
    if datos.get('precio_lista') is not None:
        producto.precio_lista = datos['precio_lista']
    if datos.get('stock') is not None:
        producto.stock = datos['stock']

    db.session.flush()

    mapeo = MapeoProductoCanal.query.filter_by(
        canal_id=canal.id,
        id_producto_externo=datos['id_producto_externo'],
        id_variante_externo=datos['id_variante_externo'],
    ).first()

    if mapeo is None:
        mapeo = MapeoProductoCanal(
            canal_id=canal.id,
            id_producto_externo=datos['id_producto_externo'],
            id_variante_externo=datos['id_variante_externo'],
        )
        db.session.add(mapeo)

    # Se reapunta aunque exista: si el comerciante le cambio el SKU a la
    # variante, el mapeo tiene que seguir al producto nuevo.
    mapeo.producto_id = producto.id
    mapeo.sku_externo = datos.get('sku_externo')
    db.session.flush()

    return producto, es_nuevo


# -- Pedidos ----------------------------------------------------------------

def _sincronizar_pedidos(canal, ingestor, fila_log):
    crudos = ingestor.traer_pedidos()
    mapa = _mapa_de_productos(canal.id)

    leidos = nuevos = actualizados = errores = 0
    items_sin_mapear = 0

    for crudo in crudos:
        leidos += 1
        try:
            with db.session.begin_nested():
                datos = ingestor.normalizar(ENTIDAD_PEDIDO, crudo)
                es_nuevo, huerfanos = _upsert_pedido(canal, datos, crudo, mapa)
            items_sin_mapear += huerfanos
            if es_nuevo:
                nuevos += 1
            else:
                actualizados += 1
        except Exception as exc:  # noqa: BLE001 - un pedido raro no voltea el resto
            errores += 1
            _log('pedido salteado en el canal %s: %r' % (canal.id, exc))

        if leidos % LOTE_COMMIT == 0:
            db.session.commit()

    db.session.commit()

    # Los items sin mapear NO son un error de la corrida: son un pedido de un
    # producto que ya no esta en el catalogo (borrado, despublicado). El pedido
    # se guarda igual y el item queda con producto_id NULL; lo unico que se
    # pierde es el costo, que para ese producto tampoco existia.
    aviso = None
    if items_sin_mapear:
        aviso = ('%d linea(s) de pedido quedaron sin producto asociado: el '
                 'producto ya no esta en el catalogo del canal.' % items_sin_mapear)

    _cerrar(fila_log, leidos, nuevos, actualizados, errores, mensaje=aviso)
    db.session.commit()
    return {'leidos': leidos, 'nuevos': nuevos, 'actualizados': actualizados,
            'error': errores, 'items_sin_mapear': items_sin_mapear}


def _mapa_de_productos(canal_id):
    """(id_producto_externo, id_variante_externo) -> producto_id.

    Se arma una sola vez por corrida en vez de un SELECT por linea de pedido:
    el catalogo entra holgado en memoria y son cientos de lookups.
    """
    mapa = {}
    for mapeo in MapeoProductoCanal.query.filter_by(canal_id=canal_id).all():
        mapa[(mapeo.id_producto_externo, mapeo.id_variante_externo or '')] = mapeo.producto_id
    return mapa


def _resolver_producto(mapa, datos_item):
    """producto_id de una linea, o None si no hay mapeo.

    Segundo intento contra la clave a nivel producto ('' de variante): cubre a
    los productos sin variantes, donde el pedido igual manda el variant_id de
    la unica variante. Como esa clave solo existe cuando el producto no tiene
    variantes, no hay riesgo de mapear al SKU equivocado.
    """
    id_producto = datos_item.get('id_producto_externo')
    if not id_producto:
        return None
    exacto = mapa.get((id_producto, datos_item.get('id_variante_externo') or ''))
    if exacto is not None:
        return exacto
    return mapa.get((id_producto, ''))


def _upsert_pedido(canal, datos, crudo, mapa):
    """Un pedido -> su fila en pedido y sus lineas en pedido_item.

    Las lineas se reemplazan enteras en cada corrida en vez de actualizarse una
    por una: pedido_item no tiene UNIQUE (una linea no tiene identidad propia
    del lado del canal) y un pedido editado puede tener lineas nuevas, menos
    lineas o cantidades distintas. Borrar y reescribir es lo unico que no
    duplica ni deja lineas fantasma.
    """
    if not datos.get('id_externo'):
        raise ErrorIngesta('El pedido vino sin id de Tiendanube.', canal='tiendanube',
                           entidad=ENTIDAD_PEDIDO)

    pedido = Pedido.query.filter_by(
        canal_id=canal.id, id_externo=datos['id_externo']).first()

    es_nuevo = pedido is None
    if es_nuevo:
        pedido = Pedido(empresa_id=canal.empresa_id, canal_id=canal.id,
                        id_externo=datos['id_externo'])
        db.session.add(pedido)

    pedido.numero_externo = datos['numero_externo']
    pedido.fecha_pedido = datos['fecha_pedido']
    pedido.estado = datos['estado']
    pedido.estado_externo = datos['estado_externo']
    pedido.moneda = datos['moneda']
    pedido.comprador_nombre = datos['comprador_nombre']
    pedido.comprador_email = datos['comprador_email']
    pedido.comprador_doc = datos['comprador_doc']
    pedido.total_bruto = datos['total_bruto']
    pedido.total_descuentos = datos['total_descuentos']
    pedido.total_envio = datos['total_envio']
    pedido.total_impuestos = datos['total_impuestos']
    pedido.total = datos['total']
    pedido.raw_payload = crudo
    pedido.fecha_sync = datetime.utcnow()
    db.session.flush()

    for viejo in list(pedido.items):
        db.session.delete(viejo)
    db.session.flush()

    sin_mapear = 0
    for datos_item in datos['items']:
        producto_id = _resolver_producto(mapa, datos_item)
        if producto_id is None:
            sin_mapear += 1

        db.session.add(PedidoItem(
            pedido_id=pedido.id,
            producto_id=producto_id,
            sku_externo=datos_item['sku_externo'],
            descripcion=datos_item['descripcion'],
            cantidad=datos_item['cantidad'],
            precio_unitario=datos_item['precio_unitario'],
            descuento_unitario=datos_item['descuento_unitario'],
            subtotal=datos_item['subtotal'],
            # Congela el costo de HOY. Va a ser NULL hasta que Roman cargue los
            # costos a mano: Tiendanube no los tiene. Es el estado real, no un
            # dato faltante que haya que rellenar con cero.
            costo_unitario_snapshot=_costo_actual(producto_id),
        ))

    db.session.flush()
    return es_nuevo, sin_mapear


def _costo_actual(producto_id):
    if producto_id is None:
        return None
    producto = db.session.get(Producto, producto_id)
    return producto.costo_unitario if producto else None
