# -*- coding: utf-8 -*-
"""Backfill del catalogo de Mercado Libre a la base (FASE-MELI-S2).

Esto es lo unico de la slice que escribe. El cliente HTTP vive en
`integracion_mercadolibre.py` y el mapeo en `ingestor_mercadolibre.py`; aca
solo se decide que se inserta, que se actualiza y que se cuenta.

DISPARO: uno solo, el boton "Sincronizar catálogo" de /integraciones. NO hay
cron: engancharlo al polling es S4, y hasta entonces la unica forma de que
corra es que alguien haga clic. La corrida va en un thread daemon con su propio
app_context, igual que la de Tiendanube: la respuesta HTTP vuelve enseguida y
el worker de gunicorn no se queda esperando a la API.

QUE TRAE Y QUE NO: solo productos. Los pedidos, el envio y la comision son S3.
Por eso `_reservar_corrida` abre UNA fila de sync_log (entidad='producto') y no
dos como la de Tiendanube -- abrir una fila de 'pedido' que nadie va a llenar
dejaria la corrida en 'corriendo' para siempre.

POR QUE UN MODULO PROPIO Y NO UN PARAMETRO EN sync_tiendanube

Las dos corridas comparten la MAQUINARIA -- reserva con lock, savepoint por
registro, upsert de producto+mapeo -- y esa parte se importa de
`sync_tiendanube` en vez de copiarse: `_upsert_producto_y_mapeo`,
`_corrida_viva` y `_cerrar` son literalmente las mismas funciones. Lo que NO
comparten es la FORMA de la corrida: Tiendanube trae productos y pedidos y
tiene token que no vence; Mercado Libre trae solo productos y necesita
refrescar el token antes de empezar. Meter las dos formas en un solo modulo
con banderas haria que cada cambio de un canal tuviera que razonarse contra el
otro.

EL TOKEN SE REFRESCA ANTES DE ARRANCAR. El access_token de Mercado Libre vence
cada seis horas, asi que a diferencia de Tiendanube no alcanza con descifrar lo
que hay guardado. Se pide por `credencial_mercadolibre.access_token_vigente`,
que refresca si hace falta adentro del lock de canal_venta -- el MISMO lock que
usa `_reservar_corrida`. Por eso el refresco va DESPUES de reservar y no antes:
si fuera al reves, `_reservar_corrida` se quedaria esperando un lock que el
refresco todavia tiene, y en el thread eso es un cuelgue silencioso. Reservar
commitea y suelta el lock; recien ahi el refresco lo vuelve a tomar.

IDEMPOTENCIA: apretar el boton dos veces no puede duplicar nada. Cada escritura
es un upsert contra la UNIQUE que ya trajo FASE2-S1:

    producto              UNIQUE (empresa_id, sku)
    mapeo_producto_canal  UNIQUE (canal_id, id_producto_externo, id_variante_externo)

Por eso esta slice NO agrega migraciones: las restricciones que hacen falta ya
existen y estan aplicadas en Supabase, y el modelo no se toca.
"""

import sys
import threading
from datetime import datetime

import credencial_mercadolibre as credencial_meli
from ingestor_canal import ENTIDAD_PRODUCTO, ErrorIngesta
from ingestor_mercadolibre import desde_canal
from models import CanalVenta, SyncLog, db
from sync_tiendanube import (
    LOTE_COMMIT,
    OPERACION,
    _cerrar,
    _corrida_viva,
    _ms,
    _upsert_producto_y_mapeo,
)

TIPO_MERCADOLIBRE = 'mercadolibre'


def _log(mensaje):
    """Detalle tecnico al log del servidor. El thread no tiene request donde
    mostrar nada, asi que esta es la unica traza que queda en vivo."""
    sys.stderr.write('[sync-meli] %s\n' % mensaje)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Estado de la corrida
# ---------------------------------------------------------------------------

def sync_en_curso(canal_id):
    """La corrida viva de este canal, o None. Para consultas sueltas (la UI).

    Comparte `_corrida_viva` con Tiendanube: la logica de "esta viva o quedo
    huerfana" es del sync_log, no del canal. Como filtra por canal_id, un canal
    de Mercado Libre nunca ve las filas de uno de Tiendanube.
    """
    viva = _corrida_viva(canal_id)
    db.session.commit()
    return viva


def ultimo_sync(canal_id):
    """Resumen de la ultima corrida, para la UI. None si nunca se sincronizo.

    Devuelve la MISMA forma que `sync_tiendanube.ultimo_sync`, con `pedidos` en
    cero, para que la plantilla de integraciones no tenga que preguntar de que
    canal se trata. El cero no miente: en esta slice el sync de Mercado Libre
    efectivamente no lee ningun pedido.
    """
    ultima = (SyncLog.query
              .filter_by(canal_id=canal_id, operacion=OPERACION)
              .order_by(SyncLog.fecha_inicio.desc(), SyncLog.id.desc())
              .first())
    if ultima is None:
        return None

    vacio = {'leidos': 0, 'nuevos': 0, 'actualizados': 0, 'error': 0}
    return {
        'estado': ultima.estado if ultima.estado in ('corriendo', 'error', 'parcial') else 'ok',
        'inicio': ultima.fecha_inicio,
        'fin': ultima.fecha_fin,
        'productos': {
            'leidos': ultima.registros_leidos,
            'nuevos': ultima.registros_nuevos,
            'actualizados': ultima.registros_actualizados,
            'error': ultima.registros_error,
        },
        'pedidos': dict(vacio),
        'mensaje_error': ultima.mensaje_error,
        # Nombre de quien la disparo a mano. Hoy siempre hay uno -- el unico
        # disparo es el boton --, pero queda None-safe para cuando S4 la
        # dispare desde el cron.
        'disparada_por': ultima.usuario.nombre if ultima.usuario_id else None,
    }


# ---------------------------------------------------------------------------
# Disparo
# ---------------------------------------------------------------------------

def _reservar_corrida(canal_id, usuario_id=None):
    """Toma el turno de sincronizacion del canal.

    Devuelve (arranque, None) si el turno quedo tomado, o (None, motivo) si ya
    hay una corrida en curso o el canal no esta conectado.

    Mismo lock de fila sobre canal_venta que usan `sync_tiendanube` y el
    refresco del token: el chequeo y la marca van en la MISMA transaccion, asi
    dos clics simultaneos no arrancan dos corridas. Es una funcion propia y no
    la de Tiendanube porque abre UNA fila de sync_log y no dos (ver el
    docstring del modulo).
    """
    canal = (CanalVenta.query
             .filter_by(id=canal_id)
             .with_for_update()
             .first())
    if canal is None or canal.tipo != TIPO_MERCADOLIBRE or not canal.activo:
        db.session.rollback()
        return None, 'El canal de Mercado Libre no está conectado.'

    if _corrida_viva(canal_id) is not None:
        # El commit no es cosmetico: cierra las huerfanas que `_corrida_viva`
        # pudo haber marcado y suelta el lock antes de irse.
        db.session.commit()
        return None, 'Ya hay una sincronización en curso. Esperá a que termine.'

    arranque = datetime.utcnow()
    db.session.add(SyncLog(
        canal_id=canal_id, entidad=ENTIDAD_PRODUCTO, operacion=OPERACION,
        estado='corriendo', fecha_inicio=arranque, usuario_id=usuario_id,
    ))
    db.session.commit()
    return arranque, None


def lanzar_sync_catalogo(app_obj, canal_id, usuario_id=None):
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
        name='sync-meli-%s' % canal_id, daemon=True,
    )
    hilo.start()
    return True, ('Sincronización del catálogo iniciada. Actualizá la página en '
                  'un rato para ver el resultado.')


def _correr_en_contexto(app_obj, canal_id, arranque):
    """Cuerpo del thread. Su propio app_context => su propia db.session."""
    with app_obj.app_context():
        try:
            correr_sync_catalogo(canal_id, arranque)
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


# ---------------------------------------------------------------------------
# La corrida
# ---------------------------------------------------------------------------

def correr_sync_catalogo(canal_id, arranque):
    """Trae el catalogo y lo persiste. Necesita app_context activo."""
    canal = db.session.get(CanalVenta, canal_id)
    if canal is None:
        raise ErrorIngesta('El canal %s ya no existe.' % canal_id)

    ingestor = desde_canal(canal, _token_de(canal))

    fila_log = (SyncLog.query
                .filter_by(canal_id=canal_id, operacion=OPERACION,
                           fecha_inicio=arranque, entidad=ENTIDAD_PRODUCTO)
                .first())

    productos = _sincronizar_productos(canal, ingestor, fila_log)

    canal.fecha_ultima_sync = datetime.utcnow()
    db.session.commit()

    _log('canal %s: %s productos' % (canal_id, productos))
    return {'productos': productos}


def _token_de(canal):
    """El access_token del canal, refrescado si estaba por vencer.

    A diferencia de Tiendanube no alcanza con descifrar lo guardado: el token
    de Mercado Libre vence cada seis horas y una corrida diaria lo encuentra
    vencido casi siempre. `access_token_vigente` refresca adentro del lock de
    canal_venta y guarda el par nuevo.
    """
    try:
        return credencial_meli.access_token_vigente(canal.id)
    except Exception as exc:  # noqa: BLE001 - se traduce al error del contrato
        error = ErrorIngesta(str(exc), canal=TIPO_MERCADOLIBRE,
                             entidad=ENTIDAD_PRODUCTO)
        # Se conserva para que la UI pueda distinguir "reconecta el canal" de
        # "proba de nuevo"; getattr porque no toda excepcion que pase por aca
        # es un ErrorMercadoLibre.
        error.reconectar = getattr(exc, 'reconectar', False)
        raise error from exc


# -- Productos --------------------------------------------------------------

def _sincronizar_productos(canal, ingestor, fila_log):
    """Catalogo -> producto + mapeo_producto_canal, a nivel SKU.

    Granularidad variacion: un item de Mercado Libre con tres talles son tres
    filas de producto, porque cada talle tiene su propio precio y su propio
    costo, y el margen se calcula por SKU vendido.

    AISLAMIENTO POR REGISTRO: cada SKU se escribe dentro de un SAVEPOINT
    propio. Si uno revienta (un dato raro, una UNIQUE inesperada), se revierte
    solo ese y la corrida sigue; sin el savepoint, un IntegrityError deja la
    sesion inutilizable y se pierde todo lo que ya se habia leido.
    """
    crudos = ingestor.traer_productos()

    leidos = nuevos = actualizados = errores = 0

    for crudo in crudos:
        for par in ingestor.variantes_de_producto(crudo):
            leidos += 1
            try:
                with db.session.begin_nested():
                    datos = ingestor.normalizar(ENTIDAD_PRODUCTO, par)
                    # El mismo upsert que usa Tiendanube, sin una linea de
                    # diferencia: costo_unitario no se toca nunca (lo carga
                    # Roman) y stock si se pisa, NULL incluido.
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
