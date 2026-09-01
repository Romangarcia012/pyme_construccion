"""Contrato de ingesta de canales de venta.

FASE2-S1 definio el contrato. FASE3-S2 escribio la primera implementacion
(`ingestor_tiendanube.py`, productos y pedidos) y en el camino lo extendio en
tres puntos, marcados abajo con "FASE3-S2":

    - ENTIDAD_ITEM, para las lineas de pedido.
    - IngestorCanal.id_tienda_externo, que antes habia que colar en
      `credenciales`.
    - variantes_de_producto(), porque un producto del canal puede ser N SKUs.

Los pagos y las liquidaciones siguen sin implementacion: eso es FASE3-S3.

La idea es que cada canal (Tiendanube, Mercado Libre) y cada procesador de
cobro (Mercado Pago, Tiendanube Pagos) exponga la misma forma, para que el
resto del sistema no sepa con quien esta hablando:

    for ingestor in ingestores_activos():
        crudos = ingestor.traer_pedidos(desde, hasta)
        for crudo in crudos:
            pedido = ingestor.normalizar('pedido', crudo)
            ...

Con menos de 500 pedidos/mes la ingesta es por polling, no por webhooks:
cada corrida deja una fila en sync_log con el cursor usado, y la proxima
corrida arranca de ahi. Por eso los metodos toman un rango de fechas
explicito y son idempotentes: volver a traer el mismo rango no debe
duplicar nada (de eso se encargan las UNIQUE de pedido, pago y
movimiento_cuenta.hash_dedup).

Los montos SIEMPRE viajan como Decimal, nunca como float. Un ingestor que
devuelva float rompe la conciliacion aguas abajo.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional


# Valores que normalizar() acepta en el parametro `entidad`.
ENTIDAD_PEDIDO = 'pedido'
ENTIDAD_PAGO = 'pago'
ENTIDAD_LIQUIDACION = 'liquidacion'
ENTIDAD_MOVIMIENTO = 'movimiento'
ENTIDAD_PRODUCTO = 'producto'
# FASE3-S2: las lineas del pedido tambien necesitan normalizarse (cantidad y
# precio_unitario son Decimal, y el sku externo hay que resolverlo contra
# mapeo_producto_canal). Sin esta entidad, el mapeo de items terminaba
# escondido dentro del de 'pedido' y no se podia testear por separado.
ENTIDAD_ITEM = 'item'

ENTIDADES_VALIDAS = (
    ENTIDAD_PEDIDO,
    ENTIDAD_PAGO,
    ENTIDAD_LIQUIDACION,
    ENTIDAD_MOVIMIENTO,
    ENTIDAD_PRODUCTO,
    ENTIDAD_ITEM,
)


class ErrorIngesta(Exception):
    """Falla al hablar con la API del canal (red, auth, rate limit, formato).

    El ingestor la levanta; el orquestador de Fase 3 la atrapa, escribe
    sync_log con estado='error' y sigue con el proximo canal en vez de
    voltear toda la corrida.
    """

    def __init__(self, mensaje, canal=None, entidad=None, reintentable=False):
        super().__init__(mensaje)
        self.canal = canal
        self.entidad = entidad
        # True cuando reintentar mas tarde tiene sentido (429, 503, timeout).
        self.reintentable = reintentable


class IngestorCanal(ABC):
    """Contrato que Fase 3 tiene que completar, uno por canal.

    Implementaciones previstas:
        - IngestorTiendanube  (pedidos + Tiendanube Pagos)
        - IngestorMercadoLibre (pedidos + Mercado Pago)

    Convenciones que toda implementacion debe respetar:

    1. Los metodos traer_* devuelven los payloads CRUDOS de la API, sin
       tocar. La traduccion al modelo interno pasa solo por normalizar().
       Asi el payload original se puede guardar en raw_payload y reprocesar
       sin volver a pegarle a la API.

    2. Son idempotentes: pedir dos veces el mismo rango devuelve lo mismo y
       no genera duplicados al persistir.

    3. Paginan internamente. El que llama recibe un iterable completo y no
       se entera de cursores ni de page tokens.

    4. Todo importe en el dict que devuelve normalizar() es Decimal.

    5. Las fechas van en UTC, timezone-naive, igual que el resto del modelo.
    """

    #: 'tiendanube' | 'mercadolibre'. Debe coincidir con canal_venta.tipo.
    tipo_canal: str = ''

    def __init__(self, canal_id: int, credenciales: Optional[Dict[str, Any]] = None,
                 id_tienda_externo: Optional[str] = None):
        #: id de la fila en canal_venta que este ingestor representa.
        self.canal_id = canal_id
        #: tokens ya descifrados. Nunca se loguean.
        self.credenciales = credenciales or {}
        # FASE3-S2: todo canal tiene un id de cuenta/tienda del lado del
        # proveedor y va en la URL de cada request (Tiendanube:
        # /2025-03/{store_id}/orders). Antes habia que meterlo a mano dentro
        # de `credenciales`, que esta documentado como "solo tokens".
        #: canal_venta.id_tienda_externo. Publico, no es un secreto.
        self.id_tienda_externo = id_tienda_externo

    # -- Lectura --------------------------------------------------------

    @abstractmethod
    def traer_pedidos(self, desde: datetime, hasta: datetime) -> Iterable[Dict[str, Any]]:
        """Pedidos creados o modificados en [desde, hasta).

        Devuelve los dicts crudos de la API, ya paginados. Levanta
        ErrorIngesta si la API falla.
        """

    @abstractmethod
    def traer_pagos(self, desde: datetime, hasta: datetime) -> Iterable[Dict[str, Any]]:
        """Pagos/cobros registrados en [desde, hasta), crudos.

        Incluye los rechazados y devueltos: el estado se decide al
        normalizar, no al traer. Filtrar aca esconderia contracargos.
        """

    @abstractmethod
    def normalizar(self, entidad: str, crudo: Dict[str, Any]) -> Dict[str, Any]:
        """Traduce un payload crudo a un dict con los nombres de campo del
        modelo interno (Pedido, Pago, etc.), listo para persistir.

        `entidad` es uno de ENTIDADES_VALIDAS.

        El dict devuelto NO se persiste aca: normalizar() es puro, no toca
        la base ni la red. Eso permite testear el mapeo de cada canal con
        payloads guardados, sin credenciales.

        Todo campo de plata sale como Decimal.
        """

    # -- Opcionales: default vacio para no obligar a cada canal ----------

    def traer_liquidaciones(self, desde: datetime, hasta: datetime) -> Iterable[Dict[str, Any]]:
        """Payouts del procesador en el rango. Vacio si el canal no expone."""
        return []

    def traer_movimientos(self, desde: datetime, hasta: datetime) -> Iterable[Dict[str, Any]]:
        """Extracto de la cuenta de cobro en el rango. Vacio si no aplica."""
        return []

    def traer_productos(self) -> Iterable[Dict[str, Any]]:
        """Catalogo publicado en el canal, para armar mapeo_producto_canal.

        FASE3-S2: sigue siendo opcional en el contrato (un procesador de pagos
        puro no tiene catalogo), pero para un canal de venta es obligatorio en
        los hechos: sin mapeo_producto_canal, las lineas de pedido no se
        pueden asociar a un Producto y el margen queda sin calcular.
        """
        return []

    def variantes_de_producto(self, crudo: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        """Parte un producto crudo en un crudo por SKU vendible.

        FASE3-S2. Hace falta porque la granularidad del canal no es la del
        modelo: Tiendanube devuelve UN producto con N variantes (talle/color),
        cada una con su id, su sku y su precio, y cada una de esas es una fila
        de `producto` mas una de `mapeo_producto_canal`. normalizar() esta
        documentado como 1 crudo -> 1 dict, asi que la explosion 1 -> N tiene
        que pasar antes y quedar explicita, no escondida adentro del mapeo.

        El default sirve para canales sin variantes: el producto es su propio
        SKU. Los dicts que devuelve se le pasan a normalizar(ENTIDAD_PRODUCTO).
        """
        return [crudo]

    def hash_movimiento(self, crudo: Dict[str, Any]) -> str:
        """Huella estable de un movimiento, para movimiento_cuenta.hash_dedup.

        Cada canal decide con que campos armarla (id del procesador cuando
        existe; si no, fecha + monto + descripcion). Tiene que ser estable
        entre corridas: si cambia, el mismo movimiento entra dos veces.
        """
        raise NotImplementedError

    # -- Salud ----------------------------------------------------------

    def verificar_credenciales(self) -> bool:
        """True si los tokens actuales sirven. Fase 3 la usa antes de
        marcar canal_venta.activo = True."""
        raise NotImplementedError


def es_decimal(valor: Any) -> bool:
    """Guarda de sanidad para los tests de Fase 3: ningun monto normalizado
    puede llegar como float."""
    return isinstance(valor, Decimal)
