# -*- coding: utf-8 -*-
"""Tests de FASE-RESULTADOS-S1 (resultado del negocio: activo vs gasto).

    python -m unittest discover -s tests -v

QUE SE PRUEBA

    la bruta es la de margen     -> utilidad bruta == suma de la ganancia de
                                    los pedidos completos de /reportes/margen
    el origen no decide          -> el mismo gasto pagado con capital o con
                                    facturacion deja la misma utilidad neta
    activo no es gasto           -> una compra de mercaderia no mueve la
                                    utilidad neta, y si las compras del bloque
                                    de activos
    el regalo no es venta        -> fuera de ventas y de CMV, adentro como
                                    costo de marketing
    sin ventas no hay division   -> los porcentajes son guion, no excepcion
    NULL != 0                    -> el pedido incompleto sale de la cuenta y
                                    se lista; el gasto sin clasificar no resta
                                    y se avisa
    el mixto se abre             -> sus partes por cuenta en los subtotales
    las otras pantallas igual    -> caja-socio, caja-general y
                                    resumen-general dan los numeros de antes

Como el resto de la suite, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import template_rendered  # noqa: E402

from app import app  # noqa: E402
from models import (  # noqa: E402
    NATURALEZA_ACTIVO,
    NATURALEZA_GASTO,
    ORIGEN_CAPITAL,
    ORIGEN_FACTURACION,
    ORIGEN_MIXTO,
    CanalVenta,
    Categoria,
    CuentaCobro,
    Empresa,
    Gasto,
    GastoOrigenMixto,
    Ingreso,
    Pedido,
    PedidoItem,
    Producto,
    Usuario,
    db,
)
from rutas_reportes import resultado_del_negocio  # noqa: E402

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


@contextmanager
def contexto_de_plantilla():
    """Lo que la ruta le paso a su plantilla, para afirmar sobre los numeros
    que de verdad llegan a la pantalla y no sobre una cuenta paralela."""
    capturado = {}

    def guardar(sender, template, context, **extra):
        capturado.update(context)

    template_rendered.connect(guardar, app)
    try:
        yield capturado
    finally:
        template_rendered.disconnect(guardar, app)


class BaseResultados(unittest.TestCase):
    """Korvo en chico: dos socios, dos canales, un producto con costo."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        empresa = Empresa(nombre='Empresa Test RESULTADOS-S1')
        db.session.add(empresa)
        db.session.flush()
        self.empresa_id = empresa.id

        usuario = Usuario(nombre='Roman Test', email='resultados1@test.local',
                          empresa_id=self.empresa_id, rol='admin',
                          verificado=True)
        usuario.set_password('irrelevante')
        db.session.add(usuario)

        self.cuenta_roman = CuentaCobro(empresa_id=self.empresa_id,
                                        nombre='Roman - Presencial y Tiendanube',
                                        tipo='mercadopago', socio='roman')
        self.cuenta_nachi = CuentaCobro(empresa_id=self.empresa_id,
                                        nombre='Nachi - Mercado Libre',
                                        tipo='mercadopago', socio='nachi')
        db.session.add_all([self.cuenta_roman, self.cuenta_nachi])

        self.cat_mercaderia = Categoria(nombre='Compra de mercadería',
                                        tipo='gasto',
                                        naturaleza=NATURALEZA_ACTIVO,
                                        empresa_id=self.empresa_id)
        self.cat_publicidad = Categoria(nombre='Publicidad', tipo='gasto',
                                        naturaleza=NATURALEZA_GASTO,
                                        empresa_id=self.empresa_id)
        self.cat_sin_clasificar = Categoria(nombre='Varios', tipo='gasto',
                                            empresa_id=self.empresa_id)
        self.cat_aporte = Categoria(nombre='Aporte de capital (socios)',
                                    tipo='ingreso', empresa_id=self.empresa_id)
        db.session.add_all([self.cat_mercaderia, self.cat_publicidad,
                            self.cat_sin_clasificar, self.cat_aporte])
        db.session.commit()

        self.canal_tn = CanalVenta(empresa_id=self.empresa_id,
                                   tipo='tiendanube', nombre='Korvo',
                                   activo=True, id_tienda_externo='9999',
                                   cuenta_cobro_id=self.cuenta_roman.id)
        self.canal_ml = CanalVenta(empresa_id=self.empresa_id,
                                   tipo='mercadolibre', nombre='Mercado Libre',
                                   activo=True,
                                   cuenta_cobro_id=self.cuenta_nachi.id)
        self.producto = Producto(empresa_id=self.empresa_id, sku='TARJ-N',
                                 nombre='Tarjetero (Negro)',
                                 costo_unitario=Decimal('4000.00'), stock=100)
        db.session.add_all([self.canal_tn, self.canal_ml, self.producto])
        db.session.commit()

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(usuario.id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def venta(self, lineas, canal=None, envio='0.00', envio_vendedor='0.00',
              comision='0.00', regalo=False, fecha=datetime(2026, 9, 10, 12),
              descuentos='0.00'):
        """Un pedido con sus lineas: [(cantidad, precio, costo_snapshot)]."""
        bruto = sum((Decimal(precio) * cantidad
                     for cantidad, precio, _ in lineas), Decimal('0.00'))
        pedido = Pedido(
            empresa_id=self.empresa_id,
            canal_id=(canal or self.canal_tn).id,
            fecha_pedido=fecha, estado='open',
            total_bruto=bruto, total_descuentos=Decimal(descuentos),
            total_envio=Decimal(envio),
            total=bruto - Decimal(descuentos) + Decimal(envio),
            costo_envio_vendedor=(None if envio_vendedor is None
                                  else Decimal(envio_vendedor)),
            comision_plataforma=None if comision is None else Decimal(comision),
            es_regalo=regalo)
        for cantidad, precio, costo in lineas:
            pedido.items.append(PedidoItem(
                producto_id=self.producto.id, descripcion=self.producto.nombre,
                cantidad=cantidad, precio_unitario=Decimal(precio),
                costo_unitario_snapshot=None if costo is None else Decimal(costo),
                subtotal=Decimal(precio) * cantidad))
        db.session.add(pedido)
        db.session.commit()
        return pedido

    def gasto(self, monto, categoria=None, origen=ORIGEN_CAPITAL, cuenta=None,
              fecha=date(2026, 9, 10), partes=None):
        fila = Gasto(empresa_id=self.empresa_id, descripcion='Un gasto',
                     monto=Decimal(monto), fecha=fecha,
                     categoria_id=(categoria or self.cat_publicidad).id,
                     origen_fondo=origen,
                     cuenta_pago_id=cuenta.id if cuenta is not None else None)
        if partes:
            fila.partes_mixto = [
                GastoOrigenMixto(cuenta_cobro_id=c.id, monto=Decimal(m))
                for c, m in partes]
        db.session.add(fila)
        db.session.commit()
        return fila

    def ventas_tipicas(self):
        """Tres ventas completas -- una con flete bonificado, una de varias
        lineas, una de Mercado Libre -- y una incompleta."""
        self.venta([(2, '9000.00', '4000.00')], envio='3000.00',
                   envio_vendedor='3800.00', comision='900.00',
                   descuentos='500.00')
        self.venta([(1, '9000.00', '4000.00'), (3, '7500.00', '4000.00')],
                   comision='0.00')
        self.venta([(1, '8000.00', '4000.00')], canal=self.canal_ml,
                   comision='1200.00')
        self.incompleto = self.venta([(1, '9000.00', '4000.00')], comision=None)

    def margen(self, **params):
        with contexto_de_plantilla() as contexto:
            respuesta = self.client.get('/reportes/margen', query_string=params)
        self.assertEqual(200, respuesta.status_code)
        return contexto, respuesta.get_data(as_text=True)

    def resultado(self, **params):
        return self.margen(**params)[0]['resultado']


# =====================================================================
# La cuenta
# =====================================================================

class TestResultado(BaseResultados):

    def test_utilidad_bruta_coincide_con_suma_de_margen(self):
        """La bruta es la suma de la ganancia de los pedidos completos que
        dibujan los cortes de /reportes/margen -- no otra cuenta."""
        self.ventas_tipicas()

        contexto, _ = self.margen()
        resultado = contexto['resultado']
        suma_margen = sum((fila['ganancia'] for fila in contexto['canales']),
                          Decimal('0.00'))

        self.assertEqual(suma_margen, resultado['utilidad_bruta'])
        # Y las lineas del bloque, sumadas a mano, dan lo mismo.
        #   ventas  = (18000-500) + (9000+22500) + 8000      = 57000
        #   comision = 900 + 0 + 1200                        =  2100
        #   cmv     = 8000 + 16000 + 4000                    = 28000
        #   envios  = 3000 - 3800                            =  -800
        self.assertEqual(Decimal('57000.00'), resultado['ventas_netas'])
        self.assertEqual(Decimal('2100.00'), resultado['comisiones'])
        self.assertEqual(Decimal('28000.00'), resultado['cmv'])
        self.assertEqual(Decimal('-800.00'), resultado['envio_neto'])
        self.assertEqual(Decimal('26100.00'), resultado['utilidad_bruta'])
        self.assertEqual(Decimal('45.8'), resultado['utilidad_bruta_pct'])

    def test_gasto_capital_y_facturacion_restan_igual(self):
        """De que bolsillo salio no decide si resta: resta entero igual."""
        self.ventas_tipicas()
        gasto = self.gasto('5000.00', origen=ORIGEN_CAPITAL)
        con_capital = self.resultado()

        # El id se lee ANTES de tocar el gasto: leerlo despues dispara un
        # autoflush con el origen nuevo y la cuenta todavia vacia, que el
        # CHECK de origen/cuenta rechaza.
        cuenta_id = self.cuenta_roman.id
        gasto.origen_fondo = ORIGEN_FACTURACION
        gasto.cuenta_pago_id = cuenta_id
        db.session.commit()
        con_facturacion = self.resultado()

        self.assertEqual(con_capital['utilidad_neta'],
                         con_facturacion['utilidad_neta'])
        self.assertEqual(Decimal('21100.00'), con_capital['utilidad_neta'])
        # Lo unico que cambia es en que subtotal aparece.
        self.assertEqual(Decimal('5000.00'), con_capital['gastos']['capital'][0])
        self.assertEqual([{'nombre': 'Roman', 'monto': Decimal('5000.00'),
                           'cantidad': 1}],
                         con_facturacion['gastos']['facturacion'])

    def test_compra_de_mercaderia_no_baja_la_utilidad(self):
        """Una compra de stock se capitaliza: no mueve la neta, si los activos."""
        self.ventas_tipicas()
        antes = self.resultado()

        self.gasto('400000.00', categoria=self.cat_mercaderia)
        contexto, _ = self.margen()

        self.assertEqual(antes['utilidad_neta'],
                         contexto['resultado']['utilidad_neta'])
        self.assertEqual(Decimal('400000.00'), contexto['activos']['compras'])

    def test_regalo_no_suma_ventas_ni_cmv(self):
        """El regalo sale de ventas y de CMV; su costo resta como marketing."""
        self.ventas_tipicas()
        antes = self.resultado()

        self.venta([(4, '1.00', '4000.00')], regalo=True)
        contexto, texto = self.margen()
        despues = contexto['resultado']

        self.assertEqual(antes['ventas_netas'], despues['ventas_netas'])
        self.assertEqual(antes['cmv'], despues['cmv'])
        self.assertEqual(antes['utilidad_bruta'], despues['utilidad_bruta'])
        self.assertEqual(Decimal('16000.00'), despues['regalos']['costo'])
        self.assertEqual(antes['utilidad_neta'] - Decimal('16000.00'),
                         despues['utilidad_neta'])
        # Y el resto de /reportes/margen tampoco lo cuenta como venta.
        self.assertEqual(4, contexto['total_pedidos'])

    def test_porcentajes_sin_ventas_no_dividen_por_cero(self):
        """Sin ventas no hay porcentaje: guion, no ZeroDivision."""
        self.gasto('3000.00')

        contexto, texto = self.margen()
        resultado = contexto['resultado']

        self.assertIsNone(resultado['utilidad_bruta_pct'])
        self.assertIsNone(resultado['utilidad_neta_pct'])
        self.assertEqual(Decimal('-3000.00'), resultado['utilidad_neta'])
        self.assertIn('—', texto)

    def test_pedido_incompleto_excluido_y_listado(self):
        """Un pedido sin comision no entra con cero: sale y se nombra."""
        self.ventas_tipicas()

        contexto, texto = self.margen()
        resultado = contexto['resultado']

        self.assertEqual(3, resultado['pedidos'])
        self.assertEqual(1, len(resultado['incompletos']))
        # Su venta de 9000 no esta en ninguna linea.
        self.assertEqual(Decimal('57000.00'), resultado['ventas_netas'])
        self.assertIn('sin todos sus costos cargados', texto)

    def test_mixto_reparte_los_subtotales(self):
        """El gasto mixto abre la parte de cada cuenta; el total lo resta una vez."""
        self.ventas_tipicas()
        self.gasto('68000.00', origen=ORIGEN_MIXTO,
                   partes=[(self.cuenta_roman, '53000.00'),
                           (self.cuenta_nachi, '15000.00')])

        gastos = self.resultado()['gastos']

        self.assertEqual(Decimal('68000.00'), gastos['total'])
        self.assertEqual(Decimal('68000.00'), gastos['mixto_total'])
        self.assertEqual([('Roman', Decimal('53000.00')),
                          ('Nachi', Decimal('15000.00'))],
                         [(fila['nombre'], fila['monto'])
                          for fila in gastos['mixto']])

    def test_gasto_sin_clasificar_no_resta_y_se_avisa(self):
        """NULL no es 'gasto': no se resta a escondidas, se muestra aparte."""
        self.ventas_tipicas()
        antes = self.resultado()

        self.gasto('7000.00', categoria=self.cat_sin_clasificar)
        contexto, texto = self.margen()

        self.assertEqual(antes['utilidad_neta'],
                         contexto['resultado']['utilidad_neta'])
        self.assertEqual((Decimal('7000.00'), 1),
                         contexto['resultado']['sin_clasificar'])
        self.assertIn('sin clasificar', texto)

    def test_el_periodo_filtra_ventas_y_gastos(self):
        """?periodo=AAAA-MM mira solo ese mes, en las dos puntas."""
        self.venta([(1, '9000.00', '4000.00')], fecha=datetime(2026, 8, 5))
        self.venta([(1, '8000.00', '4000.00')], fecha=datetime(2026, 9, 5))
        self.gasto('1000.00', fecha=date(2026, 8, 20))
        self.gasto('2000.00', fecha=date(2026, 9, 20))

        agosto = self.resultado(periodo='2026-08')
        todo = self.resultado()

        self.assertEqual(Decimal('9000.00'), agosto['ventas_netas'])
        self.assertEqual(Decimal('1000.00'), agosto['gastos']['total'])
        self.assertEqual(Decimal('17000.00'), todo['ventas_netas'])
        self.assertEqual(Decimal('3000.00'), todo['gastos']['total'])

    def test_resultado_sin_pasarle_pedidos_da_lo_mismo(self):
        """La funcion sola pide los mismos pedidos que la pantalla."""
        self.ventas_tipicas()
        with app.test_request_context():
            directo = resultado_del_negocio(self.empresa_id)
        self.assertEqual(self.resultado()['utilidad_neta'],
                         directo['utilidad_neta'])


class TestActivos(BaseResultados):

    def test_stock_valuado_y_conciliacion(self):
        """compras = vendido + regalado + deposito, con lo que no tiene costo
        o no lleva stock listado aparte y nunca como cero."""
        db.session.add_all([
            Producto(empresa_id=self.empresa_id, sku='SIN-COSTO',
                     nombre='Sin costo', stock=5),
            Producto(empresa_id=self.empresa_id, sku='COMBO',
                     nombre='Combo', costo_unitario=Decimal('12000.00')),
        ])
        db.session.commit()
        self.venta([(2, '9000.00', '4000.00')])
        self.venta([(1, '1.00', '4000.00')], regalo=True)
        # 100 en deposito + 2 vendidos + 1 regalado = 103 comprados a 4000.
        self.gasto('412000.00', categoria=self.cat_mercaderia)

        activos = self.margen()[0]['activos']

        self.assertEqual(Decimal('400000.00'), activos['stock_valuado'])
        self.assertEqual(['Sin costo'], [f['nombre'] for f in activos['sin_costo']])
        self.assertEqual(['Combo'], [f['nombre'] for f in activos['sin_control']])
        self.assertEqual(Decimal('8000.00'), activos['cmv'])
        self.assertEqual(Decimal('4000.00'), activos['regalos'])
        self.assertEqual(Decimal('0.00'), activos['diferencia'])


# =====================================================================
# Lo que no tenia que moverse
# =====================================================================

class TestLasOtrasPantallas(BaseResultados):

    def test_las_otras_pantallas_no_cambiaron(self):
        """caja-socio, caja-general y resumen-general, con los mismos numeros
        que daban antes de esta slice para el mismo juego de datos.

        Los esperados se calcularon con el codigo de master (081c3d2) sobre
        exactamente estas filas. La naturaleza no entra en ninguna de las
        tres: una compra de mercaderia sigue saliendo de la caja.

            ventas   TN 10000 (com 500) + ML 8000 (com 800); un regalo
            gastos   facturacion Roman 1000 (publicidad)
                     capital 2000 (mercaderia, activo)
                     mixto 3000 = Roman 2000 + Nachi 1000
            ingreso  aporte 50000
        """
        self.venta([(1, '10000.00', '4000.00')], comision='500.00')
        self.venta([(1, '8000.00', '4000.00')], canal=self.canal_ml,
                   comision='800.00')
        self.venta([(1, '1.00', '4000.00')], regalo=True)
        self.gasto('1000.00', origen=ORIGEN_FACTURACION,
                   cuenta=self.cuenta_roman)
        self.gasto('2000.00', categoria=self.cat_mercaderia)
        self.gasto('3000.00', origen=ORIGEN_MIXTO,
                   partes=[(self.cuenta_roman, '2000.00'),
                           (self.cuenta_nachi, '1000.00')])
        db.session.add(Ingreso(empresa_id=self.empresa_id,
                               descripcion='Aporte', monto=Decimal('50000.00'),
                               fecha=date(2026, 9, 1),
                               categoria_id=self.cat_aporte.id))
        db.session.commit()

        with contexto_de_plantilla() as socio:
            self.assertEqual(200, self.client.get('/reportes/caja-socio').status_code)
        por_socio = {fila['clave']: fila for fila in socio['socios']}
        self.assertEqual(Decimal('6500.00'), por_socio['roman']['saldo_real'])
        self.assertEqual(Decimal('3000.00'), por_socio['roman']['gastado'])
        self.assertEqual(Decimal('6200.00'), por_socio['nachi']['saldo_real'])
        self.assertEqual(Decimal('1000.00'), por_socio['nachi']['gastado'])
        self.assertEqual(Decimal('2000.00'), socio['capital_monto'])
        self.assertEqual(1, socio['regalos_totales'])

        with contexto_de_plantilla() as caja:
            self.assertEqual(200, self.client.get('/caja-general').status_code)
        self.assertEqual(Decimal('50000.00'), caja['total_entradas'])
        self.assertEqual(Decimal('6000.00'), caja['total_salidas'])
        self.assertEqual(Decimal('44000.00'), caja['saldo_final'])

        with contexto_de_plantilla() as resumen:
            self.assertEqual(200, self.client.get('/reportes/resumen-general').status_code)
        self.assertEqual(Decimal('18000.00'), resumen['ventas_monto'])
        self.assertEqual(Decimal('1300.00'), resumen['comision_monto'])
        self.assertEqual(Decimal('6000.00'), resumen['gastos_monto'])
        self.assertEqual(Decimal('60700.00'), resumen['posicion_real'])


class TestDashboardYCategorias(BaseResultados):

    def test_gastos_totales_se_abre_por_naturaleza(self):
        self.venta([(1, '10000.00', '4000.00')])
        self.gasto('1000.00')
        self.gasto('2000.00', categoria=self.cat_mercaderia)
        self.gasto('500.00', categoria=self.cat_sin_clasificar)

        texto = self.client.get('/dashboard').get_data(as_text=True)

        self.assertIn('$3500', texto)  # el total no cambia
        self.assertIn('$1000 operativos', texto)
        self.assertIn('$2000 en compras de stock', texto)
        self.assertIn('$500 sin clasificar', texto)

    def test_nueva_categoria_guarda_la_naturaleza(self):
        self.client.post('/nueva-categoria', data={
            'nombre': 'Flete de compra', 'tipo': 'gasto',
            'naturaleza': NATURALEZA_ACTIVO})
        self.client.post('/nueva-categoria', data={
            'nombre': 'Ventas varias', 'tipo': 'ingreso',
            'naturaleza': NATURALEZA_ACTIVO})

        flete = Categoria.query.filter_by(nombre='Flete de compra').one()
        ingreso = Categoria.query.filter_by(nombre='Ventas varias').one()
        self.assertEqual(NATURALEZA_ACTIVO, flete.naturaleza)
        self.assertIsNone(ingreso.naturaleza, 'un ingreso no lleva naturaleza')

    def test_editar_categoria_cambia_la_naturaleza(self):
        self.client.post('/categoria/editar/%d' % self.cat_sin_clasificar.id,
                         data={'nombre': 'Varios',
                               'naturaleza': NATURALEZA_GASTO})
        db.session.expire_all()
        self.assertEqual(NATURALEZA_GASTO,
                         db.session.get(Categoria,
                                        self.cat_sin_clasificar.id).naturaleza)

    def test_naturaleza_invalida_no_se_guarda(self):
        with self.assertRaises(ValueError):
            Categoria(nombre='X', tipo='gasto', naturaleza='Activo',
                      empresa_id=self.empresa_id)


if __name__ == '__main__':
    unittest.main()
