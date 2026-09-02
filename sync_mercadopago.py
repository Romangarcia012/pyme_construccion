# -*- coding: utf-8 -*-
"""Backfill de movimientos de una cuenta de Mercado Pago (FASE-MP-S1).

Lo unico de la slice que escribe en movimiento_cuenta. El cliente HTTP vive en
`integracion_mercadopago.py` y el mapeo en `ingestor_mercadopago.py`; aca solo
se decide que se inserta, que se actualiza y que se cuenta.

DISPARO: manual, desde POST /integraciones/mercadopago/sincronizar/<id>. Corre
en un thread daemon con su propio app_context y su propia sesion, igual que
`sync_tiendanube.py`: la respuesta HTTP vuelve enseguida y el worker de
gunicorn no se queda esperando a la API. Sin Celery ni scheduler; el volumen no
los justifica y el automatismo no es de esta slice.

POR CUENTA, NO POR CANAL: cada corrida sincroniza UNA cuenta de cobro. Las dos
cuentas (la de Roman y la de Nachi) son de personas distintas, cada una con su
token, y una no puede leer nada de la otra. El aislamiento no depende de que el
codigo se acuerde de filtrar: cada corrida arranca de un cuenta_cobro_id y el
token que usa es el de esa fila, con lo cual la API misma solo devuelve los
pagos de esa cuenta.

IDEMPOTENCIA: sincronizar dos veces no puede duplicar plata. movimiento_cuenta
ya trae de FASE2-S1 la UNIQUE sobre hash_dedup y la UNIQUE
(cuenta_id, id_externo_procesador), y el hash se deriva solo del id del pago
(ver ingestor_mercadopago.hash_movimiento). Por eso esta slice no necesita
migrar nada para deduplicar: busca por hash y actualiza en vez de insertar.

TOKEN VENCIDO: no se refresca (no hay scheduler todavia). Se detecta en dos
lugares -- antes de salir, mirando credencial.expira_en, y durante, ante un 401
de la API -- y en los dos casos la corrida termina con un mensaje que dice
"reconecta esta cuenta", no con un stack trace.
"""

import sys
import threading
from datetime import datetime, timedelta

import cripto
import integracion_mercadopago as mp
import ingestor_mercadopago as ingestor

from ingestor_canal import ENTIDAD_MOVIMIENTO
from models import CredencialCuentaCobro, CuentaCobro, MovimientoCuenta, SyncLog, db

# Marca las filas de sync_log de esta slice, para distinguirlas de las que
# escriba un polling automatico mas adelante.
OPERACION = 'backfill'

TIPO_MERCADOPAGO = 'mercadopago'

# Un sync_log en 'corriendo' mas viejo que esto quedo huerfano: el proceso se
# murio (deploy de Render, reinicio del dyno) sin poder cerrarlo. Pasado el
# plazo se lo da por perdido, si no el boton queda trabado para siempre.
TTL_CORRIENDO = timedelta(minutes=30)

# Cada cuantos movimientos se hace COMMIT. Ni uno por registro (serian cientos
# de ida y vuelta a Supabase) ni todo al final (un corte perderia la corrida).
LOTE_COMMIT = 50

# Cuantos avisos de fee_details roto se guardan en el mensaje del sync_log.
# Con ver unos pocos alcanza para saber que pasa; el resto se cuenta.
MAX_AVISOS = 5


def _log(mensaje):
    """Detalle tecnico al log del servidor. El thread no tiene request donde
    mostrar nada, asi que esta es la unica traza que queda en vivo."""
    sys.stderr.write('[sync-mercadopago] %s\n' % mensaje)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Credenciales
# ---------------------------------------------------------------------------

def credencial_de(cuenta_cobro_id):
    """La credencial de esta cuenta, o None si nunca se conecto."""
    return CredencialCuentaCobro.query.filter_by(
        cuenta_cobro_id=cuenta_cobro_id).first()


def esta_conectada(cuenta_cobro_id):
    credencial = credencial_de(cuenta_cobro_id)
    return bool(credencial and credencial.access_token_cifrado)


def _token_de(cuenta):
    """El access_token descifrado de la cuenta, o ErrorMercadoPago explicando
    que hay que reconectarla.

    Las tres fallas posibles (nunca se conecto, se roto la clave de cifrado,
    el token vencio) terminan en el mismo lugar para el usuario -- volver a
    autorizar -- asi que las tres llevan reconectar=True y un mensaje que dice
    que hacer, no que fallo.
    """
    credencial = credencial_de(cuenta.id)
    if credencial is None or not credencial.access_token_cifrado:
        raise mp.ErrorMercadoPago(
            'La cuenta "%s" no esta conectada a Mercado Pago. Conectala para '
            'poder sincronizar.' % cuenta.nombre,
            detalle='sin credencial para cuenta_cobro %s' % cuenta.id,
            reconectar=True,
        )

    if ingestor.token_vencido(credencial.expira_en):
        raise mp.ErrorMercadoPago(
            'El acceso a la cuenta "%s" vencio. Reconecta esta cuenta para '
            'volver a sincronizar.' % cuenta.nombre,
            detalle='expira_en=%s ya paso para cuenta_cobro %s'
                    % (credencial.expira_en, cuenta.id),
            reconectar=True,
        )

    try:
        return cripto.descifrar(credencial.access_token_cifrado)
    except cripto.ErrorCifrado as exc:
        raise mp.ErrorMercadoPago(
            'La credencial guardada de la cuenta "%s" no se pudo descifrar. '
            'Reconecta esta cuenta.' % cuenta.nombre,
            detalle='fallo el descifrado de cuenta_cobro %s' % cuenta.id,
            reconectar=True,
        ) from exc


# ---------------------------------------------------------------------------
# Estado de la corrida
# ---------------------------------------------------------------------------

def _corriendo(cuenta_cobro_id):
    return (SyncLog.query
            .filter_by(cuenta_cobro_id=cuenta_cobro_id, operacion=OPERACION,
                       estado='corriendo')
            .order_by(SyncLog.fecha_inicio.desc())
            .all())


def sync_en_curso(cuenta_cobro_id):
    """La corrida viva de esta cuenta, o None.

    Cierra de paso las que quedaron huerfanas: una fila en 'corriendo' de hace
    horas no es una corrida, es un proceso que se murio.
    """
    vivas = []
    limite = datetime.utcnow() - TTL_CORRIENDO
    for fila in _corriendo(cuenta_cobro_id):
        if fila.fecha_inicio and fila.fecha_inicio < limite:
            fila.estado = 'error'
            fila.mensaje_error = ('La corrida quedo sin cerrar (el proceso se '
                                  'interrumpio). Se da por perdida.')
            fila.fecha_fin = datetime.utcnow()
        else:
            vivas.append(fila)
    db.session.commit()
    return vivas[0] if vivas else None


def ultimo_sync(cuenta_cobro_id):
    """Resumen de la ultima corrida de esta cuenta, para la UI.

    Una corrida es UNA fila de sync_log (solo hay una entidad: movimiento), a
    diferencia de Tiendanube que escribe una por productos y otra por pedidos.
    """
    fila = (SyncLog.query
            .filter_by(cuenta_cobro_id=cuenta_cobro_id, operacion=OPERACION)
            .order_by(SyncLog.fecha_inicio.desc(), SyncLog.id.desc())
            .first())
    if fila is None:
        return None

    return {
        'estado': fila.estado,
        'inicio': fila.fecha_inicio,
        'fin': fila.fecha_fin,
        'leidos': fila.registros_leidos,
        'nuevos': fila.registros_nuevos,
        'actualizados': fila.registros_actualizados,
        'error': fila.registros_error,
        'mensaje_error': fila.mensaje_error,
    }


def total_de(cuenta_cobro_id):
    """(suma de movimientos, fecha del mas reciente). El numero que Roman mira.

    Se calcula en la base con SUM/MAX y no trayendo las filas: no hay motivo
    para cargar miles de movimientos en memoria para sumar una columna.
    """
    from sqlalchemy import func

    fila = (db.session.query(func.sum(MovimientoCuenta.monto),
                             func.max(MovimientoCuenta.fecha))
            .filter(MovimientoCuenta.cuenta_id == cuenta_cobro_id)
            .one())
    return fila[0], fila[1]


# ---------------------------------------------------------------------------
# Disparo
# ---------------------------------------------------------------------------

def lanzar_backfill(app_obj, cuenta_cobro_id):
    """Deja la corrida marcada como 'corriendo' y arranca el thread.

    Devuelve (arrancó: bool, mensaje para el usuario). El chequeo de "ya hay
    uno" es una lectura del ultimo sync_log, no un lock de base: con un solo
    usuario apretando un boton alcanza, y dos corridas simultaneas tampoco
    duplicarian filas (todo es upsert contra hash_dedup), solo gastarian rate
    limit al pedo.
    """
    cuenta = db.session.get(CuentaCobro, cuenta_cobro_id)
    if cuenta is None or cuenta.tipo != TIPO_MERCADOPAGO:
        return False, 'Esa cuenta de cobro no existe.'

    if not esta_conectada(cuenta_cobro_id):
        return False, 'Primero conectá la cuenta "%s" con Mercado Pago.' % cuenta.nombre

    if sync_en_curso(cuenta_cobro_id) is not None:
        return False, 'Ya hay una sincronización en curso para esta cuenta. Esperá a que termine.'

    arranque = datetime.utcnow()
    db.session.add(SyncLog(
        cuenta_cobro_id=cuenta_cobro_id, entidad=ENTIDAD_MOVIMIENTO,
        operacion=OPERACION, estado='corriendo', fecha_inicio=arranque,
    ))
    db.session.commit()

    hilo = threading.Thread(
        target=_correr_en_contexto, args=(app_obj, cuenta_cobro_id, arranque),
        name='sync-mercadopago-%s' % cuenta_cobro_id, daemon=True,
    )
    hilo.start()
    return True, ('Sincronización de "%s" iniciada. Actualizá la página en un rato '
                  'para ver el resultado.' % cuenta.nombre)


def _correr_en_contexto(app_obj, cuenta_cobro_id, arranque):
    """Cuerpo del thread. Su propio app_context => su propia db.session."""
    with app_obj.app_context():
        try:
            correr_backfill(cuenta_cobro_id, arranque)
        except Exception as exc:  # noqa: BLE001 - el thread no puede propagar
            _log('la corrida de la cuenta %s se cayo: %r' % (cuenta_cobro_id, exc))
            _cerrar_con_error(cuenta_cobro_id, arranque, exc)
        finally:
            db.session.remove()


def _mensaje_de(exc):
    """Lo que se le muestra al usuario de una excepcion.

    ErrorMercadoPago ya trae un mensaje en castellano pensado para la pantalla
    (y el detalle tecnico aparte, que va al log). Cualquier otra cosa es un bug
    y se muestra str(exc), que al menos no es un traceback.
    """
    if isinstance(exc, mp.ErrorMercadoPago):
        return str(exc)
    return str(exc)


def _cerrar_con_error(cuenta_cobro_id, arranque, exc):
    """Deja constancia del fallo pase lo que pase.

    Si el sync se cae, lo peor seria que el sync_log quedara en 'corriendo'
    para siempre: el boton quedaria trabado y nadie sabria que fallo. Por eso
    esto tiene su propio try.
    """
    try:
        db.session.rollback()
        filas = (SyncLog.query
                 .filter_by(cuenta_cobro_id=cuenta_cobro_id, operacion=OPERACION,
                            fecha_inicio=arranque)
                 .all())
        for fila in filas:
            if fila.estado == 'corriendo':
                fila.estado = 'error'
                fila.mensaje_error = _mensaje_de(exc)[:2000]
                fila.fecha_fin = datetime.utcnow()
                if fila.duracion_ms is None and fila.fecha_inicio:
                    fila.duracion_ms = _ms(fila.fecha_inicio)
        db.session.commit()
    except Exception as fallo:  # noqa: BLE001
        db.session.rollback()
        _log('no se pudo ni registrar el error de la cuenta %s: %r'
             % (cuenta_cobro_id, fallo))


def _ms(desde):
    return int((datetime.utcnow() - desde).total_seconds() * 1000)


# ---------------------------------------------------------------------------
# La corrida
# ---------------------------------------------------------------------------

def correr_backfill(cuenta_cobro_id, arranque, desde=None, hasta=None):
    """Trae los pagos de la cuenta y los vuelca a movimiento_cuenta.

    Necesita app_context activo. Sin `desde` barre todo el historico
    disponible: la primera corrida tiene que reconstruir cuanta plata entro en
    total, no solo lo nuevo.
    """
    cuenta = db.session.get(CuentaCobro, cuenta_cobro_id)
    if cuenta is None:
        raise mp.ErrorMercadoPago('La cuenta de cobro %s ya no existe.' % cuenta_cobro_id)

    token = _token_de(cuenta)

    fila_log = (SyncLog.query
                .filter_by(cuenta_cobro_id=cuenta_cobro_id, operacion=OPERACION,
                           fecha_inicio=arranque)
                .first())

    # La lectura de la API va ENTERA antes de la primera escritura, igual que
    # en el callback del OAuth: si la API falla a mitad de camino no queda una
    # cuenta con la mitad de sus movimientos y una fecha_ultima_sync que
    # miente.
    crudos = mp.traer_pagos(token, desde=desde, hasta=hasta)

    resultado = _volcar(cuenta, crudos, fila_log)

    cuenta.fecha_ultima_sync = datetime.utcnow()
    db.session.commit()

    _log('cuenta %s: %s' % (cuenta_cobro_id, resultado))
    return resultado


def _volcar(cuenta, crudos, fila_log):
    """Los pagos crudos -> filas de movimiento_cuenta.

    Cada movimiento se escribe dentro de un SAVEPOINT propio: si uno revienta
    (un dato raro, una UNIQUE inesperada) se revierte solo ese y la corrida
    sigue. Sin el savepoint, un IntegrityError deja la sesion inutilizable y se
    pierde todo lo que ya se habia leido.
    """
    leidos = nuevos = actualizados = errores = salteados = 0
    avisos = []

    for crudo in crudos:
        leidos += 1
        try:
            with db.session.begin_nested():
                datos = ingestor.normalizar_movimiento(crudo)
                es_nuevo = _upsert_movimiento(cuenta, datos, crudo)
            if datos.get('aviso'):
                avisos.append(datos['aviso'])
            if es_nuevo:
                nuevos += 1
            else:
                actualizados += 1
        except ingestor.PagoIgnorado as motivo:
            # Un pago no aprobado no es un error: es plata que todavia no
            # entro. No cuenta como fallo de la corrida.
            salteados += 1
            _log('pago salteado en la cuenta %s: %s' % (cuenta.id, motivo))
        except Exception as exc:  # noqa: BLE001 - un pago raro no voltea el resto
            errores += 1
            _log('pago con error en la cuenta %s: %r' % (cuenta.id, exc))

        if leidos % LOTE_COMMIT == 0:
            db.session.commit()

    db.session.commit()
    _cerrar(fila_log, leidos, nuevos, actualizados, errores,
            mensaje=_resumen_de_avisos(avisos))
    db.session.commit()

    return {'leidos': leidos, 'nuevos': nuevos, 'actualizados': actualizados,
            'error': errores, 'salteados': salteados}


def _resumen_de_avisos(avisos):
    if not avisos:
        return None
    texto = '; '.join(avisos[:MAX_AVISOS])
    if len(avisos) > MAX_AVISOS:
        texto += ' (y %d aviso(s) mas)' % (len(avisos) - MAX_AVISOS)
    return texto


def _upsert_movimiento(cuenta, datos, crudo):
    """Un pago -> su fila en movimiento_cuenta. Devuelve si era nueva.

    La busqueda es por hash_dedup, que es la UNIQUE global y se deriva del id
    del pago: resincronizar la misma cuenta con los mismos pagos encuentra la
    fila que ya estaba y la actualiza en vez de duplicarla.

    Se actualiza y no se deja como esta porque un pago puede cambiar despues de
    acreditado (la comision se ajusta, la fecha de acreditacion se corrige), y
    la ultima palabra de la API es la buena.
    """
    movimiento = MovimientoCuenta.query.filter_by(
        hash_dedup=datos['hash_dedup']).first()

    es_nuevo = movimiento is None
    if es_nuevo:
        movimiento = MovimientoCuenta(
            cuenta_id=cuenta.id,
            hash_dedup=datos['hash_dedup'],
        )
        db.session.add(movimiento)

    movimiento.fecha = datos['fecha']
    movimiento.tipo = datos['tipo']
    movimiento.descripcion = datos['descripcion']
    movimiento.moneda = datos['moneda']
    movimiento.monto = datos['monto']
    movimiento.id_externo_procesador = datos['id_externo_procesador']
    movimiento.raw_payload = crudo
    # conciliado NO se toca al actualizar: si una slice futura ya concilio este
    # movimiento contra un pago, resincronizar no puede deshacerlo.

    db.session.flush()
    return es_nuevo


def _cerrar(fila, leidos, nuevos, actualizados, errores, mensaje=None):
    if fila is None:
        return
    fila.registros_leidos = leidos
    fila.registros_nuevos = nuevos
    fila.registros_actualizados = actualizados
    fila.registros_error = errores
    fila.estado = 'parcial' if (errores or mensaje) else 'ok'
    fila.mensaje_error = mensaje[:2000] if mensaje else None
    fila.fecha_fin = datetime.utcnow()
    fila.duracion_ms = _ms(fila.fecha_inicio) if fila.fecha_inicio else None
