"""Rutas de integraciones: canales de venta y cuentas de cobro.

FASE3-S1: conectar la tienda de Tiendanube y guardar el token cifrado (OAuth).
FASE3-S2: disparar el backfill de productos y pedidos y mostrar como salio.
FASE-MP-S1: conectar las dos cuentas de Mercado Pago y traer sus movimientos.
FASE-MELI-S1: conectar el canal de Mercado Libre (OAuth con state, token que
               vence a las 6 horas y refresh_token de un solo uso).
FASE-SYNC-CRON-S2: disparar ese mismo backfill desde un cron externo, con
               un token en header en vez de sesion.

Los canales y las cuentas comparten pagina porque son la misma pregunta vista
de los dos lados -- por donde entro la venta y adonde entro la plata -- pero
son dos tablas distintas y dos flujos de OAuth distintos.

El blueprint se registra en app.py; ninguna ruta existente se toca.
"""

import os
import secrets
import sys
from datetime import datetime

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required

import credencial_mercadolibre as credencial_meli
import cripto
import ingestor_mercadopago as ingestor_mp
import integracion_mercadolibre as meli
import integracion_mercadopago as mp
import integracion_tiendanube as tn
import sync_mercadopago
import sync_tiendanube
from models import (
    CanalVenta,
    CredencialCanal,
    CredencialCuentaCobro,
    CuentaCobro,
    db,
)

integraciones_bp = Blueprint('integraciones', __name__, url_prefix='/integraciones')

TIPO_TIENDANUBE = 'tiendanube'
TIPO_MERCADOLIBRE = 'mercadolibre'

# Canales que la UI muestra siempre, esten o no en la base. El endpoint es
# None mientras el canal no tenga flujo de conexion implementado.
CANALES_CONOCIDOS = [
    {'tipo': TIPO_TIENDANUBE, 'nombre': 'Tiendanube',
     'endpoint_conectar': 'integraciones.conectar_tiendanube',
     'endpoint_sincronizar': 'integraciones.sincronizar_tiendanube'},
    # FASE-MELI-S1: ya tiene flujo de conexion. `endpoint_sincronizar` sigue en
    # None a proposito: traer catalogo y pedidos es S2/S3, y hasta entonces un
    # boton "Sincronizar" seria una promesa que no cumple nadie.
    {'tipo': TIPO_MERCADOLIBRE, 'nombre': 'Mercado Libre',
     'endpoint_conectar': 'integraciones.conectar_mercadolibre',
     'endpoint_sincronizar': None},
]


def _log(mensaje):
    """Detalle tecnico al log del servidor. Nunca al navegador: los detalles
    de error de OAuth pueden traer eco de credenciales."""
    sys.stderr.write(f'[integraciones] {mensaje}\n')
    sys.stderr.flush()


def _canal(tipo, crear=False):
    """El canal_venta de la empresa del usuario actual.

    La migracion de FASE2-S1 sembro las filas para las empresas que existian
    en ese momento; una empresa creada despues no las tiene, asi que el
    callback las crea al vuelo en vez de fallar.
    """
    canal = CanalVenta.query.filter_by(
        empresa_id=current_user.empresa_id, tipo=tipo
    ).first()
    if canal is None and crear:
        nombre = next((c['nombre'] for c in CANALES_CONOCIDOS if c['tipo'] == tipo), tipo)
        canal = CanalVenta(
            empresa_id=current_user.empresa_id, tipo=tipo, nombre=nombre, activo=False
        )
        db.session.add(canal)
        db.session.flush()
    return canal


@integraciones_bp.route('')
@login_required
def listar():
    """Estado de cada canal de venta y de cada cuenta de cobro."""
    por_tipo = {
        c.tipo: c
        for c in CanalVenta.query.filter_by(empresa_id=current_user.empresa_id).all()
    }

    canales = []
    for conocido in CANALES_CONOCIDOS:
        fila = por_tipo.get(conocido['tipo'])

        # El estado del sync solo aplica al canal ya conectado. Consultarlo
        # tambien cierra las corridas que quedaron huerfanas por un reinicio,
        # asi que abrir la pagina destraba el boton sin intervencion manual.
        sync = None
        corriendo = False
        if fila is not None and fila.activo and conocido['endpoint_sincronizar']:
            corriendo = sync_tiendanube.sync_en_curso(fila.id) is not None
            sync = sync_tiendanube.ultimo_sync(fila.id)

        # FASE-MELI-S1: para Mercado Libre "activo" no alcanza como estado. El
        # access_token vence cada seis horas, asi que un canal marcado activo
        # puede estar sin acceso ahora mismo; la unica forma de saberlo es
        # preguntar. `verificar_conexion` no levanta ni refresca: pintar una
        # pagina no puede quemar el refresh_token de un solo uso.
        conexion = None
        if conocido['tipo'] == TIPO_MERCADOLIBRE and fila is not None and fila.activo:
            conexion = credencial_meli.verificar_conexion(fila.id)

        canales.append({
            'tipo': conocido['tipo'],
            # Si esta conectado, el nombre guardado es el de la tienda real.
            'nombre': fila.nombre if fila else conocido['nombre'],
            'activo': bool(fila and fila.activo),
            'conexion': conexion,
            'cuenta_externa': fila.id_tienda_externo if fila else None,
            'ultima_sync': fila.fecha_ultima_sync if fila else None,
            'endpoint_conectar': conocido['endpoint_conectar'],
            'endpoint_sincronizar': conocido['endpoint_sincronizar'],
            'sync': sync,
            'sync_corriendo': corriendo,
        })

    return render_template('integraciones.html', canales=canales,
                           cuentas=_cuentas_para_la_vista())


@integraciones_bp.route('/tiendanube/conectar')
@login_required
def conectar_tiendanube():
    """Manda al usuario a aprobar la instalacion en Tiendanube.

    No escribe nada: hasta que Tiendanube no redirija al callback con un code
    valido, para esta app no paso nada.
    """
    return redirect(tn.url_autorizacion())


@integraciones_bp.route('/tiendanube/callback')
@login_required
def callback_tiendanube():
    """Vuelta de Tiendanube con ?code=.

    Todo lo que puede fallar (intercambio del code, prueba del token) pasa
    ANTES de tocar la base. Recien con las dos llamadas OK se escribe, y en un
    solo commit: no existe el estado "canal activo sin token" ni al reves.
    """
    if request.args.get('error'):
        _log(f"Tiendanube devolvio error en el callback: {request.args.get('error')}")
        flash('No se completo la conexion con Tiendanube: se cancelo la autorizacion.', 'danger')
        return redirect(url_for('integraciones.listar'))

    code = request.args.get('code')
    if not code:
        flash('Tiendanube no devolvio el codigo de autorizacion. Proba conectar de nuevo.', 'danger')
        return redirect(url_for('integraciones.listar'))

    try:
        token = tn.intercambiar_code(code)
        store = tn.traer_tienda(token['user_id'], token['access_token'])
    except tn.ErrorTiendanube as exc:
        _log(f'fallo la conexion con Tiendanube: {exc.detalle or exc}')
        flash(str(exc), 'danger')
        return redirect(url_for('integraciones.listar'))

    nombre_tienda = tn.nombre_de_tienda(store)

    try:
        canal = _canal(TIPO_TIENDANUBE, crear=True)
        canal.nombre = nombre_tienda
        canal.id_tienda_externo = token['user_id']
        canal.activo = True

        # Reconectar reutiliza la fila: un canal tiene una credencial vigente,
        # no una pila historica de tokens viejos.
        credencial = CredencialCanal.query.filter_by(canal_id=canal.id).first()
        if credencial is None:
            credencial = CredencialCanal(canal_id=canal.id, tipo_credencial='oauth2')
            db.session.add(credencial)

        credencial.access_token_cifrado = cripto.cifrar(token['access_token'])
        credencial.scope = (token.get('scope') or None)
        credencial.activo = True
        credencial.fecha_actualizacion = datetime.utcnow()
        # El token de Tiendanube no expira ni se refresca: no hay refresh_token
        # ni expira_en que guardar.

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        _log(f'fallo guardando la credencial de Tiendanube: {exc!r}')
        flash('Se autorizo la tienda pero no se pudo guardar la credencial. Proba de nuevo.', 'danger')
        return redirect(url_for('integraciones.listar'))

    flash(f'Tiendanube conectada: {nombre_tienda}.', 'success')
    return redirect(url_for('integraciones.listar'))


@integraciones_bp.route('/tiendanube/sincronizar', methods=['POST'])
@login_required
def sincronizar_tiendanube():
    """Dispara el backfill de productos y pedidos (FASE3-S2).

    Responde de inmediato: el trabajo real corre en un thread daemon con su
    propio app_context (ver sync_tiendanube.py). Nunca se le pega a la API de
    Tiendanube dentro del ciclo request/response -- un backfill de varias
    paginas dejaria al worker de gunicorn colgado y al navegador esperando.

    Es POST y no GET a proposito: dispara trabajo, asi que no puede caer por
    un prefetch del navegador ni quedar en el historial como link.

    Sin restriccion de rol: cualquier usuario logueado de la empresa puede
    sincronizar. La operacion no destruye nada (es un upsert idempotente) y el
    canal ya lo conecto un admin; pedir rol admin aca solo agregaria una
    puerta que no protege nada nuevo.
    """
    canal = _canal(TIPO_TIENDANUBE)
    if canal is None or not canal.activo:
        flash('Primero conectá la tienda de Tiendanube.', 'danger')
        return redirect(url_for('integraciones.listar'))

    # El thread sobrevive al request, asi que no puede quedarse con el proxy de
    # current_app: necesita el objeto Flask real para abrir su propio contexto.
    app_obj = current_app._get_current_object()

    try:
        # Quien apreto el boton queda anotado en sync_log (FASE-AUDITORIA-S3).
        # Esta es la unica de las tres puertas del sync que tiene una persona
        # identificada; las dos automaticas no pasan nada y dejan NULL.
        arranco, mensaje = sync_tiendanube.lanzar_backfill(
            app_obj, canal.id, usuario_id=current_user.id)
    except Exception as exc:
        db.session.rollback()
        _log(f'no se pudo lanzar el backfill de Tiendanube: {exc!r}')
        flash('No se pudo iniciar la sincronización. Probá de nuevo.', 'danger')
        return redirect(url_for('integraciones.listar'))

    flash(mensaje, 'success' if arranco else 'danger')
    return redirect(url_for('integraciones.listar'))

# ============================================================================
# FASE-SYNC-CRON-S2 - Disparo periodico desde un cron externo
# ----------------------------------------------------------------------------
# Un Cron Job de Render tiene costo; cron-job.org es gratis pero vive afuera y
# no tiene cookies de Flask-Login. De ahi este endpoint: la misma corrida que
# el boton manual, pero autenticada por un token en un header en vez de por la
# sesion del navegador.
#
# El token va en un HEADER a proposito. En la query string quedaria escrito en
# el access log de Render, en el historial del proxy y en la pantalla de
# configuracion de cron-job.org como parte de la URL; un header no aparece en
# ninguno de esos lados.
# ============================================================================

HEADER_SYNC = 'X-Sync-Token'

# Variable de entorno propia, NO SECRET_KEY. Si se filtrara este token lo peor
# que se puede hacer con el es disparar un sync de mas; con SECRET_KEY se
# firman las sesiones, y compartirlo convertiria una fuga menor en una toma de
# cuentas.
VAR_TOKEN_SYNC = 'SYNC_CRON_TOKEN'


def _token_sync_valido(recibido):
    """True solo si el header trae exactamente el token configurado.

    Se lee del entorno en cada request y no al importar el modulo: asi rotar
    el token en Render es reiniciar el servicio, no tocar codigo.

    Si la variable no esta configurada NADIE pasa. Es la unica postura segura:
    el error tipico es desplegar el codigo antes que la variable, y con un
    "si no hay token no pido token" el endpoint quedaria abierto justo en esa
    ventana.

    La comparacion es `compare_digest` y no `==` para que el tiempo de
    respuesta no delate cuantos caracteres del token acerto quien prueba.
    """
    esperado = os.environ.get(VAR_TOKEN_SYNC)
    if not esperado or not recibido:
        return False
    return secrets.compare_digest(recibido, esperado)


@integraciones_bp.route('/tiendanube/sync-externo', methods=['POST'])
def sync_externo_tiendanube():
    """Dispara el backfill de todos los canales conectados (FASE-SYNC-CRON-S2).

    Sin @login_required: el que llama es cron-job.org, que no tiene sesion. El
    header reemplaza al login y es lo unico que separa esto de una ruta
    publica, asi que se chequea antes de tocar la base.

    La respuesta del 401 es siempre la misma frase, sin importar si el canal
    existe, si esta conectado o si hay algo corriendo. Un endpoint sin login es
    una superficie escaneable: no tiene por que contarle nada al que no pasa.

    Recorre `canales_a_sincronizar()` en vez de un id fijo -- el cron no tiene
    empresa "actual", y con dos empresas manana nadie tiene que acordarse de
    volver aca.

    Responde enseguida (202) con un renglon por canal. Cada canal arranca en su
    propio thread daemon via `lanzar_backfill`, igual que el boton manual: si
    esperara el resultado, cron-job.org cortaria por timeout a los 30 segundos
    y el sync quedaria a medias sin que nadie lo sepa.
    """
    if not _token_sync_valido(request.headers.get(HEADER_SYNC)):
        # Sin detalle de por que fallo y sin eco del token recibido: este log
        # lo lee cualquiera que tenga acceso a Render.
        _log('sync-externo: rechazado por token invalido o ausente')
        return jsonify({'error': 'no autorizado'}), 401

    app_obj = current_app._get_current_object()

    resultados = []
    for canal_id in sync_tiendanube.canales_a_sincronizar():
        try:
            arranco, mensaje = sync_tiendanube.lanzar_backfill(app_obj, canal_id)
        except Exception as exc:
            # Un canal que no pudo ni arrancar no puede dejar sin sync a los
            # demas: son empresas distintas.
            db.session.rollback()
            _log(f'sync-externo: no se pudo lanzar el canal {canal_id}: {exc!r}')
            resultados.append({'canal_id': canal_id, 'estado': 'error'})
            continue

        resultados.append({
            'canal_id': canal_id,
            'estado': 'arrancado' if arranco else 'saltado',
            'detalle': mensaje,
        })

    _log('sync-externo: %d canal(es) procesados' % len(resultados))
    return jsonify({'canales': resultados}), 202



# ============================================================================
# FASE-MP-S1 - Cuentas de cobro de Mercado Pago
# ----------------------------------------------------------------------------
# Dos cuentas reales, de dos personas distintas, contra UNA sola aplicacion de
# Mercado Pago. Cada una hace su propio flujo de OAuth logueandose con su
# usuario; lo que decide en cual de las dos filas de cuenta_cobro se guarda el
# token es el `state`, no la sesion de quien apreto el boton.
#
# Por eso aca el state SI esta implementado y en Tiendanube no: alla habia un
# solo canal posible y el state era pura proteccion CSRF; aca ademas es el
# unico dato que liga el callback con la cuenta que se estaba conectando.
# ============================================================================

TIPO_MERCADOPAGO = 'mercadopago'

# Claves del state en la sesion del navegador. La sesion de Flask va firmada
# con SECRET_KEY, asi que el usuario no puede fabricarse un state valido.
SESION_STATE = 'mp_oauth_state'
SESION_CUENTA = 'mp_oauth_cuenta_id'


def _cuenta_mercadopago(cuenta_cobro_id):
    """La cuenta de cobro pedida, si existe, es de Mercado Pago y es de la
    empresa del usuario. None en cualquier otro caso.

    Los tres chequeos van juntos a proposito: separar "no existe" de "no es
    tuya" le contaria a un usuario cuantas cuentas tiene otra empresa.
    """
    cuenta = db.session.get(CuentaCobro, cuenta_cobro_id)
    if cuenta is None:
        return None
    if cuenta.tipo != TIPO_MERCADOPAGO:
        return None
    if cuenta.empresa_id != current_user.empresa_id:
        return None
    return cuenta


def _redirect_uri():
    """El redirect_uri que se manda en la autorizacion y en el intercambio.

    Tiene que ser identico en las dos llamadas y coincidir exacto con el que
    esta cargado en el panel de Mercado Pago Developers. Se prefiere la
    variable de entorno: detras del proxy de Render, url_for(_external=True)
    puede resolver a http:// y Mercado Pago rechaza el canje.
    """
    return (mp.redirect_uri_configurado()
            or url_for('integraciones.callback_mercadopago', _external=True))


def _cuentas_para_la_vista():
    """Cada cuenta de cobro de la empresa, con lo que necesita la pagina.

    'conectada' se lee de que haya credencial con token, no de cuenta.activo:
    las cuentas nacen activas de la semilla (existen y sirven), lo que puede
    faltar es la autorizacion. Es distinto de canal_venta.activo, donde el flag
    si significa "tiene credencial".
    """
    cuentas = (CuentaCobro.query
               .filter_by(empresa_id=current_user.empresa_id, tipo=TIPO_MERCADOPAGO)
               .order_by(CuentaCobro.id)
               .all())

    vista = []
    for cuenta in cuentas:
        credencial = sync_mercadopago.credencial_de(cuenta.id)
        conectada = bool(credencial and credencial.access_token_cifrado)

        # Consultar el estado tambien cierra las corridas huerfanas, asi que
        # abrir la pagina destraba el boton sin intervencion manual.
        corriendo = False
        sync = None
        if conectada:
            corriendo = sync_mercadopago.sync_en_curso(cuenta.id) is not None
            sync = sync_mercadopago.ultimo_sync(cuenta.id)

        total, ultimo_movimiento = sync_mercadopago.total_de(cuenta.id)

        vista.append({
            'id': cuenta.id,
            'alias': cuenta.nombre,
            'conectada': conectada,
            'id_cuenta_externa': cuenta.id_cuenta_externa,
            'vencida': bool(credencial
                            and ingestor_mp.token_vencido(credencial.expira_en)),
            'expira_en': credencial.expira_en if credencial else None,
            'total': total,
            'ultimo_movimiento': ultimo_movimiento,
            'sync': sync,
            'sync_corriendo': corriendo,
        })
    return vista


@integraciones_bp.route('/mercadopago/conectar/<int:cuenta_cobro_id>')
@login_required
def conectar_mercadopago(cuenta_cobro_id):
    """Manda a la persona a autorizar SU cuenta de Mercado Pago.

    No escribe nada en la base: hasta que Mercado Pago no redirija al callback
    con un code valido, para esta app no paso nada. Lo unico que queda es el
    state en la sesion del navegador.
    """
    cuenta = _cuenta_mercadopago(cuenta_cobro_id)
    if cuenta is None:
        flash('Esa cuenta de cobro no existe.', 'danger')
        return redirect(url_for('integraciones.listar'))

    try:
        # Se arma la URL ANTES de guardar el state: si falta el client_id, la
        # sesion no queda con un state colgado apuntando a una cuenta.
        state = secrets.token_urlsafe(32)
        destino = mp.url_autorizacion(state, _redirect_uri())
    except mp.ErrorMercadoPago as exc:
        _log(f'no se pudo armar la autorizacion de Mercado Pago: {exc.detalle or exc}')
        flash(str(exc), 'danger')
        return redirect(url_for('integraciones.listar'))

    session[SESION_STATE] = state
    session[SESION_CUENTA] = cuenta.id
    return redirect(destino)


def _state_valido():
    """La cuenta que se estaba conectando, si el state del callback es el que
    quedo guardado en sesion. None si no coincide, si no hay ninguno, o si el
    callback no trajo state.

    Consume el state pase lo que pase: es de un solo uso. Sin eso, un code
    viejo podria reusar el mismo state para escribir de nuevo.
    """
    guardado = session.pop(SESION_STATE, None)
    cuenta_id = session.pop(SESION_CUENTA, None)

    recibido = request.args.get('state')
    if not guardado or not recibido or cuenta_id is None:
        return None
    # compare_digest y no ==: la comparacion de un token de sesion no tiene por
    # que filtrar en cuantos caracteres coincidia.
    if not secrets.compare_digest(str(guardado), str(recibido)):
        return None
    return cuenta_id


@integraciones_bp.route('/mercadopago/callback')
@login_required
def callback_mercadopago():
    """Vuelta de Mercado Pago con ?code= y ?state=.

    El state se valida PRIMERO y contra la sesion. Si no coincide se corta ahi:
    no se canjea el code, no se lee ninguna cuenta y no se escribe nada. Es lo
    que impide que un callback fabricado escriba un token en la cuenta
    equivocada -- que con dos cuentas de dos personas distintas no es un riesgo
    teorico, es la diferencia entre ver la plata de uno o la del otro.

    Despues, el mismo orden que en Tiendanube: la llamada HTTP antes, la
    escritura a la base en un solo commit al final. No existe el estado "cuenta
    con id externo pero sin token" ni al reves.
    """
    cuenta_id = _state_valido()
    if cuenta_id is None:
        _log('callback de Mercado Pago con state invalido o ausente: se descarta')
        flash('No se pudo validar la vuelta de Mercado Pago. Empezá la conexión '
              'de nuevo desde el botón Conectar.', 'danger')
        return redirect(url_for('integraciones.listar'))

    cuenta = _cuenta_mercadopago(cuenta_id)
    if cuenta is None:
        flash('Esa cuenta de cobro no existe.', 'danger')
        return redirect(url_for('integraciones.listar'))

    if request.args.get('error'):
        _log(f"Mercado Pago devolvio error en el callback: {request.args.get('error')}")
        flash('No se completó la conexión con Mercado Pago: se canceló la autorización.',
              'danger')
        return redirect(url_for('integraciones.listar'))

    code = request.args.get('code')
    if not code:
        flash('Mercado Pago no devolvió el código de autorización. Probá conectar de nuevo.',
              'danger')
        return redirect(url_for('integraciones.listar'))

    try:
        token = mp.intercambiar_code(code, _redirect_uri())
    except mp.ErrorMercadoPago as exc:
        _log(f'fallo la conexion con Mercado Pago: {exc.detalle or exc}')
        flash(str(exc), 'danger')
        return redirect(url_for('integraciones.listar'))

    try:
        cuenta.id_cuenta_externa = token['user_id']

        # Reconectar reutiliza la fila (hay UNIQUE por cuenta_cobro_id): una
        # cuenta tiene una credencial vigente, no una pila de tokens viejos.
        credencial = CredencialCuentaCobro.query.filter_by(
            cuenta_cobro_id=cuenta.id).first()
        if credencial is None:
            credencial = CredencialCuentaCobro(cuenta_cobro_id=cuenta.id)
            db.session.add(credencial)

        credencial.access_token_cifrado = cripto.cifrar(token['access_token'])
        # El refresh_token es tan sensible como el access_token (sirve para
        # fabricar access_tokens nuevos), asi que va cifrado igual. Puede venir
        # None si la app no tiene habilitado offline_access.
        credencial.refresh_token_cifrado = cripto.cifrar(token['refresh_token'])
        credencial.expira_en = token['expira_en']
        credencial.actualizado_en = datetime.utcnow()

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        _log(f'fallo guardando la credencial de Mercado Pago: {exc!r}')
        flash('Se autorizó la cuenta pero no se pudo guardar la credencial. Probá de nuevo.',
              'danger')
        return redirect(url_for('integraciones.listar'))

    if not token['refresh_token']:
        _log('Mercado Pago no devolvio refresh_token para la cuenta %s: revisar '
             'que la aplicacion tenga habilitado offline_access' % cuenta.id)

    flash(f'Cuenta de Mercado Pago conectada: {cuenta.nombre}.', 'success')
    return redirect(url_for('integraciones.listar'))


@integraciones_bp.route('/mercadopago/sincronizar/<int:cuenta_cobro_id>', methods=['POST'])
@login_required
def sincronizar_mercadopago(cuenta_cobro_id):
    """Dispara el backfill de movimientos de UNA cuenta.

    Responde de inmediato: el trabajo real corre en un thread daemon con su
    propio app_context (ver sync_mercadopago.py). Nunca se le pega a la API de
    Mercado Pago dentro del ciclo request/response -- un historico de varios
    anios son varias ventanas de fecha y dejaria al worker de gunicorn colgado.

    Es POST y no GET a proposito: dispara trabajo, asi que no puede caer por un
    prefetch del navegador ni quedar en el historial como link.
    """
    cuenta = _cuenta_mercadopago(cuenta_cobro_id)
    if cuenta is None:
        flash('Esa cuenta de cobro no existe.', 'danger')
        return redirect(url_for('integraciones.listar'))

    app_obj = current_app._get_current_object()

    try:
        arranco, mensaje = sync_mercadopago.lanzar_backfill(app_obj, cuenta.id)
    except Exception as exc:
        db.session.rollback()
        _log(f'no se pudo lanzar el sync de Mercado Pago: {exc!r}')
        flash('No se pudo iniciar la sincronización. Probá de nuevo.', 'danger')
        return redirect(url_for('integraciones.listar'))

    flash(mensaje, 'success' if arranco else 'danger')
    return redirect(url_for('integraciones.listar'))


# ============================================================================
# FASE-MELI-S1 - Canal de venta de Mercado Libre
# ----------------------------------------------------------------------------
# Mismo esqueleto que el OAuth de Mercado Pago -- state en la sesion, validado
# en el callback, redirect_uri en las dos llamadas -- con dos diferencias que
# vienen de la API y no de una decision de disenio:
#
#   1. El state aca es SOLO proteccion CSRF. En Mercado Pago ademas decide en
#      cual de las dos cuentas de cobro se guarda el token; aca hay un unico
#      canal de Mercado Libre por empresa (UNIQUE empresa_id+tipo en
#      canal_venta), asi que lo unico que se guarda al lado del state es la
#      empresa cuyo canal se estaba conectando.
#
#   2. Se guarda el refresh_token SI O SI. El access_token de Mercado Libre
#      dura seis horas: sin refresh no hay integracion, hay una demo que
#      funciona hasta la tarde. Si la respuesta no lo trae, la conexion se
#      guarda igual (el token sirve seis horas) pero se avisa fuerte, porque
#      significa que falto el scope offline_access.
#
# El vinculo canal_venta -> cuenta_cobro (canal 2 -> cuenta 41, la de Nachi) ya
# existe y NO se toca aca: esto conecta la credencial, no reescribe a donde
# entra la plata.
# ============================================================================

SESION_STATE_MELI = 'meli_oauth_state'
SESION_EMPRESA_MELI = 'meli_oauth_empresa_id'


def _redirect_uri_meli():
    """El redirect_uri de las dos llamadas.

    A diferencia de Mercado Pago no hay fallback a url_for(_external=True): el
    de Mercado Libre tiene que coincidir caracter por caracter con el cargado
    en el DevCenter, y un url_for detras del proxy de Render puede devolver
    http:// y hacer fallar el canje con un invalid_grant que no explica nada.
    El modulo de integracion ya trae el valor correcto por defecto.
    """
    return meli.redirect_uri_configurado()


@integraciones_bp.route('/mercadolibre/conectar')
@login_required
def conectar_mercadolibre():
    """Manda a la persona a autorizar la cuenta de Mercado Libre.

    No escribe nada en la base: hasta que Mercado Libre no redirija al callback
    con un code valido, para esta app no paso nada. Lo unico que queda es el
    state en la sesion del navegador.
    """
    try:
        # Se arma la URL ANTES de guardar el state: si falta el client_id, la
        # sesion no queda con un state colgado.
        state = secrets.token_urlsafe(32)
        destino = meli.url_autorizacion(state, _redirect_uri_meli())
    except meli.ErrorMercadoLibre as exc:
        _log('no se pudo armar la autorizacion de Mercado Libre: %s' % (exc.detalle or exc))
        flash(str(exc), 'danger')
        return redirect(url_for('integraciones.listar'))

    session[SESION_STATE_MELI] = state
    session[SESION_EMPRESA_MELI] = current_user.empresa_id
    return redirect(destino)


def _state_meli_valido():
    """La empresa que se estaba conectando, si el state del callback es el que
    quedo guardado en sesion. None si no coincide, si no hay ninguno, o si el
    callback no trajo state.

    Consume el state pase lo que pase: es de un solo uso. Sin eso, un code
    viejo podria reusar el mismo state para escribir de nuevo.

    Mercado Libre no valida este campo -- la doc lo dice con todas las letras:
    "From Mercado Libre we do not validate this field". Es enteramente nuestro.
    """
    guardado = session.pop(SESION_STATE_MELI, None)
    empresa_id = session.pop(SESION_EMPRESA_MELI, None)

    recibido = request.args.get('state')
    if not guardado or not recibido or empresa_id is None:
        return None
    # compare_digest y no ==: la comparacion de un token de sesion no tiene por
    # que filtrar en cuantos caracteres coincidia.
    if not secrets.compare_digest(str(guardado), str(recibido)):
        return None
    return empresa_id


@integraciones_bp.route('/mercadolibre/callback')
@login_required
def callback_mercadolibre():
    """Vuelta de Mercado Libre con ?code= y ?state=.

    El state se valida PRIMERO. Si no coincide se corta ahi: no se canjea el
    code y no se escribe nada.

    Despues, el mismo orden que en Tiendanube y en Mercado Pago: las llamadas
    HTTP que pueden fallar van ANTES de tocar la base, y la escritura es un
    solo commit al final. No existe el estado "canal activo sin token" ni al
    reves.

    Se verifica el token contra /users/me antes de dar la conexion por buena,
    igual que Tiendanube lo prueba contra /store: un token que canjea pero no
    lee no sirve, y es mejor enterarse ahora que en la primera sincronizacion.
    """
    empresa_id = _state_meli_valido()
    if empresa_id is None:
        _log('callback de Mercado Libre con state invalido o ausente: se descarta')
        flash('No se pudo validar la vuelta de Mercado Libre. Empezá la conexión '
              'de nuevo desde el botón Conectar.', 'danger')
        return redirect(url_for('integraciones.listar'))

    # El state prueba que el flujo lo empezo esta app; que lo haya empezado
    # ESTE usuario es otra cosa. Sin este chequeo, un callback abierto en la
    # sesion de otra empresa escribiria el token en el canal equivocado.
    if empresa_id != current_user.empresa_id:
        _log('callback de Mercado Libre para la empresa %s desde la sesion de la '
             '%s: se descarta' % (empresa_id, current_user.empresa_id))
        flash('No se pudo validar la vuelta de Mercado Libre. Empezá la conexión '
              'de nuevo desde el botón Conectar.', 'danger')
        return redirect(url_for('integraciones.listar'))

    if request.args.get('error'):
        _log('Mercado Libre devolvio error en el callback: %s' % request.args.get('error'))
        flash('No se completó la conexión con Mercado Libre: se canceló la autorización.',
              'danger')
        return redirect(url_for('integraciones.listar'))

    code = request.args.get('code')
    if not code:
        flash('Mercado Libre no devolvió el código de autorización. Probá conectar de nuevo.',
              'danger')
        return redirect(url_for('integraciones.listar'))

    try:
        token = meli.intercambiar_code(code, _redirect_uri_meli())
        usuario = meli.traer_usuario(token['access_token'])
    except meli.ErrorMercadoLibre as exc:
        _log('fallo la conexion con Mercado Libre: %s' % (exc.detalle or exc))
        flash(str(exc), 'danger')
        return redirect(url_for('integraciones.listar'))

    apodo = meli.apodo_de(usuario)

    try:
        canal = _canal(TIPO_MERCADOLIBRE, crear=True)
        canal.id_tienda_externo = token['user_id']
        canal.activo = True
        # `canal.nombre` NO se pisa con el apodo de Mercado Libre. En Tiendanube
        # el nombre de la tienda ES el del canal; aca el canal se llama "Mercado
        # Libre" para todo el mundo y el apodo del vendedor es un dato de la
        # cuenta, que ya se ve al lado del id. Tampoco se toca cuenta_cobro_id:
        # a donde entra la plata lo decidio FASE-MP-S1 y esto es otra pregunta.

        # Reconectar reutiliza la fila: un canal tiene una credencial vigente,
        # no una pila historica de tokens viejos. Se pasa por el mismo lector
        # que usa el refresco para que las dos puntas miren la misma fila.
        credencial = credencial_meli.credencial_de(canal.id)
        if credencial is None:
            credencial = CredencialCanal(canal_id=canal.id, tipo_credencial='oauth2')
            db.session.add(credencial)

        credencial.access_token_cifrado = cripto.cifrar(token['access_token'])
        # El refresh_token es tan sensible como el access_token (sirve para
        # fabricar access_tokens nuevos), asi que va cifrado igual.
        credencial.refresh_token_cifrado = cripto.cifrar(token['refresh_token'])
        credencial.expira_en = token['expira_en']
        credencial.scope = (token.get('scope') or None)
        credencial.activo = True
        credencial.fecha_actualizacion = datetime.utcnow()

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        _log('fallo guardando la credencial de Mercado Libre: %r' % (exc,))
        flash('Se autorizó la cuenta pero no se pudo guardar la credencial. Probá de nuevo.',
              'danger')
        return redirect(url_for('integraciones.listar'))

    if not token['refresh_token']:
        _log('Mercado Libre no devolvio refresh_token para el canal %s: revisar que '
             'la URL de autorizacion pida el scope offline_access' % canal.id)
        flash('Mercado Libre conectado como %s, pero sin permiso para renovar el '
              'acceso: se va a cortar en unas horas. Avisá para revisar la '
              'configuración de la aplicación.' % apodo, 'danger')
        return redirect(url_for('integraciones.listar'))

    flash('Mercado Libre conectado: %s.' % apodo, 'success')
    return redirect(url_for('integraciones.listar'))
