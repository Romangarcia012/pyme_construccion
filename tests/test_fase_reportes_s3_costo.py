# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S3-COSTO (carga manual del costo de producto).

    python -m unittest discover -s tests -v

`producto.costo_unitario` existe desde FASE2-S1 y hasta esta slice no tenia
pantalla: estaba NULL en los tres productos reales y el unico modo de tocarlo
era entrar a Supabase. Sin el, `pedido_item.costo_unitario_snapshot` se guarda
NULL en cada venta y no hay margen que reportar.

La caneria del snapshot YA existe y no se toca (sync_tiendanube:627,
rutas_ventas:275). Lo que se agrega es el input, asi que lo que se prueba es:

    Decimal valido            -> se persiste con dos decimales
    negativo / texto / NaN    -> no guarda nada Y no rompe la pantalla
    vacio                     -> vuelve a NULL (borrar un costo mal cargado)
    coma decimal              -> se acepta, igual que en la venta manual
    una fila mala             -> tampoco se guardan las buenas de la misma tanda
    SKU de otra empresa       -> se ignora, no escribe nada ajeno
    sugerencia de Tiendanube  -> se MUESTRA y el input sigue vacio
    venta nueva               -> snapshotea el costo recien cargado

El ultimo es el que cierra el circulo: prueba que la caneria ya construida
arranca sola apenas hay dato, sin haberla tocado.

La sugerencia sale de `pedido.raw_payload['products'][].cost`, que ya esta
guardado desde FASE-REPORTES-S3-FIX2. Ninguna llamada sale a internet: los
payloads se siembran a mano con la misma forma que los reales.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_tiendanube  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    Empresa,
    MapeoProductoCanal,
    Pedido,
    PedidoItem,
    Producto,
    Usuario,
    db,
)
from tests.ayuda_auth import request_anonimo  # noqa: E402

ENGINE_PRODUCTIVO = None

# Los numeros del caso real: el Tarjetero Negro. Tiendanube manda 3230.81
# (compra + flete) y el costo de verdad que Roman tiene anotado es 3994.18.
COSTO_TN = '3230.81'
COSTO_REAL = Decimal('3994.18')

# Los ids externos reales de esa variante, para que el cruce que hace la
# pantalla sea el mismo que corre en produccion.
ID_PRODUCTO_TN = '360354459'
ID_VARIANTE_TN = '1574653133'


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


def payload_con_cost(id_producto, id_variante, cost):
    """Un raw_payload de Tiendanube recortado a lo que lee la pantalla.

    La forma es la del payload de DETALLE que guarda el sync
    (FASE-REPORTES-S3-FIX2): `products` es una lista y cada linea trae
    `product_id`, `variant_id` y `cost` como string.
    """
    return {
        'id': 2060210312,
        'products': [{
            'id': 3496297727,
            'product_id': int(id_producto),
            'variant_id': int(id_variante),
            'name': 'Tarjetero Minimalista de Aluminio (Negro)',
            'price': '7490.00',
            'cost': cost,
            'quantity': 1,
        }],
    }


class BaseCosto(unittest.TestCase):
    """Una empresa con tres productos, canal de Tiendanube y un pedido viejo.

        TARJ-NEGRO  costo NULL, mapeado, con `cost` en un pedido -> sugerencia
        TARJ-GRIS   costo NULL, mapeado, sin pedido              -> sin sugerencia
        MICRO-01    costo ya cargado, sin mapeo                  -> editar sobre algo

    Y una SEGUNDA empresa con su propio producto, para probar que un SKU ajeno
    que entre por el formulario no se escribe.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-REPORTES-S3-COSTO')
        self.otra_empresa = Empresa(nombre='Empresa Ajena')
        db.session.add_all([self.empresa, self.otra_empresa])
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='fasecosto@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial', activo=True)
        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True, id_tienda_externo='9999')
        db.session.add_all([self.canal_manual, self.canal_tn])
        db.session.flush()

        self.negro = Producto(empresa_id=self.empresa.id, sku='TARJ-NEGRO',
                              nombre='Tarjetero Minimalista de Aluminio (Negro)',
                              stock=198, costo_unitario=None,
                              precio_lista=Decimal('7490.00'))
        self.gris = Producto(empresa_id=self.empresa.id, sku='TARJ-GRIS',
                             nombre='Tarjetero Minimalista de Aluminio (Gris)',
                             stock=100, costo_unitario=None,
                             precio_lista=Decimal('7490.00'))
        self.micro = Producto(empresa_id=self.empresa.id, sku='MICRO-01',
                              nombre='Microfono Inalambrico Korvo',
                              stock=100, costo_unitario=Decimal('1000.00'),
                              precio_lista=Decimal('25000.00'))
        self.ajeno = Producto(empresa_id=self.otra_empresa.id, sku='AJENO-1',
                              nombre='Producto de otra empresa',
                              stock=5, costo_unitario=None)
        db.session.add_all([self.negro, self.gris, self.micro, self.ajeno])
        db.session.flush()

        db.session.add_all([
            MapeoProductoCanal(producto_id=self.negro.id, canal_id=self.canal_tn.id,
                               id_producto_externo=ID_PRODUCTO_TN,
                               id_variante_externo=ID_VARIANTE_TN),
            MapeoProductoCanal(producto_id=self.gris.id, canal_id=self.canal_tn.id,
                               id_producto_externo=ID_PRODUCTO_TN,
                               id_variante_externo='1574653135'),
        ])

        # El pedido que trae la sugerencia. Es de Tiendanube y ya sincronizado:
        # el `cost` viaja adentro del raw_payload que guardo el sync.
        db.session.add(Pedido(
            empresa_id=self.empresa.id, canal_id=self.canal_tn.id,
            id_externo='2060210312', fecha_pedido=datetime(2026, 8, 20, 10, 0),
            estado='open', moneda='ARS', total=Decimal('7490.00'),
            raw_payload=payload_con_cost(ID_PRODUCTO_TN, ID_VARIANTE_TN, COSTO_TN)))

        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.ajeno_id = self.ajeno.id

        # El push de stock a Tiendanube no tiene nada que ver con el costo, pero
        # la venta manual lo dispara. Se anula para que ningun test salga a
        # internet ni dependa del cifrado de credenciales.
        self.original_push = stock_tiendanube.empujar_stock
        stock_tiendanube.empujar_stock = lambda empresa_id, producto_ids: []

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        stock_tiendanube.empujar_stock = self.original_push
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def guardar(self, pares, **extra):
        """POST al formulario de costos. `pares`: [(sku, texto del costo)]."""
        datos = {
            'sku': [sku for sku, _ in pares],
            'costo_unitario': [texto for _, texto in pares],
        }
        datos.update(extra)
        return self.client.post('/productos/costos', data=datos,
                                follow_redirects=True)

    def costo_de(self, sku, empresa_id=None):
        producto = Producto.query.filter_by(
            empresa_id=empresa_id or self.empresa_id, sku=sku).first()
        return producto.costo_unitario

    def listado(self):
        return self.client.get('/productos/listar').get_data(as_text=True)

    def texto(self, respuesta):
        return respuesta.get_data(as_text=True)


class TestGuardarCostoUnitarioValido(BaseCosto):
    """El caso central: se tipea un numero y queda guardado."""

    def test_guardar_costo_unitario_valido(self):
        self.guardar([('TARJ-NEGRO', '3994.18')])
        self.assertEqual(self.costo_de('TARJ-NEGRO'), COSTO_REAL)

    def test_el_post_termina_bien_y_avisa(self):
        respuesta = self.guardar([('TARJ-NEGRO', '3994.18')])
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('Se actualizo el costo de 1 producto', self.texto(respuesta))

    def test_se_guarda_como_decimal_no_como_float(self):
        self.guardar([('TARJ-NEGRO', '3994.18')])
        self.assertIsInstance(self.costo_de('TARJ-NEGRO'), Decimal)

    def test_se_redondea_a_dos_decimales(self):
        self.guardar([('TARJ-NEGRO', '3994.187')])
        self.assertEqual(self.costo_de('TARJ-NEGRO'), Decimal('3994.19'))

    def test_la_coma_decimal_se_acepta(self):
        """Es lo que tipea cualquiera en Argentina."""
        self.guardar([('TARJ-NEGRO', '3994,18')])
        self.assertEqual(self.costo_de('TARJ-NEGRO'), COSTO_REAL)

    def test_cero_es_un_costo_valido(self):
        """Una muestra o un regalo cuestan cero de verdad. Es distinto de NULL."""
        self.guardar([('TARJ-NEGRO', '0')])
        self.assertEqual(self.costo_de('TARJ-NEGRO'), Decimal('0.00'))

    def test_varios_productos_en_una_sola_tanda(self):
        self.guardar([('TARJ-NEGRO', '3994.18'), ('TARJ-GRIS', '4100.00')])
        self.assertEqual(self.costo_de('TARJ-NEGRO'), COSTO_REAL)
        self.assertEqual(self.costo_de('TARJ-GRIS'), Decimal('4100.00'))

    def test_el_input_vacio_vuelve_el_costo_a_null(self):
        """Asi se saca un costo mal cargado: se borra el input, no se pone 0."""
        self.guardar([('MICRO-01', '')])
        self.assertIsNone(self.costo_de('MICRO-01'))

    def test_el_costo_guardado_se_ve_al_recargar(self):
        self.guardar([('TARJ-NEGRO', '3994.18')])
        self.assertIn('value="3994.18"', self.listado())


class TestRechazaCostoNegativoOInvalido(BaseCosto):
    """Un input malo no puede guardar basura ni voltear la pantalla."""

    def test_rechaza_costo_negativo_o_invalido(self):
        for texto in ('-1', '-3994.18', 'abc', '12abc', '1.2.3', '$3994',
                      'NaN', 'Infinity'):
            with self.subTest(texto=texto):
                respuesta = self.guardar([('TARJ-NEGRO', texto)])
                self.assertEqual(respuesta.status_code, 200)
                self.assertIsNone(self.costo_de('TARJ-NEGRO'),
                                  'se guardo basura con %r' % texto)

    def test_el_error_se_explica_con_el_nombre_del_producto(self):
        respuesta = self.texto(self.guardar([('TARJ-NEGRO', '-5')]))
        self.assertIn('no puede ser negativo', respuesta)
        self.assertIn('Tarjetero Minimalista de Aluminio (Negro)', respuesta)

    def test_el_texto_avisa_que_no_es_un_numero(self):
        self.assertIn('no es un numero',
                      self.texto(self.guardar([('TARJ-NEGRO', 'abc')])))

    def test_una_fila_mala_no_deja_guardar_las_buenas(self):
        """Todo o nada: si no, la pantalla mostraria un exito a medias."""
        self.guardar([('TARJ-NEGRO', '3994.18'), ('TARJ-GRIS', '-1')])
        self.assertIsNone(self.costo_de('TARJ-NEGRO'))
        self.assertIsNone(self.costo_de('TARJ-GRIS'))

    def test_un_valor_malo_no_pisa_el_costo_que_ya_estaba(self):
        self.guardar([('MICRO-01', 'abc')])
        self.assertEqual(self.costo_de('MICRO-01'), Decimal('1000.00'))

    def test_un_sku_de_otra_empresa_se_ignora(self):
        """El SKU llega como texto del cliente: el filtro por empresa es lo
        unico que impide escribir en el catalogo de otro."""
        respuesta = self.guardar([('AJENO-1', '999.00')])
        self.assertEqual(respuesta.status_code, 200)
        self.assertIsNone(db.session.get(Producto, self.ajeno_id).costo_unitario)

    def test_un_sku_inexistente_no_rompe(self):
        respuesta = self.guardar([('NO-EXISTE', '100.00')])
        self.assertEqual(respuesta.status_code, 200)

    def test_sin_ninguna_fila_avisa_que_no_hubo_cambios(self):
        self.assertIn('No hubo cambios', self.texto(self.guardar([])))


class TestSugerenciaDeTiendanube(BaseCosto):
    """El `cost` del canal se muestra; el input se queda vacio."""

    def test_sugerencia_tn_no_se_autocompleta(self):
        """El caso que la slice prohibe: hay sugerencia y costo_unitario NULL.

        Que el producto siga en NULL despues de abrir la pantalla es la mitad
        de la prueba; la otra mitad es que el input tampoco venga prellenado,
        porque un input con el numero adentro se guardaria al primer submit.
        """
        html = self.listado()
        self.assertIn(COSTO_TN, html)                    # la sugerencia se ve
        self.assertNotIn('value="%s"' % COSTO_TN, html)  # pero no en el input
        self.assertIsNone(self.costo_de('TARJ-NEGRO'))   # y nada se guardo

    def test_la_sugerencia_se_marca_como_parcial(self):
        html = self.listado()
        self.assertIn('parcial', html)
        self.assertIn('sin impuestos ni empaque', html)

    def test_el_producto_sin_pedido_no_tiene_sugerencia(self):
        from rutas_productos import _costo_sugerido_por_producto
        sugeridos = _costo_sugerido_por_producto(self.empresa_id)
        self.assertEqual(sugeridos.get(self.negro.id), Decimal(COSTO_TN))
        self.assertNotIn(self.gris.id, sugeridos)

    def test_gana_el_pedido_mas_nuevo(self):
        """El costo de una variante cambia; el ultimo es el unico vigente."""
        db.session.add(Pedido(
            empresa_id=self.empresa_id, canal_id=self.canal_tn.id,
            id_externo='2060299999', fecha_pedido=datetime(2026, 8, 25, 10, 0),
            estado='open', moneda='ARS', total=Decimal('7490.00'),
            raw_payload=payload_con_cost(ID_PRODUCTO_TN, ID_VARIANTE_TN, '3500.00')))
        db.session.commit()

        from rutas_productos import _costo_sugerido_por_producto
        sugeridos = _costo_sugerido_por_producto(self.empresa_id)
        self.assertEqual(sugeridos[self.negro.id], Decimal('3500.00'))

    def test_un_payload_sin_cost_no_sugiere_cero(self):
        Pedido.query.delete()
        db.session.add(Pedido(
            empresa_id=self.empresa_id, canal_id=self.canal_tn.id,
            id_externo='2060288888', fecha_pedido=datetime(2026, 8, 21, 10, 0),
            estado='open', moneda='ARS', total=Decimal('7490.00'),
            raw_payload={'id': 1, 'products': [{'product_id': int(ID_PRODUCTO_TN),
                                                'variant_id': int(ID_VARIANTE_TN)}]}))
        db.session.commit()

        from rutas_productos import _costo_sugerido_por_producto
        self.assertEqual(_costo_sugerido_por_producto(self.empresa_id), {})

    def test_un_payload_roto_no_voltea_la_pantalla(self):
        Pedido.query.delete()
        for payload in (None, [], {'products': 'no soy una lista'},
                        {'products': ['tampoco soy un dict']}):
            db.session.add(Pedido(
                empresa_id=self.empresa_id, canal_id=self.canal_tn.id,
                id_externo=None, fecha_pedido=datetime(2026, 8, 21, 10, 0),
                estado='open', moneda='ARS', total=Decimal('0.00'),
                raw_payload=payload))
        db.session.commit()

        self.assertEqual(self.client.get('/productos/listar').status_code, 200)

    def test_la_venta_de_mostrador_no_deja_sugerencia(self):
        """No tiene raw_payload: no hay canal que sugiera nada."""
        db.session.add(Pedido(
            empresa_id=self.empresa_id, canal_id=self.canal_manual.id,
            id_externo=None, fecha_pedido=datetime(2026, 8, 26, 10, 0),
            estado='completado', moneda='ARS', total=Decimal('7490.00'),
            raw_payload=None))
        db.session.commit()

        from rutas_productos import _costo_sugerido_por_producto
        self.assertEqual(_costo_sugerido_por_producto(self.empresa_id),
                         {self.negro.id: Decimal(COSTO_TN)})


class TestElSnapshotArrancaSolo(BaseCosto):
    """La caneria que ya existia: cargar el costo y vender."""

    def vender(self, sku, cantidad, precio):
        return self.client.post('/pedidos/manual/nuevo', data={
            'sku': [sku],
            'cantidad': [str(cantidad)],
            'precio_unitario': [precio],
            'fecha': date.today().isoformat(),
            'medio': 'efectivo',
            'nota': '',
        }, follow_redirects=True)

    def test_venta_nueva_snapshotea_el_costo_cargado(self):
        self.guardar([('TARJ-NEGRO', '3994.18')])
        self.vender('TARJ-NEGRO', 1, '7490.00')

        item = (PedidoItem.query
                .join(Pedido, Pedido.id == PedidoItem.pedido_id)
                .filter(Pedido.canal_id == self.canal_manual.id)
                .one())
        self.assertEqual(item.costo_unitario_snapshot, COSTO_REAL)

    def test_sin_costo_cargado_el_snapshot_sigue_en_null(self):
        """El estado de hoy: es lo que esta slice viene a destrabar."""
        self.vender('TARJ-GRIS', 1, '7490.00')

        item = (PedidoItem.query
                .join(Pedido, Pedido.id == PedidoItem.pedido_id)
                .filter(Pedido.canal_id == self.canal_manual.id)
                .one())
        self.assertIsNone(item.costo_unitario_snapshot)

    def test_cambiar_el_costo_despues_no_toca_la_venta_ya_hecha(self):
        """Para eso existe el snapshot: el margen de ayer no se reescribe."""
        self.guardar([('TARJ-NEGRO', '3994.18')])
        self.vender('TARJ-NEGRO', 1, '7490.00')
        self.guardar([('TARJ-NEGRO', '5000.00')])

        item = (PedidoItem.query
                .join(Pedido, Pedido.id == PedidoItem.pedido_id)
                .filter(Pedido.canal_id == self.canal_manual.id)
                .one())
        self.assertEqual(item.costo_unitario_snapshot, COSTO_REAL)

    def test_el_pedido_ya_sincronizado_no_se_backfillea(self):
        """Las ventas viejas con snapshot NULL se quedan asi. No hay backfill."""
        pedido_tn = Pedido.query.filter_by(id_externo='2060210312').one()
        db.session.add(PedidoItem(
            pedido_id=pedido_tn.id, producto_id=self.negro.id,
            descripcion=self.negro.nombre, cantidad=1,
            precio_unitario=Decimal('7490.00'), subtotal=Decimal('7490.00'),
            costo_unitario_snapshot=None))
        db.session.commit()

        self.guardar([('TARJ-NEGRO', '3994.18')])

        item = PedidoItem.query.filter_by(pedido_id=pedido_tn.id).one()
        self.assertIsNone(item.costo_unitario_snapshot)


class TestAuth(BaseCosto):
    """Las dos rutas piden sesion."""

    def test_el_listado_pide_login(self):
        respuesta = request_anonimo(self.ctx, 'get', '/productos/listar')
        self.assertEqual(respuesta.status_code, 302)

    def test_guardar_costos_pide_login(self):
        respuesta = request_anonimo(self.ctx, 'post', '/productos/costos',
                                    data={'sku': ['TARJ-NEGRO'],
                                          'costo_unitario': ['3994.18']})
        self.assertEqual(respuesta.status_code, 302)
        self.assertIsNone(self.costo_de('TARJ-NEGRO'))

    def test_el_listado_no_acepta_get_en_costos(self):
        self.assertEqual(self.client.get('/productos/costos').status_code, 405)


if __name__ == '__main__':
    unittest.main()
