# -*- coding: utf-8 -*-
"""Tests de FASE-EVA-S2 (alarmas falsas del dashboard + bombas latentes).

    python -m unittest discover -s tests -v

TRES MITADES, AUNQUE SEAN TRES.

LAS ALARMAS FALSAS

El dashboard es la pantalla de aterrizaje despues del login, y con la cuenta
real de Korvo -- cero filas en `gasto` y cero en `ingreso` -- mostraba tres
alarmas rojas: "Margen 0.0% / Mal", "ROI 0.0% / Mal" y, la peor, "ALERTA!
Gastas mas del 80% de tus ingresos". Ninguna de las tres era un diagnostico.
Salian de los guards de division por cero de eva_utils, que devolvian 0, 0 y
**100**; el 100 pasaba el umbral de 80 y disparaba la alarma.

El test que importa es `test_dashboard_sin_datos_no_muestra_alarma`, y su par
`test_dashboard_con_datos_reales_si_calcula`: sacar las alarmas es facil si uno
rompe de paso el caso que si funcionaba, asi que el segundo congela los numeros
de la formula con datos de verdad.

LAS BOMBAS LATENTES

Mismo patron que FASE-AUDITORIA-S2 le encontro a `registrar_cambio`: dos copias
de una funcion, la rota importable desde el lugar equivocado.
`gastos_por_categoria` / `ingresos_por_categoria` estaban duplicadas en
eva_utils.py sumando Decimals crudos, mientras las que se usan (app.py) castean
a float. Se borraron las de eva_utils, y el test lo fija para que no vuelvan.

LOS DOS BUGS SILENCIOSOS

/config/eva posteaba nombre/RUC/email y la ruta no los guardaba. Y
crear_empresa leia un `capital_invertido` que su plantilla nunca renderizo, con
un `.get(..., 0)` que convertia el KeyError en un 0 escrito en silencio.

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


class ConfigFalsa:
    """Los tres parametros del EVA, sin tocar la base.

    `generar_analisis_completo` recibe la Empresa entera pero solo le lee tres
    atributos. Un objeto pelado deja los tests de la formula legibles y sin
    depender del esquema.
    """

    def __init__(self, capital_invertido=0.0, tasa_costo_capital=10.0,
                 tasa_impuestos=0.30):
        self.capital_invertido = capital_invertido
        self.tasa_costo_capital = tasa_costo_capital
        self.tasa_impuestos = tasa_impuestos


class BaseEva(unittest.TestCase):
    """Una empresa con un usuario. Sin filas de gasto ni de ingreso.

    El estado inicial es a proposito el de produccion el dia que se escribio
    esta slice: empresa creada, capital en 0, cero movimientos.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Korvo', capital_invertido=0.0,
                               tasa_costo_capital=10.0, tasa_impuestos=0.30)
        db.session.add(self.empresa)
        db.session.flush()

        self.roman = Usuario(nombre='Roman', email='roman@test.local',
                             empresa_id=self.empresa.id, verificado=True)
        self.roman.set_password('irrelevante')
        db.session.add(self.roman)
        db.session.commit()

        self.categoria_gasto = Categoria(nombre='Compra de mercadería',
                                         tipo='gasto',
                                         empresa_id=self.empresa.id,
                                         usuario_id=self.roman.id)
        self.categoria_ingreso = Categoria(nombre='Venta', tipo='ingreso',
                                           empresa_id=self.empresa.id,
                                           usuario_id=self.roman.id)
        db.session.add_all([self.categoria_gasto, self.categoria_ingreso])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.roman_id = self.roman.id
        self.cat_gasto_id = self.categoria_gasto.id
        self.cat_ingreso_id = self.categoria_ingreso.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def pedir(self, usuario_id, metodo, ruta, **kwargs):
        """Un request logueado, en su PROPIO app_context.

        El pop/push es el guard que documenta tests/ayuda_auth.py.
        """
        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(usuario_id)
                sesion['_fresh'] = True
            return getattr(cli, metodo)(ruta, **kwargs)
        finally:
            self.ctx.push()

    def get(self, usuario_id, ruta):
        return self.pedir(usuario_id, 'get', ruta, follow_redirects=True)

    def post(self, usuario_id, ruta, **kwargs):
        kwargs.setdefault('follow_redirects', True)
        return self.pedir(usuario_id, 'post', ruta, **kwargs)

    def texto_de(self, usuario_id, ruta):
        return self.get(usuario_id, ruta).get_data(as_text=True)

    def analisis_del_dashboard(self, usuario_id):
        """El dict que /dashboard le pasa a la plantilla (FASE-EVA-S4).

        Hace falta cuando lo que se afirma es de DONDE sale un numero y no
        solo que aparezca. El aporte de capital sigue estando en la pantalla
        con todo derecho -- en el grafico de ingresos cargados y en los
        ultimos movimientos --, asi que buscarlo en el texto crudo no
        distingue "se cuenta como ingreso del EVA" de "se muestra donde
        corresponde".
        """
        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado.setdefault('ctx', context)

        template_rendered.connect(anotar, app)
        try:
            self.get(usuario_id, '/dashboard')
        finally:
            template_rendered.disconnect(anotar, app)

        return capturado.get('ctx', {}).get('analisis', {})

    def cargar_movimientos(self, ingreso, gasto):
        """Dos movimientos fechados hace EXACTAMENTE un anio.

        La fecha era `date(2026, 8, 1)`, fija. Desde FASE-EVA-S3 el costo de
        capital se prorratea al periodo que va desde el primer movimiento hasta
        hoy, asi que una fecha fija haria que los numeros congelados de
        `test_dashboard_con_datos_reales_si_calcula` cambiaran solos con cada
        dia que pasa. Un anio exacto los deja identicos a los de antes de la
        slice -- que es justamente el caso en el que el supuesto viejo (cobrar
        la tasa anual entera) era correcto.
        """
        hace_un_anio = date.today() - timedelta(days=365)
        db.session.add(Ingreso(descripcion='Venta de prueba',
                               monto=Decimal(ingreso), fecha=hace_un_anio,
                               empresa_id=self.empresa_id,
                               usuario_id=self.roman_id,
                               categoria_id=self.cat_ingreso_id))
        db.session.add(Gasto(descripcion='Compra de prueba',
                             monto=Decimal(gasto), fecha=hace_un_anio,
                             empresa_id=self.empresa_id,
                             usuario_id=self.roman_id,
                             categoria_id=self.cat_gasto_id))
        db.session.commit()

    def cargar_venta(self, monto, envio='0.00', comision='0.00'):
        """Una venta real, en Pedido (FASE-EVA-S4).

        Desde esa slice los ingresos del dashboard salen de las VENTAS y no de
        la tabla Ingreso, que hoy tiene solo el aporte de capital. Un test que
        quiere ver un numero de ingresos en la pantalla tiene que cargar una
        venta; cargar un Ingreso mide otra cosa.

        La fecha es la misma que la de `cargar_movimientos` -- hace un anio
        exacto -- pero no cambia nada aca: el periodo lo siguen armando gasto e
        ingreso, y los pedidos no entran en ese MIN (ver el comentario de
        eva_utils sobre lo que S4 dejo abierto).
        """
        canal = CanalVenta.query.filter_by(empresa_id=self.empresa_id).first()
        if canal is None:
            canal = CanalVenta(empresa_id=self.empresa_id, tipo='manual',
                               nombre='Venta manual', activo=True)
            db.session.add(canal)
            db.session.commit()

        hace_un_anio = date.today() - timedelta(days=365)
        db.session.add(Pedido(empresa_id=self.empresa_id, canal_id=canal.id,
                              fecha_pedido=datetime(hace_un_anio.year,
                                                    hace_un_anio.month,
                                                    hace_un_anio.day, 12, 0),
                              estado='open', comprador_nombre='Camila',
                              total=Decimal(monto),
                              total_envio=Decimal(envio),
                              comision_plataforma=Decimal(comision)))
        db.session.commit()

    def poner_capital(self, monto):
        empresa = db.session.get(Empresa, self.empresa_id)
        empresa.capital_invertido = monto
        db.session.commit()


# =====================================================================
# PARTE 1 - Estado vacio, no alarma
# =====================================================================

# Los textos exactos que el dashboard NO puede mostrar sobre una cuenta vacia.
# Se afirma contra el string que ve Roman y no contra la clase CSS, porque lo
# que estaba mal no era el color: era el diagnostico.
ALARMAS = (
    '❌ Mal',
    '❌ Demasiado alto',
    '❌ No rentable',
    '¡ALERTA! Gastas más del 80% de tus ingresos',
    'Tu margen de ganancia es muy bajo',
    'Tu retorno sobre inversión es bajo',
)


class TestDashboardSinDatos(BaseEva):
    """Cero ingresos, cero gastos, capital en 0: el estado real de Korvo."""

    def test_dashboard_sin_datos_no_muestra_alarma(self):
        texto = self.texto_de(self.roman_id, '/dashboard')

        for alarma in ALARMAS:
            self.assertNotIn(
                alarma, texto,
                'El dashboard de una cuenta sin una sola fila cargada muestra '
                '"%s". Es un artefacto de division por cero, no un '
                'diagnostico.' % alarma)

    def test_dashboard_sin_datos_dice_que_falta(self):
        """No alcanza con callar la alarma: hay que decir que falta cargar."""
        texto = self.texto_de(self.roman_id, '/dashboard')
        self.assertIn('Todavía no cargaste ingresos ni gastos', texto)

    def test_los_cuatro_indicadores_quedan_en_neutral(self):
        analisis = eva_utils.generar_analisis_completo(0.0, 0.0, ConfigFalsa())

        for clave in ('margen_ganancia', 'roi', 'ratio_gastos', 'eva'):
            self.assertIsNone(
                analisis[clave],
                '%s deberia ser None (no se puede calcular), no un numero '
                'inventado.' % clave)

        for clave in ('margen_estado', 'roi_estado', 'ratio_estado',
                      'eva_estado'):
            self.assertEqual(eva_utils.CLASE_NEUTRAL, analisis[clave][0])

    def test_el_ratio_ya_no_inventa_un_100(self):
        """El guard de raiz: era `if ingresos == 0: return 100`.

        Ese 100 pasaba el umbral de 80 y disparaba la alarma. Se afirma sobre la
        funcion y no sobre la pantalla porque mientras la funcion siga
        inventando el numero, cualquier pantalla nueva que la llame hereda la
        mentira.
        """
        self.assertIsNone(eva_utils.calcular_ratio_deuda_ingresos(500.0, 0.0))

    def test_los_otros_dos_guards_tampoco_inventan_un_cero(self):
        self.assertIsNone(eva_utils.calcular_margen_ganancia(0.0, 0.0))
        self.assertIsNone(eva_utils.calcular_roi(1000.0, 0.0))

    def test_una_sola_recomendacion_y_es_informativa(self):
        analisis = eva_utils.generar_analisis_completo(0.0, 0.0, ConfigFalsa())
        recomendaciones = analisis['recomendaciones']

        self.assertEqual(1, len(recomendaciones),
                         'Una cuenta vacia recibia cuatro mensajes, tres de '
                         'ellos retos por no haber cargado nada.')
        self.assertEqual('info', recomendaciones[0]['tipo'])

    def test_gastos_sin_ingresos_no_es_una_cuenta_vacia(self):
        """Compro mercaderia y todavia no vendio: eso SI es un resultado.

        `hay_movimiento` mira ingresos O gastos justamente por este caso. La
        ganancia neta es real y negativa; lo unico que sigue sin poder
        calcularse es lo que se divide por ingresos.
        """
        analisis = eva_utils.generar_analisis_completo(0.0, 200000.0,
                                                       ConfigFalsa())

        self.assertTrue(analisis['hay_movimiento'])
        self.assertEqual(-140000.0, analisis['utilidad_neta'])
        self.assertIsNone(analisis['margen_ganancia'])
        self.assertIsNone(analisis['ratio_gastos'])

    def test_con_movimientos_pero_sin_capital_roi_y_eva_siguen_neutrales(self):
        """Con capital 0 el EVA queda identico a la ganancia neta.

        Seria mostrar el mismo numero dos veces, uno de ellos bajo un nombre que
        asusta. Se muestra que falta cargar el capital, y donde.
        """
        analisis = eva_utils.generar_analisis_completo(150000.0, 120000.0,
                                                       ConfigFalsa())

        self.assertIsNone(analisis['roi'])
        self.assertIsNone(analisis['eva'])
        self.assertEqual(eva_utils.CLASE_NEUTRAL, analisis['eva_estado'][0])
        self.assertIn('Cargá el capital invertido', analisis['eva_estado'][1])
        # El margen SI se calcula: no depende del capital.
        self.assertEqual(20.0, analisis['margen_ganancia'])


class TestDashboardConDatos(BaseEva):
    """El caso que ya andaba y que no se puede romper de paso."""

    def test_dashboard_con_datos_reales_si_calcula(self):
        """Los mismos numeros de siempre, con los ingresos de donde van.

        Hasta FASE-EVA-S4 los 150000 de ingresos entraban por
        `cargar_movimientos`, o sea como una fila de Ingreso. Eso dejo de ser
        cierto A PROPOSITO: los ingresos del dashboard son ahora las ventas
        (Pedido), y la tabla Ingreso quedo con el aporte de capital, que no es
        lo que factura la empresa.

        La cuenta congelada NO cambia -- 150000 contra 120000, margen 20%, ROI
        7%, EVA -9000 -- porque lo que se corrigio es de donde sale el
        ingreso, no como se calcula con el. Lo que se agrega es el aporte de
        999999: un numero que TIENE que estar cargado y NO puede aparecer en
        pantalla. Sin el, este test pasaria igual con el bug puesto de vuelta.

        FASE-EVA-S5 partio el test en dos mitades que antes eran una sola. Los
        numeros que la PANTALLA muestra quedaron en tres -- ingresos, gastos y
        ganancia neta --; margen, ROI y EVA se siguen calculando pero ya no se
        renderizan. Las afirmaciones sobre el texto se dieron vuelta y las de
        la cuenta se mudaron al dict.
        """
        self.cargar_movimientos(ingreso='999999.00', gasto='120000.00')
        self.cargar_venta('150000.00')
        self.poner_capital(300000.0)

        texto = self.texto_de(self.roman_id, '/dashboard')

        # Los numeros de la formula, tal cual los formatea la plantilla.
        self.assertIn('$150000', texto)        # ingresos: la VENTA
        self.assertIn('$120000', texto)        # gastos

        # Y el aporte de capital NO se cuenta como ingreso. Es la afirmacion
        # que trajo la slice: con el bug viejo el numero de arriba era 999999.
        #
        # Se mira el dict y no el texto a proposito: los 999999 SIGUEN en la
        # pantalla, en el grafico de ingresos cargados y en los ultimos
        # movimientos, y ahi estan bien. Lo que no puede pasar es que sean el
        # ingreso del EVA.
        analisis = self.analisis_del_dashboard(self.roman_id)
        self.assertEqual(150000.0, analisis['ingresos'],
                         'Los ingresos del EVA tienen que ser la venta. Si '
                         'dan 999999 el aporte de capital volvio a contarse '
                         'como facturacion; si dan 1149999 se estan contando '
                         'los dos y ademas se duplica.')
        self.assertIn('$21000', texto)         # utilidad neta

        # FASE-EVA-S5: margen, ROI y EVA ya NO estan en la pantalla.
        #
        # Este test decia `assertIn('20.0%')`, `assertIn('7.0%')`,
        # `assertIn('$-9000')` y `assertIn('❌ No rentable')`. Las cuatro
        # afirmaciones eran ciertas y dejaron de serlo A PROPOSITO: los tres
        # indicadores comparaban compras historicas contra ventas historicas,
        # o sea caja disfrazada de rentabilidad, y se sacaron del dashboard.
        # El margen real vive en /reportes/margen.
        #
        # Se dan vuelta en vez de borrarse: asi este test sigue siendo el que
        # vigila que no vuelvan solos a la pantalla. La FORMULA, que no se
        # borro, la congela `test_la_formula_no_cambio` aca abajo y toda
        # TestProrrateo en test_fase_eva_s3.
        self.assertNotIn('20.0%', texto)       # margen
        self.assertNotIn('7.0%', texto)        # ROI
        self.assertNotIn('$-9000', texto)      # EVA
        self.assertNotIn('❌ No rentable', texto)

        # Pero los numeros SIGUEN calculados y siguen llegando a la plantilla:
        # lo que se saco es lo que se renderiza, no la cuenta.
        self.assertAlmostEqual(20.0, analisis['margen_ganancia'])
        self.assertAlmostEqual(7.0, analisis['roi'])
        self.assertAlmostEqual(-9000.0, analisis['eva'])

        # Y el pie de la pantalla deja de decir que falta cargar, que es lo que
        # corresponde cuando SI hay con que.
        self.assertNotIn('Todavía no cargaste', texto)

    def test_la_formula_no_cambio(self):
        """Congela la cuenta entera contra los valores de antes de la slice.

        utilidad_bruta = 150000 - 120000    = 30000
        impuestos      = 30000 * 0.30       =  9000
        utilidad_neta  = 30000 - 9000       = 21000
        costo_capital  = 300000 * 10 / 100  = 30000
        EVA            = 21000 - 30000      = -9000
        """
        analisis = eva_utils.generar_analisis_completo(
            150000.0, 120000.0, ConfigFalsa(capital_invertido=300000.0))

        self.assertEqual(30000.0, analisis['utilidad_bruta'])
        self.assertEqual(9000.0, analisis['impuestos'])
        self.assertEqual(21000.0, analisis['utilidad_neta'])
        self.assertEqual(30000.0, analisis['costo_capital'])
        self.assertEqual(-9000.0, analisis['eva'])
        self.assertEqual(20.0, analisis['margen_ganancia'])
        self.assertAlmostEqual(7.0, analisis['roi'])
        self.assertAlmostEqual(80.0, analisis['ratio_gastos'])

    def test_el_semaforo_sigue_marcando_rojo_lo_que_esta_mal(self):
        analisis = eva_utils.generar_analisis_completo(
            150000.0, 120000.0, ConfigFalsa(capital_invertido=300000.0))

        self.assertEqual('malo', analisis['eva_estado'][0])
        self.assertEqual('malo', analisis['roi_estado'][0])
        self.assertEqual('malo', analisis['ratio_estado'][0])
        self.assertEqual('excelente', analisis['margen_estado'][0])

    def test_el_semaforo_sigue_marcando_verde_lo_que_esta_bien(self):
        analisis = eva_utils.generar_analisis_completo(
            200000.0, 60000.0, ConfigFalsa(capital_invertido=100000.0))

        self.assertEqual('excelente', analisis['eva_estado'][0])
        self.assertEqual('excelente', analisis['margen_estado'][0])
        self.assertEqual('excelente', analisis['ratio_estado'][0])
        self.assertEqual(
            ['exito'], [r['tipo'] for r in analisis['recomendaciones']])

    def test_umbral_maximo_cero_no_se_cae_al_branch_equivocado(self):
        """`if umbral_max:` trataba un maximo de 0 como "no hay maximo".

        Nadie pasa 0 hoy, pero el bug es del tipo que se descubre el dia que
        alguien agrega un indicador nuevo y no entiende por que el semaforo le
        contesta al reves.
        """
        self.assertEqual('excelente',
                         eva_utils.evaluar_indicador(0, 0, 0)[0])
        self.assertEqual('malo',
                         eva_utils.evaluar_indicador(5, 0, 0)[0])


# =====================================================================
# PARTE 2 y 3 - Bombas latentes y codigo muerto
# =====================================================================

class TestEvaUtilsSinDuplicados(unittest.TestCase):
    """Mismo criterio que FASE-AUDITORIA-S2 aplico a `registrar_cambio`.

    No se corrigieron las copias: se borraron. Una segunda copia correcta invita
    igual a importarla desde el lugar equivocado.
    """

    def test_eva_utils_no_tiene_duplicados_de_categoria(self):
        for nombre in ('gastos_por_categoria', 'ingresos_por_categoria'):
            self.assertFalse(
                hasattr(eva_utils, nombre),
                'eva_utils.%s volvio a existir. Sumaba `monto` crudo (Decimal) '
                'mientras la de app.py castea a float; el dia que alguien '
                'importe esta, `tojson` revienta y se caen los graficos del '
                'dashboard.' % nombre)

    def test_las_que_se_usan_siguen_estando_en_app(self):
        """El borrado no puede haberse llevado las buenas."""
        import app as modulo_app

        self.assertTrue(callable(modulo_app.gastos_por_categoria))
        self.assertTrue(callable(modulo_app.ingresos_por_categoria))

    def test_calcular_eva_no_existe(self):
        self.assertFalse(
            hasattr(eva_utils, 'calcular_eva'),
            'calcular_eva volvio a existir. Era una segunda formula del mismo '
            'numero que no llamaba nadie: dos formulas para una cuenta es una '
            'invitacion a que se separen.')

    def test_el_registrar_cambio_roto_sigue_sin_volver(self):
        """Guard heredado de FASE-AUDITORIA-S2, en el modulo que lo tenia."""
        self.assertFalse(hasattr(eva_utils, 'registrar_cambio'))


class TestConfigEva(BaseEva):
    """La pantalla prometia guardar tres campos que la ruta ignoraba."""

    DATOS_BASE = {
        'nombre': 'Korvo',
        'ruc': '',
        'email': '',
        'tasa_costo_capital': '10',
        'capital_invertido': '0',
        'tasa_impuestos': '30',
    }

    def postear(self, **cambios):
        datos = dict(self.DATOS_BASE)
        datos.update(cambios)
        return self.post(self.roman_id, '/config/eva', data=datos)

    def test_config_eva_no_promete_lo_que_no_guarda(self):
        """El caso exacto que fallaba: corregir el nombre y perderlo.

        La ruta escribia solo los tres campos del EVA, flasheaba "Configuracion
        actualizada" y volvia al dashboard con el nombre viejo.
        """
        self.postear(nombre='Korvo Distribuciones')

        empresa = db.session.get(Empresa, self.empresa_id)
        self.assertEqual('Korvo Distribuciones', empresa.nombre)

    def test_tambien_guarda_ruc_y_email(self):
        self.postear(ruc='20-12345678-9', email='hola@korvo.test')

        empresa = db.session.get(Empresa, self.empresa_id)
        self.assertEqual('20-12345678-9', empresa.ruc)
        self.assertEqual('hola@korvo.test', empresa.email)

    def test_vaciar_un_campo_opcional_deja_null_no_string_vacio(self):
        self.postear(ruc='20-12345678-9')
        self.postear(ruc='   ')

        empresa = db.session.get(Empresa, self.empresa_id)
        self.assertIsNone(empresa.ruc)

    def test_los_tres_campos_del_eva_se_siguen_guardando(self):
        """Conectar los datos de empresa no puede haber roto lo que ya andaba."""
        self.postear(tasa_costo_capital='12.5', capital_invertido='300000',
                     tasa_impuestos='35')

        empresa = db.session.get(Empresa, self.empresa_id)
        self.assertEqual(12.5, empresa.tasa_costo_capital)
        self.assertEqual(300000.0, empresa.capital_invertido)
        self.assertAlmostEqual(0.35, empresa.tasa_impuestos)

    def test_nombre_vacio_se_rechaza_y_no_pisa_el_que_habia(self):
        """`required` es del navegador; un POST directo lo saltea."""
        self.postear(nombre='   ')

        empresa = db.session.get(Empresa, self.empresa_id)
        self.assertEqual('Korvo', empresa.nombre)

    def test_queda_constancia_en_el_historial(self):
        from models import Historial

        self.postear(nombre='Korvo Distribuciones')

        registros = Historial.query.filter_by(tipo='configuracion').all()
        self.assertEqual(1, len(registros))
        self.assertEqual(self.roman_id, registros[0].usuario_id)

    def test_pide_login(self):
        resp = request_anonimo(self.ctx, 'get', '/config/eva')
        self.assertEqual(302, resp.status_code)
        self.assertIn('/login', resp.headers.get('Location', ''))


class TestCrearEmpresa(BaseEva):
    """La lectura muerta de un campo que la plantilla nunca renderizo."""

    DATOS = {
        'nombre': 'Korvo',
        'ruc': '20-12345678-9',
        'telefono': '1122334455',
        'email': 'hola@korvo.test',
        'direccion': 'Calle Falsa 123',
        'descripcion': 'Ferretería',
    }

    def test_crear_empresa_no_lee_campo_fantasma(self):
        """El capital cargado en /config/eva sobrevive al alta de empresa.

        `float(request.form.get('capital_invertido', 0))` no fallaba nunca -- el
        `.get` con default tapaba el KeyError -- y escribia un 0 en cada POST.
        El test carga un capital primero para que el 0 se note: sin el arreglo,
        vuelve a 0 y el assert falla.
        """
        self.poner_capital(300000.0)

        self.post(self.roman_id, '/crear-empresa', data=self.DATOS)

        empresa = db.session.get(Empresa, self.empresa_id)
        self.assertEqual(300000.0, empresa.capital_invertido)

    def test_el_alta_sigue_guardando_los_datos_que_si_pide(self):
        """Sacar la linea muerta no puede haberse llevado las vivas."""
        self.post(self.roman_id, '/crear-empresa', data=self.DATOS)

        empresa = db.session.get(Empresa, self.empresa_id)
        self.assertEqual('Korvo', empresa.nombre)
        self.assertEqual('20-12345678-9', empresa.ruc)
        self.assertEqual('1122334455', empresa.telefono)
        self.assertEqual('hola@korvo.test', empresa.email)
        self.assertEqual('Calle Falsa 123', empresa.direccion)
        self.assertEqual('Ferretería', empresa.descripcion)

    def test_pide_login(self):
        resp = request_anonimo(self.ctx, 'get', '/crear-empresa')
        self.assertEqual(302, resp.status_code)
        self.assertIn('/login', resp.headers.get('Location', ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
