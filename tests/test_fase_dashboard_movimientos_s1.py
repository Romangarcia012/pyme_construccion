# -*- coding: utf-8 -*-
"""Tests de FASE-DASHBOARD-MOVIMIENTOS-S1 (las ventas entran a "Últimos movimientos").

    python -m unittest discover -s tests -v

QUE ESTABA MAL

El bloque "Últimos movimientos" del dashboard miraba Gasto e Ingreso y nada
mas, igual que /caja-general. Eso alcanzaba cuando las ventas se cargaban a
mano como Ingreso; desde FASE-AUDITORIA-EXCEL-S3 viven en Pedido, y desde
entonces una venta de ayer no aparecia en ningun lado de esa lista. La pantalla
podia decir que no habia pasado nada en una semana en la que se vendio todos
los dias.

Ademas la tabla no estaba ordenada: la plantilla ponia primero los cinco
gastos y despues los cinco ingresos, asi que un ingreso de hoy quedaba debajo
de un gasto del mes pasado.

QUE SE AFIRMA ACA

1. Una venta reciente aparece en la lista.
2. Los cancelados y los regalos NO aparecen -- el mismo criterio con el que
   `_facturado_por_canal` arma el Facturado de caja-socio. Que sea el mismo
   importa: si la lista mostrara un pedido que el total no cuenta, los dos
   numeros de la misma pantalla se contradicen.
3. Las tres fuentes se intercalan por fecha, no se agrupan por tipo.
4. Una venta vale `total - total_envio`, tambien igual que Facturado. Es la
   cuarta definicion que NO se inventa.

La app se repunta a SQLite en memoria, mismo criterio que las slices de EVA: la
base productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import TOPE_MOVIMIENTOS, app  # noqa: E402
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

HOY = date.today()


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


class BaseMovimientos(unittest.TestCase):
    """Korvo en chico: una empresa, un socio, un canal, dos categorias."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Korvo', capital_invertido=100000.0,
                               tasa_costo_capital=10.0, tasa_impuestos=0.30)
        db.session.add(self.empresa)
        db.session.flush()

        self.roman = Usuario(nombre='Roman', email='dashmov@test.local',
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
        db.session.commit()

        self.canal = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                nombre='Venta manual', activo=True)
        db.session.add(self.canal)
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.roman_id = self.roman.id
        self.cat_gasto_id = self.cat_gasto.id
        self.cat_ingreso_id = self.cat_ingreso.id
        self.canal_id = self.canal.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers de carga -----------------------------------------------

    def venta(self, dias_atras=0, total='10000.00', envio='0.00',
              estado='open', es_regalo=False, comprador='Camila'):
        pedido = Pedido(empresa_id=self.empresa_id, canal_id=self.canal_id,
                        fecha_pedido=self._momento(dias_atras),
                        estado=estado, comprador_nombre=comprador,
                        total=Decimal(total),
                        total_envio=Decimal(envio),
                        es_regalo=es_regalo,
                        comision_plataforma=Decimal('0.00'))
        db.session.add(pedido)
        db.session.commit()
        return pedido

    def gasto(self, dias_atras=0, monto='5000.00',
              descripcion='Compra de clavos'):
        g = Gasto(descripcion=descripcion, monto=Decimal(monto),
                  fecha=HOY - timedelta(days=dias_atras),
                  empresa_id=self.empresa_id, usuario_id=self.roman_id,
                  categoria_id=self.cat_gasto_id)
        db.session.add(g)
        db.session.commit()
        return g

    def aporte(self, dias_atras=0, monto='7000.00',
               descripcion='Aporte de los socios'):
        i = Ingreso(descripcion=descripcion, monto=Decimal(monto),
                    fecha=HOY - timedelta(days=dias_atras),
                    empresa_id=self.empresa_id, usuario_id=self.roman_id,
                    categoria_id=self.cat_ingreso_id)
        db.session.add(i)
        db.session.commit()
        return i

    def _momento(self, dias_atras):
        dia = HOY - timedelta(days=dias_atras)
        return datetime(dia.year, dia.month, dia.day, 12, 0)

    # -- acceso al dashboard --------------------------------------------

    def _pedir_dashboard(self):
        """(texto, contexto) de una sola visita a /dashboard.

        El pop/push del contexto es el guard que documenta tests/ayuda_auth.py.
        """
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
                resp = cli.get('/dashboard', follow_redirects=True)
            finally:
                template_rendered.disconnect(anotar, app)
        finally:
            self.ctx.push()

        return resp.get_data(as_text=True), capturado.get('ctx', {})

    def texto(self):
        return self._pedir_dashboard()[0]

    def movimientos(self):
        """La lista cruda que la ruta le pasa a la plantilla.

        Se afirma sobre esta y no sobre el HTML cuando lo que importa es el
        ORDEN o el MONTO: la tabla trunca las descripciones en 20 caracteres y
        redondea los montos a pesos enteros, asi que buscar texto no distingue
        "esta bien calculado" de "quedo parecido".
        """
        return self._pedir_dashboard()[1].get('movimientos')


# =====================================================================
# PARTE 1 - la venta aparece
# =====================================================================

class TestLaVentaEntra(BaseMovimientos):

    def test_ultimos_movimientos_incluye_ventas(self):
        """La afirmacion de la slice: una venta reciente esta en la lista."""
        self.venta(dias_atras=1, comprador='Camila')

        etiquetas = [m['etiqueta'] for m in self.movimientos()]
        self.assertIn(
            'Venta', etiquetas,
            'Una venta de ayer no aparece en "Ultimos movimientos". El bloque '
            'volvio a mirar solo Gasto e Ingreso, y las ventas viven en '
            'Pedido desde FASE-AUDITORIA-EXCEL-S3.')

    def test_la_venta_se_ve_en_la_pantalla(self):
        """No alcanza con que este en el contexto: tiene que renderizarse."""
        self.venta(dias_atras=1, comprador='Camila')

        texto = self.texto()
        self.assertIn('Venta - Camila', texto)
        self.assertNotIn('No hay movimientos registrados aún', texto)

    def test_la_venta_es_una_entrada(self):
        """Vender es plata que entra: se pinta en verde y con signo +."""
        self.venta(dias_atras=1, total='10000.00')

        venta = [m for m in self.movimientos() if m['etiqueta'] == 'Venta'][0]
        self.assertEqual('entrada', venta['tipo'])

    def test_la_venta_sin_comprador_se_nombra_por_el_canal(self):
        """Las ventas de mostrador muchas veces no traen nombre."""
        self.venta(dias_atras=1, comprador=None)

        venta = [m for m in self.movimientos() if m['etiqueta'] == 'Venta'][0]
        self.assertEqual('Venta - Venta manual', venta['descripcion'])

    def test_sin_nada_cargado_sigue_diciendo_que_no_hay_movimientos(self):
        """El estado vacio no se rompio al cambiar la variable del contexto."""
        self.assertEqual([], self.movimientos())
        self.assertIn('No hay movimientos registrados aún', self.texto())


# =====================================================================
# PARTE 2 - el mismo criterio que Facturado, no uno nuevo
# =====================================================================

class TestMismoCriterioQueFacturado(BaseMovimientos):

    def test_ultimos_movimientos_excluye_regalo_y_cancelado(self):
        """Los dos pedidos que Facturado no cuenta tampoco se listan.

        Si aparecieran, la misma pantalla mostraria un movimiento de plata que
        su propio total de ventas no cuenta, y no habria por donde empezar a
        mirar cuando los dos numeros no cierren.
        """
        self.venta(dias_atras=1, estado='cancelled', comprador='Cancelada')
        self.venta(dias_atras=2, es_regalo=True, comprador='Regalada')
        self.venta(dias_atras=3, comprador='Camila')

        descripciones = [m['descripcion'] for m in self.movimientos()]
        self.assertIn('Venta - Camila', descripciones)
        self.assertNotIn('Venta - Cancelada', descripciones)
        self.assertNotIn('Venta - Regalada', descripciones)

    def test_el_monto_de_la_venta_va_sin_envio(self):
        """`total - total_envio`, la misma cuenta que el Facturado de caja-socio.

        El envio que paga el comprador entra a la cuenta y sale hacia el correo
        el mismo dia: contarlo dice que la venta dejo una plata que estaba de
        paso.
        """
        self.venta(dias_atras=1, total='12000.00', envio='2000.00')

        venta = [m for m in self.movimientos() if m['etiqueta'] == 'Venta'][0]
        self.assertEqual(Decimal('10000.00'), venta['monto'])

    def test_el_mismo_criterio_que_facturado_neto(self):
        """La lista y el total de ventas del dashboard miran los mismos pedidos.

        Se comprueba contra `facturado_neto` -- la funcion de caja-socio y del
        resumen-general -- y no contra un numero escrito a mano: lo que se
        afirma es que las dos definiciones son UNA.
        """
        from rutas_reportes import facturado_neto

        self.venta(dias_atras=1, total='12000.00', envio='2000.00')
        self.venta(dias_atras=2, estado='cancelled')
        self.venta(dias_atras=3, es_regalo=True)

        ventas = [m for m in self.movimientos() if m['etiqueta'] == 'Venta']
        self.assertEqual(facturado_neto(self.empresa_id).bruto,
                         sum(m['monto'] for m in ventas))


# =====================================================================
# PARTE 3 - una linea de tiempo, no tres bloques
# =====================================================================

class TestOrden(BaseMovimientos):

    def test_orden_por_fecha_mezclando_las_tres_fuentes(self):
        """Gasto, ingreso y venta intercalados por fecha, no agrupados por tipo.

        Se cargan fuera de orden y alternando tipo a proposito: agrupar por
        fuente -- que es lo que hacia la plantilla antes de esta slice -- daria
        una lista distinta de esta y el test se cae.
        """
        self.gasto(dias_atras=5, descripcion='Gasto viejo')
        self.venta(dias_atras=1, comprador='Camila')
        self.aporte(dias_atras=3, descripcion='Aporte del medio')
        self.gasto(dias_atras=0, descripcion='Gasto de hoy')
        self.venta(dias_atras=4, comprador='Nadia')

        esperado = ['Gasto de hoy', 'Venta - Camila', 'Aporte del medio',
                    'Venta - Nadia', 'Gasto viejo']
        self.assertEqual(esperado,
                         [m['descripcion'] for m in self.movimientos()])

    def test_no_se_muestran_mas_filas_que_el_tope(self):
        """El corte es el mismo techo que ya tenia la pantalla."""
        for dia in range(TOPE_MOVIMIENTOS + 3):
            self.gasto(dias_atras=dia, descripcion='Gasto %d' % dia)
            self.venta(dias_atras=dia, comprador='Cliente %d' % dia)

        self.assertEqual(TOPE_MOVIMIENTOS, len(self.movimientos()))

    def test_el_corte_se_lleva_lo_mas_viejo_no_una_fuente_entera(self):
        """Con el tope lleno de gastos nuevos, una venta vieja no se cuela.

        Es la contracara del test de arriba y la razon por la que el corte es
        por fecha y no un cupo por tipo: un cupo pondria esta venta de hace un
        anio arriba de un gasto de hoy.
        """
        for dia in range(TOPE_MOVIMIENTOS):
            self.gasto(dias_atras=dia, descripcion='Gasto %d' % dia)
        self.venta(dias_atras=365, comprador='Vieja')

        descripciones = [m['descripcion'] for m in self.movimientos()]
        self.assertNotIn('Venta - Vieja', descripciones)
        self.assertEqual(TOPE_MOVIMIENTOS, len(descripciones))

    def test_el_empate_de_fecha_no_cambia_entre_dos_visitas(self):
        """Gasto e Ingreso guardan Date: dos del mismo dia empatan de verdad."""
        self.gasto(dias_atras=0, descripcion='Gasto de hoy')
        self.aporte(dias_atras=0, descripcion='Aporte de hoy')
        self.venta(dias_atras=0, comprador='Camila')

        self.assertEqual([m['descripcion'] for m in self.movimientos()],
                         [m['descripcion'] for m in self.movimientos()])


# =====================================================================
# PARTE 4 - lo que no se toco
# =====================================================================

class TestLoQueNoSeToco(BaseMovimientos):

    def test_los_gastos_y_los_ingresos_siguen_estando(self):
        """La slice suma una fuente; no saca las dos que ya estaban."""
        self.gasto(dias_atras=1, descripcion='Compra de clavos')
        self.aporte(dias_atras=2, descripcion='Aporte de los socios')

        etiquetas = [m['etiqueta'] for m in self.movimientos()]
        self.assertIn('Gasto', etiquetas)
        self.assertIn('Ingreso', etiquetas)

    def test_el_gasto_sigue_siendo_una_salida(self):
        self.gasto(dias_atras=1)

        gasto = [m for m in self.movimientos() if m['etiqueta'] == 'Gasto'][0]
        self.assertEqual('salida', gasto['tipo'])
        self.assertEqual('Compra de mercadería', gasto['categoria'])

    def test_el_aporte_sigue_siendo_una_entrada(self):
        """El aporte de capital no es una venta, pero sigue siendo plata que entro.

        FASE-EVA-S4 lo saco del total de ventas del dashboard; de esta lista
        nunca salio, y esta slice tampoco lo saca.
        """
        self.aporte(dias_atras=1)

        ingreso = [m for m in self.movimientos()
                   if m['etiqueta'] == 'Ingreso'][0]
        self.assertEqual('entrada', ingreso['tipo'])

    def test_la_empresa_de_al_lado_no_se_ve(self):
        """El filtro por empresa no se aflojo al agregar la tercera fuente."""
        otra = Empresa(nombre='Otra SA', capital_invertido=0.0,
                       tasa_costo_capital=10.0, tasa_impuestos=0.30)
        db.session.add(otra)
        db.session.flush()
        canal_ajeno = CanalVenta(empresa_id=otra.id, tipo='manual',
                                 nombre='Mostrador ajeno', activo=True)
        db.session.add(canal_ajeno)
        db.session.commit()
        db.session.add(Pedido(empresa_id=otra.id, canal_id=canal_ajeno.id,
                              fecha_pedido=self._momento(0), estado='open',
                              comprador_nombre='Ajena',
                              total=Decimal('99999.00'),
                              total_envio=Decimal('0.00')))
        db.session.commit()

        self.venta(dias_atras=1, comprador='Camila')

        descripciones = [m['descripcion'] for m in self.movimientos()]
        self.assertIn('Venta - Camila', descripciones)
        self.assertNotIn('Venta - Ajena', descripciones)


if __name__ == '__main__':
    unittest.main()
