# -*- coding: utf-8 -*-
"""Tests de FASE-CAJA-SOCIO-S5 (facturado real: sin envio y sin regalos).

    python -m unittest discover -s tests -v

DOS CORRECCIONES A LA MISMA DEFINICION

Hasta S4 "Facturado" era SUM(pedido.total): lo que pago el comprador. Dos
cosas que estaban adentro no tenian por que estarlo.

  1. EL ENVIO. Se le cobra al cliente y se le paga al correo el mismo dia. Es
     plata que pasa, no plata que queda. La formula pasa a
     SUM(total - total_envio); `total_envio` es NOT NULL y vale 0.00 en toda
     venta de mostrador, asi que el caso comun no se mueve.

  2. LOS REGALOS. El "Sorteo" -- mercaderia a un influencer -- se cargo como
     pedido de $1 la unidad para que el stock se descontara. El pedido tiene
     que existir; el ingreso no existio. Sale entero de la suma.

Lo que se prueba:

    el envio no suma           -> un pedido con total_envio>0 aporta
                                  total-envio, no total
    sin envio no cambia nada   -> total_envio=0 da lo mismo que antes; es el
                                  caso de casi todas las filas
    el regalo no suma          -> ni su total ni su envio ni su comision
    el stock sigue descontado  -> marcar un regalo no devuelve una unidad
    la marca es un campo       -> es_regalo se escribe desde el alta y desde
                                  el listado, y no depende de la nota
    el costo NO se toca        -> costo_envio_vendedor sigue afuera: cuando el
                                  correo cobra mas de lo cobrado, eso es un
                                  gasto, no menos facturacion
    la pantalla lo dice        -> el regalo se ve contado aparte, no
                                  desaparece sin explicacion

POR QUE UN CAMPO Y NO LA NOTA

`es_regalo` es una columna porque es un dato del negocio. Con la nota,
"Sorteo" excluia el pedido y "sorteo IG" no, sin que nada avisara; y el total
simbolico no sirve de senal -- $4 es una venta chica posible.
`test_la_nota_no_decide_nada` fija eso: una nota que dice "Sorteo" en un
pedido sin marcar SUMA, porque es una venta.

QUE NO CAMBIA ESTA SLICE

El costo de lo regalado sigue siendo un gasto real y se carga a mano donde se
cargan todos: la marca no lo genera sola, para no duplicar el que ya existe.
El reporte de margen no se toca. `comision_plataforma` no se toca.

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
    ORIGEN_FACTURACION,
    CanalVenta,
    Categoria,
    CuentaCobro,
    Empresa,
    Gasto,
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


class BaseCaja(unittest.TestCase):
    """El reparto real: Tiendanube cobra en Roman, el manual tambien.

    Los montos copian los del pedido #100 de produccion -- total 13696.90 con
    7630.00 de envio -- para que la resta que se prueba sea la misma que se
    verifico contra el Excel, y no una inventada que da redondo.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test CAJA-SOCIO-S5')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test',
                               email='cajasocio5@test.local',
                               empresa_id=self.empresa.id, rol='admin',
                               verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.cuenta_roman = CuentaCobro(empresa_id=self.empresa.id,
                                        nombre='Roman - Presencial y Tiendanube',
                                        tipo='mercadopago', socio='roman')
        self.cuenta_nachi = CuentaCobro(empresa_id=self.empresa.id,
                                        nombre='Nachi - Mercado Libre',
                                        tipo='mercadopago', socio='nachi')
        db.session.add_all([self.cuenta_roman, self.cuenta_nachi])

        self.categoria = Categoria(nombre='Materiales', tipo='gasto',
                                   empresa_id=self.empresa.id)
        db.session.add(self.categoria)
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.id_cuenta_roman = self.cuenta_roman.id
        self.id_cuenta_nachi = self.cuenta_nachi.id
        self.id_categoria = self.categoria.id

        self.canal_tn = CanalVenta(empresa_id=self.empresa_id,
                                   tipo='tiendanube', nombre='Korvo',
                                   activo=True, id_tienda_externo='9999',
                                   cuenta_cobro_id=self.id_cuenta_roman)
        self.canal_manual = CanalVenta(empresa_id=self.empresa_id,
                                       tipo='manual', nombre='Venta manual',
                                       activo=True,
                                       cuenta_cobro_id=self.id_cuenta_roman)
        db.session.add_all([self.canal_tn, self.canal_manual])
        db.session.commit()

        self.id_canal_tn = self.canal_tn.id
        self.id_canal_manual = self.canal_manual.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def venta(self, canal_id=None, total='10000.00', envio='0.00',
              comision='0.00', regalo=False, override=None, nota=None,
              estado='open', costo_envio_vendedor=None):
        fila = Pedido(empresa_id=self.empresa_id,
                      canal_id=canal_id or self.id_canal_tn,
                      fecha_pedido=datetime(2026, 9, 1, 12, 0),
                      estado=estado, comprador_nombre='Camila',
                      total=Decimal(total),
                      total_envio=Decimal(envio),
                      costo_envio_vendedor=(None if costo_envio_vendedor is None
                                            else Decimal(costo_envio_vendedor)),
                      comision_plataforma=(None if comision is None
                                           else Decimal(comision)),
                      cuenta_cobro_override_id=override,
                      es_regalo=regalo,
                      nota=nota)
        db.session.add(fila)
        db.session.commit()
        return fila

    def producto(self, sku='TARJ-NEG', stock=10):
        fila = Producto(empresa_id=self.empresa_id, sku=sku,
                        nombre='Tarjetero', stock=stock,
                        precio_lista=Decimal('7500.00'))
        db.session.add(fila)
        db.session.commit()
        return fila

    def reporte(self):
        """Lo que /reportes/caja-socio le pasa a la plantilla."""
        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado['context'] = context

        template_rendered.connect(anotar, app)
        try:
            respuesta = self.client.get('/reportes/caja-socio')
        finally:
            template_rendered.disconnect(anotar, app)

        return respuesta, capturado.get('context', {})

    def socio(self, contexto, clave):
        for fila in contexto['socios']:
            if fila['clave'] == clave:
                return fila
        self.fail('no salio el socio %r en el reporte' % clave)


class TestEnvio(BaseCaja):
    """PARTE 2: el envio entra y sale, no es facturacion de nadie."""

    def test_facturado_no_incluye_envio(self):
        """El #100 real: 13696.90 con 7630.00 de envio -> 6066.90.

        Es la plata de la mercaderia. Los otros 7630.00 se cobraron para
        pagarle al correo y no le quedaron a Roman ni un dia.
        """
        self.venta(total='13696.90', envio='7630.00', comision='248.59')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('6066.90'))
        self.assertEqual(contexto['total_general'], Decimal('6066.90'))

    def test_pedido_sin_envio_no_cambia(self):
        """total_envio=0.00 -- casi todas las filas -- da lo mismo que antes.

        Es la mitad de la slice que tiene que NO hacer nada: si la resta
        moviera el caso comun, el problema seria peor que el que vino a
        arreglar.
        """
        self.venta(total='6066.90', envio='0.00', comision='110.11')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('6066.90'))
        self.assertEqual(roman['saldo_real'], Decimal('5956.79'))

    def test_los_dos_pedidos_juntos_como_en_produccion(self):
        """#100 con envio y #101 sin envio: los dos aportan lo mismo."""
        self.venta(total='13696.90', envio='7630.00', comision='248.59')
        self.venta(total='6066.90', envio='0.00', comision='110.11')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('12133.80'))
        self.assertEqual(roman['comision'], Decimal('358.70'))

    def test_el_costo_de_envio_del_vendedor_no_se_resta(self):
        """Que el correo cobre mas de lo cobrado es un GASTO, no menos venta.

        En el #100 real pasa: total_envio 7630.00, costo_envio_vendedor
        8431.00. Esos 801.00 de diferencia los puso el vendedor, y restarlos
        del facturado los mostraria como si se hubiera vendido menos.
        """
        self.venta(total='13696.90', envio='7630.00', comision='248.59',
                   costo_envio_vendedor='8431.00')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('6066.90'))

    def test_el_envio_tampoco_suma_en_el_desglose_por_canal(self):
        """La linea del canal muestra el mismo neto que la fila del socio."""
        self.venta(total='13696.90', envio='7630.00', comision='248.59')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')
        canal = [c for c in roman['canales'] if c['nombre'] == 'Korvo'][0]

        self.assertEqual(canal['total'], Decimal('6066.90'))


class TestRegalo(BaseCaja):
    """PARTE 2: un regalo no es una venta."""

    def test_facturado_excluye_el_sorteo(self):
        """El Sorteo real: 4 unidades a $1. No suma ni esos $4."""
        self.venta(canal_id=self.id_canal_manual, total='97500.00')
        self.venta(canal_id=self.id_canal_manual, total='4.00',
                   regalo=True, nota='Sorteo')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('97500.00'))
        self.assertEqual(contexto['total_general'], Decimal('97500.00'))
        self.assertEqual(roman['pedidos'], 1)

    def test_el_regalo_tampoco_aporta_envio_ni_comision(self):
        """Sale el pedido entero, no solo su total.

        Un regalo despachado por correo tiene envio cobrado y podria tener
        comision; si se colaran por separado, el pedido estaria medio adentro
        y medio afuera de una cuenta que dice no incluirlo.
        """
        self.venta(total='500.00', envio='300.00', comision='45.00',
                   regalo=True)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('0.00'))
        self.assertEqual(roman['comision'], Decimal('0.00'))
        self.assertEqual(roman['sin_comision'], 0)

    def test_el_regalo_se_cuenta_aparte_y_se_ve(self):
        """No suma, pero tampoco desaparece.

        Un pedido que se cae de la cuenta sin decir nada obliga a que alguien
        descubra por que el total no cierra contra el listado de ventas.
        """
        self.venta(canal_id=self.id_canal_manual, total='97500.00')
        self.venta(canal_id=self.id_canal_manual, total='4.00', regalo=True)

        respuesta, contexto = self.reporte()
        texto = respuesta.get_data(as_text=True)

        self.assertEqual(contexto['regalos_totales'], 1)
        self.assertIn('Regalos', texto)
        self.assertIn('no suman', texto)

    def test_sin_regalos_el_contador_es_cero(self):
        self.venta(total='6066.90')

        _, contexto = self.reporte()

        self.assertEqual(contexto['regalos_totales'], 0)

    def test_la_nota_no_decide_nada(self):
        """Una nota que dice "Sorteo" en un pedido sin marcar SUMA.

        Es la razon de ser del campo: si la exclusion dependiera del texto,
        cualquiera que escriba esa palabra en una venta real la borraria de la
        facturacion sin enterarse.
        """
        self.venta(canal_id=self.id_canal_manual, total='7500.00',
                   nota='Vendido en el sorteo de la feria', regalo=False)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('7500.00'))
        self.assertEqual(contexto['regalos_totales'], 0)

    def test_el_regalo_de_un_socio_no_le_toca_el_saldo_al_otro(self):
        self.venta(canal_id=self.id_canal_manual, total='4.00', regalo=True,
                   override=self.id_cuenta_nachi)

        _, contexto = self.reporte()
        nachi = self.socio(contexto, 'nachi')

        self.assertEqual(nachi['total'], Decimal('0.00'))
        self.assertEqual(nachi['saldo_real'], Decimal('0.00'))


class TestStockDelRegalo(BaseCaja):
    """El stock es otra cosa: la mercaderia se fue igual."""

    def test_stock_del_sorteo_sigue_descontado(self):
        """Marcar un pedido como regalo NO devuelve unidades.

        Es la confusion mas facil de esta slice: "no cuenta" suena a "no
        paso". Paso -- la mercaderia salio del deposito --, lo unico que no
        paso es el ingreso.
        """
        producto = self.producto(stock=10)
        pedido = self.venta(canal_id=self.id_canal_manual, total='4.00')
        db.session.add(PedidoItem(pedido_id=pedido.id,
                                  producto_id=producto.id,
                                  sku_externo=producto.sku,
                                  descripcion=producto.nombre,
                                  cantidad=4,
                                  precio_unitario=Decimal('1.00'),
                                  subtotal=Decimal('4.00')))
        # El alta ya descontó el stock cuando se cargó la venta.
        producto.stock = 6
        db.session.commit()

        respuesta = self.client.post(
            '/pedidos/regalos',
            data={'pedido_id': str(pedido.id), 'es_regalo': str(pedido.id)},
            follow_redirects=True)
        self.assertEqual(respuesta.status_code, 200)

        db.session.refresh(producto)
        db.session.refresh(pedido)

        self.assertTrue(pedido.es_regalo)
        self.assertEqual(producto.stock, 6)
        # Y los items del pedido siguen ahi: el regalo no se deshace.
        self.assertEqual(
            PedidoItem.query.filter_by(pedido_id=pedido.id).count(), 1)

    def test_destildar_tampoco_toca_el_stock(self):
        producto = self.producto(stock=6)
        pedido = self.venta(canal_id=self.id_canal_manual, total='4.00',
                            regalo=True)

        self.client.post('/pedidos/regalos',
                         data={'pedido_id': str(pedido.id)},
                         follow_redirects=True)

        db.session.refresh(producto)
        db.session.refresh(pedido)

        self.assertFalse(pedido.es_regalo)
        self.assertEqual(producto.stock, 6)


class TestMarcarDesdeLaPantalla(BaseCaja):
    """La marca se puede poner al cargar y corregir despues."""

    def test_el_alta_manual_puede_marcar_el_regalo(self):
        producto = self.producto(stock=10)

        respuesta = self.client.post('/pedidos/manual/nuevo', data={
            'fecha': '2026-09-01',
            'medio': 'efectivo',
            'nota': 'Sorteo IG',
            'es_regalo': '1',
            'sku': producto.sku,
            'cantidad': '4',
            'precio_unitario': '1.00',
        }, follow_redirects=True)
        self.assertEqual(respuesta.status_code, 200)

        pedido = Pedido.query.order_by(Pedido.id.desc()).first()
        self.assertTrue(pedido.es_regalo)
        self.assertEqual(pedido.total, Decimal('4.00'))
        # Y el stock SI se descontó: la mercadería salió igual.
        db.session.refresh(producto)
        self.assertEqual(producto.stock, 6)

        _, contexto = self.reporte()
        self.assertEqual(contexto['total_general'], Decimal('0.00'))
        self.assertEqual(contexto['regalos_totales'], 1)

    def test_el_alta_sin_tildar_sigue_siendo_una_venta(self):
        """El default no cambio: sin tocar el checkbox, es una venta."""
        producto = self.producto(stock=10)

        self.client.post('/pedidos/manual/nuevo', data={
            'fecha': '2026-09-01',
            'medio': 'efectivo',
            'sku': producto.sku,
            'cantidad': '1',
            'precio_unitario': '7500.00',
        }, follow_redirects=True)

        pedido = Pedido.query.order_by(Pedido.id.desc()).first()
        self.assertFalse(pedido.es_regalo)

        _, contexto = self.reporte()
        self.assertEqual(contexto['total_general'], Decimal('7500.00'))

    def test_no_se_puede_marcar_un_pedido_de_otra_empresa(self):
        """El filtro por empresa es lo unico que lo impide, como en las otras
        dos tandas del listado."""
        otra = Empresa(nombre='Otra empresa')
        db.session.add(otra)
        db.session.flush()
        canal_ajeno = CanalVenta(empresa_id=otra.id, tipo='manual',
                                 nombre='Manual ajeno', activo=True)
        db.session.add(canal_ajeno)
        db.session.flush()
        ajeno = Pedido(empresa_id=otra.id, canal_id=canal_ajeno.id,
                       fecha_pedido=datetime(2026, 9, 1, 12, 0),
                       estado='open', total=Decimal('999.00'),
                       total_envio=Decimal('0.00'))
        db.session.add(ajeno)
        db.session.commit()
        id_ajeno = ajeno.id

        self.client.post('/pedidos/regalos',
                         data={'pedido_id': str(id_ajeno),
                               'es_regalo': str(id_ajeno)},
                         follow_redirects=True)

        db.session.refresh(ajeno)
        self.assertFalse(ajeno.es_regalo)

    def test_el_listado_muestra_la_marca(self):
        self.venta(canal_id=self.id_canal_manual, total='4.00', regalo=True)

        respuesta = self.client.get('/pedidos/listar')
        texto = respuesta.get_data(as_text=True)

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('form-regalos', texto)
        self.assertIn('no facturado', texto)


class TestNoRompeLoDeAntes(BaseCaja):
    """Las slices anteriores siguen valiendo sobre la formula nueva."""

    def test_la_comision_sigue_restando_sobre_el_neto(self):
        """S4 encima de S5: 13696.90 - 7630.00 envio - 248.59 comision."""
        self.venta(total='13696.90', envio='7630.00', comision='248.59')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('6066.90'))
        self.assertEqual(roman['comision'], Decimal('248.59'))
        self.assertEqual(roman['saldo_real'], Decimal('5818.31'))

    def test_el_gasto_sigue_restando(self):
        self.venta(total='13696.90', envio='7630.00', comision='248.59')
        db.session.add(Gasto(empresa_id=self.empresa_id,
                             usuario_id=self.usuario_id,
                             categoria_id=self.id_categoria,
                             descripcion='Publicidad',
                             monto=Decimal('3000.00'),
                             fecha=date(2026, 9, 1),
                             origen_fondo=ORIGEN_FACTURACION,
                             cuenta_pago_id=self.id_cuenta_roman))
        db.session.commit()

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['saldo_real'], Decimal('2818.31'))

    def test_la_comision_sin_cargar_se_sigue_marcando(self):
        self.venta(total='6066.90', comision=None)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['sin_comision'], 1)

    def test_el_regalo_sin_comision_no_cuenta_como_faltante(self):
        """No esta en la suma: tampoco puede faltarle nada a la suma."""
        self.venta(canal_id=self.id_canal_manual, total='4.00', regalo=True,
                   comision=None)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['sin_comision'], 0)
        self.assertEqual(contexto['sin_comision_total'], 0)

    def test_el_pedido_cancelado_sigue_afuera(self):
        self.venta(total='6066.90')
        self.venta(total='9999.00', estado='cancelled')

        _, contexto = self.reporte()

        self.assertEqual(contexto['total_general'], Decimal('6066.90'))


class TestAuth(BaseCaja):
    """Las rutas de la slice siguen detras de @login_required."""

    def test_guardar_regalos_pide_login(self):
        respuesta = request_anonimo(self.ctx, 'post', '/pedidos/regalos')
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers.get('Location', ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
