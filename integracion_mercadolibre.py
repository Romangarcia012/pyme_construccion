# -*- coding: utf-8 -*-
"""Cliente HTTP de Mercado Libre (FASE-MELI-S1).

Solo el OAuth y la verificacion de la credencial. Ni catalogo ni pedidos: eso
es S2/S3. Ninguna funcion de este modulo toca la base -- hablan con la API y
devuelven dicts crudos. Quien persiste es la ruta del callback o
`credencial_mercadolibre.py`.

La estructura es la de `integracion_mercadopago.py` (OAuth con state y
redirect_uri en las dos llamadas) y no la de Tiendanube, que no tiene state.
Pero Mercado Libre NO es Mercado Pago aunque sean la misma empresa: las cuatro
diferencias que obligan a un modulo propio, verificadas contra la doc oficial
(developers.mercadolibre.com.ar/en_us/authentication-and-authorization,
ultima actualizacion 19/01/2026):

  1. El token de MeLi dura SEIS HORAS (`expires_in` viene en segundos, hoy
     10800 en los ejemplos de la doc; se usa lo que diga la respuesta, no una
     constante). El de Mercado Pago dura 180 dias. Con seis horas el refresco
     deja de ser un lujo y pasa a ser la unica forma de que la conexion siga
     viva de un dia para el otro.

  2. El refresh_token es DE UN SOLO USO. Cada refresco devuelve uno nuevo y el
     anterior queda invalido en el acto ("The REFRESH_TOKEN can only be used
     once ... after being used it will become invalid"). Eso convierte dos
     refrescos simultaneos en una forma de matar el canal: el segundo llega
     con un token ya quemado y no hay como recuperarlo salvo que la persona
     vuelva a autorizar. Por eso el refresco de `credencial_mercadolibre.py`
     corre adentro del lock de canal_venta.

  3. El endpoint de token pide el cuerpo como `application/x-www-form-urlencoded`,
     no como JSON. Mercado Pago acepta JSON; mandarle JSON a MeLi devuelve
     invalid_request. Va con `data=` y no con `json=`.

  4. Los numeros de la API vienen como numeros JSON, no como strings. Pasar por
     `resp.json()` los convierte en float y ahi ya se perdieron centavos. Todo
     el modulo lee con `json.loads(resp.text, parse_float=Decimal)`.

El scope se pide con los tres valores que acepta MeLi -- offline_access, read,
write -- segun el error `invalid_scope` de la doc. `offline_access` es
obligatorio: sin el la respuesta no trae refresh_token y la conexion se muere a
las seis horas. Que la app este configurada en modo lectura en el DevCenter es
otra capa: el scope pide el maximo, el DevCenter concede lo que concede.
"""

import json
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal

import requests

# El Client ID no es secreto: viaja en la URL de autorizacion que ve el
# usuario. Queda en el repo con override por entorno, igual que el APP_ID de
# Tiendanube. La aplicacion la creo Nachi en el DevCenter de Mercado Libre.
CLIENT_ID = os.environ.get('MERCADOLIBRE_CLIENT_ID', '8020619530821827')

# El Secret Key NUNCA va al repo. Se carga como variable de entorno en Render.
VARIABLE_SECRET = 'MERCADOLIBRE_CLIENT_SECRET'

# El redirect_uri tiene que coincidir CARACTER POR CARACTER con el cargado en
# el DevCenter ("must match exactly what is registered in your application
# settings ... the url cannot contain variable information"). Por eso hay un
# default explicito en vez de armarlo con url_for(_external=True): detras del
# proxy de Render eso puede resolver a http:// o a un host interno y MeLi
# rechaza el canje sin decir por que. La variable de entorno gana, para poder
# apuntarlo a otro lado sin tocar codigo.
VARIABLE_REDIRECT_URI = 'MERCADOLIBRE_REDIRECT_URI'
REDIRECT_URI_POR_DEFECTO = (
    'https://pyme-construccion-1jfr.onrender.com/integraciones/mercadolibre/callback'
)

# El host de autorizacion es por pais: .com.ar es MLA (Argentina). El de la API
# es unico para todos los paises.
URL_AUTORIZAR = 'https://auth.mercadolibre.com.ar/authorization'
URL_TOKEN = 'https://api.mercadolibre.com/oauth/token'
URL_API = 'https://api.mercadolibre.com'

# offline_access primero porque es el que importa: es lo que hace que la
# respuesta traiga refresh_token. Sin el, a las seis horas hay que volver a
# mandar a Nachi a autorizar a mano.
SCOPE = 'offline_access read write'

TIMEOUT = 20


class ErrorMercadoLibre(Exception):
    """Falla hablando con Mercado Libre: red, credenciales o respuesta rara.

    Lleva un mensaje en castellano apto para mostrarle al usuario; el detalle
    tecnico queda en `detalle` para el log.

    `reconectar` marca el subconjunto de fallas que no se arreglan
    reintentando: el token vencio, lo revocaron, o el refresh_token ya se uso.
    La UI lo usa para mostrar "reconecta el canal" en vez de "proba de nuevo".
    """

    def __init__(self, mensaje, detalle=None, reconectar=False):
        super().__init__(mensaje)
        self.detalle = detalle
        self.reconectar = reconectar


def _client_id():
    if not CLIENT_ID:
        raise ErrorMercadoLibre(
            'La integracion con Mercado Libre no esta configurada en el servidor.',
            detalle='MERCADOLIBRE_CLIENT_ID quedo vacia'
        )
    return CLIENT_ID


def _client_secret():
    valor = os.environ.get(VARIABLE_SECRET)
    if not valor:
        raise ErrorMercadoLibre(
            'La integracion con Mercado Libre no esta configurada en el servidor.',
            detalle='%s no esta definida' % VARIABLE_SECRET
        )
    return valor


def redirect_uri_configurado():
    """El redirect_uri que se manda en la autorizacion Y en el intercambio.

    Los dos tienen que ser el mismo y el mismo que el del DevCenter; de ahi que
    haya una sola funcion y no dos.
    """
    return os.environ.get(VARIABLE_REDIRECT_URI) or REDIRECT_URI_POR_DEFECTO


def _recorte(resp, largo=300):
    """Primeros caracteres del body, para el log. Nunca se muestra al usuario:
    puede traer eco de datos del request."""
    try:
        return resp.text[:largo]
    except Exception:
        return '<sin cuerpo>'


def _leer_json(resp, donde):
    """El body como dict, con los numeros en Decimal.

    `json.loads(..., parse_float=Decimal)` y no `resp.json()`: los montos de
    Mercado Libre viajan como numeros JSON sueltos (1234.56, no "1234.56") y
    `resp.json()` los convierte en float. Un float no representa 0.1 exacto, y
    a la tercera suma de comisiones ya se corrieron los centavos. Como el paso
    por float es destructivo, tiene que evitarse en la LECTURA -- despues ya no
    hay como recuperar el valor original.

    Vale para toda respuesta del modulo, incluidas las del token: hoy ahi no
    hay montos, pero la regla es del transporte, no de cada endpoint.
    """
    try:
        datos = json.loads(resp.text, parse_float=Decimal)
    except (ValueError, TypeError) as exc:
        raise ErrorMercadoLibre(
            'Mercado Libre devolvio una respuesta que no se pudo interpretar.',
            detalle='respuesta no-JSON en %s: %s' % (donde, _recorte(resp))
        ) from exc

    if not isinstance(datos, dict):
        raise ErrorMercadoLibre(
            'Mercado Libre devolvio una respuesta con un formato inesperado.',
            detalle='%s no devolvio un objeto sino %s' % (donde, type(datos).__name__)
        )
    return datos


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def url_autorizacion(state, redirect_uri):
    """URL del consentimiento.

    La persona que la abre se loguea con SU usuario de Mercado Libre; el
    `state` es lo que despues prueba que la vuelta al callback corresponde a
    este pedido y no a uno fabricado. La doc lo dice explicito: "From Mercado
    Libre we do not validate this field" -- validarlo es enteramente nuestro.
    """
    from urllib.parse import urlencode

    parametros = {
        'response_type': 'code',
        'client_id': _client_id(),
        'redirect_uri': redirect_uri,
        'scope': SCOPE,
        'state': state,
    }
    return '%s?%s' % (URL_AUTORIZAR, urlencode(parametros))


# Cabeceras del endpoint de token, tal cual las pide la doc. El content-type es
# form-urlencoded y no JSON: es la diferencia con Mercado Pago que mas facil se
# pasa por alto, porque el resto del flujo se lee igual.
CABECERAS_TOKEN = {
    'accept': 'application/json',
    'content-type': 'application/x-www-form-urlencoded',
}


def _pedir_token(cuerpo, operacion):
    """POST /oauth/token, comun al primer canje y al refresco.

    Devuelve el dict normalizado. `operacion` solo se usa para el log: el
    cuerpo NO se loguea nunca -- lleva el client_secret y el refresh_token.
    """
    try:
        resp = requests.post(URL_TOKEN, data=cuerpo, headers=CABECERAS_TOKEN,
                             timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ErrorMercadoLibre(
            'No se pudo contactar a Mercado Libre para validar la autorizacion.',
            detalle='%s en %s: %s' % (type(exc).__name__, operacion, exc)
        ) from exc

    if resp.status_code != 200:
        datos = {}
        try:
            datos = _leer_json(resp, operacion)
        except ErrorMercadoLibre:
            pass
        error = str(datos.get('error') or '')

        # invalid_grant es la falla que no se arregla reintentando: el code o
        # el refresh_token vencio, lo revocaron, o -- el caso que importa con
        # un refresh de un solo uso -- ya se habia usado.
        if error == 'invalid_grant':
            raise ErrorMercadoLibre(
                'Mercado Libre ya no acepta esta autorizacion. Hay que volver a '
                'conectar el canal.',
                detalle='invalid_grant en %s: %s' % (operacion, _recorte(resp)),
                reconectar=True,
            )
        raise ErrorMercadoLibre(
            'Mercado Libre rechazo la autorizacion. Proba conectar de nuevo.',
            detalle='HTTP %s en %s: %s' % (resp.status_code, operacion, _recorte(resp))
        )

    datos = _leer_json(resp, operacion)

    # MeLi puede responder 200 con {"error": ...}; el patron es el mismo que ya
    # se contempla en Tiendanube y en Mercado Pago.
    if datos.get('error'):
        raise ErrorMercadoLibre(
            'Mercado Libre rechazo la autorizacion. Proba conectar de nuevo.',
            detalle='error en %s: %s' % (operacion, datos.get('error'))
        )

    access_token = datos.get('access_token')
    user_id = datos.get('user_id')
    if not access_token or not user_id:
        raise ErrorMercadoLibre(
            'Mercado Libre no devolvio un token valido.',
            detalle='faltan access_token o user_id en %s: %s'
                    % (operacion, sorted(datos.keys()))
        )

    return {
        'access_token': access_token,
        # Solo viene si se pidio offline_access. Sin el la conexion vive seis
        # horas y despues hay que reautorizar a mano: no aborta el flujo, pero
        # el que llama tiene que dejarlo asentado.
        'refresh_token': datos.get('refresh_token') or None,
        'user_id': str(user_id),
        'scope': datos.get('scope') or None,
        'expira_en': _vencimiento(datos.get('expires_in')),
    }


def intercambiar_code(code, redirect_uri):
    """Canja el ?code= del callback por access_token + refresh_token.

    Devuelve {'access_token', 'refresh_token', 'user_id', 'scope',
    'expira_en': datetime|None}. `user_id` es el id de la CUENTA en Mercado
    Libre, no el usuario de esta app.

    El redirect_uri viaja de nuevo aca aunque ya haya viajado en la
    autorizacion: MeLi lo compara contra el del grant y contra el del
    DevCenter, y si no coincide devuelve invalid_grant.
    """
    return _pedir_token({
        'grant_type': 'authorization_code',
        'client_id': _client_id(),
        'client_secret': _client_secret(),
        'code': code,
        'redirect_uri': redirect_uri,
    }, 'el intercambio del code')


def refrescar_token(refresh_token):
    """Canja el refresh_token por un access_token nuevo.

    Devuelve el MISMO dict que intercambiar_code, y el `refresh_token` que trae
    es uno NUEVO: el que se mando en la llamada quedo invalido en el acto. El
    que llama tiene la obligacion de guardar los dos juntos; guardar solo el
    access deja el canal sin forma de refrescarse la proxima vez.

    Ojo que aca NO va redirect_uri: la doc lo pide solo en el canje del code.
    """
    if not refresh_token:
        raise ErrorMercadoLibre(
            'El canal de Mercado Libre no tiene refresh_token guardado. Hay que '
            'volver a conectarlo.',
            detalle='refrescar_token() sin refresh_token',
            reconectar=True,
        )
    return _pedir_token({
        'grant_type': 'refresh_token',
        'client_id': _client_id(),
        'client_secret': _client_secret(),
        'refresh_token': refresh_token,
    }, 'el refresco del token')


def _vencimiento(expires_in):
    """utcnow + expires_in segundos, o None si Mercado Libre no lo dijo.

    Se calcula aca y no en el que llama: la referencia temporal correcta es el
    momento en que MeLi emitio el token, o sea ahora.
    """
    try:
        segundos = int(expires_in)
    except (TypeError, ValueError):
        return None
    if segundos <= 0:
        return None
    return datetime.utcnow() + timedelta(seconds=segundos)


def token_vencido(expira_en, margen=timedelta(minutes=5)):
    """True si el token ya vencio (o esta por vencer dentro del margen).

    El margen evita empezar una lectura de varias paginas con un token al que
    le quedan diez segundos. `expira_en` None significa "no se sabe" y NO se
    trata como vencido: la unica forma de saberlo es probando contra la API.
    """
    if expira_en is None:
        return False
    return expira_en <= datetime.utcnow() + margen


# ---------------------------------------------------------------------------
# Verificacion de la credencial
# ---------------------------------------------------------------------------
# El equivalente de `traer_tienda` de Tiendanube: la llamada mas barata que
# prueba que el token sirve de verdad. GET /users/me es el ejemplo que usa la
# propia doc de MeLi para mostrar como se manda el Authorization.

RUTA_USUARIO = 'users/me'

ESPERA_MAXIMA = 60.0
MAX_REINTENTOS_429 = 5

# La verificacion corre adentro del request que pinta la pagina de
# integraciones, asi que no puede usar el TIMEOUT de 20 segundos del resto: con
# Mercado Libre caido, la pagina entera quedaria colgada ese tiempo. Seis
# segundos alcanzan de sobra para un GET /users/me y acotan el peor caso.
TIMEOUT_VERIFICACION = 6


def _headers(access_token):
    return {'Authorization': 'Bearer %s' % access_token}


def _entero(valor):
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        return None


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


def _espera_tras_429(resp, intento):
    """Cuanto dormir despues de un 429 (`local_rate_limited` en la doc de MeLi,
    que dice "try again in a few seconds" sin dar un numero)."""
    retry_after = _entero(_header(resp, 'Retry-After'))
    if retry_after is not None and retry_after >= 0:
        return min(float(retry_after), ESPERA_MAXIMA)
    return min(2.0 ** intento, ESPERA_MAXIMA)


def _get_api(ruta, access_token, params=None, timeout=None):
    """GET a la API con reintentos por rate limit. Devuelve el Response crudo."""
    url = '%s/%s' % (URL_API, ruta)

    for intento in range(MAX_REINTENTOS_429 + 1):
        try:
            resp = requests.get(url, headers=_headers(access_token),
                                params=params, timeout=timeout or TIMEOUT)
        except requests.RequestException as exc:
            raise ErrorMercadoLibre(
                'No se pudo contactar a la API de Mercado Libre.',
                detalle='%s en GET %s: %s' % (type(exc).__name__, ruta, exc)
            ) from exc

        if resp.status_code != 429:
            return resp

        if intento >= MAX_REINTENTOS_429:
            break
        time.sleep(_espera_tras_429(resp, intento))

    raise ErrorMercadoLibre(
        'Mercado Libre esta limitando las consultas. Proba de nuevo en un rato.',
        detalle='429 persistente en GET %s tras %d reintentos' % (ruta, MAX_REINTENTOS_429)
    )


def traer_usuario(access_token, timeout=None):
    """GET /users/me. Prueba que el token sirve y trae con quien quedo atado.

    Levanta ErrorMercadoLibre con reconectar=True si el token vencio o lo
    revocaron (401/403), que es la unica falla que la persona puede arreglar
    sola.
    """
    resp = _get_api(RUTA_USUARIO, access_token, timeout=timeout)

    if resp.status_code in (401, 403):
        raise ErrorMercadoLibre(
            'El acceso a la cuenta de Mercado Libre vencio o fue revocado. '
            'Volve a conectar el canal.',
            detalle='HTTP %s en GET %s: %s' % (resp.status_code, RUTA_USUARIO, _recorte(resp)),
            reconectar=True,
        )

    if resp.status_code != 200:
        raise ErrorMercadoLibre(
            'Mercado Libre rechazo la consulta de la cuenta.',
            detalle='HTTP %s en GET %s: %s' % (resp.status_code, RUTA_USUARIO, _recorte(resp))
        )

    return _leer_json(resp, 'GET %s' % RUTA_USUARIO)


def verificar_credenciales(access_token, timeout=TIMEOUT_VERIFICACION):
    """"Este token, sirve ahora mismo?". Devuelve un dict, NUNCA levanta.

    {'conectado': bool, 'usuario': dict|None, 'motivo': str|None}

    Que no levante es el punto: esto lo llama la pagina de integraciones para
    pintar el estado del canal, y un token vencido es un estado normal del
    mundo -- no un error 500. La misma distincion que hace Tiendanube entre
    "no anda" y "se rompio", pero explicita, porque aca el token vence cada
    seis horas y ver el estado vencido va a ser rutina.
    """
    if not access_token:
        return {'conectado': False, 'usuario': None,
                'motivo': 'El canal no tiene credencial guardada.'}
    try:
        usuario = traer_usuario(access_token, timeout=timeout)
    except ErrorMercadoLibre as exc:
        return {'conectado': False, 'usuario': None, 'motivo': str(exc)}
    return {'conectado': True, 'usuario': usuario, 'motivo': None}


def apodo_de(usuario, por_defecto='Mercado Libre'):
    """El nickname de /users/me, para mostrar de que cuenta se trata."""
    apodo = (usuario or {}).get('nickname')
    if apodo:
        return str(apodo)[:100]
    return por_defecto
