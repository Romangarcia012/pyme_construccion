# -*- coding: utf-8 -*-
"""Tests de FASE-AUDITORIA-EXCEL-S3.

    python -m unittest discover -s tests -v

Cuatro cosas independientes que salieron de mirar la app con Roman:

  1. El envio del pedido #100 costo 8.431 y Tiendanube dice 7.630. La
     correccion se hace a mano en la base, y lo que se prueba aca NO es que
     8.431 sea el numero correcto -- eso lo sabe Roman, no el codigo -- sino
     que el proximo sync lo PISA. Es un test que documenta un comportamiento
     incomodo a proposito: si algun dia el sync deja de pisarlo, este test se
     pone rojo y obliga a decidirlo, en vez de que la correccion sobreviva o
     se pierda por accidente.

  2. Las ventas estaban cargadas dos veces: como Pedido (que alimenta
     caja-socio y el reporte de margen) y otra vez como Ingreso a mano, al
     estilo del Excel. Se borraron los Ingreso. Lo que se prueba es la regla
     que queda: Ingreso no comparte un peso con las ventas.

  3. El formulario de alta de ingreso decia "Guardar Gasto" -- copiado del de
     gasto y nunca actualizado.

  4. Los totales de /gasto/listar y /ingreso/listar daban $0.00 con filas
     reales cargadas. La causa era el scope de Jinja, no un filtro ni un SUM:
     un `set` de acumulacion DENTRO del `for` escribe una variable local al
     bloque, asi que la suma se perdia en cada vuelta y lo que se imprimia
     abajo era el 0 de la linea de arriba. El arreglo mueve la suma a la ruta;
     estos tests la miran en la PANTALLA, no en la ruta, que es donde el bug
     se veia.

Nada sale a internet ni toca la base real: setUpModule repunta la app a SQLite
en memoria.
"""

import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_tiendanube  # noqa: E402
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

ENGINE_PRODUCTIVO = None

# Los numeros del caso real, para que el test hable del mismo pedido que Roman.
ENVIO_QUE_REPORTA_TIENDANUBE = Decimal('7630.00')
ENVIO_QUE_COSTO_DE_VERDAD = Decimal('8431.00')


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


class BaseExcelS3(unittest.TestCase):
    """Una empresa, un usuario logueado, la cuenta de Roman y los dos canales."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test AUDITORIA-EXCEL-S3')
        db.session.add(self.empresa)
        db.session.flush()
        self.empresa_id = self.empresa.id

        self.usuario = Usuario(nombre='Roman Test',
                               email='auditoriaexcel3@test.local',
                               empresa_id=self.empresa_id, rol='admin',
                               verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)
        db.session.flush()
        self.usuario_id = self.usuario.id

        self.cuenta_roman = CuentaCobro(empresa_id=self.empresa_id,
                                        nombre='Roman - Presencial y Tiendanube',
                                        tipo='mercadopago', socio='roman')
        db.session.add(self.cuenta_roman)
        db.session.flush()

        self.canal_tn = CanalVenta(empresa_id=self.empresa_id, tipo='tiendanube',
                                   nombre='Tiendanube', activo=True,
                                   id_tienda_externo='8078725',
                                   cuenta_cobro_id=self.cuenta_roman.id)
        self.canal_manual = CanalVenta(empresa_id=self.empresa_id, tipo='manual',
                                       nombre='Manual', activo=True,
                                       cuenta_cobro_id=self.cuenta_roman.id)
        db.session.add_all([self.canal_tn, self.canal_manual])

        self.cat_gasto = Categoria(nombre='Materiales', tipo='gasto',
                                   empresa_id=self.empresa_id)
        self.cat_capital = Categoria(nombre='Aporte de capital (socios)',
                                     tipo='ingreso', empresa_id=self.empresa_id)
        db.session.add_all([self.cat_gasto, self.cat_capital])
        db.session.commit()

        self.id_canal_tn = self.canal_tn.id
        self.id_canal_manual = self.canal_manual.id
        self.id_cat_gasto = self.cat_gasto.id
        self.id_cat_capital = self.cat_capital.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers ---------------------------------------------------------

    def gasto(self, monto, descripcion='Gasto sembrado', fecha=date(2026, 9, 1)):
        fila = Gasto(descripcion=descripcion, monto=Decimal(monto), fecha=fecha,
                     empresa_id=self.empresa_id, usuario_id=self.usuario_id,
                     categoria_id=self.id_cat_gasto)
        db.session.add(fila)
        db.session.commit()
        return fila

    def ingreso(self, monto, descripcion='Ingreso sembrado',
                fecha=date(2026, 9, 1), categoria_id=None):
        fila = Ingreso(descripcion=descripcion, monto=Decimal(monto), fecha=fecha,
                       empresa_id=self.empresa_id, usuario_id=self.usuario_id,
                       categoria_id=categoria_id or self.id_cat_capital)
        db.session.add(fila)
        db.session.commit()
        return fila

    def pedido(self, total, canal_id=None, **extra):
        fila = Pedido(empresa_id=self.empresa_id,
                      canal_id=canal_id or self.id_canal_manual,
                      fecha_pedido=datetime(2026, 9, 4, 0, 0, 0),
                      estado='pagado', total=Decimal(total),
                      total_bruto=Decimal(total), **extra)
        db.session.add(fila)
        db.session.commit()
        return fila


# ============================================================================
# PARTE 1 - la correccion del envio del pedido #100
# ============================================================================


class TestEnvioDelVendedorCorregidoAMano(BaseExcelS3):
    """Que le pasa a una correccion manual de costo_envio_vendedor.

    El caso real: Tiendanube reporto merchant_cost 7.630 para el pedido #100 y
    el flete costo 8.431. La correccion se escribe a mano en la columna.
    """

    PAYLOAD_100 = {
        'id': '2058709648',
        'number': '100',
        'created_at': '2026-08-31T17:55:12+0000',
        'status': 'closed',
        'payment_status': 'paid',
        'currency': 'ARS',
        'subtotal': '7490.00',
        'discount': '1423.10',
        'total': '13696.90',
        'customer': {'id': 1, 'name': 'Camila Valaco'},
        'products': [],
        # El nodo del que sale el envio desde que Tiendanube saco las
        # propiedades viejas del recurso Order (2025/04/24).
        'fulfillments': [{
            'id': '01M1CFC0HPCP82Z1DSGRAAFANV',
            'shipping': {
                'merchant_cost': {'value': 7630, 'currency': 'ARS'},
                'consumer_cost': {'value': 7630, 'currency': 'ARS'},
            },
        }],
    }

    def _resincronizar(self):
        """Corre el mismo upsert que corre el sync, con el payload de siempre.

        Se llama al upsert directo -- no a la corrida entera -- para no tener
        que fingir la API: lo que se quiere ver es el unico paso que toca la
        columna.
        """
        from ingestor_tiendanube import IngestorTiendanube

        canal = db.session.get(CanalVenta, self.id_canal_tn)
        ingestor = IngestorTiendanube.__new__(IngestorTiendanube)
        datos = ingestor._normalizar_pedido(self.PAYLOAD_100)
        sync_tiendanube._upsert_pedido(canal, datos, self.PAYLOAD_100, {})
        db.session.commit()

    def test_la_correccion_se_guarda(self):
        """Escribir 8.431 en la columna queda. Es una columna comun."""
        pedido = self.pedido('13696.90', canal_id=self.id_canal_tn,
                             id_externo='2058709648', numero_externo='100',
                             total_envio=ENVIO_QUE_REPORTA_TIENDANUBE,
                             costo_envio_vendedor=ENVIO_QUE_REPORTA_TIENDANUBE)
        pedido.costo_envio_vendedor = ENVIO_QUE_COSTO_DE_VERDAD
        db.session.commit()

        self.assertEqual(db.session.get(Pedido, pedido.id).costo_envio_vendedor,
                         ENVIO_QUE_COSTO_DE_VERDAD)

    def test_costo_envio_vendedor_corregido_no_se_resincroniza_solo(self):
        """El proximo sync PISA la correccion. Documentado, no deseado.

        El upsert asigna `costo_envio_vendedor` sin condicion desde el payload,
        y el payload de este pedido trae merchant_cost 7.630. O sea: la
        correccion a mano dura hasta la proxima corrida del sync sobre este
        pedido, y despues vuelve sola al numero de Tiendanube.

        Si este test se pone rojo es porque alguien hizo que el sync respete lo
        cargado a mano. Eso seria una mejora -- pero hay que decirlo en el
        codigo y cambiar este test, no descubrirlo en la base.
        """
        self.pedido('13696.90', canal_id=self.id_canal_tn,
                    id_externo='2058709648', numero_externo='100',
                    total_envio=ENVIO_QUE_REPORTA_TIENDANUBE,
                    costo_envio_vendedor=ENVIO_QUE_COSTO_DE_VERDAD)

        self._resincronizar()

        pedido = Pedido.query.filter_by(id_externo='2058709648').one()
        self.assertEqual(pedido.costo_envio_vendedor,
                         ENVIO_QUE_REPORTA_TIENDANUBE,
                         'El sync dejo de pisar la correccion manual: si el '
                         'cambio es a proposito, actualizar este test y el '
                         'comentario del upsert en sync_tiendanube.')

    def test_total_envio_no_se_toca_al_corregir(self):
        """Lo que pago el cliente no cambia porque al vendedor le salio mas caro.

        Son dos montos distintos sobre la misma venta: total_envio es plata que
        ENTRA (consumer_cost), costo_envio_vendedor es plata que SALE
        (merchant_cost). Corregir el segundo no le da derecho a nadie a tocar
        el primero.
        """
        pedido = self.pedido('13696.90', canal_id=self.id_canal_tn,
                             id_externo='2058709648', numero_externo='100',
                             total_envio=ENVIO_QUE_REPORTA_TIENDANUBE,
                             costo_envio_vendedor=ENVIO_QUE_REPORTA_TIENDANUBE)
        pedido.costo_envio_vendedor = ENVIO_QUE_COSTO_DE_VERDAD
        db.session.commit()

        self.assertEqual(db.session.get(Pedido, pedido.id).total_envio,
                         ENVIO_QUE_REPORTA_TIENDANUBE)


# ============================================================================
# PARTE 2 - las ventas viven en Pedido, no en Ingreso
# ============================================================================


class TestCajaGeneralSinVentas(BaseExcelS3):
    """Los dos libros no comparten un peso.

    Antes de esta slice cada venta estaba en los dos: como Pedido (de donde
    salen caja-socio y el margen) y otra vez como Ingreso tipeado a mano, al
    estilo del Excel. Sumar los dos contaba la misma plata dos veces.
    """

    def test_caja_general_ya_no_incluye_ventas(self):
        """Sumar Ingreso.monto no toca ningun monto de venta.

        Se siembra el estado que queda despues del borrado: las ventas como
        Pedido, y en Ingreso unicamente el aporte de capital.
        """
        self.pedido('97500.00')
        self.pedido('84627.70')
        self.pedido('13696.90', canal_id=self.id_canal_tn,
                    id_externo='2058709648', numero_externo='100')
        capital = self.ingreso('2371037.83', 'Aporte de capital (socios)')

        ingresos = Ingreso.query.filter_by(empresa_id=self.empresa_id).all()
        total_ingresos = sum((i.monto for i in ingresos), Decimal('0.00'))

        self.assertEqual(total_ingresos, capital.monto)

        totales_de_venta = {p.total for p in
                            Pedido.query.filter_by(empresa_id=self.empresa_id)}
        for fila in ingresos:
            self.assertNotIn(fila.monto, totales_de_venta,
                             'Un Ingreso volvio a duplicar una venta: las '
                             'ventas van en Pedido y solo ahi.')

    def test_ningun_ingreso_es_de_categoria_venta(self):
        """La regla, dicha sobre la etiqueta y no sobre el monto.

        Dos ventas distintas pueden dar el mismo monto y hacer pasar el test de
        arriba por casualidad; la categoria es lo que declara la intencion.
        """
        self.ingreso('2371037.83', 'Aporte de capital (socios)')

        etiquetas = {
            (f.categoria.nombre if f.categoria else None)
            for f in Ingreso.query.filter_by(empresa_id=self.empresa_id)
        }
        self.assertNotIn('Venta', etiquetas)

    def test_el_aporte_de_capital_sobrevive(self):
        """El unico Ingreso que queda no es una venta y tiene que seguir ahi.

        Sin el, el saldo de caja-general arrancaria en el pozo de la mercaderia
        inicial y no en 0.
        """
        self.ingreso('2371037.83', 'Aporte de capital (socios)')

        resp = self.client.get('/caja-general')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Aporte de capital (socios)', resp.get_data(as_text=True))


# ============================================================================
# PARTE 3 - el formulario de ingreso decia "Gasto"
# ============================================================================


class TestTextoDelFormularioDeIngreso(BaseExcelS3):

    def test_boton_nuevo_ingreso_dice_ingreso(self):
        """El submit de /ingreso/nuevo no puede hablar de gastos."""
        cuerpo = self.client.get('/ingreso/nuevo').get_data(as_text=True)

        self.assertIn('Guardar Ingreso', cuerpo)
        self.assertNotIn('Guardar Gasto', cuerpo)

    def test_titulo_nuevo_ingreso_dice_ingreso(self):
        """El title venia copiado del formulario de gasto."""
        cuerpo = self.client.get('/ingreso/nuevo').get_data(as_text=True)

        self.assertIn('Agregar Ingreso - PYME', cuerpo)
        self.assertNotIn('Agregar Gasto - PYME', cuerpo)


# ============================================================================
# PARTE 4 - los totales de los listados daban 0
# ============================================================================


class TestTotalesDeLosListados(BaseExcelS3):
    """El total impreso tiene que ser la suma de las filas impresas.

    Los montos de cada caso son distintos entre si y ninguno es la suma de
    otros dos: asi un total que se quede con una sola fila -- el otro final
    posible del bug de scope -- tampoco pasa.
    """

    def test_total_gastos_coincide_con_la_suma_real(self):
        for monto in ('45000.00', '33000.00', '15975.16'):
            self.gasto(monto, 'Gasto de ' + monto)

        cuerpo = self.client.get('/gasto/listar').get_data(as_text=True)

        self.assertIn('Total Gastos: $93975.16', cuerpo)
        self.assertNotIn('Total Gastos: $0.00', cuerpo)

    def test_total_ingresos_coincide_con_la_suma_real(self):
        for monto in ('2371037.83', '1200.50', '77.25'):
            self.ingreso(monto, 'Ingreso de ' + monto)

        cuerpo = self.client.get('/ingreso/listar').get_data(as_text=True)

        self.assertIn('Total Ingresos: $2372315.58', cuerpo)
        self.assertNotIn('Total Ingresos: $0.00', cuerpo)

    def test_total_gastos_con_una_sola_fila(self):
        """La pantalla que Roman ve al empezar un mes, con una sola fila."""
        self.gasto('61500.00', 'Piedra que se seca')

        cuerpo = self.client.get('/gasto/listar').get_data(as_text=True)

        self.assertIn('Total Gastos: $61500.00', cuerpo)

    def test_el_total_respeta_el_filtro_de_fechas(self):
        """El total suma lo que se ve, no la tabla entera.

        Mover la suma a la ruta la puso al lado del filtro: si algun dia una de
        las dos deja de aplicarlo, la pantalla mostraria tres filas y el total
        de cinco.
        """
        self.gasto('1000.00', 'Dentro del rango', fecha=date(2026, 9, 1))
        self.gasto('2000.00', 'Dentro del rango', fecha=date(2026, 9, 2))
        self.gasto('9999.00', 'Fuera del rango', fecha=date(2026, 12, 25))

        cuerpo = self.client.get(
            '/gasto/listar?fecha_inicio=2026-09-01&fecha_fin=2026-09-30'
        ).get_data(as_text=True)

        self.assertIn('Total Gastos: $3000.00', cuerpo)
        self.assertNotIn('9999.00', cuerpo)

    def test_sin_filas_no_hay_total_ni_error(self):
        """La lista vacia cae en el else de la plantilla y no revienta."""
        resp = self.client.get('/gasto/listar')

        self.assertEqual(resp.status_code, 200)
        self.assertIn('No hay gastos registrados', resp.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
