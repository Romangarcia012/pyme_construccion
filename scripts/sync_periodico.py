# -*- coding: utf-8 -*-
"""Sincronizacion periodica con Tiendanube (FASE-SYNC-CRON-S1).

Esto es lo que ejecuta el Cron Job de Render. No implementa nada del sync: le
pide a `sync_tiendanube.correr_sync_ahora` exactamente la misma corrida que
dispara el boton de la UI, y se ocupa solo de lo que el boton no necesita --
levantar el app_context sin request, loguear lo que paso y elegir el codigo de
salida.

    python -m scripts.sync_periodico

POR QUE UN SCRIPT Y NO UN SCHEDULER ADENTRO DE LA APP: un APScheduler embebido
en gunicorn correria una vez por worker (dos workers = dos syncs) y se caeria
con cada reinicio del servicio web sin dejar rastro. Un Cron Job de Render es
un proceso aparte, con su propio log y su propio historial de corridas.

CODIGO DE SALIDA: 0 si todos los canales terminaron bien o se saltaron por
concurrencia; 1 si alguno fallo. Render marca la corrida como fallida con
cualquier codigo != 0, y esa es la unica alarma que hay: nadie mira estos logs
a menos que algo este roto.

SIN REQUEST: `correr_backfill` solo necesita un app_context (para la sesion de
SQLAlchemy). No lee `request` ni `current_user` -- si lo hiciera, esto seria
imposible de correr desde un cron.
"""

import os
import sys
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_tiendanube  # noqa: E402
from models import db  # noqa: E402


def _log(mensaje):
    """Una linea por evento a stderr, que es lo que junta el log de Render."""
    sys.stderr.write('[sync-periodico] %s %s\n'
                     % (datetime.utcnow().isoformat(timespec='seconds'), mensaje))
    sys.stderr.flush()


def _resumen(detalle):
    """'12 productos (2 nuevos), 5 pedidos (1 nuevo)' a partir del dict crudo.

    El numero que importa en el log no es el total leido sino cuantos se
    tocaron: una corrida que lee 300 productos y no cambia ninguno es normal,
    una que de golpe crea 300 no lo es.
    """
    partes = []
    for etiqueta, clave in (('productos', 'productos'), ('pedidos', 'pedidos')):
        datos = (detalle or {}).get(clave) or {}
        partes.append('%s %s (%s nuevos, %s actualizados, %s con error)' % (
            datos.get('leidos', 0), etiqueta, datos.get('nuevos', 0),
            datos.get('actualizados', 0), datos.get('error', 0)))
    return ', '.join(partes)


def sincronizar_canales():
    """Corre el sync de cada canal conectado. Devuelve cuantos fallaron.

    Un canal que revienta no frena a los demas: son empresas distintas y no
    hay razon para que el error de una le tape el sync a la otra. El detalle
    del fallo se loguea con traceback completo -- sin eso, una corrida roja en
    Render no dice nada sobre que se rompio.
    """
    canales = sync_tiendanube.canales_a_sincronizar()
    if not canales:
        _log('no hay canales de Tiendanube conectados: no hay nada que sincronizar')
        return 0

    fallidos = 0
    for canal_id in canales:
        _log('canal %s: arranca' % canal_id)
        try:
            estado, detalle = sync_tiendanube.correr_sync_ahora(canal_id)
        except Exception:  # noqa: BLE001 - se loguea y se sigue con el resto
            fallidos += 1
            _log('canal %s: FALLO' % canal_id)
            traceback.print_exc()
            sys.stderr.flush()
            db.session.rollback()
            continue

        if estado == sync_tiendanube.SALTADO:
            _log('canal %s: saltado por concurrencia (%s)' % (canal_id, detalle))
        else:
            _log('canal %s: termino -- %s' % (canal_id, _resumen(detalle)))

    return fallidos


def main():
    # La app se importa aca adentro y no arriba de todo para que el traceback
    # de un import roto (falta SECRET_KEY, falta la clave de cifrado) salga por
    # el mismo camino que cualquier otro error y no como un stacktrace pelado.
    _log('inicio')
    try:
        from app import app
    except Exception:
        _log('no se pudo levantar la app')
        traceback.print_exc()
        return 1

    with app.app_context():
        try:
            fallidos = sincronizar_canales()
        finally:
            db.session.remove()

    if fallidos:
        _log('fin CON ERRORES: %d canal(es) fallaron' % fallidos)
        return 1
    _log('fin ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
