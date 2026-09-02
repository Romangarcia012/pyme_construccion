# -*- coding: utf-8 -*-
"""Cliente HTTP de Mercado Pago (FASE-MP-S1).

Dos capas, igual que `integracion_tiendanube.py`:

  - El OAuth: url_autorizacion() e intercambiar_code().
  - La lectura de pagos: traer_pagos(), con paginacion, ventanas de fecha y
    freno de rate limit.

Ninguna funcion de este modulo toca la base: hablan con la API y devuelven
dicts crudos. Quien persiste es la ruta del callback o `sync_mercadopago.py`.
El mapeo al modelo interno vive en `ingestor_mercadopago.py`.

DIFERENCIAS CON TIENDANUBE, que son el motivo de que este modulo exista en vez
de generalizar el otro:

  1. El token VENCE. Mercado Pago documenta 180 dias (expires_in = 15552000) y
     devuelve refresh_token, pero solo si en la autorizacion se pidio el scope
     `offline_access`. Esta slice no refresca nada: guarda el refresh_token
     para cuando haya scheduler y, mientras tanto, avisa que hay que reconectar.

  2. El `state` es obligatorio en los hechos, no solo por CSRF. Una sola
     aplicacion de Mercado Pago autoriza las dos cuentas (la de Roman y la de
     Nachi) contra el mismo redirect_uri, asi que el callback no tiene forma de
     saber a cual de las dos corresponde el `code` si no es por el state que
     quedo guardado en sesion.

  3. El redirect_uri viaja en las DOS llamadas (autorizacion e intercambio) y
     tiene que coincidir exacto con el que esta cargado en el panel de Mercado
     Pago Developers. Tiendanube no lo pide en ninguna de las dos.

  4. La busqueda de pagos limita el rango a 365 dias por llamada, asi que el
     backfill historico se pagina por ventanas de fecha ademas de por offset.
"""

import os
import time
from datetime import datetime, timedelta

import requests

# El Client ID no es secreto (viaja en la URL de autorizacion que ve el
# usuario), pero a diferencia del APP_ID de Tiendanube tampoco esta en el repo:
# la aplicacion la creo Roman y el numero llega por entorno, igual que el
# secret. Sin default: una app mal configurada tiene que fallar diciendolo, no
# mandar al usuario a autorizar una aplicacion que no es la suya.
VARIABLE_CLIENT_ID = 'MERCADOPAGO_CLIENT_ID'
VARIABLE_SECRET = 'MERCADOPAGO_CLIENT_SECRET'

# El redirect_uri tiene que ser identico al cargado en el panel de Mercado Pago
# Developers. Se toma del entorno y no se arma con url_for(_external=True)
# porque detras del proxy de Render eso puede resolver a http:// o a un host
# interno, y Mercado Pago rechaza el intercambio si no coincide caracter por
# caracter. `rutas_integraciones.py` usa url_for como ultimo recurso local.
VARIABLE_REDIRECT_URI = 'MERCADOPAGO_REDIRECT_URI'

URL_AUTORIZAR = 'https://auth.mercadopago.com/authorization'
URL_TOKEN = 'https://api.mercadopago.com/oauth/token'
URL_API = 'https://api.mercadopago.com'

# offline_access es lo que hace que la respuesta traiga refresh_token; sin ese
# scope el token vence a los 180 dias y no hay forma de renovarlo salvo volver
# a mandar a la persona a autorizar. `read` alcanza: esta app solo lee pagos.
SCOPE = 'offline_access read'

TIMEOUT = 20


class ErrorMercadoPago(Exception):
    """Falla hablando con Mercado Pago: red, credenciales o respuesta rara.

    Lleva un mensaje en castellano apto para mostrarle al usuario; el detalle
    tecnico queda en `detalle` para el log.

    `reconectar` marca el subconjunto de fallas que no se arreglan
    reintentando: el token vencio o lo revocaron y hace falta que la persona
    vuelva a autorizar. La UI lo usa para mostrar "reconecta esta cuenta" en
    vez de "proba de nuevo".
    """

    def __init__(self, mensaje, detalle=None, reconectar=False):
        super().__init__(mensaje)
        self.detalle = detalle
        self.reconectar = reconectar


def _client_id():
    valor = os.environ.get(VARIABLE_CLIENT_ID)
    if not valor:
        raise ErrorMercadoPago(
            'La integracion con Mercado Pago no esta configurada en el servidor.',
            detalle='%s no esta definida' % VARIABLE_CLIENT_ID
        )
    return valor


def _client_secret():
    valor = os.environ.get(VARIABLE_SECRET)
    if not valor:
        raise ErrorMercadoPago(
            'La integracion con Mercado Pago no esta configurada en el servidor.',
            detalle='%s no esta definida' % VARIABLE_SECRET
        )
    return valor


def redirect_uri_configurado():
    """El redirect_uri del entorno, o None si no se configuro."""
    return os.environ.get(VARIABLE_REDIRECT_URI) or None


def _headers(access_token=None):
    cabeceras = {'Content-Type': 'application/json'}
    if access_token:
        cabeceras['Authorization'] = 'Bearer %s' % access_token
    return cabeceras


def _recorte(resp, largo=300):
    """Primeros caracteres del body, para el log. Nunca se muestra al usuario:
    puede traer eco de datos del request."""
    try:
        return resp.text[:largo]
    except Exception:
        return '<sin cuerpo>'


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def url_autorizacion(state, redirect_uri):
    """URL del consentimiento, para UNA cuenta.

    La persona que la abre se loguea con SU usuario de Mercado Pago; por eso la
    misma aplicacion autoriza las dos cuentas sin que haya que crear dos apps.
    Quien abre el link decide de quien es la cuenta que queda conectada, y el
    `state` decide en cual de las dos filas de cuenta_cobro se guarda.
    """
    from urllib.parse import urlencode

    parametros = {
        'client_id': _client_id(),
        'response_type': 'code',
        # 'mp' = la cuenta se elige en Mercado Pago, no en Mercado Libre.
        'platform_id': 'mp',
        'redirect_uri': redirect_uri,
        'scope': SCOPE,
        'state': state,
    }
    return '%s?%s' % (URL_AUTORIZAR, urlencode(parametros))


def intercambiar_code(code, redirect_uri):
    """Canja el ?code= del callback por un access_token.

    Devuelve {'access_token', 'refresh_token', 'user_id', 'scope',
    'expira_en': datetime|None}. `user_id` es el id de la CUENTA en Mercado
    Pago, no el usuario de esta app.

    `expira_en` sale de expires_in (segundos) y se calcula aca, no en el que
    llama: la referencia temporal correcta es el momento en que Mercado Pago
    emitio el token, o sea ahora.
    """
    cuerpo = {
        'client_id': _client_id(),
        'client_secret': _client_secret(),
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
    }
    try:
        resp = requests.post(URL_TOKEN, json=cuerpo, headers=_headers(), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ErrorMercadoPago(
            'No se pudo contactar a Mercado Pago para validar la autorizacion.',
            detalle=str(exc)
        ) from exc

    if resp.status_code != 200:
        raise ErrorMercadoPago(
            'Mercado Pago rechazo la autorizacion. Proba conectar de nuevo.',
            detalle='HTTP %s en el intercambio del code: %s' % (resp.status_code, _recorte(resp))
        )

    try:
        datos = resp.json()
    except ValueError as exc:
        raise ErrorMercadoPago(
            'Mercado Pago devolvio una respuesta que no se pudo interpretar.',
            detalle='respuesta no-JSON en el intercambio del code: %s' % _recorte(resp)
        ) from exc

    if datos.get('error'):
        raise ErrorMercadoPago(
            'Mercado Pago rechazo la autorizacion. Proba conectar de nuevo.',
            detalle='error en el intercambio del code: %s' % datos.get('error')
        )

    access_token = datos.get('access_token')
    user_id = datos.get('user_id')
    if not access_token or not user_id:
        raise ErrorMercadoPago(
            'Mercado Pago no devolvio un token valido.',
            detalle='faltan access_token o user_id en la respuesta: %s' % sorted(datos.keys())
        )

    return {
        'access_token': access_token,
        # Puede faltar si la app no tiene habilitado offline_access. No es
        # motivo para abortar la conexion -- el access_token sirve igual por
        # 180 dias -- pero si para que la UI avise que va a haber que
        # reconectar a mano cuando venza.
        'refresh_token': datos.get('refresh_token') or None,
        'user_id': str(user_id),
        'scope': datos.get('scope') or None,
        'expira_en': _vencimiento(datos.get('expires_in')),
    }


def _vencimiento(expires_in):
    """utcnow + expires_in segundos, o None si Mercado Pago no lo dijo."""
    try:
        segundos = int(expires_in)
    except (TypeError, ValueError):
        return None
    if segundos <= 0:
        return None
    return datetime.utcnow() + timedelta(seconds=segundos)


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------
# Mercado Pago no publica el numero exacto de requests por minuto, y tampoco
# promete headers de cuota. Lo unico documentado es que ante exceso responde
# 429 y que hay que reintentar con backoff. Asi que el freno es reactivo (a
# diferencia del de Tiendanube, que puede ser preventivo porque el balde viaja
# en cada respuesta): se reintenta el 429 respetando Retry-After si viene, y si
# no, con backoff exponencial.

ESPERA_MAXIMA = 60.0
MAX_REINTENTOS_429 = 5

# Corte de seguridad para la paginacion por offset: si la API nunca dice que se
# termino, esto evita un thread infinito.
MAX_PAGINAS = 500


def _header(resp, nombre, por_defecto=None):
    """Lee un header sin depender de mayusculas/minusculas.

    requests devuelve un CaseInsensitiveDict, pero los tests arman respuestas
    falsas con dicts comunes: mejor no atarse a eso.
    """
    cabeceras = getattr(resp, 'headers', None) or {}
    obtener = getattr(cabeceras, 'get', None)
    if obtener is None:
        return por_defecto
    directo = obtener(nombre)
    if directo is not None:
        return directo
    objetivo = nombre.lower()
    for clave, valor in cabeceras.items():
        if str(clave).lower() == objetivo:
            return valor
    return por_defecto


def _entero(valor):
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        return None


def _espera_tras_429(resp, intento):
    """Cuanto dormir despues de un 429: Retry-After si viene, si no backoff."""
    retry_after = _entero(_header(resp, 'Retry-After'))
    if retry_after is not None and retry_after >= 0:
        return min(float(retry_after), ESPERA_MAXIMA)
    return min(2.0 ** intento, ESPERA_MAXIMA)


def _get_api(ruta, access_token, params=None):
    """GET a la API con reintentos por rate limit.

    `ruta` va sin el host: 'v1/payments/search'. Devuelve el Response crudo;
    interpretarlo es de quien llama.
    """
    url = '%s/%s' % (URL_API, ruta)

    for intento in range(MAX_REINTENTOS_429 + 1):
        try:
            resp = requests.get(url, headers=_headers(access_token),
                                params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ErrorMercadoPago(
                'No se pudo contactar a la API de Mercado Pago.',
                detalle='%s en GET %s: %s' % (type(exc).__name__, ruta, exc)
            ) from exc

        if resp.status_code != 429:
            return resp

        if intento >= MAX_REINTENTOS_429:
            break
        time.sleep(_espera_tras_429(resp, intento))

    raise ErrorMercadoPago(
        'Mercado Pago esta limitando las consultas. Proba de nuevo en un rato.',
        detalle='429 persistente en GET %s tras %d reintentos' % (ruta, MAX_REINTENTOS_429)
    )


def _validar_respuesta(resp, ruta):
    """El body de una busqueda, validado.

    401 y 403 se traducen a "reconecta la cuenta" y no a un error generico: son
    exactamente el caso del token vencido o revocado, que es la unica falla que
    el usuario puede arreglar solo.
    """
    if resp.status_code in (401, 403):
        raise ErrorMercadoPago(
            'El acceso a esta cuenta de Mercado Pago vencio o fue revocado. '
            'Reconecta esta cuenta para volver a sincronizar.',
            detalle='HTTP %s en GET %s: %s' % (resp.status_code, ruta, _recorte(resp)),
            reconectar=True,
        )

    if resp.status_code != 200:
        raise ErrorMercadoPago(
            'Mercado Pago rechazo la consulta de pagos.',
            detalle='HTTP %s en GET %s: %s' % (resp.status_code, ruta, _recorte(resp))
        )

    try:
        datos = resp.json()
    except ValueError as exc:
        raise ErrorMercadoPago(
            'Mercado Pago devolvio una respuesta que no se pudo interpretar.',
            detalle='respuesta no-JSON en GET %s: %s' % (ruta, _recorte(resp))
        ) from exc

    if not isinstance(datos, dict):
        raise ErrorMercadoPago(
            'Mercado Pago devolvio una respuesta con un formato inesperado.',
            detalle='GET %s no devolvio un objeto sino %s' % (ruta, type(datos).__name__)
        )
    return datos


# ---------------------------------------------------------------------------
# Busqueda de pagos
# ---------------------------------------------------------------------------
# GET /v1/payments/search, documentado en:
#   https://www.mercadopago.com.ar/developers/es/reference/payments/_payments_search/get
#
# Dos ejes de paginacion, y hacen falta los dos:
#
#   offset/limit  ->  dentro de una ventana. La respuesta trae
#                     paging = {total, limit, offset}.
#   begin_date/end_date  ->  el rango maximo por llamada es 365 DIAS. Un
#                     backfill historico de varios anios no entra en una sola
#                     busqueda, hay que barrerlo en ventanas.
#
# `limit` se pide en 100 pero NO se asume que la API lo respete: la doc de
# Mercado Pago dice 30 por defecto en unas paginas y hasta 1000 en otras. El
# avance del offset se calcula con la cantidad de resultados REALMENTE
# devueltos, asi que la paginacion sale bien le haga caso o no.

RUTA_PAGOS = 'v1/payments/search'

LIMITE_POR_PAGINA = 100

# 364 y no 365: el limite de la API es inclusivo en los dos extremos y un dia
# de margen evita discutir con el borde por un problema de zona horaria.
DIAS_POR_VENTANA = 364

# Hasta donde va para atras la primera corrida. Mercado Pago no expone "desde
# cuando existe esta cuenta", asi que el historico se barre contra un horizonte
# fijo. Seis anios cubre de sobra la vida de la ferreteria y son 6 requests
# vacias en el peor caso, que no es costo.
ANIOS_HISTORICO = 6

# Formato que acepta begin_date/end_date: ISO 8601 con milisegundos y offset.
# Se manda todo en UTC (sufijo Z) porque el modelo guarda UTC naive.
FORMATO_FECHA = '%Y-%m-%dT%H:%M:%S.000Z'


def _formatear(momento):
    return momento.strftime(FORMATO_FECHA)


def ventanas(desde, hasta):
    """Parte [desde, hasta] en tramos de a lo sumo DIAS_POR_VENTANA dias.

    Existe porque la busqueda de pagos no acepta rangos de mas de 365 dias.
    Devuelve una lista de (inicio, fin), la primera arrancando en `desde`.
    """
    if desde >= hasta:
        return []

    tramos = []
    inicio = desde
    paso = timedelta(days=DIAS_POR_VENTANA)
    while inicio < hasta:
        fin = min(inicio + paso, hasta)
        tramos.append((inicio, fin))
        inicio = fin
    return tramos


def _pagina_de_pagos(access_token, inicio, fin, offset):
    """Una pagina de la busqueda. Devuelve (resultados, total)."""
    params = {
        # date_approved y no date_created: lo que se esta reconstruyendo es
        # cuanta plata ENTRO, y un pago creado el 30 y aprobado el 2 entro en
        # el mes siguiente. Ademas descarta de arranque los no aprobados, que
        # no tienen date_approved y por lo tanto caen fuera de cualquier rango.
        'sort': 'date_approved',
        'criteria': 'asc',
        'range': 'date_approved',
        'begin_date': _formatear(inicio),
        'end_date': _formatear(fin),
        'limit': LIMITE_POR_PAGINA,
        'offset': offset,
    }
    resp = _get_api(RUTA_PAGOS, access_token, params=params)
    datos = _validar_respuesta(resp, RUTA_PAGOS)

    resultados = datos.get('results')
    if not isinstance(resultados, list):
        raise ErrorMercadoPago(
            'Mercado Pago devolvio una respuesta con un formato inesperado.',
            detalle='GET %s: results no es una lista sino %s'
                    % (RUTA_PAGOS, type(resultados).__name__)
        )

    paging = datos.get('paging') or {}
    total = _entero(paging.get('total'))
    return resultados, total


def traer_pagos(access_token, desde=None, hasta=None):
    """Todos los pagos de la cuenta en el rango, crudos y ya despaginados.

    Sin `desde` se barre el historico completo (ANIOS_HISTORICO para atras):
    la primera corrida tiene que reconstruir cuanta plata entro EN TOTAL, no
    solo lo nuevo.

    No filtra por estado. Igual que en el contrato de IngestorCanal, el estado
    se decide al normalizar: filtrar aca escondería devoluciones y contracargos
    de las slices que vienen.

    Devuelve una lista y no un generador a proposito: si el consumidor abandona
    a mitad de camino, un generador dejaria la paginacion colgada sin que nadie
    se entere.
    """
    hasta = hasta or datetime.utcnow()
    if desde is None:
        desde = hasta - timedelta(days=365 * ANIOS_HISTORICO)

    pagos = []
    for inicio, fin in ventanas(desde, hasta):
        offset = 0
        for _ in range(MAX_PAGINAS):
            lote, total = _pagina_de_pagos(access_token, inicio, fin, offset)
            pagos.extend(lote)

            # Una pagina vacia es el final, diga lo que diga paging.total: sin
            # esta guarda una API que devuelve [] con total=999 haria girar el
            # bucle hasta MAX_PAGINAS.
            if not lote:
                break
            offset += len(lote)
            if total is not None and offset >= total:
                break
        else:
            raise ErrorMercadoPago(
                'La consulta de pagos a Mercado Pago no termina nunca.',
                detalle='GET %s supero las %d paginas en la ventana %s..%s'
                        % (RUTA_PAGOS, MAX_PAGINAS, _formatear(inicio), _formatear(fin))
            )

    return pagos
