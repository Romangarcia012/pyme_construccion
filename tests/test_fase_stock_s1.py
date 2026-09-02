# -*- coding: utf-8 -*-
"""Tests de FASE-STOCK-S1 (listado de stock y descuento a Tiendanube).

    python -m unittest discover -s tests -v

Dos cosas que hasta ahora no se tocaban entre si: la venta de mostrador
(FASE3-S4) y el stock que trae el resync de Tiendanube (FASE3-S3). Lo que se
prueba aca es el puente, y sobre todo que ese puente no pueda tirar abajo la
venta:

    stock suficiente      -> descuenta y le avisa a Tiendanube el valor nuevo
    stock insuficiente    -> la venta SE GUARDA IGUAL, el stock queda en 0
    la API de TN falla    -> la venta y el descuento local sobreviven
    producto sin mapeo    -> no se intenta ningun push, y no es un error
    stock NULL            -> ni se descuenta ni se empuja

Ninguna llamada sale a internet: `actualizar_stock_variante` se reemplaza por
un doble que anota lo que le pidieron. Lo que se verifica de esas llamadas es
el valor -- que sea el stock YA descontado y no el anterior ni el delta.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rutas_ventas  # noqa: E402
import stock_tiendanube  # noqa: E402
from integracion_tiendanube import ErrorTiendanube  # noqa: E402
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
from app import app  # noqa: E402

ENGINE_PRODUCTIVO = None

TOKEN_FALSO = 'token-de-mentira'


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


class ApiFalsa(object):
    """Doble de `actualizar_stock_variante`. Anota y, si se le pide, revienta.

    Registra (store_id, id_producto, id_variante, stock): con eso alcanza para
    afirmar que el PUT se armo contra la variante correcta y con el numero ya
    descontado.
    """

    def __init__(self, error=None):
        self.llamadas = []
        self.error = error

    def __call__(self, store_id, id_producto, id_variante, stock, access_token):
        self.llamadas.append({
            'store_id': store_id,
            'id_producto': id_producto,
            'id_variante': id_variante,
            'stock': stock,
            'token': access_token,
        })
        if self.error is not None:
            raise self.error
        return {'id': id_variante, 'stock': stock}


class BaseStock(unittest.TestCase):
    """Una empresa con catalogo, canal de Tiendanube conectado y mapeos.

    El catalogo cubre los cuatro casos que separan los tests:

        MART-500  stock 10, mapeado a Tiendanube      -> se descuenta y se empuja
        DEST-PH2  stock  1, mapeado a Tiendanube      -> alcanza para sobrevender
        CINTA-19  stock  5, SIN mapeo                 -> se descuenta, no se empuja
        LIJA-120  stock NULL, mapeado a Tiendanube    -> no se toca nada
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-STOCK-S1')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='fasestock@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                  nombre='Venta manual / presencial', activo=True)
        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True, id_tienda_externo='9999')
        db.session.add_all([canal_manual, self.canal_tn])
        db.session.flush()

        db.session.add(CredencialCanal(
            canal_id=self.canal_tn.id, tipo_credencial='oauth2',
            access_token_cifrado='cifrado-de-mentira', activo=True))

        self.con_stock = Producto(empresa_id=self.empresa.id, sku='MART-500',
                                  nombre='Martillo 500g', stock=10,
                                  costo_unitario=Decimal('1200.00'),
                                  precio_lista=Decimal('2500.00'))
        self.poco_stock = Producto(empresa_id=self.empresa.id, sku='DEST-PH2',
                                   nombre='Destornillador PH2', stock=1,
                                   costo_unitario=Decimal('400.00'),
                                   precio_lista=Decimal('900.00'))
        self.sin_mapeo = Producto(empresa_id=self.empresa.id, sku='CINTA-19',
                                  nombre='Cinta aisladora 19mm', stock=5,
                                  precio_lista=Decimal('300.00'))
        self.sin_control = Producto(empresa_id=self.empresa.id, sku='LIJA-120',
                                    nombre='Lija grano 120', stock=None,
                                    precio_lista=Decimal('150.00'))
        db.session.add_all([self.con_stock, self.poco_stock, self.sin_mapeo,
                            self.sin_control])
        db.session.flush()

        for producto, id_prod, id_var in (
            (self.con_stock, '111', '1111'),
            (self.poco_stock, '222', '2222'),
            (self.sin_control, '444', '4444'),
        ):
            db.session.add(MapeoProductoCanal(
                producto_id=producto.id, canal_id=self.canal_tn.id,
                id_producto_externo=id_prod, id_variante_externo=id_var,
                sku_externo=producto.sku))

        db.session.commit()

        self.empresa_id = self.empresa.id
        self.canal_tn_id = self.canal_tn.id
        self.usuario_id = self.usuario.id

        # El doble de la API. Se pincha en el modulo que lo USA
        # (stock_tiendanube), que es donde el nombre esta ligado.
        self.api = ApiFalsa()
        self.original_api = stock_tiendanube.actualizar_stock_variante
        stock_tiendanube.actualizar_stock_variante = self.api

        # Y el lector de credenciales: descifrar 'cifrado-de-mentira' fallaria,
        # y lo que se prueba aca no es el cifrado.
        self.original_token = stock_tiendanube._token_del_canal
        stock_tiendanube._token_del_canal = lambda canal: TOKEN_FALSO

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        stock_tiendanube.actualizar_stock_variante = self.original_api
        stock_tiendanube._token_del_canal = self.original_token
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def vender(self, items, medio='efectivo'):
        """POST al formulario de venta manual. `items`: (sku, cantidad, precio)."""
        datos = {
            'sku': [sku for sku, _, _ in items],
            'cantidad': [str(cant) for _, cant, _ in items],
            'precio_unitario': [str(precio) for _, _, precio in items],
            'fecha': date.today().isoformat(),
            'medio': medio,
            'nota': '',
        }
        return self.client.post('/pedidos/manual/nuevo', data=datos,
                                follow_redirects=True)

    def stock_de(self, sku):
        return Producto.query.filter_by(
            empresa_id=self.empresa_id, sku=sku).first().stock

    def texto(self, respuesta):
        return respuesta.get_data(as_text=True)


class TestVentaConStockSuficiente(BaseStock):
    """El caso central: hay stock, se descuenta y Tiendanube se entera."""

    def setUp(self):
        super().setUp()
        self.respuesta = self.vender([('MART-500', 3, '2500.00')])

    def test_el_post_termina_bien(self):
        self.assertEqual(self.respuesta.status_code, 200)

    def test_la_venta_se_guardo(self):
        self.assertEqual(Pedido.query.count(), 1)
        self.assertEqual(PedidoItem.query.count(), 1)

    def test_el_stock_local_bajo_por_la_cantidad_vendida(self):
        self.assertEqual(self.stock_de('MART-500'), 7)

    def test_se_llamo_una_sola_vez_a_la_api(self):
        self.assertEqual(len(self.api.llamadas), 1)

    def test_el_push_va_a_la_variante_correcta_con_el_stock_ya_descontado(self):
        llamada = self.api.llamadas[0]
        self.assertEqual(llamada['store_id'], '9999')
        self.assertEqual(llamada['id_producto'], '111')
        self.assertEqual(llamada['id_variante'], '1111')
        # Lo que importa: el valor ABSOLUTO que queda, no el 10 anterior ni el 3.
        self.assertEqual(llamada['stock'], 7)
        self.assertEqual(llamada['token'], TOKEN_FALSO)

    def test_no_hay_aviso_de_error_ni_fila_de_sync_log(self):
        self.assertNotIn('no se pudo actualizar el stock', self.texto(self.respuesta))
        self.assertEqual(SyncLog.query.filter_by(entidad='stock_push').count(), 0)


class TestVentaPorEncimaDelStock(BaseStock):
    """Se vendieron 4 de un producto del que el sistema creia tener 1."""

    def setUp(self):
        super().setUp()
        self.respuesta = self.vender([('DEST-PH2', 4, '900.00')])

    def test_la_venta_se_guarda_igual(self):
        # La mercaderia ya salio del mostrador: no hay forma de deshacerlo.
        pedido = Pedido.query.one()
        self.assertEqual(pedido.total, Decimal('3600.00'))
        self.assertEqual(PedidoItem.query.count(), 1)

    def test_el_stock_queda_en_cero_y_no_negativo(self):
        self.assertEqual(self.stock_de('DEST-PH2'), 0)

    def test_avisa_que_se_vendio_de_mas(self):
        texto = self.texto(self.respuesta)
        self.assertIn('más de lo que figuraba en stock', texto)
        self.assertIn('Destornillador PH2', texto)

    def test_el_push_manda_el_cero_que_quedo(self):
        self.assertEqual(len(self.api.llamadas), 1)
        self.assertEqual(self.api.llamadas[0]['stock'], 0)


class TestFallaElPushATiendanube(BaseStock):
    """La API responde mal (token sin permiso de escritura, por ejemplo)."""

    def setUp(self):
        super().setUp()
        self.api.error = ErrorTiendanube(
            'Tiendanube no autoriza a modificar el stock: a la app le falta el '
            'permiso para editar productos. Volve a conectar la tienda.',
            detalle='HTTP 403 en PUT 9999/products/111/variants/1111')
        self.respuesta = self.vender([('MART-500', 2, '2500.00')])

    def test_la_venta_queda_guardada(self):
        self.assertEqual(Pedido.query.count(), 1)
        self.assertEqual(Pedido.query.one().total, Decimal('5000.00'))

    def test_el_descuento_local_queda_guardado(self):
        # El push es best-effort; el descuento local no. El proximo resync de
        # Tiendanube va a pisar este numero de todos modos.
        self.assertEqual(self.stock_de('MART-500'), 8)

    def test_aparece_el_aviso_con_el_producto(self):
        texto = self.texto(self.respuesta)
        self.assertIn('no se pudo actualizar el stock en Tiendanube', texto)
        self.assertIn('Martillo 500g', texto)

    def test_queda_la_fila_en_sync_log(self):
        filas = SyncLog.query.filter_by(entidad='stock_push').all()
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].estado, 'error')
        self.assertEqual(filas[0].canal_id, self.canal_tn_id)
        self.assertEqual(filas[0].registros_error, 1)
        self.assertIn('MART-500', filas[0].mensaje_error)
        self.assertIn('403', filas[0].mensaje_error)


class TestUnaLineaFallaYLaOtraNo(BaseStock):
    """Un fallo por linea no puede impedir que las demas se intenten."""

    def setUp(self):
        super().setUp()

        fallar_solo_el_martillo = ApiFalsa()
        original = fallar_solo_el_martillo.__call__

        def reventar_uno(store_id, id_producto, id_variante, stock, access_token):
            resultado = original(store_id, id_producto, id_variante, stock, access_token)
            if id_producto == '111':
                raise ErrorTiendanube('Falló el martillo.', detalle='HTTP 500')
            return resultado

        self.api = fallar_solo_el_martillo
        stock_tiendanube.actualizar_stock_variante = reventar_uno

        self.respuesta = self.vender([
            ('MART-500', 1, '2500.00'),
            ('DEST-PH2', 1, '900.00'),
        ])

    def test_se_intentaron_las_dos_lineas(self):
        self.assertEqual(len(self.api.llamadas), 2)

    def test_solo_el_que_fallo_queda_registrado(self):
        self.assertIn('no se pudo actualizar el stock en Tiendanube',
                      self.texto(self.respuesta))
        filas = SyncLog.query.filter_by(entidad='stock_push').all()
        self.assertEqual(len(filas), 1)
        self.assertIn('MART-500', filas[0].mensaje_error)
        self.assertNotIn('DEST-PH2', filas[0].mensaje_error)

    def test_las_dos_lineas_descontaron_localmente(self):
        self.assertEqual(self.stock_de('MART-500'), 9)
        self.assertEqual(self.stock_de('DEST-PH2'), 0)


class TestProductoSinMapeoATiendanube(BaseStock):
    """Un producto que no vino de Tiendanube. No hay push, y no es un error."""

    def setUp(self):
        super().setUp()
        self.respuesta = self.vender([('CINTA-19', 2, '300.00')])

    def test_el_stock_local_igual_se_descuenta(self):
        self.assertEqual(self.stock_de('CINTA-19'), 3)

    def test_no_se_intento_ningun_push(self):
        self.assertEqual(self.api.llamadas, [])

    def test_no_hay_aviso_ni_sync_log(self):
        self.assertNotIn('no se pudo actualizar el stock', self.texto(self.respuesta))
        self.assertEqual(SyncLog.query.filter_by(entidad='stock_push').count(), 0)


class TestProductoSinControlDeStock(BaseStock):
    """stock NULL: nadie lleva la cuenta. Ni se descuenta ni se empuja."""

    def setUp(self):
        super().setUp()
        self.respuesta = self.vender([('LIJA-120', 7, '150.00')])

    def test_la_venta_se_guarda(self):
        self.assertEqual(Pedido.query.count(), 1)

    def test_el_stock_sigue_siendo_null_y_no_cae_a_cero(self):
        # Si esto se rompe, "no se lleva la cuenta" se convirtio en "no queda
        # ninguno", que es justo lo contrario.
        self.assertIsNone(self.stock_de('LIJA-120'))

    def test_no_se_intento_ningun_push(self):
        self.assertEqual(self.api.llamadas, [])

    def test_no_hay_aviso(self):
        self.assertNotIn('no se pudo actualizar el stock', self.texto(self.respuesta))


class TestVentaMixta(BaseStock):
    """Las cuatro clases de producto en la misma venta."""

    def setUp(self):
        super().setUp()
        self.respuesta = self.vender([
            ('MART-500', 2, '2500.00'),
            ('CINTA-19', 1, '300.00'),
            ('LIJA-120', 3, '150.00'),
        ])

    def test_se_empuja_solo_el_que_tiene_mapeo_y_control_de_stock(self):
        self.assertEqual(len(self.api.llamadas), 1)
        self.assertEqual(self.api.llamadas[0]['id_producto'], '111')
        self.assertEqual(self.api.llamadas[0]['stock'], 8)

    def test_los_descuentos_locales_son_los_esperados(self):
        self.assertEqual(self.stock_de('MART-500'), 8)
        self.assertEqual(self.stock_de('CINTA-19'), 4)
        self.assertIsNone(self.stock_de('LIJA-120'))


class TestElMismoProductoEnDosLineas(BaseStock):
    """Dos lineas del mismo SKU descuentan las dos, y se empuja una sola vez."""

    def setUp(self):
        super().setUp()
        self.respuesta = self.vender([
            ('MART-500', 2, '2500.00'),
            ('MART-500', 3, '2500.00'),
        ])

    def test_el_descuento_es_acumulado(self):
        self.assertEqual(self.stock_de('MART-500'), 5)

    def test_se_empuja_una_sola_vez_con_el_total_descontado(self):
        self.assertEqual(len(self.api.llamadas), 1)
        self.assertEqual(self.api.llamadas[0]['stock'], 5)


class TestSinCanalDeTiendanube(BaseStock):
    """Tienda desconectada: se descuenta local y no se intenta nada."""

    def setUp(self):
        super().setUp()
        canal = db.session.get(CanalVenta, self.canal_tn_id)
        canal.activo = False
        db.session.commit()
        self.respuesta = self.vender([('MART-500', 1, '2500.00')])

    def test_el_stock_local_se_descuenta_igual(self):
        self.assertEqual(self.stock_de('MART-500'), 9)

    def test_no_se_llama_a_la_api_ni_se_avisa_nada(self):
        self.assertEqual(self.api.llamadas, [])
        self.assertNotIn('no se pudo actualizar el stock', self.texto(self.respuesta))


class TestCredencialIlegible(BaseStock):
    """Sin token no hay push, pero la venta y el descuento local sobreviven."""

    def setUp(self):
        super().setUp()

        def explotar(canal):
            raise RuntimeError('la credencial no se pudo descifrar')

        stock_tiendanube._token_del_canal = explotar
        self.respuesta = self.vender([('MART-500', 1, '2500.00')])

    def test_la_venta_y_el_descuento_sobreviven(self):
        self.assertEqual(Pedido.query.count(), 1)
        self.assertEqual(self.stock_de('MART-500'), 9)

    def test_avisa_y_deja_rastro(self):
        self.assertIn('no se pudo actualizar el stock en Tiendanube',
                      self.texto(self.respuesta))
        filas = SyncLog.query.filter_by(entidad='stock_push').all()
        self.assertEqual(len(filas), 1)
        self.assertIn('credencial', filas[0].mensaje_error)


class TestMapeoSinVariante(BaseStock):
    """Un mapeo sin id de variante no tiene a donde escribir: se avisa."""

    def setUp(self):
        super().setUp()
        mapeo = MapeoProductoCanal.query.filter_by(
            canal_id=self.canal_tn_id, id_producto_externo='111').first()
        mapeo.id_variante_externo = ''
        db.session.commit()
        self.respuesta = self.vender([('MART-500', 1, '2500.00')])

    def test_no_se_llama_a_la_api(self):
        self.assertEqual(self.api.llamadas, [])

    def test_avisa_y_deja_rastro(self):
        self.assertIn('no se pudo actualizar el stock en Tiendanube',
                      self.texto(self.respuesta))
        self.assertEqual(SyncLog.query.filter_by(entidad='stock_push').count(), 1)

    def test_el_descuento_local_igual_quedo(self):
        self.assertEqual(self.stock_de('MART-500'), 9)


class TestListadoDeStock(BaseStock):
    """La pantalla de la Parte 1."""

    def setUp(self):
        super().setUp()
        self.respuesta = self.client.get('/productos/listar')
        self.html = self.texto(self.respuesta)

    def test_responde_ok(self):
        self.assertEqual(self.respuesta.status_code, 200)

    def test_estan_los_cuatro_productos_con_su_sku(self):
        for sku in ('MART-500', 'DEST-PH2', 'CINTA-19', 'LIJA-120'):
            self.assertIn(sku, self.html)
        self.assertIn('Martillo 500g', self.html)

    def test_el_producto_sin_control_no_muestra_un_numero(self):
        self.assertIn('sin control de stock', self.html)

    def test_muestra_el_canal_de_origen(self):
        self.assertIn('Tiendanube', self.html)

    def test_pide_login(self):
        # El app_context que deja pusheado setUp se saca a proposito antes de
        # este request: flask_login cachea el usuario en `g`, `g` vive en el
        # app_context, y Flask reusa el que ya esta arriba en vez de crear uno
        # nuevo. Sin este pop, un cliente sin cookies igual entraria con el
        # usuario que dejo cargado el request anterior, y el test pasaria
        # siempre -- incluso si a la ruta le faltara el @login_required.
        self.ctx.pop()
        try:
            respuesta = app.test_client().get('/productos/listar')
        finally:
            self.ctx.push()

        self.assertIn(respuesta.status_code, (301, 302))
        self.assertIn('/login', respuesta.headers.get('Location', ''))

    def test_no_muestra_productos_de_otra_empresa(self):
        otra = Empresa(nombre='Ferretería Ajena')
        db.session.add(otra)
        db.session.flush()
        db.session.add(Producto(empresa_id=otra.id, sku='AJENO-1',
                                nombre='Producto de otra empresa', stock=99))
        db.session.commit()

        html = self.texto(self.client.get('/productos/listar'))
        self.assertNotIn('AJENO-1', html)
        self.assertNotIn('Producto de otra empresa', html)


class TestDescontarStockAislado(unittest.TestCase):
    """La funcion de descuento, sin request de por medio.

    No necesita base: opera sobre los objetos que le pasan.
    """

    class ProductoFalso(object):
        def __init__(self, id, nombre, stock):
            self.id = id
            self.nombre = nombre
            self.stock = stock

    def _items(self, *pares):
        return [{'producto': producto, 'cantidad': cantidad}
                for producto, cantidad in pares]

    def test_descuenta_y_devuelve_el_id(self):
        producto = self.ProductoFalso(1, 'Martillo', 10)
        sobrevendidos, ids = rutas_ventas._descontar_stock(self._items((producto, 4)))
        self.assertEqual(producto.stock, 6)
        self.assertEqual(sobrevendidos, [])
        self.assertEqual(ids, [1])

    def test_el_negativo_se_guarda_como_cero_y_se_avisa(self):
        producto = self.ProductoFalso(1, 'Martillo', 2)
        sobrevendidos, ids = rutas_ventas._descontar_stock(self._items((producto, 5)))
        self.assertEqual(producto.stock, 0)
        self.assertEqual(sobrevendidos, ['Martillo'])
        self.assertEqual(ids, [1])

    def test_el_stock_null_no_se_toca_ni_se_reporta(self):
        producto = self.ProductoFalso(1, 'Lija', None)
        sobrevendidos, ids = rutas_ventas._descontar_stock(self._items((producto, 5)))
        self.assertIsNone(producto.stock)
        self.assertEqual(sobrevendidos, [])
        self.assertEqual(ids, [])

    def test_dejar_el_stock_exactamente_en_cero_no_es_sobreventa(self):
        # Vender la ultima unidad es correcto: no hay nada que avisar.
        producto = self.ProductoFalso(1, 'Martillo', 3)
        sobrevendidos, _ = rutas_ventas._descontar_stock(self._items((producto, 3)))
        self.assertEqual(producto.stock, 0)
        self.assertEqual(sobrevendidos, [])

    def test_el_mismo_producto_se_reporta_una_sola_vez(self):
        producto = self.ProductoFalso(1, 'Martillo', 1)
        sobrevendidos, ids = rutas_ventas._descontar_stock(
            self._items((producto, 5), (producto, 5)))
        self.assertEqual(producto.stock, 0)
        self.assertEqual(sobrevendidos, ['Martillo'])
        self.assertEqual(ids, [1])


if __name__ == '__main__':
    unittest.main()
