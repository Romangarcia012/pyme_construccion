# -*- coding: utf-8 -*-
"""Tests de FASE-EVA-S4 (los ingresos del dashboard son las ventas).

    python -m unittest discover -s tests -v

EL BUG

El dashboard calculaba `ingresos_totales = SUM(Ingreso.monto)`. Eso fue
correcto hasta FASE-AUDITORIA-EXCEL-S3, el dia que las ventas SALIERON de esa
tabla: se cargaban a mano y duplicaban lo que ya vivia en Pedido. Desde
entonces Ingreso tiene una sola cosa -- el aporte de capital de los socios --
y nadie volvio a mirar el dashboard.

O sea que la pantalla venia comparando el APORTE contra los gastos y llamando
"margen" al resultado. Con los numeros de Korvo daba -33.000 y los cuatro
indicadores en rojo, sobre un negocio que factura. La plata que los socios
pusieron no es lo que la empresa vendio.

LO QUE ARREGLA LA SLICE

    ingresos_totales = facturado_neto(empresa).neto

`facturado_neto` es la MISMA funcion que usan /reportes/caja-socio y
/reportes/resumen-general: SUM(pedido.total - total_envio) de los no
cancelados y no regalo, menos la comision de plataforma. No es una consulta
nueva que da lo mismo -- es la misma.

LAS TRES COSAS QUE NO SE PUEDEN CONFUNDIR

Esta suite existe sobre todo por esto. Hay tres numeros con nombres parecidos
y significados distintos, y el bug nacio de mezclar dos:

    Ingreso.monto            la plata que ENTRO y se cargo a mano. Hoy: el
                             aporte de los socios. Sigue en /caja-general y en
                             /reportes/resumen-general, intacta.
    Pedido (facturado neto)  lo que la empresa VENDIO. Es lo que mide
                             rentabilidad, y es lo que ahora usa el EVA.
    Empresa.capital_invertido  el capital contra el que se cobra el costo de
                             capital. Es un parametro de /config/eva, un
                             Float, y no es un movimiento de plata.

`test_capital_invertido_no_se_confunde_con_ingreso` y
`test_las_tres_cosas_son_tres` fijan que sigan siendo tres.

LO QUE NO CAMBIA

El estado neutral de S2, el prorrateo de S3 y la formula entera del EVA. Solo
cambia de donde sale uno de los dos numeros que entran. `gastos_totales` sigue
siendo SUM(Gasto.monto), todos, sin filtrar por origen.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eva_utils  # noqa: E402

from tests.ayuda_auth import request_anonimo  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
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
from rutas_reportes import facturado_neto  # noqa: E402

ENGINE_PRODUCTIVO = None


class ConfigFalsaS4:
    """Los tres parametros del EVA, sin tocar la base. Igual que en S2 y S3.

    La usa el unico test que llama a la formula a mano -- el que congela el
    numero VIEJO para poder compararlo con el nuevo.
    """

    def __init__(self, capital_invertido=0.0, tasa_costo_capital=10.0,
                 tasa_impuestos=0.30):
        self.capital_invertido = capital_invertido
        self.tasa_costo_capital = tasa_costo_capital
        self.tasa_impuestos = tasa_impuestos


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


class BaseEvaS4(unittest.TestCase):
    """Korvo en chico: capital cargado, un canal, y las tres tablas a mano."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Korvo', capital_invertido=0.0,
                               tasa_costo_capital=10.0, tasa_impuestos=0.30)
        db.session.add(self.empresa)
        db.session.flush()

        self.roman = Usuario(nombre='Roman', email='evas4@test.local',
                             empresa_id=self.empresa.id, verificado=True)
        self.roman.set_password('irrelevante')
        db.session.add(self.roman)
        db.session.commit()

        self.cat_gasto = Categoria(nombre='Compra de mercadería', tipo='gasto',
                                   empresa_id=self.empresa.id,
                                   usuario_id=self.roman.id)
        self.cat_ingreso = Categoria(nombre='Aporte de capital (socios)',
                                     tipo='ingreso',
                                     empresa_id=self.empresa.id,
                                     usuario_id=self.roman.id)
        db.session.add_all([self.cat_gasto, self.cat_ingreso])

        self.cuenta = CuentaCobro(empresa_id=self.empresa.id,
                                  nombre='Roman - Presencial y Tiendanube',
                                  tipo='mercadopago', socio='roman')
        db.session.add(self.cuenta)
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.roman_id = self.roman.id
        self.cat_gasto_id = self.cat_gasto.id
        self.cat_ingreso_id = self.cat_ingreso.id
        self.cuenta_id = self.cuenta.id

        self.canal = CanalVenta(empresa_id=self.empresa_id, tipo='manual',
                                nombre='Venta manual', activo=True,
                                cuenta_cobro_id=self.cuenta_id)
        db.session.add(self.canal)
        db.session.commit()
        self.canal_id = self.canal.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def venta(self, total='10000.00', envio='0.00', comision='0.00',
              regalo=False, estado='open'):
        fila = Pedido(empresa_id=self.empresa_id, canal_id=self.canal_id,
                      fecha_pedido=datetime(2026, 9, 1, 12, 0),
                      estado=estado, comprador_nombre='Camila',
                      total=Decimal(total), total_envio=Decimal(envio),
                      comision_plataforma=(None if comision is None
                                           else Decimal(comision)),
                      es_regalo=regalo)
        db.session.add(fila)
        db.session.commit()
        return fila

    def aporte(self, monto='2371037.83', hace_dias=365):
        fila = Ingreso(descripcion='Aporte de los socios',
                       monto=Decimal(monto),
                       fecha=date.today() - timedelta(days=hace_dias),
                       empresa_id=self.empresa_id, usuario_id=self.roman_id,
                       categoria_id=self.cat_ingreso_id)
        db.session.add(fila)
        db.session.commit()
        return fila

    def gasto(self, monto='120000.00', hace_dias=365):
        fila = Gasto(descripcion='Compra', monto=Decimal(monto),
                     fecha=date.today() - timedelta(days=hace_dias),
                     empresa_id=self.empresa_id, usuario_id=self.roman_id,
                     categoria_id=self.cat_gasto_id)
        db.session.add(fila)
        db.session.commit()
        return fila

    def poner_capital(self, monto):
        empresa = db.session.get(Empresa, self.empresa_id)
        empresa.capital_invertido = monto
        db.session.commit()

    def analisis(self):
        """El dict que /dashboard le pasa a la plantilla."""
        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado.setdefault('ctx', context)

        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(self.roman_id)
                sesion['_fresh'] = True
            template_rendered.connect(anotar, app)
            try:
                self.respuesta = cli.get('/dashboard', follow_redirects=True)
            finally:
                template_rendered.disconnect(anotar, app)
        finally:
            self.ctx.push()

        return capturado.get('ctx', {}).get('analisis', {})


# =====================================================================
# PARTE 2 - El fix
# =====================================================================

class TestElOrigenDeLosIngresos(BaseEvaS4):

    def test_ingresos_totales_usa_ventas_no_ingreso(self):
        """El caso exacto del bug: capital cargado, ventas cargadas.

        El aporte es enorme y las ventas chicas -- que es la forma real de
        Korvo -- asi que los dos numeros no se pueden confundir por accidente.
        """
        self.aporte('2371037.83')
        self.venta(total='150000.00', comision='0.00')

        analisis = self.analisis()

        self.assertEqual(150000.0, analisis['ingresos'])
        self.assertNotEqual(2371037.83, analisis['ingresos'],
                            'El aporte de capital volvio a contarse como '
                            'ingreso: es plata que los socios pusieron, no '
                            'plata que la empresa facturo.')

    def test_sin_ventas_los_ingresos_son_cero_aunque_haya_aporte(self):
        """Un aporte grande y ninguna venta NO es facturacion.

        Es el estado de una empresa que puso la plata y todavia no vendio, y
        el dashboard tiene que decir eso -- no que factura millones.
        """
        self.aporte('2371037.83')

        analisis = self.analisis()

        self.assertEqual(0.0, analisis['ingresos'])

    def test_es_la_misma_funcion_que_los_reportes(self):
        """No una consulta nueva que da lo mismo: la misma.

        Si el dashboard tuviera su propia copia de la cuenta, el dia que una
        de las dos cambie los numeros se separan sin que nada avise.
        """
        self.aporte('2371037.83')
        self.venta(total='13696.90', envio='7630.00', comision='358.70')
        self.venta(total='4000.00', comision='120.00')

        analisis = self.analisis()

        self.assertEqual(float(facturado_neto(self.empresa_id).neto),
                         analisis['ingresos'])

    def test_el_facturado_es_neto_de_envio_y_comision(self):
        """13696.90 - 7630.00 de envio - 358.70 de comision = 5708.20."""
        self.venta(total='13696.90', envio='7630.00', comision='358.70')

        analisis = self.analisis()

        self.assertEqual(5708.20, round(analisis['ingresos'], 2))

    def test_el_regalo_y_el_cancelado_no_son_ingreso(self):
        """Las mismas exclusiones que caja-socio, porque es la misma cuenta."""
        self.venta(total='10000.00', comision='0.00')
        self.venta(total='1.00', comision='0.00', regalo=True)
        self.venta(total='9999.00', comision='0.00', estado='cancelled')

        analisis = self.analisis()

        self.assertEqual(10000.0, analisis['ingresos'])

    def test_pedido_sin_comision_no_resta_cero(self):
        """NULL != 0 llega hasta aca: sale de la misma funcion."""
        self.venta(total='10000.00', comision='350.00')
        self.venta(total='5000.00', comision=None)

        analisis = self.analisis()

        # 15000 - 350. El pedido sin cargar no aporta una comision de 0.00
        # disfrazada de dato, y por eso el ingreso esta por ARRIBA del que va
        # a terminar siendo.
        self.assertEqual(14650.0, analisis['ingresos'])
        self.assertEqual(1, facturado_neto(self.empresa_id).sin_comision)

    def test_no_mezcla_empresas(self):
        """Las ventas de otra empresa no son ingresos de esta."""
        self.venta(total='10000.00', comision='0.00')

        otra = Empresa(nombre='Otra')
        db.session.add(otra)
        db.session.flush()
        canal_ajeno = CanalVenta(empresa_id=otra.id, tipo='manual',
                                 nombre='Ajeno', activo=True)
        db.session.add(canal_ajeno)
        db.session.commit()
        db.session.add(Pedido(empresa_id=otra.id, canal_id=canal_ajeno.id,
                              fecha_pedido=datetime(2026, 9, 1, 12, 0),
                              estado='open', comprador_nombre='Ajena',
                              total=Decimal('9999999.00'),
                              total_envio=Decimal('0.00'),
                              comision_plataforma=Decimal('0.00')))
        db.session.commit()

        self.assertEqual(10000.0, self.analisis()['ingresos'])


# =====================================================================
# Las tres cosas que no se pueden confundir
# =====================================================================

class TestTresCosasDistintas(BaseEvaS4):

    def test_capital_invertido_no_se_confunde_con_ingreso(self):
        """`Empresa.capital_invertido` no es plata que entro.

        Es el parametro de /config/eva contra el que se cobra el costo de
        capital. Cambiarlo tiene que mover el costo de capital y NO los
        ingresos; si moviera los dos, seria el mismo numero con dos nombres.
        """
        self.venta(total='150000.00', comision='0.00')
        self.gasto('120000.00')
        self.poner_capital(300000.0)

        analisis = self.analisis()

        self.assertEqual(150000.0, analisis['ingresos'])
        self.assertNotEqual(300000.0, analisis['ingresos'])
        # Y sigue haciendo lo suyo: costo de capital sobre un anio exacto.
        self.assertAlmostEqual(30000.0, analisis['costo_capital'], places=2)

    def test_mover_el_capital_no_mueve_los_ingresos(self):
        """La otra mitad de lo mismo, medida como un cambio."""
        self.venta(total='150000.00', comision='0.00')
        self.gasto('120000.00')

        self.poner_capital(300000.0)
        antes = self.analisis()
        self.poner_capital(900000.0)
        despues = self.analisis()

        self.assertEqual(antes['ingresos'], despues['ingresos'])
        self.assertNotEqual(antes['costo_capital'], despues['costo_capital'])

    def test_las_tres_cosas_son_tres(self):
        """Tres numeros distintos cargados a la vez, y ninguno se pisa."""
        self.aporte('2371037.83')          # Ingreso: la plata que entro
        self.venta(total='150000.00', comision='0.00')   # Pedido: lo vendido
        self.gasto('120000.00')
        self.poner_capital(300000.0)       # Empresa: el parametro del EVA

        analisis = self.analisis()
        empresa = db.session.get(Empresa, self.empresa_id)

        self.assertEqual(150000.0, analisis['ingresos'])
        self.assertEqual(120000.0, analisis['gastos'])
        self.assertEqual(300000.0, empresa.capital_invertido)
        # Y el aporte sigue existiendo, entero, donde le corresponde.
        self.assertEqual(
            Decimal('2371037.83'),
            db.session.query(db.func.sum(Ingreso.monto))
            .filter(Ingreso.empresa_id == self.empresa_id).scalar())

    def test_el_aporte_sigue_entero_en_caja_general(self):
        """La constraint de la slice: no se toca ninguno de los tres reportes.

        El aporte dejo de ser "ingresos" para el EVA y NO dejo de ser plata
        que entro. /caja-general lo tiene que seguir mostrando igual.
        """
        self.aporte('2371037.83')
        self.gasto('120000.00')

        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado['ctx'] = context

        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(self.roman_id)
                sesion['_fresh'] = True
            template_rendered.connect(anotar, app)
            try:
                cli.get('/caja-general')
            finally:
                template_rendered.disconnect(anotar, app)
        finally:
            self.ctx.push()

        contexto = capturado.get('ctx', {})
        self.assertEqual(Decimal('2371037.83'), contexto['total_entradas'])
        self.assertEqual(Decimal('120000.00'), contexto['total_salidas'])


# =====================================================================
# PARTE 3 - Los numeros reales
# =====================================================================

class TestDatosReales(BaseEvaS4):
    """Korvo tal como esta en Supabase, leido el 2026-09-06.

        capital_invertido  : 2248562.63   (/config/eva, SI esta cargado)
        tasa_costo_capital :      10.00
        tasa_impuestos     :       0.00   (por eso neta == bruta)
        aporte de capital  : 2371037.83   (Ingreso; NO es ingreso del EVA)
        facturado neto     :  208152.80   (208511.50 - 358.70 de comision)
        gastos             : 2404037.83
        dias de operacion  :         40

    Los montos se SIEMBRAN, no se consultan -- mismo criterio que
    FASE-RESUMEN-GENERAL-S1: un test que le pregunta a produccion se pone rojo
    el dia que Roman carga un gasto, y ese rojo no seria una regresion.

    Las tres tasas tambien se siembran con los valores REALES y no con los
    defaults de la clase base. Un test que dice "los numeros de hoy" y usa una
    tasa de impuestos del 30% cuando la empresa la tiene en 0 no esta
    mostrando los numeros de hoy.
    """

    def sembrar_produccion(self):
        empresa = db.session.get(Empresa, self.empresa_id)
        empresa.capital_invertido = 2248562.63
        empresa.tasa_costo_capital = 10.0
        empresa.tasa_impuestos = 0.0
        db.session.commit()

        self.aporte('2371037.83', hace_dias=40)
        self.venta(total='208511.50', comision='358.70')
        self.gasto('2404037.83', hace_dias=40)

    def test_dashboard_con_datos_reales_de_hoy(self):
        """El panorama que Roman ve hoy, con el fix puesto.

        NO sale mas lindo: sale mucho peor, y ese es el punto. El -33.000 de
        antes no era una mala noticia leve, era una cuenta sin sentido --
        comparaba la plata que los socios PUSIERON contra la que se GASTO, que
        son casi la misma plata por construccion y por eso daba cerca de cero.

        Lo que se ve ahora es lo que de verdad pasa: se vendieron 208.152,80 y
        se gastaron 2.404.037,83. La mayor parte de ese gasto es mercaderia que
        todavia esta en el deposito sin vender, y esta pantalla no lo sabe --
        suma compras historicas contra ventas historicas. Eso NO lo arregla
        esta slice y esta anotado en el reporte: el margen de verdad por
        producto vive en /reportes/margen, que si descuenta el costo de lo
        vendido y no el de lo comprado.
        """
        self.sembrar_produccion()

        analisis = self.analisis()

        self.assertEqual(208152.80, round(analisis['ingresos'], 2))
        self.assertEqual(2404037.83, round(analisis['gastos'], 2))
        self.assertEqual(40, analisis['dias_transcurridos'])

        # La cuenta, con el ingreso que corresponde. Con la tasa de impuestos
        # en 0 la neta es la bruta: no hay impuesto que restarle a una perdida.
        self.assertEqual(-2195885.03, round(analisis['utilidad_bruta'], 2))
        self.assertEqual(0.0, round(analisis['impuestos'], 2))
        self.assertEqual(-2195885.03, round(analisis['utilidad_neta'], 2))

        # Costo de capital prorrateado a 40 dias (S3 sigue funcionando).
        self.assertEqual(24641.78, round(analisis['costo_capital'], 2))

        self.assertEqual(-1054.94, round(analisis['margen_ganancia'], 2))
        self.assertEqual(1154.94, round(analisis['ratio_gastos'], 2))
        self.assertEqual(-97.66, round(analisis['roi'], 2))
        self.assertEqual(-2220526.81, round(analisis['eva'], 2))

    def test_el_numero_viejo_era_el_aporte_contra_los_gastos(self):
        """Congela lo que la pantalla decia ANTES, para poder comparar.

        Con los mismos gastos y el aporte como ingreso, la utilidad bruta daba
        -33.000: la diferencia entre lo que los socios pusieron y lo que se
        gasto. Es una cuenta de caja, no de rentabilidad, y el dashboard la
        estaba presentando como margen.

        Se prueba llamando a la formula a mano, no por la pantalla: el bug ya
        no existe en el dashboard y esto documenta de que numero venimos.
        """
        viejo = eva_utils.generar_analisis_completo(
            2371037.83, 2404037.83,
            ConfigFalsaS4(capital_invertido=2248562.63, tasa_impuestos=0.0),
            40)

        self.assertEqual(-33000.0, round(viejo['utilidad_bruta'], 2))
        self.assertEqual(-1.39, round(viejo['margen_ganancia'], 2))
        self.assertEqual(-1.47, round(viejo['roi'], 2))
        self.assertEqual(-57641.78, round(viejo['eva'], 2))


# =====================================================================
# Lo que NO cambia
# =====================================================================

class TestLoQueNoCambia(BaseEvaS4):

    def test_los_gastos_siguen_siendo_todos(self):
        """SUM(Gasto.monto), sin filtrar por origen. No lo toca esta slice."""
        self.gasto('1000.00')
        self.gasto('2500.00')

        self.assertEqual(3500.0, self.analisis()['gastos'])

    def test_el_estado_neutral_sigue_intacto(self):
        """Una empresa sin nada cargado no gana alarmas por el cambio."""
        analisis = self.analisis()

        self.assertFalse(analisis['hay_movimiento'])
        for clave in ('margen_ganancia', 'roi', 'ratio_gastos', 'eva'):
            self.assertIsNone(analisis[clave])

    def test_una_venta_sola_ya_es_movimiento(self):
        """Con ventas y sin gastos el dashboard tiene algo que decir.

        Antes hacia falta una fila de Ingreso o de Gasto para salir del estado
        neutral. Ahora una venta alcanza, que es lo correcto: la empresa
        facturo.
        """
        self.venta(total='10000.00', comision='0.00')

        analisis = self.analisis()

        self.assertTrue(analisis['hay_movimiento'])
        self.assertIsNotNone(analisis['margen_ganancia'])

    def test_el_periodo_sigue_saliendo_de_gasto_e_ingreso(self):
        """El prorrateo de S3 no se toca (y lo que quedo abierto, se ve).

        Los pedidos NO entran en el MIN(fecha) que arma el periodo. Con una
        venta y ninguna fila de gasto ni de ingreso, el periodo es desconocido
        y `periodo_conocido` viaja en False -- la pantalla lo dice en vez de
        inventar un anio.
        """
        self.venta(total='10000.00', comision='0.00')

        analisis = self.analisis()

        self.assertFalse(analisis['periodo_conocido'])

        # Y con un gasto fechado, el periodo vuelve a conocerse igual que antes.
        self.gasto('1000.00', hace_dias=45)
        self.assertTrue(self.analisis()['periodo_conocido'])
        self.assertEqual(45, self.analisis()['dias_transcurridos'])

    def test_pide_sesion(self):
        respuesta = request_anonimo(self.ctx, 'get', '/dashboard')

        self.assertEqual(302, respuesta.status_code)
        self.assertIn('/login', respuesta.headers['Location'])


if __name__ == '__main__':
    unittest.main()
