# -*- coding: utf-8 -*-
"""Tests de FASE-CAJA-SOCIO-S1 (cuanto factura cada socio).

    python -m unittest discover -s tests -v

La pantalla contesta una sola pregunta: cuanta plata le entro a cada socio
segun por que canal se vendio. NO es conciliacion contra Mercado Pago -- no
lee un pago, ni una liquidacion, ni un movimiento de cuenta -- y esta suite
tampoco los toca.

Lo que se prueba:

    el socio sale del CAMPO          -> renombrar la cuenta de cobro no mueve
    y no del nombre                     un peso de un socio al otro
    la suma da                       -> el total de Roman es la suma exacta de
                                        los pedido.total de sus canales
    el desglose no se mezcla         -> Tiendanube y presencial se ven por
                                        separado abajo del socio
    Nachi en cero se ve              -> un socio sin ventas y su canal apagado
                                        siguen en pantalla
    los cancelados no suman          -> mismo filtro que el resto de los
                                        reportes
    la plata sin dueño se ve         -> una cuenta sin socio no se reparte
                                        entre los conocidos ni desaparece

El primero es el que le da sentido a la slice entera: antes de que existiera
`cuenta_cobro.socio`, la unica forma de saber de quien era una cuenta era leer
su `nombre`, que es texto libre. Ese test falla contra la version vieja.

Los montos de "la suma da" son los dos pedidos reales de la base (Camila
$13696.90 y Matias $6066.90, los dos por Tiendanube): asi el numero de la
pantalla se puede cruzar a mano contra lo que hay cargado.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.ayuda_auth import request_anonimo  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    SOCIOS,
    CanalVenta,
    CuentaCobro,
    Empresa,
    Pedido,
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
    """El reparto real: dos cuentas, tres canales, dos ventas por Tiendanube.

    Es una copia de la base de produccion en chico -- canal 1 y 14 cobrando en
    la cuenta de Roman, canal 2 apagado cobrando en la de Nachi -- porque la
    pregunta que contesta el reporte es exactamente esa configuracion.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test CAJA-SOCIO')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='cajasocio@test.local',
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
        db.session.flush()

        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True,
                                   id_tienda_externo='9999',
                                   cuenta_cobro_id=self.cuenta_roman.id)
        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial',
                                       activo=True,
                                       cuenta_cobro_id=self.cuenta_roman.id)
        # Apagado, igual que en produccion: la integracion de ventas de Mercado
        # Libre no existe todavia.
        self.canal_meli = CanalVenta(empresa_id=self.empresa.id,
                                     tipo='mercadolibre', nombre='Mercado Libre',
                                     activo=False,
                                     cuenta_cobro_id=self.cuenta_nachi.id)
        db.session.add_all([self.canal_tn, self.canal_manual, self.canal_meli])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.id_cuenta_roman = self.cuenta_roman.id
        self.id_cuenta_nachi = self.cuenta_nachi.id
        self.id_canal_tn = self.canal_tn.id
        self.id_canal_manual = self.canal_manual.id
        self.id_canal_meli = self.canal_meli.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def pedido(self, canal_id, total, estado='open', comprador='Cliente'):
        """Un pedido con lo minimo que el reporte mira: canal, estado y total.

        El resto de los montos se dejan afuera a proposito: si algun dia el
        reporte empezara a sumar por total_bruto o por ingreso_neto, estos
        tests se caerian, que es justo lo que se quiere.
        """
        fila = Pedido(empresa_id=self.empresa_id, canal_id=canal_id,
                      fecha_pedido=datetime(2026, 9, 1, 12, 0),
                      estado=estado, comprador_nombre=comprador,
                      total=Decimal(total))
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
        """La fila de un socio, por su clave. Falla si no esta en la pantalla."""
        for fila in contexto['socios']:
            if fila['clave'] == clave:
                return fila
        self.fail('el socio %r no aparece en el reporte' % (clave,))


class TestElSocioSaleDelCampo(BaseCaja):
    """El nombre de la cuenta es una etiqueta, no un dato de negocio."""

    def test_socio_se_lee_del_campo_no_del_nombre(self):
        """Renombrar la cuenta de cobro no mueve la facturacion de socio.

        Es la razon de ser de la slice. Con el socio deducido del string, este
        rename mandaba los $13696.90 de Roman a "no se sabe" -- o peor, a otro
        socio -- sin que nada avisara.
        """
        self.pedido(self.id_canal_tn, '13696.90')

        _, antes = self.reporte()
        self.assertEqual(self.socio(antes, 'roman')['total'], Decimal('13696.90'))

        cuenta = db.session.get(CuentaCobro, self.id_cuenta_roman)
        cuenta.nombre = 'Cuenta principal (renombrada)'
        db.session.commit()

        _, despues = self.reporte()
        self.assertEqual(self.socio(despues, 'roman')['total'], Decimal('13696.90'),
                         'el socio no puede depender de como se llame la cuenta')
        self.assertEqual(self.socio(despues, 'nachi')['total'], Decimal('0.00'))

    def test_el_nombre_nuevo_igual_se_muestra(self):
        """Que no decida nada no significa que se esconda.

        El nombre sigue siendo lo que Roman ve al lado de cada canal para saber
        en que cuenta cae esa plata; lo unico que cambio es que ya no manda.
        """
        self.pedido(self.id_canal_tn, '13696.90')
        cuenta = db.session.get(CuentaCobro, self.id_cuenta_roman)
        cuenta.nombre = 'Cuenta principal (renombrada)'
        db.session.commit()

        _, contexto = self.reporte()
        cuentas = {canal['cuenta'] for canal in self.socio(contexto, 'roman')['canales']}
        self.assertEqual(cuentas, {'Cuenta principal (renombrada)'})

    def test_el_vocabulario_no_admite_cualquier_cosa(self):
        """Un typo revienta al cargarlo, no seis meses despues al cuadrar.

        Las cuentas de cobro no se cargan por ninguna pantalla -- se siembran
        desde migraciones -- asi que el modelo es el punto de validacion que si
        existe. La otra mitad es el CHECK de la base, que cubre el SQL crudo.
        """
        with self.assertRaises(ValueError):
            CuentaCobro(empresa_id=self.empresa_id, nombre='Cuenta rara',
                        tipo='mercadopago', socio='Roman')  # mayuscula

        with self.assertRaises(ValueError):
            CuentaCobro(empresa_id=self.empresa_id, nombre='Cuenta rara',
                        tipo='mercadopago', socio='pepe')

    def test_las_claves_del_vocabulario_son_las_esperadas(self):
        self.assertEqual(list(SOCIOS), ['roman', 'nachi'])


class TestLaSuma(BaseCaja):
    """El numero grande de cada socio."""

    def test_suma_correcta_por_socio(self):
        """Los dos pedidos reales de Tiendanube, sumados a mano.

            Camila  13696.90
            Matias   6066.90
                    --------
                    19763.80
        """
        self.pedido(self.id_canal_tn, '13696.90', comprador='Camila Valaco')
        self.pedido(self.id_canal_tn, '6066.90', comprador='Matias Oehrli')

        respuesta, contexto = self.reporte()
        self.assertEqual(respuesta.status_code, 200)

        roman = self.socio(contexto, 'roman')
        self.assertEqual(roman['total'], Decimal('19763.80'))
        self.assertEqual(roman['pedidos'], 2)
        self.assertEqual(contexto['total_general'], Decimal('19763.80'))

    def test_suma_el_total_del_pedido_sin_el_envio(self):
        """Lo que pago el comprador POR LA MERCADERIA, que es lo que queda.

        Este pedido tiene los otros montos cargados y distintos a proposito: si
        el reporte sumara total_bruto (7490.00) o el ingreso neto del reporte
        de margen, el numero no daria.

        FASE-CAJA-SOCIO-S5 le cambio el numero esperado a este test, y es el
        unico de S1 que se movio. Hasta entonces se afirmaba `total` pelado
        (13696.90) con el argumento de que el envio tambien es plata que entra
        a la cuenta. Entra, pero no queda: se cobra y se le paga al correo, asi
        que contarlo como facturacion de Roman le inflaba el numero con plata
        de paso. El resto de S1 -- de quien es cada canal, que los cancelados
        no suman, como se desglosa -- no cambio: esto es la definicion de UN
        numero, no el reparto.
        """
        fila = self.pedido(self.id_canal_tn, '13696.90')
        fila.total_bruto = Decimal('7490.00')
        fila.total_descuentos = Decimal('1423.10')
        fila.total_envio = Decimal('7630.00')
        db.session.commit()

        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'roman')['total'],
                         Decimal('6066.90'))

    def test_cancelados_no_suman(self):
        """Mismo filtro que los reportes anteriores: lo cancelado no entro."""
        self.pedido(self.id_canal_tn, '13696.90')
        self.pedido(self.id_canal_tn, '99999.00', estado='cancelled')
        self.pedido(self.id_canal_manual, '50000.00', estado='cancelado')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')
        self.assertEqual(roman['total'], Decimal('13696.90'))
        self.assertEqual(roman['pedidos'], 1,
                         'el cancelado tampoco cuenta como pedido')
        self.assertEqual(contexto['total_general'], Decimal('13696.90'))


class TestElDesglose(BaseCaja):
    """De donde sale cada parte del total del socio."""

    def test_desglose_por_canal_dentro_del_socio(self):
        """Tiendanube y presencial se ven separados, no en un solo numero."""
        self.pedido(self.id_canal_tn, '13696.90')
        self.pedido(self.id_canal_manual, '5000.00')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        por_canal = {canal['nombre']: canal['total'] for canal in roman['canales']}
        self.assertEqual(por_canal, {
            'Korvo': Decimal('13696.90'),
            'Venta manual / presencial': Decimal('5000.00'),
        })
        self.assertEqual(roman['total'], Decimal('18696.90'),
                         'el total del socio es la suma de su desglose')

    def test_el_canal_sin_ventas_del_socio_sale_en_cero(self):
        """No se cae de la lista por no haber vendido todavia.

        Si el barrido empezara por los pedidos en vez de por los canales, el
        presencial desapareceria de la pantalla el mes que no se venda nada de
        mostrador, y no habria forma de distinguirlo de un canal que no existe.
        """
        self.pedido(self.id_canal_tn, '13696.90')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')
        por_canal = {canal['nombre']: canal['total'] for canal in roman['canales']}
        self.assertEqual(por_canal.get('Venta manual / presencial'), Decimal('0.00'))


class TestNachiEnCero(BaseCaja):
    """Un socio sin ventas sigue siendo un socio."""

    def test_meli_en_cero_no_se_oculta(self):
        """Nachi aparece con $0 y su canal de Mercado Libre a la vista."""
        self.pedido(self.id_canal_tn, '13696.90')

        respuesta, contexto = self.reporte()
        nachi = self.socio(contexto, 'nachi')

        self.assertEqual(nachi['total'], Decimal('0.00'))
        self.assertEqual(nachi['pedidos'], 0)
        self.assertEqual([canal['nombre'] for canal in nachi['canales']],
                         ['Mercado Libre'])
        self.assertFalse(nachi['canales'][0]['activo'],
                         'el canal sigue apagado: el cero es por eso')

        cuerpo = respuesta.get_data(as_text=True)
        self.assertIn('Nachi', cuerpo)
        self.assertIn('Mercado Libre', cuerpo)

    def test_los_dos_socios_salen_siempre_y_en_orden(self):
        """Sin una sola venta cargada la pantalla ya muestra el reparto."""
        _, contexto = self.reporte()
        self.assertEqual([fila['clave'] for fila in contexto['socios']],
                         ['roman', 'nachi'])
        self.assertEqual(contexto['total_general'], Decimal('0.00'))


class TestLaPlataSinDueno(BaseCaja):
    """Lo que no se le pudo atribuir a nadie se ve; no se reparte."""

    def test_canal_con_cuenta_sin_socio_no_se_le_suma_a_nadie(self):
        cuenta_huerfana = CuentaCobro(empresa_id=self.empresa_id,
                                      nombre='Cuenta nueva sin dueño',
                                      tipo='mercadopago')
        db.session.add(cuenta_huerfana)
        db.session.flush()
        canal = CanalVenta(empresa_id=self.empresa_id, tipo='otro',
                           nombre='Canal nuevo', activo=True,
                           cuenta_cobro_id=cuenta_huerfana.id)
        db.session.add(canal)
        db.session.commit()

        self.pedido(canal.id, '1000.00')
        self.pedido(self.id_canal_tn, '13696.90')

        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'roman')['total'], Decimal('13696.90'))
        self.assertEqual(self.socio(contexto, 'nachi')['total'], Decimal('0.00'))

        sin_dueno = self.socio(contexto, None)
        self.assertEqual(sin_dueno['total'], Decimal('1000.00'))
        self.assertEqual(contexto['total_general'], Decimal('14696.90'),
                         'el total general no pierde la plata sin dueño')

    def test_canal_sin_cuenta_de_cobro_tambien_cae_ahi(self):
        canal = CanalVenta(empresa_id=self.empresa_id, tipo='otro',
                           nombre='Canal sin cuenta', activo=True)
        db.session.add(canal)
        db.session.commit()
        self.pedido(canal.id, '750.00')

        _, contexto = self.reporte()
        sin_dueno = self.socio(contexto, None)
        self.assertEqual(sin_dueno['total'], Decimal('750.00'))
        self.assertIsNone(sin_dueno['canales'][0]['cuenta'])

    def test_sin_huerfanos_la_fila_no_existe(self):
        """No se muestra un renglon de "sin socio" que siempre este en cero."""
        _, contexto = self.reporte()
        self.assertNotIn(None, [fila['clave'] for fila in contexto['socios']])


class TestAislamientoYAuth(BaseCaja):
    """Lo de siempre: la pantalla pide sesion y no cruza empresas."""

    def test_pide_sesion(self):
        respuesta = request_anonimo(self.ctx, 'get', '/reportes/caja-socio')
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers.get('Location', ''))

    def test_no_se_ve_la_facturacion_de_otra_empresa(self):
        otra = Empresa(nombre='Otra Empresa')
        db.session.add(otra)
        db.session.flush()
        cuenta_ajena = CuentaCobro(empresa_id=otra.id, nombre='MP Ajena',
                                   tipo='mercadopago', socio='roman')
        db.session.add(cuenta_ajena)
        db.session.flush()
        canal_ajeno = CanalVenta(empresa_id=otra.id, tipo='tiendanube',
                                 nombre='Tienda ajena', activo=True,
                                 cuenta_cobro_id=cuenta_ajena.id)
        db.session.add(canal_ajeno)
        db.session.commit()

        fila = Pedido(empresa_id=otra.id, canal_id=canal_ajeno.id,
                      fecha_pedido=datetime(2026, 9, 1, 12, 0), estado='open',
                      total=Decimal('999999.00'))
        db.session.add(fila)
        db.session.commit()

        self.pedido(self.id_canal_tn, '13696.90')

        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'roman')['total'], Decimal('13696.90'))
        self.assertEqual(contexto['total_general'], Decimal('13696.90'))
        nombres = {canal['nombre']
                   for fila in contexto['socios']
                   for canal in fila['canales']}
        self.assertNotIn('Tienda ajena', nombres)


class TestNoTocaMercadoPago(BaseCaja):
    """La slice es de facturacion, no de conciliacion.

    El reporte tiene que dar el mismo numero con la base de Mercado Pago
    completamente vacia: si algun dia empezara a leer pagos o liquidaciones,
    dejaria de contestar "cuanto se facturo" para contestar "cuanto entro", que
    es otra pregunta y otra pantalla.
    """

    def test_el_reporte_no_necesita_un_solo_pago(self):
        from models import Liquidacion, MovimientoCuenta, Pago

        self.pedido(self.id_canal_tn, '13696.90')
        self.pedido(self.id_canal_manual, '5000.00')

        self.assertEqual(Pago.query.count(), 0)
        self.assertEqual(Liquidacion.query.count(), 0)
        self.assertEqual(MovimientoCuenta.query.count(), 0)

        respuesta, contexto = self.reporte()
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self.socio(contexto, 'roman')['total'], Decimal('18696.90'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
