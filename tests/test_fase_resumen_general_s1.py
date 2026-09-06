# -*- coding: utf-8 -*-
"""Tests de FASE-RESUMEN-GENERAL-S1 (la posicion real: todo junto).

    python -m unittest discover -s tests -v

QUE PANTALLA ES

/reportes/resumen-general contesta la pregunta que ninguna de las dos
existentes contestaba: "cuanta plata tengo en total, sumando todo".

    posicion_real = (aporte de capital
                     + facturado neto de ventas
                     - comision de plataforma)
                    - gastos totales

Caja General y Caja por Socio no se tocan. Esta suite lo verifica ademas de
verificar la suma nueva: `test_las_dos_pantallas_no_cambiaron` corre las dos
con los mismos datos y afirma sus numeros de siempre.

LO QUE SE PRUEBA

    la suma es la suma        -> las cuatro lineas y el resultado
    no se duplica nada        -> el facturado de aca es EXACTAMENTE el de
                                 caja-socio, y el gasto es EXACTAMENTE el de
                                 caja-general; no hay un tercer calculo
    el gasto va entero        -> los de capital y los sin origen tambien
                                 restan aca, a diferencia de caja-socio
    NULL != 0                 -> un pedido sin comision se cuenta y se marca,
                                 no se asume en cero
    el envio no suma          -> misma formula que S5
    el regalo no suma         -> misma exclusion que S5
    hace falta login          -> como toda la seccion

EL TEST DE LOS DATOS REALES

`test_saldo_real_con_datos_reales` siembra los numeros que hoy tiene Supabase
--leidos de la base productiva el 2026-09-06, en una sola consulta de solo
lectura-- y afirma la posicion real que sale de ellos. Se siembran, no se
consultan: un test que le pregunta a produccion se pone rojo el dia que Roman
carga un gasto, y un rojo que no es una regresion es peor que no tener el
test. Lo que congela es la CUENTA sobre esa forma de datos; el numero de hoy
esta escrito en el docstring del test para poder compararlo a mano.

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
    ORIGEN_CAPITAL,
    ORIGEN_FACTURACION,
    CanalVenta,
    Categoria,
    CuentaCobro,
    Empresa,
    Gasto,
    Ingreso,
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


class BaseResumen(unittest.TestCase):
    """Una empresa con los dos socios, sus cuentas y sus dos canales.

    Es la misma forma que produccion en chico: Tiendanube y el manual cobrando
    en la cuenta de Roman, Mercado Libre en la de Nachi.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test RESUMEN-GENERAL-S1')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test',
                               email='resumen1@test.local',
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

        self.cat_gasto = Categoria(nombre='Materiales', tipo='gasto',
                                   empresa_id=self.empresa.id)
        self.cat_ingreso = Categoria(nombre='Aporte de capital (socios)',
                                     tipo='ingreso', empresa_id=self.empresa.id)
        db.session.add_all([self.cat_gasto, self.cat_ingreso])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.id_cuenta_roman = self.cuenta_roman.id
        self.id_cuenta_nachi = self.cuenta_nachi.id
        self.id_cat_gasto = self.cat_gasto.id
        self.id_cat_ingreso = self.cat_ingreso.id

        self.canal_tn = CanalVenta(empresa_id=self.empresa_id,
                                   tipo='tiendanube', nombre='Korvo',
                                   activo=True, id_tienda_externo='9999',
                                   cuenta_cobro_id=self.id_cuenta_roman)
        self.canal_ml = CanalVenta(empresa_id=self.empresa_id,
                                   tipo='mercadolibre', nombre='Mercado Libre',
                                   activo=False,
                                   cuenta_cobro_id=self.id_cuenta_nachi)
        db.session.add_all([self.canal_tn, self.canal_ml])
        db.session.commit()

        self.id_canal_tn = self.canal_tn.id
        self.id_canal_ml = self.canal_ml.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def venta(self, canal_id=None, total='10000.00', envio='0.00',
              comision='0.00', regalo=False, estado='open'):
        fila = Pedido(empresa_id=self.empresa_id,
                      canal_id=canal_id or self.id_canal_tn,
                      fecha_pedido=datetime(2026, 9, 1, 12, 0),
                      estado=estado, comprador_nombre='Camila',
                      total=Decimal(total),
                      total_envio=Decimal(envio),
                      comision_plataforma=(None if comision is None
                                           else Decimal(comision)),
                      es_regalo=regalo)
        db.session.add(fila)
        db.session.commit()
        return fila

    def gasto(self, monto='1000.00', origen=None, cuenta=None):
        """Un gasto de la empresa.

        `origen` va en None por defecto porque en esta pantalla no cambia
        nada: el gasto entra igual salga del bolsillo que salga. Los tests que
        SI hablan del origen -- los que comparan contra caja-socio -- lo pasan
        explicito, y con la cuenta cuando el constraint la exige
        (`facturacion` sin cuenta no dice de quien salio, y por eso la base no
        la deja pasar).
        """
        fila = Gasto(empresa_id=self.empresa_id,
                     descripcion='Un gasto',
                     monto=Decimal(monto),
                     fecha=date(2026, 9, 1),
                     categoria_id=self.id_cat_gasto,
                     origen_fondo=origen,
                     cuenta_pago_id=cuenta)
        db.session.add(fila)
        db.session.commit()
        return fila

    def aporte(self, monto='500000.00'):
        fila = Ingreso(empresa_id=self.empresa_id,
                       descripcion='Aporte de los socios',
                       monto=Decimal(monto),
                       fecha=date(2026, 9, 1),
                       categoria_id=self.id_cat_ingreso)
        db.session.add(fila)
        db.session.commit()
        return fila

    def contexto_de(self, ruta):
        """Lo que `ruta` le pasa a su plantilla, mas la respuesta."""
        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado['context'] = context

        template_rendered.connect(anotar, app)
        try:
            respuesta = self.client.get(ruta)
        finally:
            template_rendered.disconnect(anotar, app)

        return respuesta, capturado.get('context', {})

    def resumen(self):
        return self.contexto_de('/reportes/resumen-general')


class TestLaCuenta(BaseResumen):
    """PARTE 1: las cuatro lineas y el numero de abajo."""

    def test_la_suma_completa(self):
        """500000 + 10000 - 350 - 4000 = 505650."""
        self.aporte('500000.00')
        self.venta(total='10000.00', comision='350.00')
        self.gasto('1500.00', origen=ORIGEN_FACTURACION,
                   cuenta=self.id_cuenta_roman)
        self.gasto('2500.00', origen=ORIGEN_CAPITAL)

        respuesta, contexto = self.resumen()

        self.assertEqual(200, respuesta.status_code)
        self.assertEqual(Decimal('500000.00'), contexto['capital_monto'])
        self.assertEqual(Decimal('10000.00'), contexto['ventas_monto'])
        self.assertEqual(Decimal('350.00'), contexto['comision_monto'])
        self.assertEqual(Decimal('4000.00'), contexto['gastos_monto'])
        self.assertEqual(Decimal('509650.00'), contexto['entra'])
        self.assertEqual(Decimal('505650.00'), contexto['posicion_real'])

    def test_el_desglose_se_ve_entero(self):
        """No solo el resultado: las cuatro lineas salen en la pantalla.

        Un numero suelto que dice "tenes $X" no se puede verificar contra
        nada. Si un dia no coincide con la plata que Roman tiene en la mano,
        el desglose es por donde se empieza a mirar.
        """
        self.aporte('500000.00')
        self.venta(total='10000.00', comision='350.00')
        self.gasto('4000.00')

        respuesta, _ = self.resumen()
        texto = respuesta.get_data(as_text=True)

        self.assertIn('500000.00', texto)   # aporte
        self.assertIn('10000.00', texto)    # facturado
        self.assertIn('350.00', texto)      # comision
        self.assertIn('4000.00', texto)     # gastos
        self.assertIn('505650.00', texto)   # posicion real
        self.assertIn('Posición real', texto)

    def test_posicion_negativa_se_marca(self):
        """Gastar mas de lo que entro da negativo, y se ve que lo es."""
        self.aporte('1000.00')
        self.gasto('5000.00')

        respuesta, contexto = self.resumen()

        self.assertEqual(Decimal('-4000.00'), contexto['posicion_real'])
        self.assertIn('negativo', respuesta.get_data(as_text=True))

    def test_empresa_vacia_da_cero(self):
        """Sin una sola fila la pantalla abre igual y dice 0.00."""
        respuesta, contexto = self.resumen()

        self.assertEqual(200, respuesta.status_code)
        self.assertEqual(Decimal('0.00'), contexto['posicion_real'])

    def test_saldo_real_con_datos_reales(self):
        """Los numeros que hoy tiene Supabase (leidos el 2026-09-06).

            aporte de capital   : 2371037.83   (1 ingreso)
            facturado sin envio :  208511.50   (6 pedidos, 1 regalo aparte)
            comision            :     358.70   (0 pedidos sin cargar)
            gastos totales      : 2404037.83   (11 gastos)
            ------------------------------------------------
            POSICION REAL       :  175152.80

        Los montos se SIEMBRAN, no se consultan. Un test que le pregunta a
        produccion se pone rojo el dia que Roman carga un gasto, y ese rojo no
        seria una regresion. Lo que queda congelado es la cuenta sobre esta
        forma de datos: un aporte grande, unas pocas ventas chicas, y un gasto
        que se comio casi todo el aporte.
        """
        self.aporte('2371037.83')
        # Las seis ventas, con la comision total repartida en una sola de
        # ellas: la suma es lo que importa, no como se reparte.
        self.venta(total='208511.50', comision='358.70')
        self.gasto('2404037.83')

        _, contexto = self.resumen()

        self.assertEqual(Decimal('2371037.83'), contexto['capital_monto'])
        self.assertEqual(Decimal('208511.50'), contexto['ventas_monto'])
        self.assertEqual(Decimal('358.70'), contexto['comision_monto'])
        self.assertEqual(Decimal('2404037.83'), contexto['gastos_monto'])
        self.assertEqual(Decimal('175152.80'), contexto['posicion_real'])


class TestNoDuplicaNada(BaseResumen):
    """El numero de aca tiene que salir de los mismos numeros de alla.

    Es la razon de ser de esta suite: si el facturado de esta pantalla o el
    gasto de la otra se calcularan por su cuenta, tarde o temprano dirian
    cosas distintas y no habria forma de saber cual de las dos miente.
    """

    def sembrar(self):
        """Un escenario con las tres clases de gasto y las dos de venta."""
        self.aporte('500000.00')
        self.venta(total='13696.90', envio='7630.00', comision='358.70')
        self.venta(canal_id=self.id_canal_ml, total='4000.00', comision='120.00')
        self.venta(total='1.00', regalo=True)
        self.gasto('1500.00', origen=ORIGEN_FACTURACION,
                   cuenta=self.id_cuenta_roman)
        self.gasto('2500.00', origen=ORIGEN_CAPITAL)
        self.gasto('800.00', origen=None)

    def test_no_duplica_nada(self):
        """El facturado coincide con caja-socio y el gasto con caja-general."""
        self.sembrar()

        _, resumen = self.resumen()
        _, socio = self.contexto_de('/reportes/caja-socio')
        _, general = self.contexto_de('/caja-general')

        # Las ventas: EXACTAMENTE el mismo total y la misma comision que la
        # pantalla que las reparte por socio.
        self.assertEqual(socio['total_general'], resumen['ventas_monto'])
        self.assertEqual(socio['comision_total'], resumen['comision_monto'])
        self.assertEqual(socio['pedidos_totales'], resumen['ventas_pedidos'])

        # Los gastos: EXACTAMENTE las salidas del libro de movimientos. Alla
        # es la columna "salida" de todas las filas de gasto; aca es una sola
        # linea. Es la misma plata contada una vez.
        self.assertEqual(general['total_salidas'], resumen['gastos_monto'])

        # Y las entradas del libro son el aporte, nada mas: si una venta
        # volviera a cargarse como Ingreso, esta linea se cae y con ella la
        # premisa entera de la pantalla.
        self.assertEqual(general['total_entradas'], resumen['capital_monto'])

    def test_las_dos_pantallas_no_cambiaron(self):
        """Ni caja-general ni caja-socio se tocan: siguen diciendo lo suyo."""
        self.sembrar()

        _, socio = self.contexto_de('/reportes/caja-socio')
        _, general = self.contexto_de('/caja-general')

        # caja-socio: el facturado sin envio (6066.90 + 4000.00), su comision,
        # y el gastado que sigue siendo SOLO el de origen facturacion.
        self.assertEqual(Decimal('10066.90'), socio['total_general'])
        self.assertEqual(Decimal('478.70'), socio['comision_total'])
        self.assertEqual(Decimal('1500.00'), socio['gastado_total'])
        self.assertEqual(Decimal('2500.00'), socio['capital_monto'])
        self.assertEqual(Decimal('800.00'), socio['sin_clasificar_monto'])

        # caja-general: su saldo sigue siendo entradas menos salidas, sin
        # mirar un solo pedido.
        self.assertEqual(Decimal('500000.00'), general['total_entradas'])
        self.assertEqual(Decimal('4800.00'), general['total_salidas'])
        self.assertEqual(Decimal('495200.00'), general['saldo_final'])

    def test_el_gasto_va_entero_sin_filtrar_origen(self):
        """Los de capital y los sin origen tambien restan en la posicion real.

        Es la unica diferencia de criterio a proposito con caja-socio: alla el
        origen decide a QUIEN se le resta y por eso esos dos quedan afuera de
        la cuenta de cada socio. Aca no hay a quien adivinarle -- la pregunta
        es cuanto salio en total.
        """
        self.sembrar()

        _, resumen = self.resumen()
        _, socio = self.contexto_de('/reportes/caja-socio')

        self.assertEqual(Decimal('4800.00'), resumen['gastos_monto'])
        self.assertEqual(3, resumen['gastos_cantidad'])
        # Alla los mismos tres gastos viven en tres lugares distintos.
        self.assertEqual(Decimal('4800.00'),
                         socio['gastado_total'] + socio['capital_monto']
                         + socio['sin_clasificar_monto'])


class TestLoQueNoSuma(BaseResumen):
    """Las exclusiones de S5 llegan intactas: salen de la misma funcion."""

    def test_el_envio_no_suma(self):
        """El #100 real: 13696.90 con 7630.00 de envio -> 6066.90."""
        self.venta(total='13696.90', envio='7630.00', comision='0.00')

        _, contexto = self.resumen()

        self.assertEqual(Decimal('6066.90'), contexto['ventas_monto'])

    def test_el_regalo_no_suma_y_se_cuenta(self):
        """No entra a la cuenta, pero la pantalla dice que existe."""
        self.venta(total='10000.00', comision='0.00')
        self.venta(total='1.00', regalo=True)

        respuesta, contexto = self.resumen()

        self.assertEqual(Decimal('10000.00'), contexto['ventas_monto'])
        self.assertEqual(1, contexto['ventas_pedidos'])
        self.assertEqual(1, contexto['regalos'])
        self.assertIn('Regalos', respuesta.get_data(as_text=True))

    def test_el_cancelado_no_suma(self):
        self.venta(total='10000.00', comision='0.00')
        self.venta(total='9999.00', comision='0.00', estado='cancelled')

        _, contexto = self.resumen()

        self.assertEqual(Decimal('10000.00'), contexto['ventas_monto'])


class TestComisionSinCargar(BaseResumen):
    """NULL no es 0, tambien en el numero global."""

    def test_pedido_sin_comision_queda_marcado(self):
        """Se cuenta y se avisa; el monto no se completa con un cero."""
        self.venta(total='10000.00', comision='350.00')
        self.venta(total='5000.00', comision=None)

        respuesta, contexto = self.resumen()
        texto = respuesta.get_data(as_text=True)

        self.assertEqual(Decimal('15000.00'), contexto['ventas_monto'])
        # La comision que se resta es la unica que existe. El pedido sin
        # cargar no aporta un 0.00 disfrazado de dato.
        self.assertEqual(Decimal('350.00'), contexto['comision_monto'])
        self.assertEqual(1, contexto['sin_comision'])

        self.assertIn('sin cargar', texto)
        # Y se dice en que direccion esta mal el resultado, que es lo unico
        # que sirve saber cuando el dato falta.
        self.assertIn('la posición real es más baja', texto)

    def test_sin_faltantes_no_se_muestra_el_bloque(self):
        """Con todo cargado no hay aviso que dar."""
        self.venta(total='10000.00', comision='350.00')

        respuesta, contexto = self.resumen()

        self.assertEqual(0, contexto['sin_comision'])
        self.assertNotIn('Lo que esta cuenta todavía no sabe',
                         respuesta.get_data(as_text=True))


class TestAislamiento(BaseResumen):
    """La plata de otra empresa no entra en la de esta."""

    def test_no_mezcla_empresas(self):
        self.aporte('500000.00')
        self.gasto('1000.00')

        otra = Empresa(nombre='Otra')
        db.session.add(otra)
        db.session.flush()
        db.session.add(Ingreso(empresa_id=otra.id, descripcion='Ajeno',
                               monto=Decimal('9999999.00'),
                               fecha=date(2026, 9, 1)))
        db.session.add(Gasto(empresa_id=otra.id, descripcion='Ajeno',
                             monto=Decimal('8888888.00'),
                             fecha=date(2026, 9, 1)))
        db.session.commit()

        _, contexto = self.resumen()

        self.assertEqual(Decimal('500000.00'), contexto['capital_monto'])
        self.assertEqual(Decimal('1000.00'), contexto['gastos_monto'])
        self.assertEqual(Decimal('499000.00'), contexto['posicion_real'])


class TestAuth(BaseResumen):
    """Como toda la seccion: sin sesion no se ve."""

    def test_hace_falta_login(self):
        respuesta = request_anonimo(self.ctx, 'get', '/reportes/resumen-general')

        self.assertEqual(302, respuesta.status_code)
        self.assertIn('/login', respuesta.headers['Location'])


if __name__ == '__main__':
    unittest.main()
