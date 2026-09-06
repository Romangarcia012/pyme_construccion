# -*- coding: utf-8 -*-
"""Tests de FASE-VENTAS-EDITAR-S1 (editar una venta manual ya cargada).

    python -m unittest discover -s tests -v

Hasta esta slice, de una venta guardada se podian corregir tres cosas desde el
listado -- comision, cuenta y marca de regalo -- y ninguna de las cuatro que de
verdad se tipean mal: fecha, medio, producto y monto. Una venta cargada con el
precio equivocado no tenia arreglo dentro de la app.

Lo que se prueba:

    corregir el monto            -> el total del pedido y el del pago siguen al
                                    precio nuevo
    subir la cantidad            -> descuenta SOLO la diferencia, no la venta
                                    entera de nuevo
    bajar la cantidad            -> devuelve la diferencia al stock
    cambiar de producto          -> devuelve al viejo y descuenta del nuevo
    pedido sincronizado          -> se rechaza y no se toca nada
    la auditoria lo ve           -> queda fila con valor anterior y nuevo
    el snapshot no se recalcula  -> una linea que sobrevive conserva el costo
                                    del dia de la venta
    la cuenta y la comision      -> son las MISMAS columnas que ya editaba el
                                    listado, no una segunda implementacion
    el boton solo en manuales    -> el listado no ofrece editar un pedido de
                                    Tiendanube

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.ayuda_auth import request_anonimo  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    CuentaCobro,
    Empresa,
    Historial,
    Pago,
    Pedido,
    PedidoItem,
    Producto,
    Usuario,
    db,
)

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


class BaseEdicion(unittest.TestCase):
    """Una venta manual ya cargada, que es lo que esta slice viene a corregir.

    El escenario es el real: canal manual que cobra en la cuenta de Roman, dos
    productos con stock controlado, y una venta cargada por la ruta de alta
    -- no insertada a mano -- para que el stock arranque descontado como en
    produccion.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test VENTAS-EDITAR-S1')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='editar1@test.local',
                               empresa_id=self.empresa.id, rol='admin',
                               verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.cuenta_roman = CuentaCobro(empresa_id=self.empresa.id,
                                        nombre='Roman - Presencial',
                                        tipo='mercadopago', socio='roman')
        self.cuenta_nachi = CuentaCobro(empresa_id=self.empresa.id,
                                        nombre='Nachi - Mercado Libre',
                                        tipo='mercadopago', socio='nachi')
        db.session.add_all([self.cuenta_roman, self.cuenta_nachi])
        db.session.flush()

        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial',
                                       activo=True,
                                       cuenta_cobro_id=self.cuenta_roman.id)
        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Tiendanube', activo=True,
                                   cuenta_cobro_id=self.cuenta_roman.id)
        db.session.add_all([self.canal_manual, self.canal_tn])

        self.martillo = Producto(empresa_id=self.empresa.id, sku='MART-500',
                                 nombre='Martillo 500g', stock=10,
                                 costo_unitario=Decimal('1200.00'),
                                 precio_lista=Decimal('2500.00'))
        self.pinza = Producto(empresa_id=self.empresa.id, sku='PINZ-200',
                              nombre='Pinza universal', stock=8,
                              costo_unitario=Decimal('900.00'),
                              precio_lista=Decimal('1800.00'))
        db.session.add_all([self.martillo, self.pinza])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.id_cuenta_roman = self.cuenta_roman.id
        self.id_cuenta_nachi = self.cuenta_nachi.id
        self.id_canal_manual = self.canal_manual.id
        self.id_canal_tn = self.canal_tn.id
        self.id_martillo = self.martillo.id
        self.id_pinza = self.pinza.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def cargar(self, sku='MART-500', cantidad='1', precio='5000.00',
               medio='efectivo', nota=''):
        """Alta de una venta manual por la ruta real, con su descuento de stock."""
        respuesta = self.client.post('/pedidos/manual/nuevo', data={
            'sku': [sku],
            'cantidad': [cantidad],
            'precio_unitario': [precio],
            'fecha': date.today().isoformat(),
            'medio': medio,
            'nota': nota,
            'cuenta_cobro_override': '',
        }, follow_redirects=True)
        self.assertEqual(respuesta.status_code, 200)
        return self.unico_pedido()

    def editar(self, pedido_id, skus=('MART-500',), cantidades=('1',),
               precios=('5000.00',), medio='efectivo', fecha=None, nota='',
               cuenta='', comision='', regalo=False, seguir=True):
        datos = {
            'sku': list(skus),
            'cantidad': list(cantidades),
            'precio_unitario': list(precios),
            'fecha': fecha or date.today().isoformat(),
            'medio': medio,
            'nota': nota,
            'cuenta_cobro_override': cuenta,
            'comision_plataforma': comision,
        }
        if regalo:
            datos['es_regalo'] = '1'
        return self.client.post('/pedidos/manual/editar/%d' % pedido_id,
                                data=datos, follow_redirects=seguir)

    def unico_pedido(self):
        pedidos = Pedido.query.all()
        self.assertEqual(len(pedidos), 1, 'se esperaba exactamente un pedido')
        return pedidos[0]

    def stock(self, producto_id):
        return db.session.get(Producto, producto_id).stock

    def pedido_tiendanube(self):
        """Un pedido sincronizado, con su id del canal como en produccion."""
        pedido = Pedido(empresa_id=self.empresa_id, canal_id=self.id_canal_tn,
                        id_externo='998877', numero_externo='1042',
                        fecha_pedido=datetime(2026, 3, 4, 10, 0),
                        estado='completado', moneda='ARS',
                        total_bruto=Decimal('7000.00'), total=Decimal('7000.00'))
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id,
                                  producto_id=self.id_martillo,
                                  sku_externo='MART-500',
                                  descripcion='Martillo 500g', cantidad=2,
                                  precio_unitario=Decimal('3500.00'),
                                  descuento_unitario=Decimal('0.00'),
                                  subtotal=Decimal('7000.00')))
        db.session.commit()
        return pedido


class TestCorregirElMonto(BaseEdicion):
    """El caso que trajo la slice: la venta quedo cargada con el monto mal."""

    def test_editar_venta_manual_corrige_monto(self):
        """Cambiar el precio de una linea actualiza el total y no rompe nada."""
        pedido = self.cargar(precio='5000.00')
        pedido_id = pedido.id
        self.assertEqual(pedido.total, Decimal('5000.00'))

        respuesta = self.editar(pedido_id, precios=('7250.50',))
        self.assertEqual(respuesta.status_code, 200)

        pedido = db.session.get(Pedido, pedido_id)
        self.assertEqual(pedido.total, Decimal('7250.50'))
        self.assertEqual(pedido.total_bruto, Decimal('7250.50'))

        self.assertEqual(len(pedido.items), 1)
        self.assertEqual(pedido.items[0].precio_unitario, Decimal('7250.50'))
        self.assertEqual(pedido.items[0].subtotal, Decimal('7250.50'))

        # El pago tiene que decir lo mismo que la venta: un pago por $5000 sobre
        # una venta de $7250,50 rompe la conciliacion sin que nada avise.
        pagos = Pago.query.filter_by(pedido_id=pedido_id).all()
        self.assertEqual(len(pagos), 1, 'la edicion no puede duplicar el pago')
        self.assertEqual(pagos[0].monto_bruto, Decimal('7250.50'))
        self.assertEqual(pagos[0].monto_neto, Decimal('7250.50'))

    def test_corregir_el_monto_no_toca_el_stock(self):
        """Cambiar solo el precio no mueve una sola unidad."""
        pedido = self.cargar(cantidad='2', precio='5000.00')
        self.assertEqual(self.stock(self.id_martillo), 8)

        self.editar(pedido.id, cantidades=('2',), precios=('6000.00',))

        self.assertEqual(self.stock(self.id_martillo), 8)

    def test_se_puede_corregir_la_fecha_y_el_medio(self):
        pedido = self.cargar(medio='efectivo')
        pedido_id = pedido.id

        self.editar(pedido_id, fecha='2026-01-15', medio='tarjeta')

        pedido = db.session.get(Pedido, pedido_id)
        self.assertEqual(pedido.fecha_pedido.date(), date(2026, 1, 15))

        pago = Pago.query.filter_by(pedido_id=pedido_id).one()
        self.assertEqual(pago.metodo, 'tarjeta')
        # Tarjeta vuelve a pendiente con la comision en NULL, que es "todavia
        # no se sabe" -- la misma regla del alta, no una excepcion de la
        # edicion.
        self.assertEqual(pago.estado, 'pendiente')
        self.assertIsNone(pago.comision)
        self.assertIsNone(pago.fecha_acreditacion)
        self.assertEqual(pago.fecha_pago.date(), date(2026, 1, 15))


class TestElStockSeAjustaPorLaDiferencia(BaseEdicion):
    """El corazon de la slice: editar no puede volver a descontar la venta."""

    def test_editar_ajusta_stock_por_diferencia(self):
        """De 1 a 3 descuenta 2 mas, no 3."""
        pedido = self.cargar(cantidad='1')
        self.assertEqual(self.stock(self.id_martillo), 9)

        self.editar(pedido.id, cantidades=('3',))

        self.assertEqual(self.stock(self.id_martillo), 7,
                         'descontar 3 de nuevo dejaria 6: la venta se cobro '
                         'una sola vez')

    def test_editar_reduce_cantidad_devuelve_stock(self):
        """De 3 a 1 devuelve 2."""
        pedido = self.cargar(cantidad='3')
        self.assertEqual(self.stock(self.id_martillo), 7)

        self.editar(pedido.id, cantidades=('1',))

        self.assertEqual(self.stock(self.id_martillo), 9)

    def test_cambiar_de_producto_devuelve_al_viejo_y_descuenta_del_nuevo(self):
        pedido = self.cargar(sku='MART-500', cantidad='2')
        self.assertEqual(self.stock(self.id_martillo), 8)
        self.assertEqual(self.stock(self.id_pinza), 8)

        self.editar(pedido.id, skus=('PINZ-200',), cantidades=('2',))

        self.assertEqual(self.stock(self.id_martillo), 10, 'las 2 vuelven')
        self.assertEqual(self.stock(self.id_pinza), 6, 'y salen de la pinza')

    def test_agregar_una_linea_descuenta_solo_la_nueva(self):
        pedido = self.cargar(sku='MART-500', cantidad='1')

        self.editar(pedido.id, skus=('MART-500', 'PINZ-200'),
                    cantidades=('1', '4'), precios=('5000.00', '1800.00'))

        self.assertEqual(self.stock(self.id_martillo), 9, 'la linea vieja no se '
                                                          'vuelve a descontar')
        self.assertEqual(self.stock(self.id_pinza), 4)

        pedido = db.session.get(Pedido, pedido.id)
        self.assertEqual(len(pedido.items), 2)
        self.assertEqual(pedido.total, Decimal('12200.00'))

    def test_quitar_una_linea_devuelve_su_stock(self):
        pedido = self.cargar(sku='MART-500', cantidad='1')
        self.editar(pedido.id, skus=('MART-500', 'PINZ-200'),
                    cantidades=('1', '4'), precios=('5000.00', '1800.00'))
        self.assertEqual(self.stock(self.id_pinza), 4)

        self.editar(pedido.id, skus=('MART-500',), cantidades=('1',),
                    precios=('5000.00',))

        self.assertEqual(self.stock(self.id_pinza), 8, 'la linea quitada devuelve '
                                                       'todo lo suyo')
        self.assertEqual(self.stock(self.id_martillo), 9)
        pedido = db.session.get(Pedido, pedido.id)
        self.assertEqual(len(pedido.items), 1)
        self.assertEqual(pedido.total, Decimal('5000.00'))

    def test_un_producto_sin_control_de_stock_no_se_inventa(self):
        """`stock` NULL es "nadie lleva la cuenta", no "hay cero"."""
        self.martillo.stock = None
        db.session.commit()

        pedido = self.cargar(cantidad='1')
        self.editar(pedido.id, cantidades=('5',))

        self.assertIsNone(self.stock(self.id_martillo))


class TestNoSePuedeEditarUnPedidoSincronizado(BaseEdicion):
    """Tiendanube y Mercado Libre son la fuente de verdad de sus pedidos."""

    def test_no_se_puede_editar_pedido_sincronizado(self):
        """El POST rebota y no toca ni el pedido ni el stock."""
        pedido = self.pedido_tiendanube()
        pedido_id = pedido.id
        stock_previo = self.stock(self.id_martillo)

        respuesta = self.editar(pedido_id, cantidades=('9',),
                                precios=('1.00',))
        self.assertEqual(respuesta.status_code, 200)

        pedido = db.session.get(Pedido, pedido_id)
        self.assertEqual(pedido.total, Decimal('7000.00'))
        self.assertEqual(len(pedido.items), 1)
        self.assertEqual(pedido.items[0].cantidad, 2)
        self.assertEqual(self.stock(self.id_martillo), stock_previo)

    def test_el_get_de_un_pedido_sincronizado_tampoco_abre(self):
        pedido = self.pedido_tiendanube()

        respuesta = self.client.get('/pedidos/manual/editar/%d' % pedido.id,
                                    follow_redirects=True)
        cuerpo = respuesta.get_data(as_text=True)
        self.assertIn('no es una venta manual', cuerpo)

    def test_un_pedido_de_otra_empresa_no_se_edita(self):
        """El filtro por empresa es lo unico que separa las dos empresas."""
        otra = Empresa(nombre='Otra empresa')
        db.session.add(otra)
        db.session.flush()
        canal = CanalVenta(empresa_id=otra.id, tipo='manual',
                           nombre='Manual ajeno', activo=True)
        db.session.add(canal)
        db.session.flush()
        ajeno = Pedido(empresa_id=otra.id, canal_id=canal.id, id_externo=None,
                       fecha_pedido=datetime(2026, 2, 2), estado='completado',
                       moneda='ARS', total_bruto=Decimal('100.00'),
                       total=Decimal('100.00'))
        db.session.add(ajeno)
        db.session.commit()
        ajeno_id = ajeno.id

        respuesta = self.editar(ajeno_id)
        self.assertIn('no existe o no es de tu empresa',
                      respuesta.get_data(as_text=True))
        self.assertEqual(db.session.get(Pedido, ajeno_id).total,
                         Decimal('100.00'))

    def test_un_pedido_que_no_existe_no_revienta(self):
        respuesta = self.editar(99999)
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('no existe o no es de tu empresa',
                      respuesta.get_data(as_text=True))


class TestLaEdicionQuedaEnElHistorial(BaseEdicion):
    """El hook de auditoria ya cubre `Pedido`: no hace falta codigo nuevo, pero
    si hace falta comprobar que la edicion pasa por ahi."""

    def filas_del_pedido(self, pedido_id):
        return (Historial.query
                .filter_by(empresa_id=self.empresa_id, tipo='pedido',
                           id_registro=pedido_id, accion='editar')
                .order_by(Historial.id)
                .all())

    def test_edicion_queda_en_historial(self):
        """Con el valor anterior y el nuevo, no solo "algo cambio"."""
        pedido = self.cargar(precio='5000.00')
        pedido_id = pedido.id
        # El alta ya dejo su fila de 'crear'; lo que se mira es lo que agrega
        # la edicion.
        self.assertEqual(self.filas_del_pedido(pedido_id), [])

        self.editar(pedido_id, precios=('7250.50',))

        filas = self.filas_del_pedido(pedido_id)
        self.assertTrue(filas, 'la edicion no dejo rastro en el historial')

        totales = [f for f in filas if ' - total' in (f.descripcion or '')
                   and 'total_bruto' not in (f.descripcion or '')]
        self.assertEqual(len(totales), 1,
                         'se esperaba una fila para el campo `total`')
        self.assertEqual(Decimal(totales[0].valor_anterior), Decimal('5000.00'))
        self.assertEqual(Decimal(totales[0].valor_nuevo), Decimal('7250.50'))
        self.assertEqual(totales[0].usuario_id, self.usuario_id)

    def test_la_marca_de_regalo_tambien_queda_registrada(self):
        pedido = self.cargar()
        pedido_id = pedido.id

        self.editar(pedido_id, regalo=True)

        self.assertTrue(db.session.get(Pedido, pedido_id).es_regalo)
        regalos = [f for f in self.filas_del_pedido(pedido_id)
                   if 'es_regalo' in (f.descripcion or '')]
        self.assertEqual(len(regalos), 1)


class TestLoQueLaEdicionNoToca(BaseEdicion):
    """Los limites explicitos de la slice."""

    def test_el_costo_snapshot_no_se_recalcula(self):
        """Una linea que sobrevive conserva el costo del dia de la venta."""
        pedido = self.cargar(cantidad='1', precio='5000.00')
        pedido_id = pedido.id
        self.assertEqual(pedido.items[0].costo_unitario_snapshot,
                         Decimal('1200.00'))

        # La lista de costos cambia DESPUES de la venta. Editar el precio de
        # venta no puede arrastrar el costo: la venta no se hizo de nuevo.
        self.martillo.costo_unitario = Decimal('1900.00')
        db.session.commit()

        self.editar(pedido_id, cantidades=('4',), precios=('9000.00',))

        pedido = db.session.get(Pedido, pedido_id)
        self.assertEqual(pedido.items[0].costo_unitario_snapshot,
                         Decimal('1200.00'),
                         'el snapshot es el costo del dia de la venta')

    def test_una_linea_agregada_hoy_nace_con_el_costo_de_hoy(self):
        """No hay snapshot anterior que respetarle: es lo mismo que hace el alta."""
        pedido = self.cargar(sku='MART-500', cantidad='1')

        self.editar(pedido.id, skus=('MART-500', 'PINZ-200'),
                    cantidades=('1', '1'), precios=('5000.00', '1800.00'))

        pedido = db.session.get(Pedido, pedido.id)
        nueva = [i for i in pedido.items if i.producto_id == self.id_pinza][0]
        self.assertEqual(nueva.costo_unitario_snapshot, Decimal('900.00'))

    def test_la_edicion_no_cambia_el_canal_ni_el_id_externo(self):
        pedido = self.cargar()
        pedido_id = pedido.id

        self.editar(pedido_id, precios=('1.00',))

        pedido = db.session.get(Pedido, pedido_id)
        self.assertEqual(pedido.canal_id, self.id_canal_manual)
        self.assertIsNone(pedido.id_externo)


class TestLaCuentaYLaComisionSonLasMismas(BaseEdicion):
    """No se duplico la logica de S2/S3 ni la de REPORTES-S3-COMISION: son las
    mismas dos columnas, leidas con las mismas dos funciones."""

    def test_la_edicion_setea_la_misma_columna_de_cuenta(self):
        pedido = self.cargar()
        pedido_id = pedido.id
        self.assertIsNone(pedido.cuenta_cobro_override_id)

        self.editar(pedido_id, cuenta=str(self.id_cuenta_nachi))

        pedido = db.session.get(Pedido, pedido_id)
        self.assertEqual(pedido.cuenta_cobro_override_id, self.id_cuenta_nachi)
        self.assertEqual(pedido.cuenta_cobro_efectiva.id, self.id_cuenta_nachi)

    def test_una_cuenta_de_otra_empresa_voltea_la_edicion_entera(self):
        otra = Empresa(nombre='Otra empresa con cuenta')
        db.session.add(otra)
        db.session.flush()
        ajena = CuentaCobro(empresa_id=otra.id, nombre='Cuenta ajena',
                            tipo='mercadopago', socio='roman')
        db.session.add(ajena)
        db.session.commit()

        pedido = self.cargar(precio='5000.00')
        pedido_id = pedido.id

        respuesta = self.editar(pedido_id, precios=('9999.00',),
                                cuenta=str(ajena.id))
        self.assertIn('no es de esta empresa', respuesta.get_data(as_text=True))

        pedido = db.session.get(Pedido, pedido_id)
        self.assertEqual(pedido.total, Decimal('5000.00'),
                         'el precio no se guarda si la cuenta era invalida')
        self.assertIsNone(pedido.cuenta_cobro_override_id)

    def test_la_comision_vacia_no_es_cero(self):
        """Mismo criterio que el listado: vacio es "todavia no la se"."""
        pedido = self.cargar()
        pedido_id = pedido.id

        self.editar(pedido_id, comision='250.00')
        self.assertEqual(db.session.get(Pedido, pedido_id).comision_plataforma,
                         Decimal('250.00'))

        self.editar(pedido_id, comision='')
        self.assertIsNone(db.session.get(Pedido, pedido_id).comision_plataforma)

    def test_una_comision_negativa_voltea_la_edicion_entera(self):
        pedido = self.cargar(precio='5000.00')
        pedido_id = pedido.id

        respuesta = self.editar(pedido_id, precios=('9999.00',), comision='-5')
        self.assertIn('no puede ser negativa', respuesta.get_data(as_text=True))
        self.assertEqual(db.session.get(Pedido, pedido_id).total,
                         Decimal('5000.00'))


class TestValidacionIgualQueElAlta(BaseEdicion):
    """Mismo vocabulario, mismos mensajes: `_leer_items` / `_leer_fecha` /
    `_leer_precio` son las del alta, no una copia."""

    def test_una_venta_sin_items_no_se_guarda(self):
        pedido = self.cargar(precio='5000.00')
        pedido_id = pedido.id

        respuesta = self.editar(pedido_id, skus=('',), cantidades=('',),
                                precios=('',))
        self.assertIn('no tiene ningun item cargado',
                      respuesta.get_data(as_text=True))
        self.assertEqual(db.session.get(Pedido, pedido_id).total,
                         Decimal('5000.00'))

    def test_un_sku_que_no_existe_voltea_la_edicion(self):
        pedido = self.cargar()
        pedido_id = pedido.id
        stock_previo = self.stock(self.id_martillo)

        respuesta = self.editar(pedido_id, skus=('NO-EXISTE',))
        self.assertIn('No existe ningun producto', respuesta.get_data(as_text=True))
        self.assertEqual(self.stock(self.id_martillo), stock_previo)

    def test_cantidad_cero_voltea_la_edicion(self):
        pedido = self.cargar(cantidad='2')
        pedido_id = pedido.id

        respuesta = self.editar(pedido_id, cantidades=('0',))
        self.assertIn('mayor a cero', respuesta.get_data(as_text=True))
        self.assertEqual(db.session.get(Pedido, pedido_id).items[0].cantidad, 2)

    def test_una_fecha_invalida_voltea_la_edicion(self):
        pedido = self.cargar()
        pedido_id = pedido.id

        respuesta = self.editar(pedido_id, fecha='15/01/2026')
        self.assertIn('La fecha no es valida', respuesta.get_data(as_text=True))

    def test_un_medio_invalido_voltea_la_edicion(self):
        pedido = self.cargar()
        respuesta = self.editar(pedido.id, medio='trueque')
        self.assertIn('Elegi un medio de cobro', respuesta.get_data(as_text=True))


class TestLaPantalla(BaseEdicion):
    """Que el formulario llegue cargado y que el boton este donde tiene que estar."""

    def test_el_formulario_llega_con_la_venta_cargada(self):
        pedido = self.cargar(cantidad='3', precio='4321.00', medio='tarjeta',
                             nota='Cliente Juan')
        cuerpo = self.client.get(
            '/pedidos/manual/editar/%d' % pedido.id).get_data(as_text=True)

        self.assertIn('MART-500', cuerpo)
        self.assertIn('4321.00', cuerpo)
        self.assertIn('Cliente Juan', cuerpo)
        self.assertIn('value="3"', cuerpo)

    def test_el_listado_ofrece_editar_solo_las_manuales(self):
        manual = self.cargar()
        sincronizado = self.pedido_tiendanube()

        cuerpo = self.client.get('/pedidos/listar').get_data(as_text=True)
        self.assertIn('/pedidos/manual/editar/%d' % manual.id, cuerpo)
        self.assertNotIn('/pedidos/manual/editar/%d' % sincronizado.id, cuerpo)


class TestAuthDeLaRutaDeEdicion(BaseEdicion):
    """Sin sesion no se ve ni se toca nada. Ver `tests/ayuda_auth`."""

    def test_el_get_pide_login(self):
        pedido = self.cargar()
        respuesta = request_anonimo(self.ctx, 'get',
                                    '/pedidos/manual/editar/%d' % pedido.id)
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers.get('Location', ''))

    def test_el_post_pide_login_y_no_escribe(self):
        pedido = self.cargar(precio='5000.00')
        pedido_id = pedido.id

        respuesta = request_anonimo(
            self.ctx, 'post', '/pedidos/manual/editar/%d' % pedido_id,
            data={'sku': ['MART-500'], 'cantidad': ['1'],
                  'precio_unitario': ['1.00'], 'fecha': '2026-01-01',
                  'medio': 'efectivo'})
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(db.session.get(Pedido, pedido_id).total,
                         Decimal('5000.00'))


if __name__ == '__main__':
    unittest.main()
