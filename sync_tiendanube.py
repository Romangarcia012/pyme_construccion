# -*- coding: utf-8 -*-
"""Backfill de productos y pedidos de Tiendanube a la base (FASE3-S2).

Esto es lo unico de la slice que escribe. El cliente HTTP vive en
`integracion_tiendanube.py` y el mapeo en `ingestor_tiendanube.py`; aca solo
se decide que se inserta, que se actualiza y que se cuenta.

DISPARO: dos, con el mismo cuerpo.

  - Manual, desde POST /integraciones/tiendanube/sincronizar: corre en un
    thread daemon con su propio app_context y su propia sesion de SQLAlchemy,
    asi la respuesta HTTP vuelve enseguida y el worker de gunicorn no se queda
    esperando a la API.
  - Periodico, desde scripts/sync_periodico.py, que es lo que ejecuta el Cron
    Job de Render (FASE-SYNC-CRON-S1): ahi NO hay thread -- el proceso muere
    apenas el comando retorna, asi que la corrida es sincronica.

Los dos pasan por `_reservar_corrida` y por `correr_backfill`; lo unico que
cambia es quien espera el resultado -- y quien queda anotado: el manual estampa
en sync_log el usuario que hizo clic (FASE-AUDITORIA-S3), el periodico deja esa
columna en NULL porque no hay nadie a quien atribuirle la corrida. Sigue sin haber Celery, RQ, Redis ni
webhooks: el volumen (menos de 500 pedidos/mes) no los justifica.

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

TIPO_TIENDANUBE = 'tiendanube'

# El estado que devuelve `correr_sync_ahora` cuando la guarda de concurrencia
# le nego el turno. No es un error: es "otro lo esta haciendo".
SALTADO = 'saltado'


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


def _corrida_viva(canal_id):
    """La corrida viva de este canal, o None. NO commitea.

    Marca de paso como perdidas las que quedaron huerfanas: una fila en
    'corriendo' de hace horas no es una corrida, es un proceso que se murio.
    Deja la transaccion abierta a proposito -- el que llama decide cuando
    cerrarla, porque `_reservar_corrida` necesita chequear y marcar dentro de
    la misma transaccion que sostiene el lock.
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
    return vivas[0] if vivas else None


def sync_en_curso(canal_id):
    """La corrida viva de este canal, o None. Para consultas sueltas (la UI)."""
    viva = _corrida_viva(canal_id)
    db.session.commit()
    return viva


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

    # Las dos filas de una corrida se crean juntas con el mismo disparador, asi
    # que alcanza con mirar una. None = la disparo el cron.
    disparador = ultima.usuario.nombre if ultima.usuario_id else None

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
        # Nombre de quien la disparo a mano, o None si fue automatica.
        'disparada_por': disparador,
    }


# ---------------------------------------------------------------------------
# Disparo
# ---------------------------------------------------------------------------

def _reservar_corrida(canal_id, usuario_id=None):
    """Toma el turno de sincronizacion del canal (FASE-SYNC-CRON-S1).

    Devuelve (arranque, None) si el turno quedo tomado, o (None, motivo) si ya
    hay una corrida en curso o el canal no esta conectado.

    `usuario_id` es quien apreto el boton (FASE-AUDITORIA-S3). Es opcional y
    por defecto None porque las dos puertas automaticas -- el endpoint con
    token y el script periodico -- no tienen a nadie a quien atribuirle la
    corrida. Se estampa aca y no adentro del thread justamente porque aca
    todavia estamos en el request, con la sesion del que hizo clic.

    Desde que el cron corre solo, "ya hay uno" dejo de ser una lectura
    informativa y paso a ser la guarda: el boton manual y el cron (o dos
    corridas del cron, si una se pasa de los 20 minutos) pueden llegar al mismo
    tiempo sin que nadie lo vea. Por eso el chequeo y la marca van en la MISMA
    transaccion, detras de un SELECT ... FOR UPDATE sobre la fila de
    canal_venta: el segundo en llegar espera al primero y recien ahi lee, con
    las filas en 'corriendo' ya commiteadas. Sin el lock, los dos leerian
    "libre" al mismo tiempo y arrancarian los dos.

    Es un lock de fila, no una tabla nueva: canal_venta ya existe y ya es el
    dueno del canal. SQLite ignora el FOR UPDATE (no lo soporta y el dialecto
    de SQLAlchemy no lo emite), lo cual esta bien: local hay un solo proceso.
    """
    canal = (CanalVenta.query
             .filter_by(id=canal_id)
             .with_for_update()
             .first())
    if canal is None or not canal.activo:
        db.session.rollback()
        return None, 'El canal de Tiendanube no está conectado.'

    if _corrida_viva(canal_id) is not None:
        # El commit no es cosmetico: cierra las huerfanas que `_corrida_viva`
        # pudo haber marcado y suelta el lock antes de irse.
        db.session.commit()
        return None, 'Ya hay una sincronización en curso. Esperá a que termine.'

    arranque = datetime.utcnow()
    for entidad in (ENTIDAD_PRODUCTO, ENTIDAD_PEDIDO):
        db.session.add(SyncLog(
            canal_id=canal_id, entidad=entidad, operacion=OPERACION,
            estado='corriendo', fecha_inicio=arranque, usuario_id=usuario_id,
        ))
    db.session.commit()
    return arranque, None


def lanzar_backfill(app_obj, canal_id, usuario_id=None):
    """Deja la corrida marcada como 'corriendo' y arranca el thread.

    Devuelve (arrancó: bool, mensaje para el usuario).

    `usuario_id` viaja solo hasta `_reservar_corrida`, que corre ANTES del
    thread: el hilo no lo recibe ni lo necesita. Sin sesion ni request adentro
    del thread, cualquier intento de averiguar ahi quien fue seria imposible.
    """
    arranque, motivo = _reservar_corrida(canal_id, usuario_id=usuario_id)
    if arranque is None:
        return False, motivo

    hilo = threading.Thread(
        target=_correr_en_contexto, args=(app_obj, canal_id, arranque),
        name='sync-tiendanube-%s' % canal_id, daemon=True,
    )
    hilo.start()
    return True, 'Sincronización iniciada. Actualizá la página en un rato para ver el resultado.'


def canales_a_sincronizar():
    """Los ids de canal de Tiendanube conectados, de todas las empresas.

    El cron no tiene usuario logueado ni empresa "actual", asi que barre todo
    lo que este activo. Hoy es un solo canal; la lista evita que manana, con
    dos empresas, alguien tenga que acordarse de tocar el script.
    """
    return [canal.id for canal in CanalVenta.query
            .filter_by(tipo=TIPO_TIENDANUBE, activo=True)
            .order_by(CanalVenta.id)
            .all()]


def correr_sync_ahora(canal_id):
    """Corrida completa, sincronica, en el proceso que llama (FASE-SYNC-CRON-S1).

    Misma reserva y mismo cuerpo que el boton manual -- `_reservar_corrida` y
    `correr_backfill` son literalmente las mismas funciones. Lo unico que
    cambia es que no hay thread: el cron de Render mata el proceso apenas el
    comando retorna, asi que un thread daemon moriria a mitad de camino.

    Devuelve (estado, detalle), con estado 'ok' o SALTADO. Si el sync revienta,
    deja el fallo asentado en sync_log y RE-LANZA: el que llama decide el
    codigo de salida, y una corrida fallida tiene que verse en los logs.
    """
    arranque, motivo = _reservar_corrida(canal_id)
    if arranque is None:
        return SALTADO, motivo

    try:
        return 'ok', correr_backfill(canal_id, arranque)
    except Exception as exc:  # noqa: BLE001 - se asienta y se re-lanza
        # Sin esto el sync_log queda en 'corriendo' hasta que venza el TTL de
        # 30 minutos, y en el medio el boton manual aparece trabado.
        _cerrar_con_error(canal_id, arranque, exc)
        raise


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

    Dos reglas opuestas a proposito, segun de quien sea el dato:

    `costo_unitario` NO se toca nunca. Tiendanube no lo sabe y Roman lo carga a
    mano; pisarlo con NULL en cada sync borraria el unico dato de costo que
    existe y dejaria todos los margenes en cero.

    `stock` SI se pisa en cada corrida, igual que nombre y activo, y con NULL
    incluido: la fuente de verdad del stock es la tienda, no la base. Un
    producto al que le apagaron el control de stock tiene que volver a NULL
    ("no se lleva la cuenta"), no quedarse con el ultimo numero que se vio ni
    caer a 0 ("no queda ninguno"), que son tres cosas distintas.
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

    if datos.get('precio_lista') is not None:
        producto.precio_lista = datos['precio_lista']

    # Sin guardia de None, a diferencia del precio: aca el NULL tambien es un
    # valor que hay que escribir (ver el docstring).
    producto.stock = datos.get('stock')

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

    Lo que NO se reescribe es costo_unitario_snapshot: se rescata de las lineas
    viejas ANTES de borrarlas y se le devuelve a la linea nueva del mismo
    producto. Sin ese rescate el "snapshot" no congelaba nada -- cada corrida
    del sync volvia a leer producto.costo_unitario, asi que cambiar una lista
    de precios reescribia el margen de todos los pedidos ya vendidos, que es
    justo lo que el campo existe para impedir (models.py, PedidoItem).
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
    pedido.costo_envio_vendedor = datos['costo_envio_vendedor']
    pedido.total_impuestos = datos['total_impuestos']
    pedido.total = datos['total']
    pedido.raw_payload = crudo
    pedido.fecha_sync = datetime.utcnow()
    db.session.flush()

    # Antes del borrado, no despues: las filas se destruyen enteras y con ellas
    # el unico registro de cuanto costaba la mercaderia el dia de la venta.
    previos = _snapshots_previos(pedido)

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
            # El costo del dia de la venta si la linea ya lo tenia, el de hoy
            # si es la primera vez que se ve. Va a ser NULL mientras Roman no
            # cargue los costos a mano: Tiendanube no los tiene. Es el estado
            # real, no un dato faltante que haya que rellenar con cero.
            costo_unitario_snapshot=_snapshot_de(previos, producto_id),
        ))

    db.session.flush()
    return es_nuevo, sin_mapear


def _snapshots_previos(pedido):
    """producto_id -> los costos ya congelados de sus lineas, en orden de fila.

    Se lee ANTES de que _upsert_pedido borre las lineas. La clave es el
    producto y no la fila porque pedido_item no tiene identidad propia del lado
    del canal: el producto es lo unico de una linea que sobrevive a que
    Tiendanube le reescriba el nombre, la cantidad o el precio.

    Un producto que aparece en dos lineas del mismo pedido deja sus dos costos
    en una lista y se consumen en orden. No pretende emparejar cada costo con
    "su" linea -- para eso no hay dato -- pero es determinista y conserva el
    conjunto, que es lo que el reporte de margen suma.

    Las lineas sin producto_id no entran: su snapshot es NULL por definicion
    (_costo_actual devuelve None sin producto) y no hay nada que conservar.
    """
    previos = {}
    for item in pedido.items:
        if item.producto_id is None or item.costo_unitario_snapshot is None:
            continue
        previos.setdefault(item.producto_id, []).append(item.costo_unitario_snapshot)
    return previos


def _snapshot_de(previos, producto_id):
    """El costo que le toca a una linea que se esta reinsertando.

    Si la linea ya venia con costo congelado se conserva ESE, aunque
    producto.costo_unitario haya cambiado desde entonces: es el costo del dia
    de la venta y el margen de un pedido cerrado no se reescribe porque hoy la
    mercaderia salga mas cara.

    Solo toma el costo de hoy una linea nueva, o una que todavia no tenia
    ninguno. Ese segundo caso es a proposito y es como se completan los pedidos
    que se sincronizaron antes de que existiera la pantalla de costos: mientras
    el snapshot sea NULL no hay nada congelado que proteger.
    """
    pendientes = previos.get(producto_id)
    if pendientes:
        return pendientes.pop(0)
    return _costo_actual(producto_id)


def _costo_actual(producto_id):
    if producto_id is None:
        return None
    producto = db.session.get(Producto, producto_id)
    return producto.costo_unitario if producto else None
