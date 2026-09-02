# -*- coding: utf-8 -*-
"""Tests de la FASE3-S2 (backfill de productos y pedidos de Tiendanube).

    python -m unittest discover -s tests -v

NINGUN test pega contra la API real de Tiendanube. Toda la capa HTTP esta
mockeada en `tn.requests.get`, igual que en FASE3-S1, y la clase
TestNoSePegaALaApiReal del final verifica que no haya otra puerta de salida.
Tampoco se toca la base productiva: el modulo repunta la app a un SQLite en
memoria mientras corre y la devuelve a DATABASE_URL al terminar.

El backfill se ejecuta SINCRONICAMENTE en los tests (llamando a
correr_backfill directo, sin thread): lo que se testea es que escribe bien y
que no duplica, no que threading.Thread funcione. Que la ruta no bloquee el
request tiene su propio test, que verifica que durante el POST no se le pega a
la API.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('CREDENTIALS_ENCRYPTION_KEY',
                      'sO1mHTMYm4Rfy9ii1YV8dqmM1J4KrHnQPy_2xGx0nMk=')
os.environ.setdefault('SECRET_KEY', 'clave-de-test')

import cripto  # noqa: E402
import integracion_tiendanube as tn  # noqa: E402
import ingestor_tiendanube  # noqa: E402
import sync_tiendanube  # noqa: E402
from app import app  # noqa: E402
from tests.ayuda_auth import request_anonimo  # noqa: E402
from ingestor_canal import ENTIDAD_PEDIDO, ENTIDAD_PRODUCTO  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    CredencialCanal,
    Empresa,
    MapeoProductoCanal,
    Pedido,
    PedidoItem,
    Producto,
    SyncLog,
    Usuario,
    db,
)

ENGINE_PRODUCTIVO = None

TOKEN_FALSO = 'tn_token_de_prueba_no_es_real_1234567890'
STORE_ID_FALSO = '9876543'


def setUpModule():
    """Repunta la app a SQLite en memoria. La base real no se toca."""
    global ENGINE_PRODUCTIVO
    engines = db._app_engines[app]
    ENGINE_PRODUCTIVO = engines[None]
    engines[None] = db._make_engine(None, {'url': 'sqlite://'}, app)
    app.config['TESTING'] = True
    with app.app_context():
        db.create_all()


def tearDownModule():
    with app.app_context():
        db.drop_all()
        db.session.remove()
    db._app_engines[app][None].dispose()
    db._app_engines[app][None] = ENGINE_PRODUCTIVO
    app.config['TESTING'] = False


# ---------------------------------------------------------------------------
# Payloads de Tiendanube (recortados a los campos que el ingestor mira)
# ---------------------------------------------------------------------------

# Un producto con dos variantes (cada variante es un SKU y una fila de
# producto) y otro sin SKU cargado, que obliga a generar uno.
PRODUCTOS = [
    {
        'id': 111,
        'name': {'es': 'Martillo carpintero'},
        'published': True,
        'variants': [
            {'id': 1001, 'sku': 'MART-500', 'price': '12500.50', 'stock': 7,
             'values': [{'es': '500g'}]},
            {'id': 1002, 'sku': 'MART-800', 'price': '15900.00', 'stock': 3,
             'values': [{'es': '800g'}]},
        ],
    },
    {
        'id': 222,
        'name': {'es': 'Cinta metrica 5m'},
        'published': False,
        'variants': [
            {'id': 2001, 'sku': '', 'price': '4300.99', 'stock': None, 'values': []},
        ],
    },
]

PEDIDOS = [
    {
        'id': 5001,
        'number': '1042',
        'created_at': '2026-08-14T10:20:30-0300',
        'status': 'open',
        'payment_status': 'paid',
        'currency': 'ARS',
        'subtotal': '28401.49',
        'discount': '1000.00',
        'shipping_cost_customer': '3500.00',
        'total': '30901.49',
        'customer': {'id': 77, 'name': 'Cliente Uno', 'email': 'uno@test.local'},
        'products': [
            {'product_id': 111, 'variant_id': 1001, 'sku': 'MART-500',
             'name': 'Martillo carpintero - 500g', 'quantity': 2, 'price': '12500.50'},
            {'product_id': 222, 'variant_id': 2001, 'sku': '',
             'name': 'Cinta metrica 5m', 'quantity': 1, 'price': '4300.99'},
        ],
    },
    {
        'id': 5002,
        'number': '1043',
        'created_at': '2026-08-20T18:05:00-0300',
        'status': 'cancelled',
        'payment_status': 'voided',
        'currency': 'ARS',
        'subtotal': '15900.00',
        'discount': '0.00',
        'shipping_cost_customer': '0.00',
        'total': '15900.00',
        'customer': {'id': 88, 'name': 'Cliente Dos', 'email': 'dos@test.local'},
        'products': [
            {'product_id': 111, 'variant_id': 1002, 'sku': 'MART-800',
             'name': 'Martillo carpintero - 800g', 'quantity': 1, 'price': '15900.00'},
        ],
    },
]


class RespuestaFalsa:
    """Lo minimo de requests.Response que usa integracion_tiendanube."""

    def __init__(self, status_code=200, datos=None, headers=None, texto=None):
        self.status_code = status_code
        self._datos = datos
        self.headers = headers if headers is not None else {}
        self.text = texto if texto is not None else str(datos)

    def json(self):
        if self._datos is None:
            raise ValueError('no es JSON')
        return self._datos


class ApiFalsa:
    """requests.get de mentira, con paginacion y rate limit simulados.

    Guarda cada llamada en `self.llamadas` para poder afirmar sobre los params
    (page, per_page) sin salir a internet.
    """

    def __init__(self, productos=None, pedidos=None, por_pagina=None):
        self.productos = list(productos if productos is not None else PRODUCTOS)
        self.pedidos = list(pedidos if pedidos is not None else PEDIDOS)
        # Paginado chico para poder testear la paginacion con 2-3 items.
        self.por_pagina = por_pagina
        self.llamadas = []
        #: cuantas veces contestar 429 antes de dejar pasar, por recurso.
        self.rechazos_429 = 0
        #: excepcion a levantar cuando se pida este recurso ('orders'|'products')
        self.romper_en = None
        self.headers_extra = {'x-rate-limit-remaining': '35',
                              'x-rate-limit-reset': '1000'}

    def __call__(self, url, headers=None, params=None, timeout=None):
        params = params or {}
        self.llamadas.append({'url': url, 'params': dict(params), 'headers': headers})

        if self.romper_en and self.romper_en in url:
            import requests as requests_real
            raise requests_real.ConnectionError('sin red (simulado)')

        if self.rechazos_429 > 0:
            self.rechazos_429 -= 1
            return RespuestaFalsa(429, {'message': 'Too many requests'},
                                  headers={'Retry-After': '7'})

        if '/products' in url:
            datos = self.productos
        elif '/orders' in url:
            datos = self.pedidos
        elif '/store' in url:
            return RespuestaFalsa(200, {'id': int(STORE_ID_FALSO), 'name': 'Tienda'})
        else:
            return RespuestaFalsa(404, [])

        return self._pagina(datos, params)

    def _pagina(self, datos, params):
        """Corta la lista segun ?page=, y avisa con Link si hay siguiente."""
        tamano = self.por_pagina or tn.POR_PAGINA
        pagina = int(params.get('page', 1))
        desde = (pagina - 1) * tamano
        lote = datos[desde:desde + tamano]

        cabeceras = dict(self.headers_extra)
        if desde + tamano < len(datos):
            cabeceras['Link'] = '<https://api.tiendanube.com/next>; rel="next"'
        return RespuestaFalsa(200, lote, headers=cabeceras)


class BaseSync(unittest.TestCase):
    """Una empresa con Tiendanube ya conectada (el estado que dejo FASE3-S1)."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE3-S2')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='fase3s2@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        canal = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                           nombre='Ferreteria Roman', activo=True,
                           id_tienda_externo=STORE_ID_FALSO)
        db.session.add(canal)
        db.session.add(CanalVenta(empresa_id=self.empresa.id, tipo='mercadolibre',
                                  nombre='Mercado Libre', activo=False))
        db.session.flush()

        db.session.add(CredencialCanal(
            canal_id=canal.id, tipo_credencial='oauth2', activo=True,
            access_token_cifrado=cripto.cifrar(TOKEN_FALSO)))
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.canal_id = canal.id
        self.usuario_id = self.usuario.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def correr(self, api=None, arranque=None):
        """Una corrida completa del backfill, sincronica y con la API mockeada.

        Replica lo que hace lanzar_backfill (las dos filas de sync_log en
        'corriendo') y despues ejecuta el cuerpo, que es lo que interesa.
        """
        api = api or ApiFalsa()
        arranque = arranque or datetime.utcnow()
        for entidad in (ENTIDAD_PRODUCTO, ENTIDAD_PEDIDO):
            db.session.add(SyncLog(canal_id=self.canal_id, entidad=entidad,
                                   operacion=sync_tiendanube.OPERACION,
                                   estado='corriendo', fecha_inicio=arranque))
        db.session.commit()

        with mock.patch.object(tn.requests, 'get', side_effect=api):
            resultado = sync_tiendanube.correr_backfill(self.canal_id, arranque)
        return api, arranque, resultado

    def sku(self, sku):
        return Producto.query.filter_by(empresa_id=self.empresa_id, sku=sku).one()

    def pedido(self, id_externo):
        return Pedido.query.filter_by(canal_id=self.canal_id,
                                      id_externo=id_externo).one()


# ---------------------------------------------------------------------------
# Parte 1: productos
# ---------------------------------------------------------------------------

class TestProductos(BaseSync):

    def test_una_fila_de_producto_por_variante(self):
        self.correr()

        # 2 variantes del martillo + 1 de la cinta = 3 SKUs, no 2 productos.
        self.assertEqual(Producto.query.count(), 3)
        self.assertEqual(MapeoProductoCanal.query.count(), 3)

        martillo = self.sku('MART-500')
        self.assertEqual(martillo.nombre, 'Martillo carpintero (500g)')
        self.assertTrue(martillo.activo)

    def test_variante_sin_sku_recibe_uno_generado(self):
        self.correr()

        generado = self.sku('TN-222-2001')
        self.assertEqual(generado.nombre, 'Cinta metrica 5m')
        # El producto no esta publicado en la tienda: se refleja tal cual.
        self.assertFalse(generado.activo)

    def test_costo_unitario_queda_null(self):
        """Tiendanube no da un costo confiable: lo carga Roman a mano."""
        self.correr()

        for producto in Producto.query.all():
            self.assertIsNone(producto.costo_unitario, producto.sku)

    def test_no_pisa_el_costo_que_cargo_roman(self):
        """El caso que arruinaria los margenes: resincronizar borrando costos."""
        self.correr()

        martillo = self.sku('MART-500')
        martillo.costo_unitario = Decimal('8000.00')
        db.session.commit()

        self.correr()

        self.assertEqual(self.sku('MART-500').costo_unitario, Decimal('8000.00'))

    def test_los_precios_son_decimal_exacto(self):
        self.correr()

        precio = self.sku('MART-500').precio_lista
        self.assertIsInstance(precio, Decimal)
        self.assertEqual(precio, Decimal('12500.50'))

    def test_el_mapeo_apunta_al_producto_y_a_la_variante(self):
        self.correr()

        mapeo = MapeoProductoCanal.query.filter_by(
            canal_id=self.canal_id, id_producto_externo='111',
            id_variante_externo='1001').one()
        self.assertEqual(mapeo.producto_id, self.sku('MART-500').id)
        self.assertEqual(mapeo.sku_externo, 'MART-500')

    def test_actualiza_nombre_y_activo_de_un_producto_que_ya_existia(self):
        self.correr()

        renombrado = [dict(p) for p in PRODUCTOS]
        renombrado[0] = dict(renombrado[0], name={'es': 'Martillo de carpintero PRO'},
                             published=False)
        self.correr(api=ApiFalsa(productos=renombrado))

        martillo = self.sku('MART-500')
        self.assertEqual(martillo.nombre, 'Martillo de carpintero PRO (500g)')
        self.assertFalse(martillo.activo)
        self.assertEqual(Producto.query.count(), 3)


# ---------------------------------------------------------------------------
# Parte 2: pedidos
# ---------------------------------------------------------------------------

class TestPedidos(BaseSync):

    def test_guarda_los_pedidos_con_sus_totales_en_decimal(self):
        self.correr()

        self.assertEqual(Pedido.query.count(), 2)

        pedido = self.pedido('5001')
        self.assertEqual(pedido.numero_externo, '1042')
        self.assertEqual(pedido.moneda, 'ARS')
        self.assertEqual(pedido.total, Decimal('30901.49'))
        self.assertEqual(pedido.total_bruto, Decimal('28401.49'))
        self.assertEqual(pedido.total_envio, Decimal('3500.00'))
        self.assertEqual(pedido.total_descuentos, Decimal('1000.00'))
        for monto in (pedido.total, pedido.total_bruto, pedido.total_envio):
            self.assertIsInstance(monto, Decimal)

    def test_el_estado_se_guarda_tal_cual_viene_de_tiendanube(self):
        """No se traduce a un vocabulario propio en esta slice."""
        self.assertEqual(self.correr() and self.pedido('5001').estado, 'open')
        self.assertEqual(self.pedido('5002').estado, 'cancelled')
        # El payment_status queda aparte, para cuando FASE3-S3 concilie.
        self.assertEqual(self.pedido('5001').estado_externo, 'paid')

    def test_la_fecha_se_guarda_en_utc_naive(self):
        self.correr()

        # '2026-08-14T10:20:30-0300' en UTC son las 13:20:30.
        fecha = self.pedido('5001').fecha_pedido
        self.assertIsNone(fecha.tzinfo)
        self.assertEqual(fecha, datetime(2026, 8, 14, 13, 20, 30))

    def test_los_items_se_asocian_al_producto_por_el_mapeo(self):
        self.correr()

        items = PedidoItem.query.filter_by(pedido_id=self.pedido('5001').id).all()
        self.assertEqual(len(items), 2)

        por_sku = {i.descripcion: i for i in items}
        martillo = por_sku['Martillo carpintero - 500g']
        self.assertEqual(martillo.producto_id, self.sku('MART-500').id)
        self.assertEqual(martillo.cantidad, 2)
        self.assertEqual(martillo.precio_unitario, Decimal('12500.50'))
        self.assertEqual(martillo.subtotal, Decimal('25001.00'))

    def test_el_snapshot_de_costo_congela_el_costo_de_hoy(self):
        """Es NULL mientras Roman no cargue costos: ese es el estado real."""
        self.correr()
        for item in PedidoItem.query.all():
            self.assertIsNone(item.costo_unitario_snapshot)

        # Con el costo cargado, la proxima corrida ya lo congela.
        martillo = self.sku('MART-500')
        martillo.costo_unitario = Decimal('8000.00')
        db.session.commit()
        self.correr()

        item = PedidoItem.query.filter_by(producto_id=martillo.id).first()
        self.assertEqual(item.costo_unitario_snapshot, Decimal('8000.00'))

    def test_guarda_el_payload_crudo_para_reprocesar(self):
        self.correr()
        crudo = self.pedido('5001').raw_payload
        self.assertEqual(crudo['id'], 5001)
        # El id de cliente de Tiendanube queda ahi: `pedido` no tiene columna
        # cliente_ref y esta slice no agrega ninguna.
        self.assertEqual(crudo['customer']['id'], 77)
        self.assertFalse(hasattr(Pedido, 'cliente_ref'))


class TestItemSinMapear(BaseSync):
    """Una linea de un producto que ya no esta en el catalogo."""

    def _pedido_con_linea_huerfana(self):
        pedido = dict(PEDIDOS[0])
        pedido['products'] = [
            # Este si esta en el catalogo.
            {'product_id': 111, 'variant_id': 1001, 'sku': 'MART-500',
             'name': 'Martillo carpintero - 500g', 'quantity': 2, 'price': '12500.50'},
            # Este no: producto borrado de la tienda despues de venderse.
            {'product_id': 999, 'variant_id': 9001, 'sku': 'FANTASMA',
             'name': 'Producto que ya no existe', 'quantity': 3, 'price': '999.00'},
        ]
        return pedido

    def test_el_pedido_y_sus_otras_lineas_se_guardan_igual(self):
        api = ApiFalsa(pedidos=[self._pedido_con_linea_huerfana()])
        _, _, resultado = self.correr(api=api)

        # El sync no se cae: el pedido entra entero.
        self.assertEqual(Pedido.query.count(), 1)
        items = PedidoItem.query.filter_by(pedido_id=self.pedido('5001').id).all()
        self.assertEqual(len(items), 2)

        por_desc = {i.descripcion: i for i in items}
        self.assertEqual(por_desc['Martillo carpintero - 500g'].producto_id,
                         self.sku('MART-500').id)

        huerfano = por_desc['Producto que ya no existe']
        self.assertIsNone(huerfano.producto_id)
        # La linea se guarda con sus datos igual: la venta existio.
        self.assertEqual(huerfano.cantidad, 3)
        self.assertEqual(huerfano.precio_unitario, Decimal('999.00'))
        self.assertEqual(huerfano.sku_externo, 'FANTASMA')

    def test_la_linea_sin_mapear_no_cuenta_como_error_pero_si_se_reporta(self):
        api = ApiFalsa(pedidos=[self._pedido_con_linea_huerfana()])
        _, arranque, resultado = self.correr(api=api)

        self.assertEqual(resultado['pedidos']['error'], 0)
        self.assertEqual(resultado['pedidos']['items_sin_mapear'], 1)

        fila = SyncLog.query.filter_by(canal_id=self.canal_id, entidad=ENTIDAD_PEDIDO,
                                       fecha_inicio=arranque).one()
        self.assertEqual(fila.estado, 'ok')
        self.assertIn('sin producto asociado', fila.mensaje_error)


# ---------------------------------------------------------------------------
# Idempotencia: lo que no se puede romper
# ---------------------------------------------------------------------------

class TestIdempotencia(BaseSync):

    def test_doble_backfill_no_debe_duplicar_ventas(self):
        """Roman va a apretar el boton mas de una vez.

        Dos corridas con los mismos datos tienen que dejar exactamente las
        mismas filas: si esto falla, los reportes de ventas cuentan cada pedido
        dos veces y todo lo que se calcule arriba queda mal.
        """
        self.correr()

        conteos = {
            'producto': Producto.query.count(),
            'mapeo': MapeoProductoCanal.query.count(),
            'pedido': Pedido.query.count(),
            'item': PedidoItem.query.count(),
        }
        total_vendido = sum(p.total for p in Pedido.query.all())

        self.correr()

        self.assertEqual(Producto.query.count(), conteos['producto'])
        self.assertEqual(MapeoProductoCanal.query.count(), conteos['mapeo'])
        self.assertEqual(Pedido.query.count(), conteos['pedido'])
        self.assertEqual(PedidoItem.query.count(), conteos['item'])
        self.assertEqual(sum(p.total for p in Pedido.query.all()), total_vendido)

    def test_la_segunda_corrida_actualiza_en_vez_de_insertar(self):
        _, _, primera = self.correr()
        self.assertEqual(primera['pedidos']['nuevos'], 2)
        self.assertEqual(primera['pedidos']['actualizados'], 0)

        _, _, segunda = self.correr()
        self.assertEqual(segunda['pedidos']['nuevos'], 0)
        self.assertEqual(segunda['pedidos']['actualizados'], 2)
        self.assertEqual(segunda['productos']['nuevos'], 0)
        self.assertEqual(segunda['productos']['actualizados'], 3)

    def test_un_pedido_editado_no_acumula_lineas_viejas(self):
        """Si el comerciante saca un item, la linea no puede quedar colgada."""
        self.correr()
        self.assertEqual(
            PedidoItem.query.filter_by(pedido_id=self.pedido('5001').id).count(), 2)

        editado = dict(PEDIDOS[0])
        editado['products'] = [PEDIDOS[0]['products'][0]]
        editado['total'] = '25001.00'
        self.correr(api=ApiFalsa(pedidos=[editado, PEDIDOS[1]]))

        self.assertEqual(
            PedidoItem.query.filter_by(pedido_id=self.pedido('5001').id).count(), 1)
        self.assertEqual(self.pedido('5001').total, Decimal('25001.00'))

    def test_los_ids_de_las_filas_no_cambian_entre_corridas(self):
        """Prueba de que es UPDATE y no DELETE+INSERT disfrazado: si los ids
        cambiaran, cualquier FK que apunte a un pedido (los pagos de FASE3-S3)
        quedaria colgando."""
        self.correr()
        antes = {p.id_externo: p.id for p in Pedido.query.all()}

        self.correr()
        despues = {p.id_externo: p.id for p in Pedido.query.all()}

        self.assertEqual(antes, despues)


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------

class TestRateLimit(BaseSync):

    def test_un_429_con_retry_after_espera_antes_de_reintentar(self):
        api = ApiFalsa()
        api.rechazos_429 = 1

        arranque = datetime.utcnow()
        for entidad in (ENTIDAD_PRODUCTO, ENTIDAD_PEDIDO):
            db.session.add(SyncLog(canal_id=self.canal_id, entidad=entidad,
                                   operacion=sync_tiendanube.OPERACION,
                                   estado='corriendo', fecha_inicio=arranque))
        db.session.commit()

        # time.sleep mockeado: se verifica CUANTO iba a dormir, sin dormirlo.
        with mock.patch.object(tn.requests, 'get', side_effect=api), \
             mock.patch.object(tn.time, 'sleep') as dormir:
            sync_tiendanube.correr_backfill(self.canal_id, arranque)

        esperas = [llamada.args[0] for llamada in dormir.call_args_list]
        self.assertIn(7.0, esperas, 'no respeto el Retry-After: 7')
        # Y despues de esperar, reintento y trajo los datos igual.
        self.assertEqual(Producto.query.count(), 3)
        self.assertEqual(Pedido.query.count(), 2)

    def test_sin_retry_after_usa_el_x_rate_limit_reset(self):
        resp = RespuestaFalsa(429, {}, headers={'x-rate-limit-reset': '2500'})
        self.assertEqual(tn._espera_tras_429(resp, 0), 2.5)

    def test_sin_ningun_header_hace_backoff_exponencial(self):
        resp = RespuestaFalsa(429, {}, headers={})
        esperas = [tn._espera_tras_429(resp, intento) for intento in range(4)]
        self.assertEqual(esperas, [1.0, 2.0, 4.0, 8.0])

    def test_la_espera_tiene_techo(self):
        resp = RespuestaFalsa(429, {}, headers={'Retry-After': '99999'})
        self.assertEqual(tn._espera_tras_429(resp, 0), tn.ESPERA_MAXIMA)

    def test_frena_solo_cuando_el_balde_esta_por_llenarse(self):
        con_lugar = RespuestaFalsa(200, [], headers={'x-rate-limit-remaining': '30'})
        sin_lugar = RespuestaFalsa(200, [], headers={'x-rate-limit-remaining': '1',
                                                     'x-rate-limit-reset': '1500'})

        with mock.patch.object(tn.time, 'sleep') as dormir:
            self.assertEqual(tn._frenar_si_el_balde_esta_lleno(con_lugar), 0.0)
            dormir.assert_not_called()

            self.assertEqual(tn._frenar_si_el_balde_esta_lleno(sin_lugar), 1.5)
            dormir.assert_called_once_with(1.5)

    def test_los_headers_se_leen_sin_importar_mayusculas(self):
        """requests los devuelve case-insensitive, pero no hay que depender."""
        resp = RespuestaFalsa(429, {}, headers={'RETRY-AFTER': '3'})
        self.assertEqual(tn._espera_tras_429(resp, 0), 3.0)

    def test_un_429_que_no_afloja_nunca_termina_en_error_de_ingesta(self):
        api = ApiFalsa()
        api.rechazos_429 = 99

        with mock.patch.object(tn.requests, 'get', side_effect=api), \
             mock.patch.object(tn.time, 'sleep'):
            with self.assertRaises(tn.ErrorTiendanube):
                tn.traer_productos(STORE_ID_FALSO, TOKEN_FALSO)


# ---------------------------------------------------------------------------
# Paginacion y transporte
# ---------------------------------------------------------------------------

class TestPaginacion(BaseSync):

    def test_recorre_todas_las_paginas(self):
        api = ApiFalsa(por_pagina=1)
        self.correr(api=api)

        # 2 productos en paginas de 1 => 2 requests + la que devuelve vacio no
        # hace falta porque el Link deja de venir en la ultima.
        paginas_productos = [ll['params']['page'] for ll in api.llamadas
                             if '/products' in ll['url']]
        self.assertEqual(paginas_productos, [1, 2])

        # Y trajo todo igual.
        self.assertEqual(Producto.query.count(), 3)
        self.assertEqual(Pedido.query.count(), 2)

    def test_pide_el_maximo_por_pagina(self):
        api, _, _ = self.correr()
        for llamada in api.llamadas:
            self.assertEqual(llamada['params']['per_page'], 200)

    def test_todas_las_llamadas_llevan_user_agent_y_el_header_propietario(self):
        api, _, _ = self.correr()

        self.assertTrue(api.llamadas)
        for llamada in api.llamadas:
            self.assertTrue(llamada['headers']['User-Agent'].strip())
            self.assertEqual(llamada['headers']['Authentication'],
                             'bearer %s' % TOKEN_FALSO)

    def test_pega_a_la_version_2025_03_de_la_api(self):
        api, _, _ = self.correr()
        for llamada in api.llamadas:
            self.assertIn('/2025-03/%s/' % STORE_ID_FALSO, llamada['url'])


# ---------------------------------------------------------------------------
# sync_log
# ---------------------------------------------------------------------------

class TestSyncLog(BaseSync):

    def test_una_corrida_ok_deja_los_conteos(self):
        _, arranque, _ = self.correr()

        productos = SyncLog.query.filter_by(canal_id=self.canal_id,
                                            entidad=ENTIDAD_PRODUCTO,
                                            fecha_inicio=arranque).one()
        self.assertEqual(productos.estado, 'ok')
        self.assertEqual(productos.registros_leidos, 3)
        self.assertEqual(productos.registros_nuevos, 3)
        self.assertIsNotNone(productos.fecha_fin)
        self.assertIsNotNone(productos.duracion_ms)

        pedidos = SyncLog.query.filter_by(canal_id=self.canal_id,
                                          entidad=ENTIDAD_PEDIDO,
                                          fecha_inicio=arranque).one()
        self.assertEqual(pedidos.estado, 'ok')
        self.assertEqual(pedidos.registros_leidos, 2)

    def test_marca_fecha_ultima_sync_en_el_canal(self):
        self.correr()
        canal = db.session.get(CanalVenta, self.canal_id)
        self.assertIsNotNone(canal.fecha_ultima_sync)

    def test_si_revienta_a_mitad_de_camino_el_sync_log_queda_en_error(self):
        """Lo peor seria perder el registro de que fallo: el boton quedaria
        trabado en 'corriendo' y nadie sabria por que."""
        api = ApiFalsa()
        api.romper_en = '/orders'   # los productos entran, los pedidos no

        arranque = datetime.utcnow()
        for entidad in (ENTIDAD_PRODUCTO, ENTIDAD_PEDIDO):
            db.session.add(SyncLog(canal_id=self.canal_id, entidad=entidad,
                                   operacion=sync_tiendanube.OPERACION,
                                   estado='corriendo', fecha_inicio=arranque))
        db.session.commit()

        with mock.patch.object(tn.requests, 'get', side_effect=api):
            with self.assertRaises(Exception):
                sync_tiendanube.correr_backfill(self.canal_id, arranque)
            sync_tiendanube._cerrar_con_error(
                self.canal_id, arranque, RuntimeError('sin red (simulado)'))

        productos = SyncLog.query.filter_by(entidad=ENTIDAD_PRODUCTO,
                                            fecha_inicio=arranque).one()
        pedidos = SyncLog.query.filter_by(entidad=ENTIDAD_PEDIDO,
                                          fecha_inicio=arranque).one()

        # Lo que alcanzo a entrar quedo registrado como ok...
        self.assertEqual(productos.estado, 'ok')
        self.assertEqual(Producto.query.count(), 3)
        # ...y lo que fallo quedo como error, con su motivo y su fecha de fin.
        self.assertEqual(pedidos.estado, 'error')
        self.assertIn('sin red', pedidos.mensaje_error)
        self.assertIsNotNone(pedidos.fecha_fin)
        self.assertEqual(Pedido.query.count(), 0)

        # Y nada queda colgado en 'corriendo': el boton se destraba solo.
        self.assertEqual(SyncLog.query.filter_by(estado='corriendo').count(), 0)

    def test_una_corrida_huerfana_vieja_se_da_por_perdida(self):
        """Un deploy de Render puede matar el thread sin cerrar el sync_log."""
        vieja = datetime.utcnow() - timedelta(hours=3)
        db.session.add(SyncLog(canal_id=self.canal_id, entidad=ENTIDAD_PEDIDO,
                               operacion=sync_tiendanube.OPERACION,
                               estado='corriendo', fecha_inicio=vieja))
        db.session.commit()

        self.assertIsNone(sync_tiendanube.sync_en_curso(self.canal_id))
        self.assertEqual(SyncLog.query.filter_by(estado='corriendo').count(), 0)

    def test_una_corrida_reciente_bloquea(self):
        db.session.add(SyncLog(canal_id=self.canal_id, entidad=ENTIDAD_PEDIDO,
                               operacion=sync_tiendanube.OPERACION,
                               estado='corriendo', fecha_inicio=datetime.utcnow()))
        db.session.commit()

        self.assertIsNotNone(sync_tiendanube.sync_en_curso(self.canal_id))

    def test_ultimo_sync_resume_las_dos_entidades(self):
        self.correr()
        resumen = sync_tiendanube.ultimo_sync(self.canal_id)

        self.assertEqual(resumen['estado'], 'ok')
        self.assertEqual(resumen['productos']['leidos'], 3)
        self.assertEqual(resumen['pedidos']['leidos'], 2)
        self.assertIsNotNone(resumen['fin'])

    def test_ultimo_sync_es_none_si_nunca_se_sincronizo(self):
        self.assertIsNone(sync_tiendanube.ultimo_sync(self.canal_id))


# ---------------------------------------------------------------------------
# Ruta y UI
# ---------------------------------------------------------------------------

class TestRuta(BaseSync):

    def test_el_post_responde_sin_pegarle_a_la_api(self):
        """El requisito de fondo: el worker de gunicorn no se queda esperando."""
        with mock.patch.object(sync_tiendanube.threading, 'Thread') as Hilo, \
             mock.patch.object(tn.requests, 'get') as get:
            resp = self.client.post('/integraciones/tiendanube/sincronizar')

        self.assertEqual(resp.status_code, 302)
        self.assertIn('/integraciones', resp.headers['Location'])
        # Durante el request no salio ni una llamada a Tiendanube.
        get.assert_not_called()
        # El trabajo quedo delegado a un thread daemon.
        Hilo.assert_called_once()
        self.assertTrue(Hilo.call_args.kwargs['daemon'])
        Hilo.return_value.start.assert_called_once()

    def test_deja_el_sync_log_en_corriendo_y_avisa(self):
        with mock.patch.object(sync_tiendanube.threading, 'Thread'):
            self.client.post('/integraciones/tiendanube/sincronizar')

        filas = SyncLog.query.filter_by(canal_id=self.canal_id,
                                        estado='corriendo').all()
        self.assertEqual(len(filas), 2)
        self.assertEqual({f.entidad for f in filas}, {ENTIDAD_PRODUCTO, ENTIDAD_PEDIDO})

        pagina = self.client.get('/integraciones').get_data(as_text=True)
        self.assertIn('Sincronización iniciada', pagina)

    def test_no_deja_disparar_dos_en_paralelo(self):
        with mock.patch.object(sync_tiendanube.threading, 'Thread') as Hilo:
            self.client.post('/integraciones/tiendanube/sincronizar')
            self.client.post('/integraciones/tiendanube/sincronizar')

            # El segundo POST no arranco nada.
            self.assertEqual(Hilo.call_count, 1)

        self.assertEqual(SyncLog.query.count(), 2)
        pagina = self.client.get('/integraciones').get_data(as_text=True)
        self.assertIn('Ya hay una sincronización en curso', pagina)

    def test_requiere_login(self):
        with mock.patch.object(sync_tiendanube.threading, 'Thread') as Hilo:
            resp = request_anonimo(self.ctx, 'post',
                                   '/integraciones/tiendanube/sincronizar')

        self.assertEqual(resp.status_code, 302)
        Hilo.assert_not_called()
        self.assertEqual(SyncLog.query.count(), 0)

    def test_no_sincroniza_un_canal_sin_conectar(self):
        canal = db.session.get(CanalVenta, self.canal_id)
        canal.activo = False
        db.session.commit()

        with mock.patch.object(sync_tiendanube.threading, 'Thread') as Hilo:
            self.client.post('/integraciones/tiendanube/sincronizar')

        Hilo.assert_not_called()
        self.assertEqual(SyncLog.query.count(), 0)

    def test_no_se_puede_sincronizar_por_get(self):
        resp = self.client.get('/integraciones/tiendanube/sincronizar')
        self.assertEqual(resp.status_code, 405)


class TestPaginaIntegraciones(BaseSync):

    def test_muestra_el_boton_de_sincronizar_en_el_canal_conectado(self):
        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('/integraciones/tiendanube/sincronizar', pagina)
        self.assertIn('Sincronizar ahora', pagina)
        self.assertIn('Nunca se sincronizó', pagina)

    def test_muestra_el_resultado_de_la_ultima_corrida(self):
        self.correr()

        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('Última sync OK', pagina)
        self.assertIn('3 producto(s)', pagina)
        self.assertIn('2 pedido(s)', pagina)

    def test_con_un_sync_corriendo_el_boton_queda_deshabilitado(self):
        db.session.add(SyncLog(canal_id=self.canal_id, entidad=ENTIDAD_PEDIDO,
                               operacion=sync_tiendanube.OPERACION,
                               estado='corriendo', fecha_inicio=datetime.utcnow()))
        db.session.commit()

        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('disabled', pagina)
        self.assertIn('Sincronizando', pagina)

    def test_el_canal_sin_conectar_no_ofrece_sincronizar(self):
        """Mercado Libre no tiene ni conexion ni sync todavia."""
        pagina = self.client.get('/integraciones').get_data(as_text=True)
        self.assertIn('Próximamente', pagina)
        self.assertEqual(pagina.count('Sincronizar ahora'), 1)


# ---------------------------------------------------------------------------
# El mapeo, aislado (sin base ni red)
# ---------------------------------------------------------------------------

class TestNormalizar(unittest.TestCase):
    """normalizar() es puro: se testea con payloads y sin credenciales."""

    def setUp(self):
        self.ingestor = ingestor_tiendanube.IngestorTiendanube(
            canal_id=1, credenciales={'access_token': TOKEN_FALSO},
            id_tienda_externo=STORE_ID_FALSO)

    def test_todo_importe_sale_como_decimal(self):
        from ingestor_canal import es_decimal

        datos = self.ingestor.normalizar(ENTIDAD_PEDIDO, PEDIDOS[0])
        for campo in ('total', 'total_bruto', 'total_envio', 'total_descuentos',
                      'total_impuestos'):
            self.assertTrue(es_decimal(datos[campo]), campo)
        for item in datos['items']:
            self.assertTrue(es_decimal(item['precio_unitario']))
            self.assertTrue(es_decimal(item['subtotal']))

    def test_no_pasa_por_float_en_el_camino(self):
        """El bug clasico: Decimal(0.1) != Decimal('0.1')."""
        datos = self.ingestor.normalizar(ENTIDAD_PEDIDO, dict(PEDIDOS[0], total='0.1'))
        self.assertEqual(datos['total'], Decimal('0.1'))
        self.assertEqual(str(datos['total']), '0.1')

    def test_un_producto_sin_variantes_igual_da_un_sku(self):
        pares = self.ingestor.variantes_de_producto({'id': 5, 'name': 'Suelto'})
        self.assertEqual(len(pares), 1)

        datos = self.ingestor.normalizar(ENTIDAD_PRODUCTO, pares[0])
        self.assertEqual(datos['sku'], 'TN-5-0')
        # '' y no None: en Postgres un NULL no colisiona consigo mismo y la
        # UNIQUE del mapeo dejaria entrar duplicados.
        self.assertEqual(datos['id_variante_externo'], '')

    def test_fechas_en_los_tres_formatos_de_offset(self):
        a_utc = ingestor_tiendanube.a_fecha_utc
        esperado = datetime(2026, 8, 14, 13, 20, 30)
        self.assertEqual(a_utc('2026-08-14T10:20:30-0300'), esperado)
        self.assertEqual(a_utc('2026-08-14T10:20:30-03:00'), esperado)
        self.assertEqual(a_utc('2026-08-14T13:20:30Z'), esperado)
        self.assertIsNone(a_utc(None))
        self.assertIsNone(a_utc('no es una fecha'))

    def test_nombres_multi_idioma(self):
        texto = ingestor_tiendanube.texto_idioma
        self.assertEqual(texto({'es': 'Martillo', 'pt': 'Martelo'}), 'Martillo')
        self.assertEqual(texto({'pt': 'Martelo'}), 'Martelo')
        self.assertEqual(texto('Martillo'), 'Martillo')
        self.assertEqual(texto(None, 'sin nombre'), 'sin nombre')

    def test_los_textos_se_cortan_al_largo_de_la_columna(self):
        """Un nombre largo no puede voltear el INSERT del pedido entero."""
        largo = {'id': 9, 'name': 'x' * 500, 'variants': [{'id': 90, 'sku': 'y' * 200}]}
        datos = self.ingestor.normalizar(
            ENTIDAD_PRODUCTO, self.ingestor.variantes_de_producto(largo)[0])
        self.assertLessEqual(len(datos['sku']), 60)
        self.assertLessEqual(len(datos['nombre']), 200)

    def test_una_entidad_desconocida_se_rechaza(self):
        from ingestor_canal import ErrorIngesta
        with self.assertRaises(ErrorIngesta):
            self.ingestor.normalizar('pago', {})

    def test_sin_token_o_sin_store_id_no_se_construye(self):
        from ingestor_canal import ErrorIngesta
        with self.assertRaises(ErrorIngesta):
            ingestor_tiendanube.IngestorTiendanube(1, {}, STORE_ID_FALSO)
        with self.assertRaises(ErrorIngesta):
            ingestor_tiendanube.IngestorTiendanube(1, {'access_token': 'x'}, None)


class TestRegresion(BaseSync):
    """Lo de FASE3-S1 y anteriores tiene que seguir andando."""

    def test_dashboard_sigue_dando_200(self):
        self.assertEqual(self.client.get('/dashboard').status_code, 200)

    def test_integraciones_sigue_dando_200(self):
        self.assertEqual(self.client.get('/integraciones').status_code, 200)

    def test_el_canal_conectado_sigue_mostrandose(self):
        pagina = self.client.get('/integraciones').get_data(as_text=True)
        self.assertIn('Conectado', pagina)
        self.assertIn('Ferreteria Roman', pagina)


class TestNoSePegaALaApiReal(unittest.TestCase):
    """Guarda explicita: ningun test de este modulo sale a internet."""

    def test_el_cliente_solo_sale_por_requests_get_y_post(self):
        # Los tests parchean requests.get. Si el cliente usara otra puerta
        # (Session, urllib), el parche no la taparia y la suite le pegaria a
        # Tiendanube de verdad.
        import inspect
        fuente = inspect.getsource(tn)
        self.assertNotIn('requests.request(', fuente)
        self.assertNotIn('requests.Session(', fuente)
        self.assertNotIn('urllib', fuente)

    def test_ni_el_ingestor_ni_el_sync_abren_conexiones_por_su_cuenta(self):
        import inspect
        for modulo in (ingestor_tiendanube, sync_tiendanube):
            fuente = inspect.getsource(modulo)
            self.assertNotIn('import requests', fuente, modulo.__name__)
            self.assertNotIn('urllib', fuente, modulo.__name__)


if __name__ == '__main__':
    unittest.main()
