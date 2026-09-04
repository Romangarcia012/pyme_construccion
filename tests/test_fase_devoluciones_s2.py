# -*- coding: utf-8 -*-
"""Tests de FASE-DEVOLUCIONES-S2 (devoluciones a nivel de item + vuelta de stock).

    python -m unittest discover -s tests -v

`devolucion` existia desde FASE2-S1 y no la escribia nadie. Esta slice le
agrega el vinculo con la linea del pedido, la pantalla para cargarla y el
movimiento de stock. Lo que se prueba aca son las dos mitades:

    QUE SE MUEVE          stock sube por la cantidad devuelta, sumando y no
                          pisando; una sola vez por cadena append-only; se le
                          avisa a Tiendanube si hay mapeo
    QUE NO SE MUEVE       vendido, margen, ingreso y ganancia dan EXACTAMENTE
                          lo mismo que antes de la devolucion (opcion C: lo
                          devuelto es una columna al lado, no una resta)

Los tests de reportes hacen el GET dos veces -- antes y despues de devolver --
y comparan contra si mismos. No alcanza con afirmar un numero esperado: lo que
hay que demostrar es que el numero no CAMBIO, y eso solo se ve teniendo el de
antes.

Ninguna llamada sale a internet: `actualizar_stock_variante` se reemplaza por
un doble, igual que en FASE-STOCK-S1. La app se repunta a SQLite en memoria y
la base productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rutas_devoluciones  # noqa: E402
import rutas_productos  # noqa: E402
import stock_tiendanube  # noqa: E402
from integracion_tiendanube import ErrorTiendanube  # noqa: E402
from models import (  # noqa: E402
    DEVOLUCION_ABIERTA,
    DEVOLUCION_CERRADA,
    CanalVenta,
    CredencialCanal,
    Devolucion,
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
from tests.ayuda_auth import request_anonimo  # noqa: E402

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

    Mismo doble que FASE-STOCK-S1: lo que interesa afirmar es el valor que se
    empuja, que tiene que ser el stock YA sumado y no un delta.
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


class BaseDevoluciones(unittest.TestCase):
    """Una empresa con catalogo, canal de Tiendanube conectado y mapeos.

    Mismo catalogo que FASE-STOCK-S1, por los mismos cuatro casos:

        MART-500  stock 10, mapeado, con costo   -> el caso central
        DEST-PH2  stock  1, mapeado              -> sirve para sobrevender
        CINTA-19  stock  5, SIN mapeo            -> se suma local, no se empuja
        LIJA-120  stock NULL, mapeado            -> no se toca nada
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-DEVOLUCIONES-S2')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='fasedev@test.local',
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
                                  costo_unitario=Decimal('100.00'),
                                  precio_lista=Decimal('300.00'))
        self.sin_control = Producto(empresa_id=self.empresa.id, sku='LIJA-120',
                                    nombre='Lija grano 120', stock=None,
                                    costo_unitario=Decimal('50.00'),
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

        self.api = ApiFalsa()
        self.original_api = stock_tiendanube.actualizar_stock_variante
        stock_tiendanube.actualizar_stock_variante = self.api

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

    def devolver(self, item_id, cantidad, motivo=''):
        """POST al formulario de devolucion del pedido de ese item."""
        item = db.session.get(PedidoItem, item_id)
        return self.client.post(
            '/devoluciones/nueva/%d' % item.pedido_id,
            data={'pedido_item_id': str(item_id),
                  'cantidad': str(cantidad),
                  'motivo': motivo},
            follow_redirects=True)

    def item_de(self, sku):
        """La linea de pedido del producto con ese SKU. Asume una sola."""
        producto = Producto.query.filter_by(
            empresa_id=self.empresa_id, sku=sku).one()
        return PedidoItem.query.filter_by(producto_id=producto.id).one()

    def stock_de(self, sku):
        return Producto.query.filter_by(
            empresa_id=self.empresa_id, sku=sku).first().stock

    def texto(self, respuesta):
        return respuesta.get_data(as_text=True)

    def crear_devolucion(self, item, cantidad, estado=DEVOLUCION_CERRADA,
                         evento_previo=None):
        """Una fila de devolucion escrita a mano, sin pasar por la pantalla.

        La usan los tests que necesitan armar una CADENA, que es algo que la
        pantalla no sabe hacer (crea siempre cadenas de una sola fila).
        """
        devolucion = Devolucion(
            pedido_id=item.pedido_id,
            pedido_item_id=item.id,
            cantidad=cantidad,
            tipo='devolucion',
            moneda='ARS',
            monto=Decimal('0.00'),
            estado=estado,
            fecha_evento=datetime.utcnow(),
            evento_previo_id=evento_previo.id if evento_previo else None,
        )
        db.session.add(devolucion)
        db.session.flush()
        return devolucion


# ==========================================================================
# El caso central
# ==========================================================================


class TestDevolucionDeUnItem(BaseDevoluciones):
    """Vender 5 y devolver 2: el stock sube en 2, no se pisa con un absoluto."""

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 5, '2500.00')])
        self.assertEqual(self.stock_de('MART-500'), 5)   # 10 - 5, precondicion
        self.api.llamadas = []                            # el push de la venta ya paso

        self.item_id = self.item_de('MART-500').id
        self.respuesta = self.devolver(self.item_id, 2, motivo='Vino fallado')

    def test_el_post_termina_bien(self):
        self.assertEqual(self.respuesta.status_code, 200)

    def test_el_stock_sube_por_la_cantidad_devuelta(self):
        # 5 que habian quedado + 2 que volvieron. Si esto diera 2, la
        # devolucion estaria PISANDO el stock con la cantidad devuelta en vez
        # de sumarsela, que es el error que este numero existe para atrapar.
        self.assertEqual(self.stock_de('MART-500'), 7)

    def test_queda_una_fila_de_devolucion_con_su_item_y_su_cantidad(self):
        devolucion = Devolucion.query.one()
        self.assertEqual(devolucion.pedido_item_id, self.item_id)
        self.assertEqual(devolucion.cantidad, 2)
        self.assertEqual(devolucion.tipo, 'devolucion')
        self.assertEqual(devolucion.estado, DEVOLUCION_CERRADA)
        self.assertEqual(devolucion.motivo, 'Vino fallado')

    def test_el_monto_es_lo_que_se_cobro_por_esas_unidades(self):
        # 2 x 2500, el precio de la LINEA. No 0 (seria afirmar que no volvio
        # un peso) ni el precio de lista de hoy.
        self.assertEqual(Devolucion.query.one().monto, Decimal('5000.00'))

    def test_avisa_que_quedo_registrada(self):
        self.assertIn('Devolución registrada', self.texto(self.respuesta))

    def test_la_venta_no_se_toca(self):
        # La devolucion NO borra ni edita el pedido: son dos hechos distintos.
        self.assertEqual(Pedido.query.count(), 1)
        self.assertEqual(PedidoItem.query.one().cantidad, 5)


class TestDevolucionParcialYDespuesElResto(BaseDevoluciones):
    """Dos devoluciones sucesivas del mismo item suman las dos."""

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 5, '2500.00')])
        self.item_id = self.item_de('MART-500').id
        self.devolver(self.item_id, 2)
        self.respuesta = self.devolver(self.item_id, 3)

    def test_el_stock_acumula_las_dos(self):
        # 10 - 5 vendidas + 2 + 3 devueltas = 10, todo de vuelta.
        self.assertEqual(self.stock_de('MART-500'), 10)

    def test_quedan_dos_filas_independientes(self):
        # Dos devoluciones distintas, no una cadena: ninguna reemplaza a la
        # otra, las dos valen.
        devoluciones = Devolucion.query.all()
        self.assertEqual(len(devoluciones), 2)
        self.assertEqual([d.evento_previo_id for d in devoluciones], [None, None])


# ==========================================================================
# La validacion contra el pedido
# ==========================================================================


class TestNoSePuedeDevolverMasDeLoVendido(BaseDevoluciones):
    """Devolver 4 de un item que llevaba 3 es un tipeo, no un hecho."""

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 3, '2500.00')])
        self.api.llamadas = []
        self.item_id = self.item_de('MART-500').id
        self.respuesta = self.devolver(self.item_id, 4)

    def test_se_rechaza_con_un_mensaje_claro(self):
        texto = self.texto(self.respuesta)
        self.assertIn('se vendieron 3', texto)
        self.assertIn('no se pueden devolver 4', texto)
        self.assertIn('Martillo 500g', texto)

    def test_no_se_guardo_ninguna_devolucion(self):
        self.assertEqual(Devolucion.query.count(), 0)

    def test_el_stock_no_se_movio(self):
        self.assertEqual(self.stock_de('MART-500'), 7)

    def test_no_se_empujo_nada_a_tiendanube(self):
        self.assertEqual(self.api.llamadas, [])


class TestNoSePuedeDevolverDeMasEntreDosCargas(BaseDevoluciones):
    """Devolver 2 y despues 2 mas de un item de 3 tampoco entra.

    Sin contar lo ya devuelto, cada carga se valida sola contra el pedido y
    entre las dos devuelven 4 de 3.
    """

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 3, '2500.00')])
        self.item_id = self.item_de('MART-500').id
        self.devolver(self.item_id, 2)
        self.respuesta = self.devolver(self.item_id, 2)

    def test_la_segunda_se_rechaza_diciendo_cuanto_ya_volvio(self):
        texto = self.texto(self.respuesta)
        self.assertIn('ya se devolvieron 2', texto)

    def test_quedo_solo_la_primera(self):
        self.assertEqual(Devolucion.query.count(), 1)

    def test_el_stock_refleja_solo_la_primera(self):
        self.assertEqual(self.stock_de('MART-500'), 9)   # 10 - 3 + 2


class TestCantidadInvalida(BaseDevoluciones):
    """Cero, negativo y basura se rechazan antes de tocar nada."""

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 3, '2500.00')])
        self.item_id = self.item_de('MART-500').id

    def test_cero_se_rechaza(self):
        respuesta = self.devolver(self.item_id, 0)
        self.assertIn('mayor a cero', self.texto(respuesta))
        self.assertEqual(Devolucion.query.count(), 0)

    def test_negativo_se_rechaza(self):
        respuesta = self.devolver(self.item_id, -2)
        self.assertIn('mayor a cero', self.texto(respuesta))
        self.assertEqual(Devolucion.query.count(), 0)

    def test_texto_que_no_es_numero_se_rechaza(self):
        respuesta = self.client.post(
            '/devoluciones/nueva/%d' % db.session.get(PedidoItem, self.item_id).pedido_id,
            data={'pedido_item_id': str(self.item_id), 'cantidad': 'dos', 'motivo': ''},
            follow_redirects=True)
        self.assertIn('número entero', self.texto(respuesta))
        self.assertEqual(Devolucion.query.count(), 0)

    def test_un_item_de_otro_pedido_se_rechaza(self):
        # El item existe, pero no es de este pedido: cambiar el numero del
        # formulario no puede devolver stock de una venta ajena.
        self.vender([('CINTA-19', 1, '300.00')])
        ajeno = self.item_de('CINTA-19')
        respuesta = self.client.post(
            '/devoluciones/nueva/%d' % db.session.get(PedidoItem, self.item_id).pedido_id,
            data={'pedido_item_id': str(ajeno.id), 'cantidad': '1', 'motivo': ''},
            follow_redirects=True)
        self.assertIn('no es parte de este pedido', self.texto(respuesta))
        self.assertEqual(Devolucion.query.count(), 0)


class TestCheckDeLaBase(BaseDevoluciones):
    """El CHECK del esquema, sin pasar por la pantalla.

    El formulario no es el unico camino de escritura: un script o una
    migracion escriben SQL crudo y no pasan por ninguna validacion de Python.
    """

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 3, '2500.00')])
        self.item = self.item_de('MART-500')

    def _rechaza(self, **campos):
        base = dict(pedido_id=self.item.pedido_id, tipo='devolucion',
                    moneda='ARS', monto=Decimal('0.00'),
                    estado=DEVOLUCION_CERRADA, fecha_evento=datetime.utcnow())
        base.update(campos)
        db.session.add(Devolucion(**base))
        with self.assertRaises(Exception):
            db.session.flush()
        db.session.rollback()

    def test_item_sin_cantidad_no_entra(self):
        self._rechaza(pedido_item_id=self.item.id, cantidad=None)

    def test_cantidad_sin_item_no_entra(self):
        self._rechaza(pedido_item_id=None, cantidad=3)

    def test_cantidad_cero_no_entra(self):
        self._rechaza(pedido_item_id=self.item.id, cantidad=0)

    def test_cantidad_negativa_no_entra(self):
        self._rechaza(pedido_item_id=self.item.id, cantidad=-1)

    def test_un_evento_de_plata_sin_item_ni_cantidad_si_entra(self):
        # Un contracargo: al comprador le devolvieron el dinero y se quedo con
        # el producto. No hay ninguna cantidad que poner, y el esquema no
        # obliga a inventarla.
        db.session.add(Devolucion(
            pedido_id=self.item.pedido_id, pedido_item_id=None, cantidad=None,
            tipo='contracargo', moneda='ARS', monto=Decimal('7500.00'),
            estado=DEVOLUCION_ABIERTA, fecha_evento=datetime.utcnow()))
        db.session.flush()
        self.assertEqual(Devolucion.query.count(), 1)


# ==========================================================================
# Stock NULL y el item sin producto
# ==========================================================================


class TestDevolucionConStockNone(BaseDevoluciones):
    """Producto sin control de stock: la devolucion se registra igual."""

    def setUp(self):
        super().setUp()
        self.vender([('LIJA-120', 7, '150.00')])
        self.api.llamadas = []
        self.item_id = self.item_de('LIJA-120').id
        self.respuesta = self.devolver(self.item_id, 3)

    def test_la_devolucion_queda_guardada(self):
        devolucion = Devolucion.query.one()
        self.assertEqual(devolucion.cantidad, 3)

    def test_el_stock_sigue_en_none_y_no_arranca_en_tres(self):
        # Sumarle a un None lo convertiria en un numero, o sea inventaria un
        # control de stock que nadie pidio. NULL no es 0 y tampoco es 3.
        self.assertIsNone(self.stock_de('LIJA-120'))

    def test_no_se_empuja_nada_a_tiendanube(self):
        self.assertEqual(self.api.llamadas, [])

    def test_avisa_que_no_hubo_nada_que_sumar(self):
        self.assertIn('no lleva control de stock', self.texto(self.respuesta))


class TestDevolucionDeUnItemSinProducto(BaseDevoluciones):
    """Una linea que ningun mapeo pudo atar al catalogo (producto_id NULL).

    Se puede devolver: el evento queda registrado aunque no haya inventario
    que corregir. Lo que no puede es romper.
    """

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 3, '2500.00')])
        item = self.item_de('MART-500')
        item.producto_id = None
        db.session.commit()
        self.api.llamadas = []
        self.item_id = item.id
        self.respuesta = self.devolver(self.item_id, 1)

    def test_la_devolucion_queda_guardada(self):
        self.assertEqual(Devolucion.query.one().cantidad, 1)

    def test_no_se_movio_ningun_stock(self):
        self.assertEqual(self.stock_de('MART-500'), 7)

    def test_avisa_que_no_habia_producto_al_que_corregirle_el_stock(self):
        self.assertIn('no está asociado a ningún producto', self.texto(self.respuesta))

    def test_no_se_empuja_nada(self):
        self.assertEqual(self.api.llamadas, [])


# ==========================================================================
# El push a Tiendanube -- reusado, no duplicado
# ==========================================================================


class TestDevolucionEmpujaStockATiendanube(BaseDevoluciones):
    """Con mapeo: se le avisa a la tienda el numero nuevo."""

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 5, '2500.00')])
        self.api.llamadas = []
        self.respuesta = self.devolver(self.item_de('MART-500').id, 2)

    def test_se_llamo_una_sola_vez(self):
        self.assertEqual(len(self.api.llamadas), 1)

    def test_va_a_la_variante_correcta_con_el_stock_ya_sumado(self):
        llamada = self.api.llamadas[0]
        self.assertEqual(llamada['store_id'], '9999')
        self.assertEqual(llamada['id_producto'], '111')
        self.assertEqual(llamada['id_variante'], '1111')
        # El ABSOLUTO que quedo (5 + 2), no el 5 anterior ni el delta de 2.
        # Es el mismo contrato que usa la venta: `empujar_stock` relee de la
        # base despues del commit.
        self.assertEqual(llamada['stock'], 7)
        self.assertEqual(llamada['token'], TOKEN_FALSO)

    def test_no_hay_aviso_de_error(self):
        self.assertNotIn('no se pudo actualizar el stock', self.texto(self.respuesta))
        self.assertEqual(SyncLog.query.filter_by(entidad='stock_push').count(), 0)


class TestDevolucionSinMapeoNoIntentaEmpujar(BaseDevoluciones):
    """Producto de alta manual: la devolucion es local y no es un error."""

    def setUp(self):
        super().setUp()
        self.vender([('CINTA-19', 3, '300.00')])
        self.api.llamadas = []
        self.respuesta = self.devolver(self.item_de('CINTA-19').id, 2)

    def test_el_stock_local_sube_igual(self):
        self.assertEqual(self.stock_de('CINTA-19'), 4)   # 5 - 3 + 2

    def test_no_se_intento_ningun_push(self):
        self.assertEqual(self.api.llamadas, [])

    def test_no_hay_aviso_ni_fila_de_sync_log(self):
        self.assertNotIn('no se pudo actualizar el stock', self.texto(self.respuesta))
        self.assertEqual(SyncLog.query.filter_by(entidad='stock_push').count(), 0)


class TestFallaElPushDeLaDevolucion(BaseDevoluciones):
    """La API responde mal. La devolucion y el stock local sobreviven."""

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 5, '2500.00')])
        self.api.llamadas = []
        self.api.error = ErrorTiendanube(
            'Tiendanube no autoriza a modificar el stock.',
            detalle='HTTP 403 en PUT 9999/products/111/variants/1111')
        self.respuesta = self.devolver(self.item_de('MART-500').id, 2)

    def test_la_devolucion_quedo_guardada(self):
        self.assertEqual(Devolucion.query.one().cantidad, 2)

    def test_el_stock_local_quedo_sumado(self):
        # El push es best-effort; la suma local no.
        self.assertEqual(self.stock_de('MART-500'), 7)

    def test_avisa_y_deja_rastro(self):
        self.assertIn('no se pudo actualizar el stock en Tiendanube',
                      self.texto(self.respuesta))
        filas = SyncLog.query.filter_by(entidad='stock_push').all()
        self.assertEqual(len(filas), 1)
        self.assertIn('403', filas[0].mensaje_error)


# ==========================================================================
# La cadena append-only
# ==========================================================================


class TestElStockNoSeSumaDosVecesEnLaCadena(BaseDevoluciones):
    """Dos filas encadenadas por el mismo item no duplican la suma.

    `devolucion` es append-only: cada cambio de estado entra como fila nueva
    que apunta a la anterior. Si el stock se moviera "cuando hay una
    devolucion", un contracargo revisado tres veces sumaria tres veces.
    """

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 5, '2500.00')])
        self.item = self.item_de('MART-500')

    def test_la_segunda_fila_de_la_cadena_no_vuelve_a_sumar(self):
        primera = self.crear_devolucion(self.item, 2)
        self.assertEqual(rutas_devoluciones.devolver_stock(primera), self.con_stock.id)
        self.assertEqual(self.stock_de('MART-500'), 7)

        segunda = self.crear_devolucion(self.item, 2, evento_previo=primera)
        self.assertIsNone(rutas_devoluciones.devolver_stock(segunda))
        self.assertEqual(self.stock_de('MART-500'), 7)

    def test_una_tercera_fila_tampoco(self):
        # El chequeo sube por TODA la cadena, no solo mira al padre directo.
        primera = self.crear_devolucion(self.item, 2)
        rutas_devoluciones.devolver_stock(primera)
        segunda = self.crear_devolucion(self.item, 2, evento_previo=primera)
        tercera = self.crear_devolucion(self.item, 2, evento_previo=segunda)

        self.assertIsNone(rutas_devoluciones.devolver_stock(tercera))
        self.assertEqual(self.stock_de('MART-500'), 7)

    def test_una_cadena_que_arranca_abierta_suma_al_cerrar(self):
        # El caso al derecho: mientras el reclamo esta abierto la mercaderia
        # no volvio y el stock no se toca. Cuando cierra, se mueve una vez.
        abierta = self.crear_devolucion(self.item, 2, estado=DEVOLUCION_ABIERTA)
        self.assertIsNone(rutas_devoluciones.devolver_stock(abierta))
        self.assertEqual(self.stock_de('MART-500'), 5)

        cerrada = self.crear_devolucion(self.item, 2, evento_previo=abierta)
        self.assertEqual(rutas_devoluciones.devolver_stock(cerrada), self.con_stock.id)
        self.assertEqual(self.stock_de('MART-500'), 7)

    def test_dos_cadenas_distintas_suman_las_dos(self):
        # Sin evento_previo_id son dos hechos independientes, no un mismo
        # hecho revisado dos veces.
        primera = self.crear_devolucion(self.item, 2)
        rutas_devoluciones.devolver_stock(primera)
        segunda = self.crear_devolucion(self.item, 1)
        rutas_devoluciones.devolver_stock(segunda)
        self.assertEqual(self.stock_de('MART-500'), 8)


class TestLoDevueltoQueCuentaEsLaFilaVigente(BaseDevoluciones):
    """La agregacion de los reportes cuenta una fila por cadena."""

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 5, '2500.00')])
        self.item = self.item_de('MART-500')

    def test_una_cadena_de_tres_filas_cuenta_una_sola_vez(self):
        primera = self.crear_devolucion(self.item, 2)
        segunda = self.crear_devolucion(self.item, 2, evento_previo=primera)
        self.crear_devolucion(self.item, 2, evento_previo=segunda)
        db.session.commit()

        self.assertEqual(
            rutas_devoluciones.devuelto_por_item(self.empresa_id),
            {self.item.id: 2})

    def test_una_cadena_que_quedo_abierta_no_cuenta(self):
        primera = self.crear_devolucion(self.item, 2)
        self.crear_devolucion(self.item, 2, estado=DEVOLUCION_ABIERTA,
                              evento_previo=primera)
        db.session.commit()

        self.assertEqual(rutas_devoluciones.devuelto_por_item(self.empresa_id), {})

    def test_un_pedido_cancelado_no_aporta_devuelto(self):
        # Mismo criterio que los reportes: un pedido cancelado sale de
        # "vendido", asi que sus devoluciones salen de "devuelto". Si no,
        # habria una fila devolviendo algo que nunca se vendio.
        self.crear_devolucion(self.item, 2)
        db.session.get(Pedido, self.item.pedido_id).estado = 'cancelled'
        db.session.commit()

        self.assertEqual(rutas_devoluciones.devuelto_por_item(self.empresa_id), {})


# ==========================================================================
# PARTE 4 -- los reportes: la columna nueva y los numeros viejos
# ==========================================================================


class TestReporteS1MuestraDevueltoSinTocarVendido(BaseDevoluciones):
    """El resumen de vendido por producto y canal (FASE-REPORTES-S1)."""

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 5, '2500.00')])
        self.antes = self.texto(self.client.get('/productos/resumen'))
        self.item_id = self.item_de('MART-500').id
        self.devolver(self.item_id, 2)
        self.despues = self.texto(self.client.get('/productos/resumen'))

    def test_aparece_la_columna_devuelto(self):
        self.assertIn('Devuelto', self.despues)

    def test_la_columna_existe_aunque_no_haya_devoluciones(self):
        # Mismo criterio que un canal en cero o que "sin identificar": la
        # columna informa, no se esconde cuando esta vacia.
        self.assertIn('Devuelto', self.antes)

    def test_el_vendido_no_cambio(self):
        # El numero que importa: 5 unidades vendidas antes y despues de
        # devolver 2. Opcion C -- lo devuelto NO se resta.
        vendido = rutas_productos._vendido_por_producto_y_canal(self.empresa_id)
        self.assertEqual(sum(vendido.values()), 5)

    def test_lo_devuelto_se_cuenta_aparte(self):
        devuelto = rutas_devoluciones.devuelto_por_producto_y_canal(self.empresa_id)
        self.assertEqual(sum(devuelto.values()), 2)

    def test_el_stock_actual_si_refleja_la_devolucion(self):
        # La otra mitad: lo devuelto NO toca el reporte de ventas pero SI el
        # inventario. Son dos preguntas distintas y esta slice las separa.
        self.assertEqual(self.stock_de('MART-500'), 7)

    def test_la_pagina_responde_ok_con_devoluciones_cargadas(self):
        self.assertEqual(self.client.get('/productos/resumen').status_code, 200)


class TestReporteMargenMuestraDevueltoSinTocarMargen(BaseDevoluciones):
    """El reporte de margen (FASE-REPORTES-S3-MARGEN).

    La venta de mostrador tiene los tres componentes de costo cargados
    (comision 0, envio 0, snapshot del costo del producto), asi que el pedido
    entra al calculo y hay margen real que comparar.
    """

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 2, '2500.00')])
        self.antes = self.texto(self.client.get('/reportes/margen'))
        self.devolver(self.item_de('MART-500').id, 1)
        self.despues = self.texto(self.client.get('/reportes/margen'))

    def test_aparece_la_columna_devuelto(self):
        self.assertIn('Devuelto', self.despues)
        self.assertIn('Devuelto', self.antes)

    def test_el_ingreso_el_costo_y_la_ganancia_no_cambiaron(self):
        # ingreso 5000 (2 x 2500), costo 2400 (2 x 1200 + 0 + 0),
        # ganancia 2600. Los tres numeros tienen que estar en las DOS
        # cargas: si devolver hubiera tocado el calculo, alguno faltaria en
        # la segunda.
        for numero in ('5000.00', '2400.00', '2600.00'):
            self.assertIn(numero, self.antes)
            self.assertIn(numero, self.despues)

    def test_el_margen_porcentual_no_cambio(self):
        # 2600 / 5000 = 52.0%
        self.assertIn('52.0', self.antes)
        self.assertIn('52.0', self.despues)

    def test_la_linea_del_pedido_no_se_toco(self):
        # Las "Unid." del reporte salen de `pedido_item.cantidad`. Devolver no
        # edita ni borra la linea: sigue diciendo que salieron 2, y por eso el
        # margen de arriba no se movio. La devolucion vive en su propia tabla.
        self.assertEqual(PedidoItem.query.one().cantidad, 2)
        self.assertEqual(
            rutas_devoluciones.devuelto_por_item(self.empresa_id),
            {PedidoItem.query.one().id: 1})

    def test_la_pagina_responde_ok_con_devoluciones_cargadas(self):
        self.assertEqual(self.client.get('/reportes/margen').status_code, 200)


class TestDevueltoEnUnPedidoSinMargenCalculable(BaseDevoluciones):
    """Un pedido al que le falta un costo igual muestra lo devuelto.

    Son preguntas distintas: que la comision no este cargada no hace que la
    unidad que volvio no haya vuelto.
    """

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 3, '2500.00')])
        pedido = Pedido.query.one()
        pedido.comision_plataforma = None      # lo saca del calculo de margen
        db.session.commit()
        self.devolver(self.item_de('MART-500').id, 1)
        self.html = self.texto(self.client.get('/reportes/margen'))

    def test_el_pedido_sigue_apareciendo_como_incompleto(self):
        self.assertIn('Sin margen', self.html)

    def test_lo_devuelto_se_ve_igual(self):
        devuelto = rutas_devoluciones.devuelto_por_item(self.empresa_id)
        self.assertEqual(sum(devuelto.values()), 1)
        self.assertEqual(self.client.get('/reportes/margen').status_code, 200)


# ==========================================================================
# Las pantallas
# ==========================================================================


class TestPantallasDeDevolucion(BaseDevoluciones):
    """Las tres vistas nuevas."""

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 5, '2500.00')])

    def test_el_listado_de_devoluciones_responde(self):
        respuesta = self.client.get('/devoluciones/listar')
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('Todavía no se registró ninguna devolución',
                      self.texto(respuesta))

    def test_elegir_pedido_muestra_la_venta(self):
        html = self.texto(self.client.get('/devoluciones/nueva'))
        self.assertIn('Devolver', html)

    def test_el_formulario_muestra_las_lineas_del_pedido(self):
        pedido_id = Pedido.query.one().id
        html = self.texto(self.client.get('/devoluciones/nueva/%d' % pedido_id))
        self.assertIn('Martillo 500g', html)
        self.assertIn('MART-500', html)

    def test_el_formulario_muestra_cuanto_queda_por_devolver(self):
        pedido_id = Pedido.query.one().id
        self.devolver(self.item_de('MART-500').id, 2)
        html = self.texto(self.client.get('/devoluciones/nueva/%d' % pedido_id))
        self.assertIn('Ya devueltas', html)

    def test_la_devolucion_aparece_en_el_listado(self):
        self.devolver(self.item_de('MART-500').id, 2)
        html = self.texto(self.client.get('/devoluciones/listar'))
        self.assertIn('Martillo 500g', html)
        self.assertIn(DEVOLUCION_CERRADA, html)

    def test_un_pedido_de_otra_empresa_no_se_puede_devolver(self):
        otra = Empresa(nombre='Ferretería Ajena')
        db.session.add(otra)
        db.session.flush()
        canal = CanalVenta(empresa_id=otra.id, tipo='manual', nombre='Mostrador ajeno')
        db.session.add(canal)
        db.session.flush()
        ajeno = Pedido(empresa_id=otra.id, canal_id=canal.id,
                       fecha_pedido=datetime.utcnow(), estado='completado',
                       moneda='ARS', total=Decimal('100.00'))
        db.session.add(ajeno)
        db.session.commit()

        respuesta = self.client.get('/devoluciones/nueva/%d' % ajeno.id,
                                    follow_redirects=True)
        self.assertIn('no es de tu empresa', self.texto(respuesta))

    def test_las_tres_rutas_piden_login(self):
        pedido_id = Pedido.query.one().id
        for ruta in ('/devoluciones/listar', '/devoluciones/nueva',
                     '/devoluciones/nueva/%d' % pedido_id):
            respuesta = request_anonimo(self.ctx, 'get', ruta)
            self.assertIn(respuesta.status_code, (301, 302), ruta)
            self.assertIn('/login', respuesta.headers.get('Location', ''), ruta)


# ==========================================================================
# La funcion de stock, aislada
# ==========================================================================


class TestDevolverStockAislado(BaseDevoluciones):
    """`devolver_stock` sin request de por medio."""

    def setUp(self):
        super().setUp()
        self.vender([('MART-500', 5, '2500.00')])
        self.item = self.item_de('MART-500')

    def test_una_fila_abierta_no_mueve_nada(self):
        abierta = self.crear_devolucion(self.item, 2, estado=DEVOLUCION_ABIERTA)
        self.assertIsNone(rutas_devoluciones.devolver_stock(abierta))
        self.assertEqual(self.stock_de('MART-500'), 5)

    def test_una_fila_sin_item_ni_cantidad_no_mueve_nada(self):
        contracargo = Devolucion(
            pedido_id=self.item.pedido_id, tipo='contracargo', moneda='ARS',
            monto=Decimal('7500.00'), estado=DEVOLUCION_CERRADA,
            fecha_evento=datetime.utcnow())
        db.session.add(contracargo)
        db.session.flush()

        self.assertIsNone(rutas_devoluciones.devolver_stock(contracargo))
        self.assertEqual(self.stock_de('MART-500'), 5)

    def test_devuelve_el_id_del_producto_que_cambio(self):
        devolucion = self.crear_devolucion(self.item, 2)
        self.assertEqual(rutas_devoluciones.devolver_stock(devolucion),
                         self.con_stock.id)

    def test_la_suma_no_clampea_contra_nada(self):
        # El clamp heredado de la venta: se vendieron 5 de un stock de 1, el
        # descuento real fue de 1 (quedo en 0) y la devolucion de esas 5 suma
        # 5 enteras. El numero final no es el fisico y el sistema no puede
        # saberlo: ya aviso al vender. Lo que se prueba es que el
        # comportamiento sea PREDECIBLE, no que el numero sea correcto.
        self.vender([('DEST-PH2', 5, '900.00')])
        self.assertEqual(self.stock_de('DEST-PH2'), 0)

        item = self.item_de('DEST-PH2')
        devolucion = self.crear_devolucion(item, 5)
        rutas_devoluciones.devolver_stock(devolucion)
        self.assertEqual(self.stock_de('DEST-PH2'), 5)


if __name__ == '__main__':
    unittest.main()
