"""Cliente HTTP de Tiendanube: solo lo que hace falta para autenticar.

FASE3-S1 es unicamente el OAuth. Aca no hay pedidos ni productos: eso es
FASE3-S2, y va a implementar IngestorCanal (ver ingestor_canal.py) usando el
token que esta slice deja guardado.

Todas las funciones son puras respecto de la base: hablan con la API y
devuelven dicts. Quien persiste es la ruta del callback, recien cuando las
tres llamadas salieron bien, para no dejar un canal medio conectado.
"""

import os

import requests

# El App ID no es secreto (viaja en la URL de autorizacion que ve el usuario),
# asi que puede quedar en el repo. El Client Secret NO: solo por entorno.
APP_ID = os.environ.get('TIENDANUBE_APP_ID', '40970')

VARIABLE_SECRET = 'TIENDANUBE_CLIENT_SECRET'

URL_AUTORIZAR = 'https://www.tiendanube.com/apps/{app_id}/authorize'
URL_TOKEN = 'https://www.tiendanube.com/apps/authorize/token'
URL_API = 'https://api.tiendanube.com/{version}'

VERSION_API = '2025-03'

# Tiendanube rechaza (o limita) las llamadas sin User-Agent identificable.
# Va en TODAS las llamadas, incluido el intercambio del code.
USER_AGENT = os.environ.get(
    'TIENDANUBE_USER_AGENT',
    'GestionContableInterna (romangcia0@gmail.com)'
)

TIMEOUT = 20


class ErrorTiendanube(Exception):
    """Falla hablando con Tiendanube: red, credenciales o respuesta rara.

    Lleva un mensaje en castellano apto para mostrarle al usuario; el detalle
    tecnico queda en `detalle` para el log.
    """

    def __init__(self, mensaje, detalle=None):
        super().__init__(mensaje)
        self.detalle = detalle


def _client_secret():
    secret = os.environ.get(VARIABLE_SECRET)
    if not secret:
        raise ErrorTiendanube(
            'La integracion con Tiendanube no esta configurada en el servidor.',
            detalle=f'{VARIABLE_SECRET} no esta definida'
        )
    return secret


def _headers(access_token=None):
    cabeceras = {
        'User-Agent': USER_AGENT,
        'Content-Type': 'application/json',
    }
    if access_token:
        # Tiendanube usa el header no estandar "Authentication", no "Authorization".
        cabeceras['Authentication'] = f'bearer {access_token}'
    return cabeceras


def url_autorizacion():
    """URL del consentimiento. El usuario la abre, aprueba, y Tiendanube
    redirige al redirect_uri configurado en el panel de Partners con ?code=."""
    return URL_AUTORIZAR.format(app_id=APP_ID)


def intercambiar_code(code):
    """Canjea el ?code= del callback por un access_token permanente.

    Devuelve {'access_token': str, 'user_id': str, 'scope': str|None}.
    `user_id` es el id de la TIENDA en Tiendanube, no el usuario de esta app.
    """
    cuerpo = {
        'client_id': str(APP_ID),
        'client_secret': _client_secret(),
        'grant_type': 'authorization_code',
        'code': code,
    }
    try:
        # La doc oficial muestra el body como JSON.
        resp = requests.post(URL_TOKEN, json=cuerpo, headers=_headers(), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ErrorTiendanube(
            'No se pudo contactar a Tiendanube para validar la autorizacion.',
            detalle=str(exc)
        ) from exc

    if resp.status_code != 200:
        raise ErrorTiendanube(
            'Tiendanube rechazo la autorizacion. Proba conectar de nuevo.',
            detalle=f'HTTP {resp.status_code} en el intercambio del code: {_recorte(resp)}'
        )

    try:
        datos = resp.json()
    except ValueError as exc:
        raise ErrorTiendanube(
            'Tiendanube devolvio una respuesta que no se pudo interpretar.',
            detalle=f'respuesta no-JSON en el intercambio del code: {_recorte(resp)}'
        ) from exc

    # Tiendanube puede responder 200 con {"error": ...} ante un code invalido.
    if datos.get('error'):
        raise ErrorTiendanube(
            'Tiendanube rechazo la autorizacion. Proba conectar de nuevo.',
            detalle=f"error en el intercambio del code: {datos.get('error')}"
        )

    access_token = datos.get('access_token')
    user_id = datos.get('user_id')
    if not access_token or not user_id:
        raise ErrorTiendanube(
            'Tiendanube no devolvio un token valido.',
            detalle=f'faltan access_token o user_id en la respuesta: {sorted(datos.keys())}'
        )

    return {
        'access_token': access_token,
        'user_id': str(user_id),
        'scope': datos.get('scope'),
    }


def traer_tienda(user_id, access_token):
    """GET /{user_id}/store. Sirve de doble proposito: confirma que el token
    funciona de verdad y trae el nombre de la tienda para mostrar en la UI."""
    url = f'{URL_API.format(version=VERSION_API)}/{user_id}/store'
    try:
        resp = requests.get(url, headers=_headers(access_token), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ErrorTiendanube(
            'No se pudo contactar a la API de Tiendanube.',
            detalle=str(exc)
        ) from exc

    if resp.status_code != 200:
        raise ErrorTiendanube(
            'El token que devolvio Tiendanube no sirve para leer la tienda.',
            detalle=f'HTTP {resp.status_code} en GET /store: {_recorte(resp)}'
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise ErrorTiendanube(
            'Tiendanube devolvio una respuesta que no se pudo interpretar.',
            detalle=f'respuesta no-JSON en GET /store: {_recorte(resp)}'
        ) from exc


def nombre_de_tienda(store, por_defecto='Tiendanube'):
    """El campo `name` de /store viene como dict por idioma ({'es': 'Mi
    Tienda'}), pero no siempre. Se contemplan las dos formas."""
    nombre = (store or {}).get('name')
    if isinstance(nombre, dict):
        for idioma in ('es', 'pt', 'en'):
            if nombre.get(idioma):
                return str(nombre[idioma])[:100]
        for valor in nombre.values():
            if valor:
                return str(valor)[:100]
        return por_defecto
    if nombre:
        return str(nombre)[:100]
    return por_defecto


def _recorte(resp, largo=300):
    """Primeros caracteres del body, para el log. Nunca se muestra al usuario:
    puede traer eco de datos del request."""
    try:
        return resp.text[:largo]
    except Exception:
        return '<sin cuerpo>'
