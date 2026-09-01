"""Tests de la FASE2-S1 (modelo de datos del ecommerce).

Corren contra la base real definida en DATABASE_URL, pero SIN dejar nada:
cada test que escribe lo hace dentro de una transaccion que se revierte al
terminar. Los tests de solo lectura no escriben nada.

    python -m unittest discover -s tests -v

Se usa unittest (stdlib) a proposito: la slice no puede agregar dependencias
nuevas a requirements.txt y pytest no esta instalado.
"""

import json
import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import app
from models import (
    CanalVenta,
    CuentaCobro,
    Empresa,
    MovimientoCuenta,
    Pago,
    Pedido,
    PedidoItem,
    Producto,
)

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(RAIZ, 'tests', 'baseline_pre_fase2_s1.json')

TABLAS_VIEJAS = ['empresa', 'usuario', 'categoria', 'historial', 'gasto', 'ingreso']


class BaseTransaccional(unittest.TestCase):
    """Abre una transaccion por test y la revierte al final.

    Todo lo que el test escriba (empresas, pedidos, pagos de prueba) muere
    con el rollback. La base productiva queda igual que antes.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        from models import db
        self.conn = db.engine.connect()
        self.trans = self.conn.begin()
        self.s = Session(bind=self.conn)

    def tearDown(self):
        self.s.close()
        self.trans.rollback()
        self.conn.close()
        self.ctx.pop()

    def _empresa_de_prueba(self):
        e = Empresa(nombre='EMPRESA TEST FASE2-S1')
        self.s.add(e)
        self.s.flush()
        return e


class TestPlataNoSeCalculaConFloat(BaseTransaccional):
    """Por que Float rompia la conciliacion.

    Conciliar es comparar dos importes por igualdad: lo que el canal dice
    que cobro contra lo que realmente entro a la cuenta. Con Float, un
    cobro de 61499.98 menos una comision de 4304.99 da 57194.990000000005
    en vez de 57194.99, y esa diferencia de 5e-12 convierte un pago
    perfectamente conciliado en una discrepancia. Con Numeric(14,2) ->
    Decimal la resta da 0 exacto.
    """

    def test_float_efectivamente_deriva_con_estos_importes(self):
        # Esto es lo que pasaba antes. Se deja explicito para que quede
        # claro que los importes elegidos no son un caso de laboratorio:
        # son el bruto, la comision y el neto del pago que arma el test
        # siguiente.
        self.assertNotEqual(61499.98 - 4304.99, 57194.99)
        self.assertEqual(Decimal('61499.98') - Decimal('4304.99'),
                         Decimal('57194.99'))

    def test_total_del_pedido_y_del_pago_cierran_en_decimal_exacto(self):
        empresa = self._empresa_de_prueba()

        canal = CanalVenta(empresa_id=empresa.id, tipo='tiendanube',
                           nombre='Tiendanube (test)', activo=False)
        cuenta = CuentaCobro(empresa_id=empresa.id, nombre='MP test',
                             tipo='mercadopago', metodo_ingesta='api')
        producto = Producto(empresa_id=empresa.id, sku='SKU-TEST-1',
                            nombre='Bolsa de cemento 50kg',
                            costo_unitario=Decimal('12500.00'),
                            precio_lista=Decimal('19999.99'))
        self.s.add_all([canal, cuenta, producto])
        self.s.flush()

        envio = Decimal('1500.01')
        pedido = Pedido(
            empresa_id=empresa.id, canal_id=canal.id, id_externo='TEST-0001',
            fecha_pedido=datetime(2026, 8, 20, 10, 30), estado='pagado',
            total_bruto=Decimal('59999.97'), total_envio=envio,
            total=Decimal('61499.98'),
        )
        self.s.add(pedido)
        self.s.flush()

        item = PedidoItem(
            pedido_id=pedido.id, producto_id=producto.id,
            descripcion='Bolsa de cemento 50kg', cantidad=3,
            precio_unitario=Decimal('19999.99'),
            costo_unitario_snapshot=Decimal('12500.00'),
            subtotal=Decimal('59999.97'),
        )
        pago = Pago(
            pedido_id=pedido.id, canal_id=canal.id, cuenta_cobro_id=cuenta.id,
            procesador='mercadopago', id_externo='MP-TEST-0001',
            estado='aprobado', fecha_pago=datetime(2026, 8, 20, 10, 31),
            monto_bruto=Decimal('61499.98'), comision=Decimal('4304.99'),
            monto_neto=Decimal('57194.99'),
        )
        self.s.add_all([item, pago])
        self.s.flush()
        self.s.expire_all()   # fuerza releer desde Postgres, no desde memoria

        item = self.s.get(PedidoItem, item.id)
        pago = self.s.get(Pago, pago.id)
        pedido = self.s.get(Pedido, pedido.id)

        # Vuelven de la base como Decimal, no como float.
        for valor in (item.precio_unitario, item.subtotal, pago.monto_bruto,
                      pago.comision, pago.monto_neto, pedido.total):
            self.assertIsInstance(valor, Decimal)

        # El item reconstruye su subtotal sin arrastre.
        self.assertEqual(item.precio_unitario * item.cantidad, Decimal('59999.97'))

        # Items + envio == total del pedido, exacto.
        self.assertEqual(item.subtotal + pedido.total_envio, pedido.total)

        # Y el pago cierra contra el pedido: diferencia CERO, no "casi cero".
        self.assertEqual(pago.monto_bruto - pedido.total, Decimal('0'))
        self.assertEqual(pago.monto_bruto - pago.comision, pago.monto_neto)

        # El costo quedo congelado en el item. Si maniana sube el costo del
        # producto, el margen de este pedido no se reescribe.
        producto.costo_unitario = Decimal('15000.00')
        self.s.flush()
        self.s.expire_all()
        self.assertEqual(self.s.get(PedidoItem, item.id).costo_unitario_snapshot,
                         Decimal('12500.00'))


class TestDedupDeMovimientos(BaseTransaccional):
    """hash_dedup UNIQUE es la defensa contra reimportar un extracto.

    Si la corrida de polling se solapa o alguien repite un rango de fechas,
    el mismo movimiento llega dos veces. Sin la UNIQUE, el saldo de la
    cuenta se duplica en silencio.
    """

    def _movimiento(self, cuenta_id, hash_dedup, id_externo=None):
        return MovimientoCuenta(
            cuenta_id=cuenta_id, fecha=datetime(2026, 8, 20, 11, 0),
            tipo='cobro', descripcion='Cobro pedido TEST-0001',
            monto=Decimal('61499.98'), hash_dedup=hash_dedup,
            id_externo_procesador=id_externo,
        )

    def test_reimportar_el_mismo_movimiento_es_rechazado_por_la_base(self):
        empresa = self._empresa_de_prueba()
        cuenta = CuentaCobro(empresa_id=empresa.id, nombre='MP test',
                             tipo='mercadopago', metodo_ingesta='api')
        self.s.add(cuenta)
        self.s.flush()

        huella = 'a' * 64
        self.s.add(self._movimiento(cuenta.id, huella, 'MP-MOV-1'))
        self.s.flush()

        # Segunda pasada del mismo movimiento: misma huella, otro id.
        self.s.add(self._movimiento(cuenta.id, huella, 'MP-MOV-2'))
        with self.assertRaises(IntegrityError):
            self.s.flush()

    def test_dos_movimientos_distintos_conviven(self):
        empresa = self._empresa_de_prueba()
        cuenta = CuentaCobro(empresa_id=empresa.id, nombre='MP test',
                             tipo='mercadopago', metodo_ingesta='api')
        self.s.add(cuenta)
        self.s.flush()

        self.s.add(self._movimiento(cuenta.id, 'a' * 64, 'MP-MOV-1'))
        self.s.add(self._movimiento(cuenta.id, 'b' * 64, 'MP-MOV-2'))
        self.s.flush()   # no debe levantar nada

        total = self.s.query(MovimientoCuenta).filter_by(cuenta_id=cuenta.id).count()
        self.assertEqual(total, 2)


class TestLaMigracionNoTocoDatosExistentes(unittest.TestCase):
    """La promesa "esta slice es puramente aditiva", verificada.

    El baseline de tests/baseline_pre_fase2_s1.json se capturo en vivo
    contra Supabase justo antes de correr `flask db upgrade`. Si la
    migracion hubiera borrado, insertado o duplicado una sola fila de las 6
    tablas originales, este test lo marca.
    """

    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        with open(BASELINE, encoding='utf-8') as fh:
            cls.baseline = json.load(fh)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def _contar(self, tabla):
        from sqlalchemy import text
        from models import db
        with db.engine.connect() as conn:
            return conn.execute(text('select count(*) from "%s"' % tabla)).scalar()

    def test_las_6_tablas_originales_tienen_las_mismas_filas_que_antes(self):
        esperado = self.baseline['conteos']
        for tabla in TABLAS_VIEJAS:
            with self.subTest(tabla=tabla):
                self.assertEqual(
                    self._contar(tabla), esperado[tabla],
                    'la tabla %s cambio de cantidad de filas al migrar' % tabla,
                )

    def test_la_migracion_de_esta_slice_esta_aplicada(self):
        from sqlalchemy import text
        from models import db
        with db.engine.connect() as conn:
            actual = conn.execute(text('select version_num from alembic_version')).scalar()
        self.assertEqual(actual, '4a3c449fc7b6')

    def test_los_dos_canales_semilla_existen_y_estan_inactivos(self):
        from models import db
        with app.app_context():
            canales = db.session.query(CanalVenta).order_by(CanalVenta.tipo).all()
        tipos = sorted({c.tipo for c in canales})
        self.assertEqual(tipos, ['mercadolibre', 'tiendanube'])
        for c in canales:
            self.assertFalse(c.activo, 'el canal %s no deberia estar activo sin credenciales' % c.tipo)


class TestRutasViejasSiguenVivas(unittest.TestCase):
    """Regresion: agregar 15 tablas y 5 columnas a gasto no puede voltear
    las pantallas que Roman ya usa todos los dias."""

    @classmethod
    def setUpClass(cls):
        app.config['WTF_CSRF_ENABLED'] = False
        cls.client = app.test_client()
        cls.ctx = app.app_context()
        cls.ctx.push()
        from models import Usuario, db
        cls.usuario_id = db.session.query(Usuario.id).order_by(Usuario.id).limit(1).scalar()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def setUp(self):
        if self.usuario_id is None:
            self.skipTest('no hay usuarios en la base para probar las rutas')
        # Se inicia sesion escribiendo la cookie de flask-login directamente:
        # el test no conoce (ni tiene por que conocer) la password real.
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def test_dashboard_responde_200(self):
        self.assertEqual(self.client.get('/dashboard').status_code, 200)

    def test_listar_gastos_responde_200(self):
        self.assertEqual(self.client.get('/gasto/listar').status_code, 200)

    def test_listar_ingresos_responde_200(self):
        self.assertEqual(self.client.get('/ingreso/listar').status_code, 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
