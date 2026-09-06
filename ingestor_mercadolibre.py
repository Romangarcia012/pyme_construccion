# -*- coding: utf-8 -*-
"""IngestorCanal para Mercado Libre: catalogo (FASE-MELI-S2).

Segunda implementacion real del contrato de `ingestor_canal.py`, hermana de
`ingestor_tiendanube.py`. Cubre lo que hace falta para el backfill de catalogo:

    traer_productos()          publicaciones crudas (search + bulk), ya paginadas
    variantes_de_producto()    un item de N variations -> N crudos vendibles
    normalizar()               producto -> dict con los nombres de columna

`traer_pedidos()` y `traer_pagos()` devuelven vacio a proposito: los pedidos,
el envio y la comision son S3. Se implementan igual porque el contrato las
declara abstractas y sin cuerpo la clase no se puede instanciar.

Este modulo NO toca la base ni decide que insertar: eso es `sync_mercadolibre.py`.
normalizar() es puro (ni red ni base), asi que se testea con payloads guardados
y sin credenciales.

TRES COSAS QUE MERCADO LIBRE HACE DISTINTO DE TIENDANUBE

  1. El SKU no es un campo, es un ATRIBUTO. No existe `item['sku']`: hay que
     buscarlo adentro de `attributes` (o de `variations[].attributes`) por
     `id == 'SELLER_SKU'`. Ver `sku_de` para por que ese y no
     `seller_custom_field`.

  2. Los importes vienen como NUMEROS JSON, no como strings. Tiendanube manda
     "1234.50" y aca llega 1234.5. Por eso el transporte
     (`integracion_mercadolibre._leer_json` y `_json_de_bulk`) parsea con
     `parse_float=Decimal`: para cuando el payload llega hasta aca, los precios
     YA son Decimal y no hubo ningun float en el medio. `a_decimal` de este
     modulo es la segunda linea de defensa, para payloads guardados o de test
     que hayan pasado por un `json.loads` comun.

  3. El stock puede no existir. Una publicacion con logistica Full (los
     productos que estan en un deposito de Mercado Libre) no lleva el stock
     vendible en `available_quantity`, y una cuenta sin permiso de stock lo
     recibe rangueado ('RANGO_1_50') en vez de como numero. Los dos casos
     terminan en NULL y no en 0; ver `a_stock`.
"""

from decimal import Decimal, InvalidOperation

import integracion_mercadolibre as meli
from ingestor_canal import (
    ENTIDAD_PRODUCTO,
    ErrorIngesta,
    IngestorCanal,
)

CERO = Decimal('0.00')

# Largos de las columnas del modelo, iguales a los de Tiendanube porque son las
# mismas columnas. Cortar aca y no en la base: Postgres rechaza el INSERT
# entero por un nombre largo y voltearia el producto completo.
LARGO_SKU = 60
LARGO_NOMBRE = 200
LARGO_ID_EXTERNO = 100

# El atributo donde Mercado Libre guarda el codigo del vendedor. Es el que la
# API expone como tal, el que se ve en el panel de publicaciones y el que viaja
# en las lineas de los pedidos (S3). NO es `seller_custom_field`: ese es un
# campo top-level heredado del sistema viejo, esta deprecado, viene en null en
# las publicaciones nuevas y en las viejas puede tener cualquier cosa que el
# vendedor haya escrito antes de que existiera SELLER_SKU. Leer el campo viejo
# haria que el catalogo se sincronice con SKUs vacios sin que nada falle.
ATRIBUTO_SKU = 'SELLER_SKU'

# `logistic_type` cuando la publicacion esta en un deposito de Mercado Libre
# (Full). Ver `a_stock`.
LOGISTICA_FULL = 'fulfillment'

# Estados de una publicacion que cuentan como "activa" en el catalogo interno.
# `paused` no entra: una publicacion pausada no se puede comprar hoy. `closed`
# y `under_review` tampoco. La fila del producto sigue existiendo igual -- se
# le vendio en el pasado y su margen se sigue calculando --, solo que marcada
# inactiva.
ESTADOS_ACTIVOS = ('active',)


def texto(valor, por_defecto=''):
    """Un campo de texto de la API -> str limpio, o el default."""
    if valor is None:
        return por_defecto
    limpio = str(valor).strip()
    return limpio or por_defecto


def a_decimal(valor, por_defecto=CERO):
    """Importe de la API -> Decimal, sin pasar por float en el medio.

    Para un payload que vino por el transporte del modulo esto es casi un
    no-op: `parse_float=Decimal` ya lo convirtio. Existe igual porque
    normalizar() esta documentado como puro y testeable con payloads guardados,
    y un payload leido de un archivo con `json.load()` a secas trae floats.

    El float se convierte con `repr()` y no con `Decimal(float)`: `Decimal` del
    float arrastra los 17 digitos del binario (Decimal(1234.5) da
    1234.5000000000000000, pero Decimal(0.1) da 0.1000000000000000055511...).
    Es un rescate, no una garantia: el valor exacto ya se perdio antes.
    """
    if valor is None or valor == '':
        return por_defecto
    if isinstance(valor, Decimal):
        return valor
    if isinstance(valor, float):
        return Decimal(repr(valor))
    try:
        return Decimal(str(valor).strip())
    except (InvalidOperation, ValueError):
        return por_defecto


def _atributos(nodo):
    """La lista `attributes` de un item o de una variacion, siempre iterable."""
    atributos = (nodo or {}).get('attributes')
    return atributos if isinstance(atributos, list) else []


def _valor_de_atributo(nodo, id_atributo):
    """El valor de un atributo por id, o None.

    Mercado Libre manda el mismo dato en dos claves: `value_name` (el texto
    lindo) y `values[0].name`. Se prueban las dos porque no siempre vienen las
    dos -- en las variaciones el `value_name` a veces llega vacio y el dato
    real esta adentro de `values`.
    """
    for atributo in _atributos(nodo):
        if not isinstance(atributo, dict):
            continue
        if texto(atributo.get('id')).upper() != id_atributo:
            continue

        directo = texto(atributo.get('value_name'))
        if directo:
            return directo

        for valor in (atributo.get('values') or []):
            if not isinstance(valor, dict):
                continue
            nombre = texto(valor.get('name'))
            if nombre:
                return nombre
    return None


def sku_de(item, variacion, id_item, id_variacion):
    """El SKU del vendedor, o uno fabricado con los ids.

    Se busca el atributo SELLER_SKU primero en la VARIACION y despues en el
    item: un item con variaciones puede tener un SKU por variacion (rojo-M,
    rojo-L), y el del item -- si existe -- es el del producto entero. Tomar el
    del item cuando la variacion tiene el suyo juntaria dos SKUs distintos en
    una sola fila de producto y arruinaria el margen de los dos.

    Sin SELLER_SKU no hay identidad estable del lado del canal, asi que se
    fabrica una con los ids, que si son estables. Va con prefijo ML- para que
    se vea de una que la puso el sync y no Roman, exactamente igual que el
    TN-{prod}-{var} de Tiendanube.

    El fallback usa '0' cuando no hay variacion, no cadena vacia: 'ML-123-' se
    lee como un SKU truncado y 'ML-123-0' como uno completo.
    """
    propio = _valor_de_atributo(variacion, ATRIBUTO_SKU)
    if propio:
        return propio, propio
    propio = _valor_de_atributo(item, ATRIBUTO_SKU)
    if propio:
        return propio, propio
    return 'ML-%s-%s' % (id_item, id_variacion or '0'), None


def a_stock(item, variacion):
    """Stock vendible -> int, o None si el dato no existe.

    None y 0 NO son lo mismo y esta funcion existe para no confundirlos, igual
    que `ingestor_tiendanube.a_stock`. Aca hay dos motivos distintos por los
    que el numero puede no existir:

    FULL (logistic_type == 'fulfillment'). El stock de una publicacion Full
    vive en el deposito de Mercado Libre, no en `available_quantity`: ese campo
    puede venir en 0, desactualizado, o reflejar solo lo que le queda al
    vendedor en su propio deposito. Como el dato real esta en otro lado (la API
    de stock de fulfillment, que esta slice no consulta), la unica respuesta
    honesta es NULL: "no lo controlamos desde aca". Un 0 afirmaria que no queda
    ninguno y frenaria ventas de un producto que tiene stock de sobra en el
    deposito de Mercado Libre. Es el mismo criterio que se uso con el Combo de
    Tarjeteros, que tampoco tiene stock propio.

    RANGUEADO. Sin permiso de lectura de stock, Mercado Libre devuelve un rango
    ('RANGO_1_50') en vez de un numero. Un rango no es un stock: no se puede
    descontar ni comparar. Cae en None por el mismo motivo -- no hay dato
    utilizable -- y no en un promedio inventado.

    Un stock de verdad en cero (publicacion propia, ultima unidad vendida) si
    entra como 0: ese es un dato, y ademas el que deberia frenar una venta.
    """
    if texto((item or {}).get('logistic_type')).lower() == LOGISTICA_FULL:
        return None

    # En un item con variaciones el stock es por variacion; el del item es la
    # suma y repartirla entre las variaciones seria inventarla.
    origen = variacion if variacion else item
    valor = (origen or {}).get('available_quantity')

    # isinstance(True, int) es True en Python: un bool colado en el campo no
    # puede terminar guardado como 1 unidad.
    if valor is None or valor == '' or isinstance(valor, bool):
        return None
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        # Aca cae el rangueado ('RANGO_1_50') y cualquier otro texto.
        return None


def _cortar(valor, largo):
    if valor is None:
        return None
    return str(valor)[:largo]


class IngestorMercadoLibre(IngestorCanal):
    """Lee la cuenta de Mercado Libre que representa una fila de canal_venta."""

    tipo_canal = 'mercadolibre'

    def __init__(self, canal_id, credenciales=None, id_tienda_externo=None):
        super().__init__(canal_id, credenciales, id_tienda_externo)
        # id_tienda_externo es el user_id de Mercado Libre: va en la URL de
        # /users/{id}/items/search y sin el no hay catalogo que traer.
        if not self.id_tienda_externo:
            raise ErrorIngesta(
                'El canal de Mercado Libre no tiene guardado el id de la cuenta.',
                canal=self.tipo_canal
            )
        if not self.credenciales.get('access_token'):
            raise ErrorIngesta(
                'El canal de Mercado Libre no tiene un access_token vigente.',
                canal=self.tipo_canal
            )

    @property
    def _token(self):
        return self.credenciales['access_token']

    # -- Lectura --------------------------------------------------------

    def traer_productos(self):
        try:
            return meli.traer_catalogo(self.id_tienda_externo, self._token)
        except meli.ErrorMercadoLibre as exc:
            raise self._como_error_ingesta(exc, ENTIDAD_PRODUCTO) from exc

    def traer_pedidos(self, desde=None, hasta=None):
        """Vacio: los pedidos son S3."""
        return []

    def traer_pagos(self, desde=None, hasta=None):
        """Vacio: los pagos y la comision son S3."""
        return []

    def variantes_de_producto(self, crudo):
        """Un item -> un crudo por variacion vendible.

        Un item sin variaciones igual devuelve una: el SKU es el item mismo,
        con id_variante_externo = '' (la convencion de FASE2-S1 para que la
        UNIQUE de mapeo_producto_canal no deje entrar duplicados; en Postgres
        un NULL no colisiona consigo mismo).

        Misma forma de salida que el de Tiendanube -- {'producto', 'variante'}
        -- para que `sync_mercadolibre` y `sync_tiendanube` puedan compartir el
        upsert sin que ninguno sepa de que canal viene el par.
        """
        variaciones = crudo.get('variations')
        if not isinstance(variaciones, list) or not variaciones:
            variaciones = [{}]
        return [{'producto': crudo, 'variante': variacion}
                for variacion in variaciones]

    # -- Mapeo ----------------------------------------------------------

    def normalizar(self, entidad, crudo):
        if entidad == ENTIDAD_PRODUCTO:
            return self._normalizar_producto(crudo)
        raise ErrorIngesta(
            'Entidad no soportada por el ingestor de Mercado Libre: %s' % entidad,
            canal=self.tipo_canal, entidad=entidad
        )

    def _normalizar_producto(self, crudo):
        """Un par {'producto', 'variante'} de variantes_de_producto()."""
        item = crudo.get('producto') or {}
        variacion = crudo.get('variante') or {}

        id_item = texto(item.get('id'))
        id_variacion = texto(variacion.get('id'))
        sku, sku_externo = sku_de(item, variacion, id_item, id_variacion)

        return {
            'sku': _cortar(sku, LARGO_SKU),
            'nombre': _cortar(self._nombre_de_variacion(item, variacion), LARGO_NOMBRE),
            # El precio de la variacion si la variacion trae el suyo; si no, el
            # del item. Se guarda TAL CUAL lo da la API.
            #
            # Ojo que este no siempre es el precio que ve un comprador en la
            # vidriera: Mercado Libre puede mostrar otro (tipicamente el mas
            # alto de las variaciones, o uno con recargo por cuotas sin
            # interes) y el item tiene ademas `original_price` y
            # `base_price`, que son otras dos cosas. El que se guarda es
            # `price`, que es lo que la cuenta cobra por esa variacion. La
            # discrepancia con la vidriera es real y queda documentada aca en
            # vez de tapada con una correccion inventada: corregirla sin datos
            # de la promocion daria un numero que no es el de ninguno de los
            # dos lados.
            'precio_lista': self._precio(item, variacion),
            # costo_unitario queda NULL a proposito: Mercado Libre no sabe
            # cuanto costo la mercaderia. Lo carga Roman a mano y el sync no lo
            # pisa nunca, ni al crear ni al actualizar. Mismo criterio que
            # Tiendanube.
            'costo_unitario': None,
            # El stock si viene del canal y si se refresca en cada corrida
            # (ver a_stock: Full o rangueado -> NULL, nunca 0).
            'stock': a_stock(item, variacion),
            'activo': texto(item.get('status')).lower() in ESTADOS_ACTIVOS,
            'id_producto_externo': _cortar(id_item, LARGO_ID_EXTERNO),
            'id_variante_externo': _cortar(id_variacion, LARGO_ID_EXTERNO) or '',
            'sku_externo': _cortar(sku_externo, LARGO_ID_EXTERNO) or None,
        }

    def _precio(self, item, variacion):
        """El precio de la variacion, o el del item si la variacion no trae."""
        if variacion and variacion.get('price') is not None:
            return a_decimal(variacion.get('price'), por_defecto=None)
        return a_decimal(item.get('price'), por_defecto=None)

    def _nombre_de_variacion(self, item, variacion):
        """'Guantes' + los atributos de la variacion -> 'Guantes (Rojo / M)'.

        Los valores salen de `attribute_combinations`, que es donde Mercado
        Libre pone lo que distingue a una variacion de otra (color, talle). Es
        el equivalente del array `values` de Tiendanube.
        """
        base = texto(item.get('title'), por_defecto='Producto sin nombre')

        etiquetas = []
        for combinacion in (variacion.get('attribute_combinations') or []):
            if not isinstance(combinacion, dict):
                continue
            etiqueta = texto(combinacion.get('value_name'))
            if not etiqueta:
                for valor in (combinacion.get('values') or []):
                    if isinstance(valor, dict):
                        etiqueta = texto(valor.get('name'))
                        if etiqueta:
                            break
            if etiqueta:
                etiquetas.append(etiqueta)

        if not etiquetas:
            return base
        return '%s (%s)' % (base, ' / '.join(etiquetas))

    # -- Salud ----------------------------------------------------------

    def verificar_credenciales(self):
        try:
            meli.traer_usuario(self._token)
            return True
        except meli.ErrorMercadoLibre:
            return False

    def _como_error_ingesta(self, exc, entidad):
        """ErrorMercadoLibre -> ErrorIngesta, que es lo que espera el contrato.

        Se marca reintentable cuando el detalle habla de rate limit o de red,
        igual que en Tiendanube. La diferencia es `reconectar`: cuando viene en
        True, el problema es la autorizacion y reintentar no lo arregla, asi
        que gana sobre cualquier pista de red que pueda haber en el detalle.
        """
        detalle = (exc.detalle or '').lower()
        reintentable = (not exc.reconectar) and any(
            pista in detalle for pista in
            ('429', 'timeout', 'connection', '503', '502', '504'))
        error = ErrorIngesta(str(exc), canal=self.tipo_canal, entidad=entidad,
                             reintentable=reintentable)
        error.detalle = exc.detalle
        error.reconectar = exc.reconectar
        return error


def desde_canal(canal, access_token):
    """Arma el ingestor a partir de la fila de canal_venta y el token ya
    descifrado. El token NO se lee de la base aca: quien descifra es el
    llamador, para que este modulo no dependa de cripto ni del modelo."""
    return IngestorMercadoLibre(
        canal_id=canal.id,
        credenciales={'access_token': access_token},
        id_tienda_externo=canal.id_tienda_externo,
    )
