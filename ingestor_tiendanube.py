# -*- coding: utf-8 -*-
"""IngestorCanal para Tiendanube: productos y pedidos (FASE3-S2).

Primera implementacion real del contrato de `ingestor_canal.py`. Cubre lo que
hace falta para el backfill manual:

    traer_productos()          catalogo crudo, paginado
    variantes_de_producto()    un producto de N variantes -> N crudos vendibles
    traer_pedidos()            pedidos crudos, paginados
    normalizar()               producto | pedido | item -> dict del modelo

`traer_pagos()` queda vacio a proposito: los pagos son de una slice posterior.

Este modulo NO toca la base ni decide que insertar: eso es `sync_tiendanube.py`.
normalizar() es puro (ni red ni base), asi que se testea con payloads guardados
y sin credenciales.

Regla que atraviesa todo el archivo: la API devuelve los importes como string
decimal ("1234.50"). Se parsean con Decimal(str(...)), nunca con float(): un
float intermedio ya perdio centavos antes de llegar a la base.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import integracion_tiendanube as tn
from ingestor_canal import (
    ENTIDAD_ITEM,
    ENTIDAD_PEDIDO,
    ENTIDAD_PRODUCTO,
    ErrorIngesta,
    IngestorCanal,
)

CERO = Decimal('0.00')

# Orden en que se busca un texto multi-idioma. Tiendanube devuelve
# {'es': 'Martillo', 'pt': ...} en name y description.
IDIOMAS = ('es', 'pt', 'en')

# Largos de las columnas del modelo. Cortar aca y no en la base: Postgres
# rechaza el INSERT entero por un nombre largo y voltearia el pedido completo.
LARGO_SKU = 60
LARGO_NOMBRE = 200
LARGO_ESTADO = 30
LARGO_ESTADO_EXTERNO = 50
LARGO_DESCRIPCION_ITEM = 200
LARGO_ID_EXTERNO = 100
LARGO_PERSONA = 150


def texto_idioma(valor, por_defecto=''):
    """Un campo que puede venir como str o como dict por idioma."""
    if isinstance(valor, dict):
        for idioma in IDIOMAS:
            if valor.get(idioma):
                return str(valor[idioma]).strip()
        for contenido in valor.values():
            if contenido:
                return str(contenido).strip()
        return por_defecto
    if valor is None:
        return por_defecto
    texto = str(valor).strip()
    return texto or por_defecto


def a_decimal(valor, por_defecto=CERO):
    """Importe de la API -> Decimal, sin pasar por float en el medio.

    Un None o un '' devuelven el default (0 para totales, None para precios
    opcionales, segun lo que pase el llamador).
    """
    if valor is None or valor == '':
        return por_defecto
    if isinstance(valor, Decimal):
        return valor
    if isinstance(valor, float):
        # No deberia pasar (la API manda strings), pero si un dia manda un
        # numero JSON, str() del float es mejor que Decimal(float): Decimal
        # del float arrastra los 17 digitos del binario.
        return Decimal(repr(valor))
    try:
        return Decimal(str(valor).strip())
    except (InvalidOperation, ValueError):
        return por_defecto


def a_entero(valor, por_defecto=0):
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        return por_defecto


def a_stock(variante):
    """Stock de una variante -> int, o None si el dato no existe.

    None y 0 NO son lo mismo y esta funcion existe para no confundirlos.
    Tiendanube manda dos campos por variante: `stock_management` (bool, si el
    producto lleva control de stock) y `stock` (int). Con el control apagado
    el stock no es un dato de la tienda y la API manda null; escribir 0 ahi
    seria afirmar "no queda ninguno", que es justo lo contrario de "nadie
    lleva la cuenta". Por eso este caso devuelve None y no cae en el default
    de a_entero(), que es 0.

    Un stock de verdad en cero (control prendido, ultima unidad vendida) si
    entra como 0: ese es un dato, y ademas el que deberia frenar una venta.
    """
    if variante.get('stock_management') is False:
        return None
    valor = variante.get('stock')
    # isinstance(True, int) es True en Python: un bool colado en el campo no
    # puede terminar guardado como 1 unidad.
    if valor is None or valor == '' or isinstance(valor, bool):
        return None
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        return None


def a_fecha_utc(valor):
    """ISO 8601 de la API -> datetime naive en UTC.

    El modelo guarda todo naive-UTC (asi lo dejo FASE2-S1). Tiendanube manda
    el offset del huso de la tienda ('2025-03-12T10:20:30-0300'), a veces con
    dos puntos y a veces sin, y a veces con 'Z'. Se contemplan las tres.
    """
    if valor is None or valor == '':
        return None
    if isinstance(valor, datetime):
        crudo = valor
    else:
        texto = str(valor).strip().replace('Z', '+00:00')
        # '+0000' -> '+00:00': fromisoformat de Python 3.10 no acepta el
        # offset compacto.
        if len(texto) >= 5 and texto[-5] in '+-' and texto[-3] != ':':
            texto = texto[:-2] + ':' + texto[-2:]
        try:
            crudo = datetime.fromisoformat(texto)
        except ValueError:
            return None

    if crudo.tzinfo is None:
        return crudo
    return crudo.astimezone(timezone.utc).replace(tzinfo=None)


def _cortar(texto, largo):
    if texto is None:
        return None
    return str(texto)[:largo]


def envio_del_cliente(crudo):
    """Lo que el comprador pago de envio, o None si Tiendanube no lo mando.

    El 2025/04/24 Tiendanube saco las propiedades de envio del recurso Order
    ("Removed deprecated shipping properties from the Order resource in favor
    of Fulfillment Order properties"). `shipping_cost_customer` ya no viene en
    los pedidos de una tienda migrada a multi-inventario: el monto vive ahora
    en cada fulfillment order, en shipping.consumer_cost.value.

    Un pedido puede partirse en varios despachos y cada uno cobra su envio, asi
    que los fulfillments se SUMAN. El campo viejo queda de respaldo para los
    payloads guardados antes del cambio.

    Devuelve None -no CERO- cuando el dato no esta en ningun formato conocido.
    None es "Tiendanube no lo mando"; CERO es "el envio salio gratis" (la doc
    del campo viejo dice textual que 0 es free shipping). Confundirlos hace que
    un pedido con envio caro parezca uno con envio bonificado.
    """
    total = CERO
    hubo_dato = False
    for fulfillment in (crudo.get('fulfillments') or []):
        # Sin ?aggregates=fulfillment_orders la API manda solo los ids (strings
        # sueltos): no hay nada que leer y se cae al campo viejo.
        if not isinstance(fulfillment, dict):
            continue
        envio = fulfillment.get('shipping')
        if not isinstance(envio, dict):
            continue
        costo = envio.get('consumer_cost')
        if not isinstance(costo, dict) or costo.get('value') is None:
            continue
        total += a_decimal(costo.get('value'))
        hubo_dato = True

    if hubo_dato:
        return total
    return a_decimal(crudo.get('shipping_cost_customer'), por_defecto=None)


class IngestorTiendanube(IngestorCanal):
    """Lee la tienda de Tiendanube que representa una fila de canal_venta."""

    tipo_canal = 'tiendanube'

    def __init__(self, canal_id, credenciales=None, id_tienda_externo=None):
        super().__init__(canal_id, credenciales, id_tienda_externo)
        if not self.id_tienda_externo:
            raise ErrorIngesta(
                'El canal de Tiendanube no tiene guardado el id de la tienda.',
                canal=self.tipo_canal
            )
        if not self.credenciales.get('access_token'):
            raise ErrorIngesta(
                'El canal de Tiendanube no tiene un access_token vigente.',
                canal=self.tipo_canal
            )

    @property
    def _token(self):
        return self.credenciales['access_token']

    # -- Lectura --------------------------------------------------------

    def traer_productos(self):
        try:
            return tn.traer_productos(self.id_tienda_externo, self._token)
        except tn.ErrorTiendanube as exc:
            raise self._como_error_ingesta(exc, ENTIDAD_PRODUCTO) from exc

    def traer_pedidos(self, desde=None, hasta=None):
        """Pedidos del rango. Sin rango, todo el historico.

        El contrato los declara obligatorios; aca son opcionales porque el
        backfill manual de esta slice trae todo y despues hace upsert, que es
        mas simple de razonar que un cursor y con este volumen igual de barato.
        """
        try:
            return tn.traer_pedidos(self.id_tienda_externo, self._token, desde, hasta)
        except tn.ErrorTiendanube as exc:
            raise self._como_error_ingesta(exc, ENTIDAD_PEDIDO) from exc

    def traer_pagos(self, desde=None, hasta=None):
        """Vacio: los pagos (y Tiendanube Pagos) son de una slice posterior.

        Se implementa igual porque el contrato la declara abstracta y sin
        cuerpo la clase no se puede instanciar.
        """
        return []

    def variantes_de_producto(self, crudo):
        """Un producto -> un crudo por variante vendible.

        Un producto sin variantes igual devuelve una: el SKU es el producto
        mismo, con id_variante_externo = '' (la convencion de FASE2-S1 para
        que la UNIQUE de mapeo_producto_canal no deje entrar duplicados; en
        Postgres un NULL no colisiona consigo mismo).
        """
        variantes = crudo.get('variants') or [{}]
        return [{'producto': crudo, 'variante': variante} for variante in variantes]

    # -- Mapeo ----------------------------------------------------------

    def normalizar(self, entidad, crudo):
        if entidad == ENTIDAD_PRODUCTO:
            return self._normalizar_producto(crudo)
        if entidad == ENTIDAD_PEDIDO:
            return self._normalizar_pedido(crudo)
        if entidad == ENTIDAD_ITEM:
            return self._normalizar_item(crudo)
        raise ErrorIngesta(
            'Entidad no soportada por el ingestor de Tiendanube: %s' % entidad,
            canal=self.tipo_canal, entidad=entidad
        )

    def _normalizar_producto(self, crudo):
        """Un par {'producto', 'variante'} de variantes_de_producto()."""
        producto = crudo.get('producto') or {}
        variante = crudo.get('variante') or {}

        id_producto = str(producto.get('id') or '')
        id_variante = str(variante.get('id') or '')

        sku_externo = texto_idioma(variante.get('sku'))
        # Sin SKU propio no hay identidad estable del lado del canal, asi que
        # se fabrica una a partir de los ids, que si son estables. Va con
        # prefijo TN- para que se vea de una que la puso el sync y no Roman.
        sku = sku_externo or 'TN-%s-%s' % (id_producto, id_variante or '0')

        return {
            'sku': _cortar(sku, LARGO_SKU),
            'nombre': _cortar(self._nombre_de_variante(producto, variante), LARGO_NOMBRE),
            'precio_lista': a_decimal(variante.get('price'), por_defecto=None),
            # Tiendanube NO expone un costo confiable: el campo `cost` de la
            # variante viene vacio salvo que el comerciante lo haya cargado en
            # el panel. costo_unitario queda NULL y lo carga Roman a mano; el
            # sync no lo pisa nunca, ni al crear ni al actualizar.
            'costo_unitario': None,
            # El stock si viene del canal y si se refresca en cada corrida
            # (ver a_stock: null del canal -> NULL en la base, nunca 0).
            'stock': a_stock(variante),
            'activo': bool(producto.get('published', True)),
            'id_producto_externo': _cortar(id_producto, LARGO_ID_EXTERNO),
            'id_variante_externo': _cortar(id_variante, LARGO_ID_EXTERNO) or '',
            'sku_externo': _cortar(sku_externo, LARGO_ID_EXTERNO) or None,
        }

    def _nombre_de_variante(self, producto, variante):
        """'Martillo' + los valores de la variante -> 'Martillo (500g / Azul)'."""
        base = texto_idioma(producto.get('name'), por_defecto='Producto sin nombre')
        etiquetas = []
        for valor in (variante.get('values') or []):
            etiqueta = texto_idioma(valor)
            if etiqueta:
                etiquetas.append(etiqueta)
        if not etiquetas:
            return base
        return '%s (%s)' % (base, ' / '.join(etiquetas))

    def _normalizar_pedido(self, crudo):
        """Un pedido crudo -> dict con los nombres de columna de Pedido.

        `estado` guarda el status de Tiendanube TAL CUAL viene ('open',
        'closed', 'cancelled'). No se traduce a un vocabulario propio en esta
        slice: traducir sin tener los pagos delante (FASE3-S3) es adivinar.
        `estado_externo` guarda el payment_status, que es el que va a hacer
        falta para conciliar.
        """
        cliente = crudo.get('customer') or {}

        subtotal = a_decimal(crudo.get('subtotal'))
        envio = envio_del_cliente(crudo)
        if envio is None:
            # Tiendanube no mando el costo de envio en ningun formato conocido.
            # `pedido.total_envio` es NOT NULL, asi que el 0 es lo unico que se
            # puede guardar, pero significa "no disponible", NO "envio gratis":
            # cuando pasa, subtotal - descuentos + envio no llega al `total` del
            # pedido y esa diferencia es el envio que falta.
            envio = CERO
        descuentos = a_decimal(crudo.get('discount'))
        # La tienda argentina factura con IVA incluido y la API no manda un
        # campo de impuestos en el pedido. Si algun dia lo manda, entra aca
        # sin tocar nada mas.
        impuestos = a_decimal(crudo.get('taxes'))
        total = a_decimal(crudo.get('total'))

        return {
            'id_externo': _cortar(str(crudo.get('id') or ''), LARGO_ID_EXTERNO),
            'numero_externo': _cortar(crudo.get('number'), 50),
            'fecha_pedido': a_fecha_utc(crudo.get('created_at')) or datetime.utcnow(),
            'estado': _cortar(texto_idioma(crudo.get('status'), 'pendiente'), LARGO_ESTADO),
            'estado_externo': _cortar(texto_idioma(crudo.get('payment_status')) or None,
                                      LARGO_ESTADO_EXTERNO),
            'moneda': (texto_idioma(crudo.get('currency'), 'ARS')[:3] or 'ARS').upper(),
            'comprador_nombre': _cortar(texto_idioma(cliente.get('name')) or None, LARGO_PERSONA),
            'comprador_email': _cortar(texto_idioma(cliente.get('email')
                                                    or crudo.get('contact_email')) or None,
                                       LARGO_PERSONA),
            'comprador_doc': _cortar(texto_idioma(cliente.get('identification')) or None, 30),
            'total_bruto': subtotal,
            'total_descuentos': descuentos,
            'total_envio': envio,
            'total_impuestos': impuestos,
            'total': total,
            # No hay tabla de clientes en este proyecto y `pedido` no tiene
            # columna cliente_ref: el id de cliente de Tiendanube queda
            # unicamente dentro de raw_payload (customer.id), disponible para
            # cuando exista donde ponerlo. No se inventa una columna aca.
            'cliente_ref': str(cliente.get('id')) if cliente.get('id') else None,
            'items': [self._normalizar_item(linea)
                      for linea in (crudo.get('products') or [])],
        }

    def _normalizar_item(self, crudo):
        """Una linea de pedido -> dict con los nombres de columna de PedidoItem.

        No resuelve producto_id: eso necesita la base (mapeo_producto_canal) y
        normalizar() esta documentado como puro. Devuelve los ids externos
        para que el sincronizador haga el lookup.
        """
        cantidad = a_entero(crudo.get('quantity'), 1)
        precio = a_decimal(crudo.get('price'))

        return {
            'descripcion': _cortar(texto_idioma(crudo.get('name'), 'Item sin descripcion'),
                                   LARGO_DESCRIPCION_ITEM),
            'sku_externo': _cortar(texto_idioma(crudo.get('sku')) or None, LARGO_ID_EXTERNO),
            'cantidad': cantidad,
            'precio_unitario': precio,
            'descuento_unitario': CERO,
            # La API no manda subtotal por linea; se calcula con Decimal, que
            # es exacto para precio * entero.
            'subtotal': precio * cantidad,
            'id_producto_externo': _cortar(str(crudo.get('product_id') or '') or None,
                                           LARGO_ID_EXTERNO),
            'id_variante_externo': _cortar(str(crudo.get('variant_id') or ''), LARGO_ID_EXTERNO) or '',
        }

    # -- Salud ----------------------------------------------------------

    def verificar_credenciales(self):
        try:
            tn.traer_tienda(self.id_tienda_externo, self._token)
            return True
        except tn.ErrorTiendanube:
            return False

    def _como_error_ingesta(self, exc, entidad):
        """ErrorTiendanube -> ErrorIngesta, que es lo que espera el contrato.

        Se marca reintentable cuando el detalle habla de rate limit o de red:
        el orquestador de FASE3-S4 va a querer distinguir "volve a intentar en
        un rato" de "el token no sirve mas".
        """
        detalle = (exc.detalle or '').lower()
        reintentable = any(pista in detalle for pista in
                           ('429', 'timeout', 'connection', '503', '502', '504'))
        error = ErrorIngesta(str(exc), canal=self.tipo_canal, entidad=entidad,
                             reintentable=reintentable)
        error.detalle = exc.detalle
        return error


def desde_canal(canal, access_token):
    """Arma el ingestor a partir de la fila de canal_venta y el token ya
    descifrado. El token NO se lee de la base aca: quien descifra es el
    llamador, para que este modulo no dependa de cripto ni del modelo."""
    return IngestorTiendanube(
        canal_id=canal.id,
        credenciales={'access_token': access_token},
        id_tienda_externo=canal.id_tienda_externo,
    )
