# -*- coding: utf-8 -*-
"""Tests de la FASE3-S4 (carga manual de ventas presenciales).

    python -m unittest discover -s tests -v

Lo que se prueba aca es una traduccion: un formulario que tipea una persona ->
las mismas tres tablas (pedido / pedido_item / pago) donde aterriza lo que baja
de Tiendanube. Los casos interesantes son los que separan "cero" de "no se":

    efectivo  -> pago acreditado, comision 0     (no hubo comision)
    tarjeta   -> pago pendiente,  comision NULL  (todavia no se sabe cuanto)

y el mismo contraste en el costo: costo_unitario_snapshot queda NULL cuando el
producto no tiene costo cargado, igual que ya pasa con los pedidos de
Tiendanube (test_fase3_s2), en vez de rellenarse con cero.

Como en FASE3-S2 y S3, la app se repunta a SQLite en memoria: ningun test toca
la base productiva ni sale a internet -- esta slice, de hecho, no tiene a donde
salir.
"""

import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (  # noqa: E402
    CanalVenta,
    Empresa,
    Pago,
    Pedido,
    PedidoItem,
    Producto,
    Usuario,
    db,
)
from app import app  # noqa: E402

ENGINE_PRODUCTIVO = None


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


class BaseVentaManual(unittest.TestCase):
    """Una empresa con catalogo y el canal manual ya sembrado."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE3-S4')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='fase3s4@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        # Los tres canales que existen despues de esta slice.
        canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                  nombre='Venta manual / presencial', activo=True)
        canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                              nombre='Korvo', activo=True, id_tienda_externo='9999')
        db.session.add_all([canal_manual, canal_tn])

        # Dos productos con costo y uno sin, que es el caso que importa.
        self.con_costo = Producto(empresa_id=self.empresa.id, sku='MART-500',
                                  nombre='Martillo 500g', costo_unitario=Decimal('1200.00'),
                                  precio_lista=Decimal('2500.00'))
        self.otro = Producto(empresa_id=self.empresa.id, sku='DEST-PH2',
                             nombre='Destornillador PH2', costo_unitario=Decimal('400.00'),
                             precio_lista=Decimal('900.00'))
        self.sin_costo = Producto(empresa_id=self.empresa.id, sku='CINTA-19',
                                  nombre='Cinta aisladora 19mm', costo_unitario=None,
                                  precio_lista=Decimal('300.00'))
        db.session.add_all([self.con_costo, self.otro, self.sin_costo])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.canal_manual_id = canal_manual.id
        self.canal_tn_id = canal_tn.id
        self.usuario_id = self.usuario.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def cargar(self, items, medio='efectivo', fecha=None, nota='', seguir=True):
        """POST al formulario. `items` son tuplas (sku, cantidad, precio)."""
        datos = {
            'sku': [sku for sku, _, _ in items],
            'cantidad': [str(cant) for _, cant, _ in items],
            'precio_unitario': [str(precio) for _, _, precio in items],
            'fecha': fecha if fecha is not None else date.today().isoformat(),
            'medio': medio,
            'nota': nota,
        }
        return self.client.post('/pedidos/manual/nuevo', data=datos,
                                follow_redirects=seguir)

    def unico_pedido(self):
        pedidos = Pedido.query.all()
        self.assertEqual(len(pedidos), 1, 'se esperaba exactamente un pedido')
        return pedidos[0]

    def pedido_tiendanube(self, id_externo='TN-1', total='5000.00'):
        """Un pedido que ya estaba en la base, como si lo hubiera traido el
        sync. Sirve para que el listado tenga con que mezclar."""
        pedido = Pedido(empresa_id=self.empresa_id, canal_id=self.canal_tn_id,
                        id_externo=id_externo, numero_externo='1001',
                        fecha_pedido=datetime(2026, 8, 20, 15, 0),
                        estado='pendiente', estado_externo='open',
                        moneda='ARS', total_bruto=Decimal(total), total=Decimal(total))
        db.session.add(pedido)
        db.session.commit()
        return pedido


class TestVentaEnEfectivo(BaseVentaManual):
    """El caso central: dos items cobrados en efectivo."""

    def setUp(self):
        super().setUp()
        self.respuesta = self.cargar([
            ('MART-500', 2, '2500.00'),
            ('DEST-PH2', 3, '900.00'),
        ], medio='efectivo', nota='cliente: Juan Perez')

    def test_el_post_termina_bien(self):
        self.assertEqual(self.respuesta.status_code, 200)

    def test_el_pedido_queda_con_los_totales_de_las_lineas(self):
        pedido = self.unico_pedido()
        # 2 x 2500 + 3 x 900 = 7700
        self.assertEqual(pedido.total, Decimal('7700.00'))
        self.assertEqual(pedido.total_bruto, Decimal('7700.00'))
        self.assertEqual(pedido.total_descuentos, Decimal('0.00'))
        self.assertEqual(pedido.total_envio, Decimal('0.00'))
        self.assertEqual(pedido.total_impuestos, Decimal('0.00'))

    def test_el_pedido_cuelga_del_canal_manual_y_no_tiene_id_externo(self):
        pedido = self.unico_pedido()
        self.assertEqual(pedido.canal_id, self.canal_manual_id)
        self.assertEqual(pedido.canal.tipo, 'manual')
        self.assertIsNone(pedido.id_externo)

    def test_una_venta_de_mostrador_nace_cerrada(self):
        self.assertEqual(self.unico_pedido().estado, 'completado')

    def test_la_nota_se_guarda(self):
        self.assertEqual(self.unico_pedido().nota, 'cliente: Juan Perez')

    def test_la_fecha_es_la_de_hoy_por_defecto(self):
        self.assertEqual(self.unico_pedido().fecha_pedido.date(), date.today())

    def test_quedan_los_dos_items_con_su_sku_cantidad_y_precio(self):
        pedido = self.unico_pedido()
        items = PedidoItem.query.filter_by(pedido_id=pedido.id).order_by(
            PedidoItem.id).all()
        self.assertEqual(len(items), 2)

        self.assertEqual(items[0].producto_id, self.con_costo.id)
        self.assertEqual(items[0].sku_externo, 'MART-500')
        self.assertEqual(items[0].descripcion, 'Martillo 500g')
        self.assertEqual(items[0].cantidad, 2)
        self.assertEqual(items[0].precio_unitario, Decimal('2500.00'))
        self.assertEqual(items[0].subtotal, Decimal('5000.00'))

        self.assertEqual(items[1].sku_externo, 'DEST-PH2')
        self.assertEqual(items[1].cantidad, 3)
        self.assertEqual(items[1].subtotal, Decimal('2700.00'))

    def test_el_costo_del_dia_queda_congelado_en_cada_linea(self):
        pedido = self.unico_pedido()
        items = PedidoItem.query.filter_by(pedido_id=pedido.id).order_by(
            PedidoItem.id).all()
        self.assertEqual(items[0].costo_unitario_snapshot, Decimal('1200.00'))
        self.assertEqual(items[1].costo_unitario_snapshot, Decimal('400.00'))

    def test_el_snapshot_no_sigue_al_costo_del_producto(self):
        """Cambiar el costo del catalogo no puede reescribir el margen de una
        venta que ya paso. Es la misma regla que ya vale para Tiendanube."""
        pedido = self.unico_pedido()
        producto = db.session.get(Producto, self.con_costo.id)
        producto.costo_unitario = Decimal('1800.00')
        db.session.commit()

        item = PedidoItem.query.filter_by(
            pedido_id=pedido.id, producto_id=producto.id).first()
        db.session.refresh(item)
        self.assertEqual(item.costo_unitario_snapshot, Decimal('1200.00'))

    def test_el_pago_en_efectivo_queda_acreditado_y_sin_comision(self):
        pedido = self.unico_pedido()
        pagos = Pago.query.filter_by(pedido_id=pedido.id).all()
        self.assertEqual(len(pagos), 1)
        pago = pagos[0]

        self.assertEqual(pago.canal_id, self.canal_manual_id)
        self.assertIsNone(pago.id_externo)
        self.assertEqual(pago.metodo, 'efectivo')
        self.assertEqual(pago.estado, 'acreditado')
        self.assertEqual(pago.monto_bruto, Decimal('7700.00'))
        self.assertEqual(pago.monto_neto, Decimal('7700.00'))
        # 0, no NULL: en efectivo la comision se sabe y es cero.
        self.assertEqual(pago.comision, Decimal('0.00'))


class TestVentaConTarjeta(BaseVentaManual):
    """La otra mitad del contraste: lo que todavia no se sabe queda NULL."""

    def setUp(self):
        super().setUp()
        self.cargar([('MART-500', 1, '2500.00')], medio='tarjeta')

    def test_el_pago_queda_pendiente(self):
        pago = Pago.query.one()
        self.assertEqual(pago.metodo, 'tarjeta')
        self.assertEqual(pago.estado, 'pendiente')

    def test_la_comision_queda_null_porque_todavia_no_se_conoce(self):
        """NULL y 0 no son lo mismo. 0 diria "esta tarjeta no cobro comision",
        que es falso; NULL dice "el numero lo va a traer la conciliacion"."""
        pago = Pago.query.one()
        self.assertIsNone(pago.comision)

    def test_el_neto_arranca_igual_al_bruto(self):
        pago = Pago.query.one()
        self.assertEqual(pago.monto_bruto, Decimal('2500.00'))
        self.assertEqual(pago.monto_neto, Decimal('2500.00'))

    def test_no_se_acredita_todavia(self):
        self.assertIsNone(Pago.query.one().fecha_acreditacion)


class TestMercadoPagoNoDisparaNadaExterno(BaseVentaManual):
    """El medio es un dato descriptivo: elegir 'mercado_pago' no conecta nada."""

    def test_se_guarda_igual_que_la_tarjeta(self):
        self.cargar([('MART-500', 1, '2500.00')], medio='mercado_pago')
        pago = Pago.query.one()
        self.assertEqual(pago.metodo, 'mercado_pago')
        self.assertEqual(pago.estado, 'pendiente')
        self.assertIsNone(pago.comision)

    def test_no_se_crea_ninguna_cuenta_de_cobro(self):
        from models import CuentaCobro, MovimientoCuenta
        self.cargar([('MART-500', 1, '2500.00')], medio='mercado_pago')
        self.assertEqual(CuentaCobro.query.count(), 0)
        self.assertEqual(MovimientoCuenta.query.count(), 0)
        self.assertIsNone(Pago.query.one().cuenta_cobro_id)


class TestProductoSinCosto(BaseVentaManual):
    """Vender algo cuyo costo Roman todavia no cargo no puede romper nada."""

    def setUp(self):
        super().setUp()
        self.respuesta = self.cargar([
            ('CINTA-19', 4, '350.00'),
            ('MART-500', 1, '2500.00'),
        ], medio='efectivo')

    def test_la_venta_se_guarda(self):
        self.assertEqual(self.respuesta.status_code, 200)
        self.assertEqual(self.unico_pedido().total, Decimal('3900.00'))

    def test_el_snapshot_del_producto_sin_costo_queda_null(self):
        item = PedidoItem.query.filter_by(producto_id=self.sin_costo.id).one()
        self.assertIsNone(item.costo_unitario_snapshot)

    def test_la_otra_linea_conserva_su_costo(self):
        """El NULL de una linea no contagia a la de al lado."""
        item = PedidoItem.query.filter_by(producto_id=self.con_costo.id).one()
        self.assertEqual(item.costo_unitario_snapshot, Decimal('1200.00'))


class TestValidaciones(BaseVentaManual):
    """Lo que no llega a ser una venta no deja nada escrito."""

    def assertNoQuedoNada(self):
        self.assertEqual(Pedido.query.count(), 0)
        self.assertEqual(PedidoItem.query.count(), 0)
        self.assertEqual(Pago.query.count(), 0)

    def test_sin_items_no_guarda(self):
        self.cargar([])
        self.assertNoQuedoNada()

    def test_sin_medio_de_cobro_no_guarda(self):
        self.cargar([('MART-500', 1, '2500.00')], medio='')
        self.assertNoQuedoNada()

    def test_un_sku_que_no_existe_no_guarda(self):
        self.cargar([('NO-EXISTE', 1, '100.00')])
        self.assertNoQuedoNada()

    def test_un_producto_de_otra_empresa_no_se_puede_vender(self):
        otra = Empresa(nombre='Ferreteria Ajena')
        db.session.add(otra)
        db.session.flush()
        db.session.add(Producto(empresa_id=otra.id, sku='AJENO-1',
                                nombre='Producto ajeno', precio_lista=Decimal('100.00')))
        db.session.commit()

        self.cargar([('AJENO-1', 1, '100.00')])
        self.assertNoQuedoNada()

    def test_cantidad_cero_no_guarda(self):
        self.cargar([('MART-500', 0, '2500.00')])
        self.assertNoQuedoNada()

    def test_precio_negativo_no_guarda(self):
        self.cargar([('MART-500', 1, '-10.00')])
        self.assertNoQuedoNada()

    def test_fecha_invalida_no_guarda(self):
        self.cargar([('MART-500', 1, '2500.00')], fecha='no-es-una-fecha')
        self.assertNoQuedoNada()

    def test_una_linea_mala_tira_abajo_toda_la_venta(self):
        """La transaccion es una sola: no puede quedar el pedido con la mitad
        de los items y sin pago."""
        self.cargar([
            ('MART-500', 1, '2500.00'),
            ('NO-EXISTE', 1, '100.00'),
        ])
        self.assertNoQuedoNada()

    def test_las_filas_vacias_del_formulario_se_ignoran(self):
        """El formulario puede mandar filas en blanco; no son un error."""
        self.cargar([('MART-500', 1, '2500.00'), ('', '', '')])
        pedido = self.unico_pedido()
        self.assertEqual(PedidoItem.query.filter_by(pedido_id=pedido.id).count(), 1)


class TestFechaEditable(BaseVentaManual):
    def test_se_puede_cargar_una_venta_de_otro_dia(self):
        self.cargar([('MART-500', 1, '2500.00')], fecha='2026-08-15')
        self.assertEqual(self.unico_pedido().fecha_pedido, datetime(2026, 8, 15))


class TestVariasVentasManuales(BaseVentaManual):
    def test_dos_ventas_manuales_conviven_sin_id_externo(self):
        """La UNIQUE (canal_id, id_externo) no puede impedir la segunda venta:
        dos NULL no colisionan entre si."""
        self.cargar([('MART-500', 1, '2500.00')])
        self.cargar([('DEST-PH2', 1, '900.00')])

        pedidos = Pedido.query.filter_by(canal_id=self.canal_manual_id).all()
        self.assertEqual(len(pedidos), 2)
        self.assertEqual([p.id_externo for p in pedidos], [None, None])
        self.assertEqual(Pago.query.count(), 2)


class TestListado(BaseVentaManual):
    """GET /pedidos/listar."""

    def test_muestra_juntos_los_pedidos_de_tiendanube_y_los_manuales(self):
        self.pedido_tiendanube()
        self.cargar([('MART-500', 2, '2500.00')], medio='efectivo')

        pagina = self.client.get('/pedidos/listar').get_data(as_text=True)

        self.assertIn('Tiendanube', pagina)
        self.assertIn('Manual', pagina)
        self.assertIn('5000.00', pagina)   # el de Tiendanube
        self.assertIn('Efectivo', pagina)  # el medio de la venta manual

    def test_el_medio_solo_aparece_en_las_ventas_manuales(self):
        """El pedido de Tiendanube no tiene medio elegido a mano: su fila
        muestra un guion, no el medio de la venta de al lado."""
        self.pedido_tiendanube()
        self.cargar([('MART-500', 1, '2500.00')], medio='tarjeta')

        pagina = self.client.get('/pedidos/listar').get_data(as_text=True)
        self.assertIn('Tarjeta', pagina)
        self.assertIn('>-<', pagina.replace(' ', '').replace('\n', ''))

    def test_no_muestra_pedidos_de_otra_empresa(self):
        otra = Empresa(nombre='Ferreteria Ajena')
        db.session.add(otra)
        db.session.flush()
        canal_ajeno = CanalVenta(empresa_id=otra.id, tipo='manual',
                                 nombre='Venta manual / presencial', activo=True)
        db.session.add(canal_ajeno)
        db.session.flush()
        db.session.add(Pedido(empresa_id=otra.id, canal_id=canal_ajeno.id,
                              fecha_pedido=datetime(2026, 8, 1), estado='completado',
                              moneda='ARS', total_bruto=Decimal('99999.99'),
                              total=Decimal('99999.99')))
        db.session.commit()

        pagina = self.client.get('/pedidos/listar').get_data(as_text=True)
        self.assertNotIn('99999.99', pagina)

    def test_sin_ventas_no_rompe(self):
        pagina = self.client.get('/pedidos/listar').get_data(as_text=True)
        self.assertIn('Todavía no hay ventas cargadas', pagina)


class TestRequierenLogin(BaseVentaManual):
    def test_el_formulario_requiere_login(self):
        anonimo = app.test_client()
        self.assertEqual(anonimo.get('/pedidos/manual/nuevo').status_code, 302)

    def test_el_listado_requiere_login(self):
        anonimo = app.test_client()
        self.assertEqual(anonimo.get('/pedidos/listar').status_code, 302)

    def test_cargar_sin_login_no_escribe_nada(self):
        anonimo = app.test_client()
        anonimo.post('/pedidos/manual/nuevo', data={
            'sku': ['MART-500'], 'cantidad': ['1'],
            'precio_unitario': ['2500.00'], 'medio': 'efectivo'})
        self.assertEqual(Pedido.query.count(), 0)


class TestFormularioGet(BaseVentaManual):
    """El selector de productos: el catalogo real puede tener cientos, asi que
    la pantalla los ofrece por datalist en vez de un <select> gigante."""

    def test_el_formulario_lista_el_catalogo_en_un_datalist(self):
        pagina = self.client.get('/pedidos/manual/nuevo').get_data(as_text=True)
        self.assertIn('<datalist', pagina)
        self.assertIn('MART-500', pagina)
        self.assertIn('Martillo 500g', pagina)
        self.assertNotIn('<select id="producto"', pagina)

    def test_no_ofrece_productos_de_otra_empresa(self):
        otra = Empresa(nombre='Ferreteria Ajena')
        db.session.add(otra)
        db.session.flush()
        db.session.add(Producto(empresa_id=otra.id, sku='AJENO-1',
                                nombre='Producto ajeno', precio_lista=Decimal('100.00')))
        db.session.commit()

        pagina = self.client.get('/pedidos/manual/nuevo').get_data(as_text=True)
        self.assertNotIn('AJENO-1', pagina)

    def test_los_tres_medios_estan_disponibles(self):
        pagina = self.client.get('/pedidos/manual/nuevo').get_data(as_text=True)
        for valor in ('efectivo', 'tarjeta', 'mercado_pago'):
            self.assertIn('value="%s"' % valor, pagina)


class TestCanalManual(BaseVentaManual):
    """El canal manual es distinto de los externos, a proposito."""

    def test_esta_activo_sin_credencial(self):
        from models import CredencialCanal
        canal = db.session.get(CanalVenta, self.canal_manual_id)
        self.assertTrue(canal.activo)
        self.assertEqual(
            CredencialCanal.query.filter_by(canal_id=canal.id).count(), 0)

    def test_se_crea_al_vuelo_si_la_empresa_no_lo_tiene(self):
        """La semilla cubrio las empresas que existian; una nueva no lo tiene y
        la ruta no puede fallar por eso."""
        canal = db.session.get(CanalVenta, self.canal_manual_id)
        db.session.delete(canal)
        db.session.commit()

        self.cargar([('MART-500', 1, '2500.00')])

        creado = CanalVenta.query.filter_by(
            empresa_id=self.empresa_id, tipo='manual').one()
        self.assertTrue(creado.activo)
        self.assertEqual(self.unico_pedido().canal_id, creado.id)

    def test_el_pago_manual_no_apunta_a_ningun_procesador_externo(self):
        self.cargar([('MART-500', 1, '2500.00')])
        self.assertEqual(Pago.query.one().procesador, 'manual')


if __name__ == '__main__':
    unittest.main(verbosity=2)
