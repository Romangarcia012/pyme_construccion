"""Cliente HTTP de Tiendanube.

Tres capas, agregadas en tres slices:

  - FASE3-S1: el OAuth (intercambiar_code, traer_tienda).
  - FASE3-S2: la lectura del catalogo y de los pedidos (traer_productos,
    traer_pedidos), con paginacion y freno de rate limit.
  - FASE-STOCK-S1: la unica escritura (actualizar_stock_variante).

Ninguna funcion de este modulo toca la base: hablan con la API y devuelven
dicts crudos. Quien persiste es la ruta del callback (S1) o el sincronizador
`sync_tiendanube.py` (S2). El mapeo al modelo interno vive en
`ingestor_tiendanube.py`, que implementa el contrato IngestorCanal.
"""

import os
import time

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


# ============================================================================
# FASE3-S2 - Lectura del catalogo y de los pedidos
# ----------------------------------------------------------------------------
# Lo de arriba es el OAuth de FASE3-S1 y no se toco. Lo de aca abajo es el
# transporte que usa el ingestor: GET paginado, con freno de rate limit.
#
# Tiendanube limita con leaky bucket (balde de 40, drenaje de 2 req/s; x10 en
# los planes altos) y lo informa en tres headers:
#
#     x-rate-limit-limit      tamano del balde
#     x-rate-limit-remaining  cuanto falta para llenarlo
#     x-rate-limit-reset      milisegundos hasta vaciarlo del todo
#
# Los nombres llegan en minusculas; requests los expone case-insensitive, pero
# la busqueda de aca abajo tambien lo es para no depender de eso.
# ============================================================================

# Maximo que acepta la API. Con menos de 500 pedidos/mes, todo el historico de
# Roman entra en un punado de paginas.
POR_PAGINA = 200

# Cuando queda esto o menos en el balde, se frena antes de la proxima request
# en vez de esperar el 429. Con 2 alcanza: el balde drena 2 req/s.
UMBRAL_RATE_LIMIT = 2

# Espera maxima entre reintentos, para que un Retry-After absurdo no deje el
# thread colgado media hora.
ESPERA_MAXIMA = 60.0

MAX_REINTENTOS_429 = 5

# Corte de seguridad: si la paginacion nunca termina (API que ignora ?page=),
# esto evita un thread infinito consumiendo rate limit.
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
    """Cuanto dormir despues de un 429.

    Prioridad: Retry-After (segundos) > x-rate-limit-reset (milisegundos) >
    backoff exponencial. La doc de Tiendanube no promete Retry-After, pero si
    lo manda hay que respetarlo: es el unico dato real de cuando reabre.
    """
    retry_after = _entero(_header(resp, 'Retry-After'))
    if retry_after is not None and retry_after >= 0:
        return min(float(retry_after), ESPERA_MAXIMA)

    reset_ms = _entero(_header(resp, 'x-rate-limit-reset'))
    if reset_ms is not None and reset_ms > 0:
        return min(reset_ms / 1000.0, ESPERA_MAXIMA)

    return min(2.0 ** intento, ESPERA_MAXIMA)


def _frenar_si_el_balde_esta_lleno(resp):
    """Pausa preventiva cuando quedan pocas requests en el balde.

    Es mas barato dormir un segundo que comerse un 429 y reintentar: el 429
    igual gasta una request contra el limite.
    """
    restantes = _entero(_header(resp, 'x-rate-limit-remaining'))
    if restantes is None or restantes > UMBRAL_RATE_LIMIT:
        return 0.0

    reset_ms = _entero(_header(resp, 'x-rate-limit-reset'))
    # Si no dice cuanto falta, un segundo alcanza: drena 2 req/s.
    espera = min(reset_ms / 1000.0, ESPERA_MAXIMA) if reset_ms else 1.0
    if espera > 0:
        time.sleep(espera)
    return espera


def _get_api(ruta, access_token, params=None):
    """GET a la API con reintentos por rate limit.

    `ruta` va sin la version ni el host: '9876543/products'. Devuelve el
    Response crudo; interpretarlo es de quien llama.
    """
    url = '%s/%s' % (URL_API.format(version=VERSION_API), ruta)

    for intento in range(MAX_REINTENTOS_429 + 1):
        try:
            resp = requests.get(url, headers=_headers(access_token),
                                params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ErrorTiendanube(
                'No se pudo contactar a la API de Tiendanube.',
                detalle='%s en GET %s: %s' % (type(exc).__name__, ruta, exc)
            ) from exc

        if resp.status_code != 429:
            _frenar_si_el_balde_esta_lleno(resp)
            return resp

        if intento >= MAX_REINTENTOS_429:
            break
        time.sleep(_espera_tras_429(resp, intento))

    raise ErrorTiendanube(
        'Tiendanube esta limitando las consultas. Proba de nuevo en un rato.',
        detalle='429 persistente en GET %s tras %d reintentos' % (ruta, MAX_REINTENTOS_429)
    )


def _json_de_lista(resp, ruta):
    """El body de un listado, validado. 404 se trata como lista vacia: una
    tienda sin productos publicados responde asi."""
    if resp.status_code == 404:
        return []

    if resp.status_code != 200:
        raise ErrorTiendanube(
            'Tiendanube rechazo la consulta de datos.',
            detalle='HTTP %s en GET %s: %s' % (resp.status_code, ruta, _recorte(resp))
        )

    try:
        datos = resp.json()
    except ValueError as exc:
        raise ErrorTiendanube(
            'Tiendanube devolvio una respuesta que no se pudo interpretar.',
            detalle='respuesta no-JSON en GET %s: %s' % (ruta, _recorte(resp))
        ) from exc

    if not isinstance(datos, list):
        raise ErrorTiendanube(
            'Tiendanube devolvio una respuesta con un formato inesperado.',
            detalle='GET %s no devolvio una lista sino %s' % (ruta, type(datos).__name__)
        )
    return datos


def _hay_pagina_siguiente(resp, cantidad):
    """Si seguir pidiendo paginas.

    El header Link (RFC 5988) es la fuente autoritativa; cuando no viene, una
    pagina incompleta significa que era la ultima.
    """
    link = _header(resp, 'Link')
    if link:
        return 'rel="next"' in link or 'rel=next' in link
    return cantidad >= POR_PAGINA


def paginar(ruta, access_token, params=None):
    """Recorre un listado paginado y devuelve todos los items juntos.

    Quien llama no ve paginas ni cursores, como pide el contrato IngestorCanal.
    Devuelve una lista y no un generador a proposito: si el consumidor abandona
    a mitad de camino, un generador dejaria el balde de rate limit a medias sin
    que nadie se entere.
    """
    consulta = dict(params or {})
    consulta['per_page'] = POR_PAGINA

    items = []
    for pagina in range(1, MAX_PAGINAS + 1):
        consulta['page'] = pagina
        resp = _get_api(ruta, access_token, params=consulta)
        lote = _json_de_lista(resp, ruta)
        items.extend(lote)

        if not lote or not _hay_pagina_siguiente(resp, len(lote)):
            return items

    raise ErrorTiendanube(
        'La consulta a Tiendanube no termina nunca.',
        detalle='GET %s supero las %d paginas' % (ruta, MAX_PAGINAS)
    )


# Pide que cada pedido venga con sus fulfillment orders enteros en vez de solo
# los ids. Es lo unico que trae el costo de envio (shipping.consumer_cost).
AGREGADO_FULFILLMENT = 'fulfillment_orders'


def traer_productos(store_id, access_token):
    """Catalogo completo. Cada item trae su array `variants` adentro."""
    return paginar('%s/products' % store_id, access_token)


def traer_pedidos(store_id, access_token, desde=None, hasta=None):
    """Pedidos de la tienda, crudos.

    Sin rango se trae todo el historico: es un backfill manual y con este
    volumen (menos de 500 pedidos/mes) son unas pocas paginas. Filtrar por
    fecha es una optimizacion, no un requisito de correctitud: el upsert
    aguanta traer lo mismo dos veces.

    `aggregates=fulfillment_orders` no es opcional: sin el, `fulfillments`
    vuelve resumido y el costo de envio no viene en ningun lado del pedido.
    Tiendanube saco las propiedades de envio del recurso Order el 2025/04/24
    ("Removed deprecated shipping properties from the Order resource in favor
    of Fulfillment Order properties") y el monto quedo adentro de cada
    fulfillment order. Ver AGREGADO_FULFILLMENT.
    """
    params = {'aggregates': AGREGADO_FULFILLMENT}
    if desde is not None:
        params['updated_at_min'] = desde.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    if hasta is not None:
        params['updated_at_max'] = hasta.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    return paginar('%s/orders' % store_id, access_token, params=params or None)


# ============================================================================
# FASE-STOCK-S1 - Escritura de stock
# ----------------------------------------------------------------------------
# Lo primero que este modulo ESCRIBE del lado de Tiendanube. Hasta esta slice
# todo era GET; el permiso "Edit Products" que Roman agrego a la app habilita
# este unico verbo y nada mas: no se tocan pedidos ni clientes aunque el token
# alcance para hacerlo.
#
# El endpoint es el de la variante puntual:
#
#     PUT /{store_id}/products/{product_id}/variants/{variant_id}
#     {"stock": 7}
#
# (doc: tiendanube.github.io/api-documentation/resources/product-variant)
#
# Se manda SOLO `stock`. El PUT de una variante acepta muchos campos mas
# (price, sku, weight, cost) y mandarlos seria pisar con datos nuestros lo que
# el comerciante tenga cargado en la tienda. `stock_management` ni siquiera es
# escribible por API: lo maneja Tiendanube.
#
# Sobre multi-inventario: en las tiendas con varias sucursales el stock real
# vive en `inventory_levels` y `variant.stock` figura como deprecado, pero la
# doc aclara que se sigue soportando y que un PUT con `stock` actualiza el
# primer inventory_level de la variante. Con una sola sucursal -- el caso de
# Roman -- eso es exactamente el stock de la tienda. Si algun dia se abre un
# segundo deposito, esta funcion hay que rehacerla contra inventory_levels.
# ============================================================================

def _put_api(ruta, access_token, cuerpo):
    """PUT a la API con la misma politica de rate limit que _get_api.

    Devuelve el Response crudo. Se reintenta solo el 429: un PUT de stock es
    idempotente (manda un valor absoluto, no un delta), asi que repetirlo no
    puede descontar dos veces.
    """
    url = '%s/%s' % (URL_API.format(version=VERSION_API), ruta)

    for intento in range(MAX_REINTENTOS_429 + 1):
        try:
            resp = requests.put(url, headers=_headers(access_token),
                                json=cuerpo, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ErrorTiendanube(
                'No se pudo contactar a la API de Tiendanube.',
                detalle='%s en PUT %s: %s' % (type(exc).__name__, ruta, exc)
            ) from exc

        if resp.status_code != 429:
            _frenar_si_el_balde_esta_lleno(resp)
            return resp

        if intento >= MAX_REINTENTOS_429:
            break
        time.sleep(_espera_tras_429(resp, intento))

    raise ErrorTiendanube(
        'Tiendanube esta limitando las consultas. Proba de nuevo en un rato.',
        detalle='429 persistente en PUT %s tras %d reintentos' % (ruta, MAX_REINTENTOS_429)
    )


def actualizar_stock_variante(store_id, id_producto, id_variante, stock, access_token):
    """Deja el stock de una variante en `stock`. Devuelve el body de la respuesta.

    `stock` es el valor final, no un descuento: si la llamada se repite el
    resultado es el mismo.

    Los mensajes de error estan separados por causa porque cada uno se arregla
    distinto: el 401/403 es "falta el permiso, reconecta la tienda" y el 404 es
    "ese producto ya no existe en Tiendanube, revisa el mapeo".
    """
    ruta = '%s/products/%s/variants/%s' % (store_id, id_producto, id_variante)
    resp = _put_api(ruta, access_token, {'stock': int(stock)})

    if resp.status_code in (401, 403):
        raise ErrorTiendanube(
            'Tiendanube no autoriza a modificar el stock: a la app le falta el '
            'permiso para editar productos. Volve a conectar la tienda.',
            detalle='HTTP %s en PUT %s: %s' % (resp.status_code, ruta, _recorte(resp))
        )

    if resp.status_code == 404:
        raise ErrorTiendanube(
            'Tiendanube no encontro ese producto. Puede que lo hayan borrado de '
            'la tienda; resincronizá el catálogo.',
            detalle='HTTP 404 en PUT %s: %s' % (ruta, _recorte(resp))
        )

    if resp.status_code not in (200, 201):
        raise ErrorTiendanube(
            'Tiendanube rechazó la actualización de stock.',
            detalle='HTTP %s en PUT %s: %s' % (resp.status_code, ruta, _recorte(resp))
        )

    try:
        return resp.json()
    except ValueError:
        # El stock ya quedo escrito; que el body no sea JSON no lo deshace.
        return {}
