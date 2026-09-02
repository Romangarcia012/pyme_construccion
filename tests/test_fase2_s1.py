"""Tests de la FASE2-S1 (modelo de datos del ecommerce).

Corren contra la base real definida en DATABASE_URL, pero SIN dejar nada:
cada test que escribe lo hace dentro de una transaccion que se revierte al
terminar. Los tests de solo lectura no escriben nada.

    python -m unittest discover -s tests -v

Se usa unittest (stdlib) a proposito: la slice no puede agregar dependencias
nuevas a requirements.txt y pytest no esta instalado.
"""

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

MIGRACIONES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'migrations')

TABLAS_VIEJAS = ['empresa', 'usuario', 'categoria', 'historial', 'gasto', 'ingreso']

# Todas las claves foraneas que salen de las 6 tablas originales:
# (tabla, columna, tabla a la que apunta). `empresa` no tiene ninguna, es la
# raiz del arbol. `gasto.cuenta_pago_id` apunta a una tabla de FASE2-S1, pero
# la columna vive en una tabla vieja y ya dio problemas una vez (el select de
# cuenta huerfana que arreglo el commit 399f5ef), asi que entra igual.
#
# Los nombres de esta lista se interpolan en el SQL de mas abajo. Son
# constantes de este modulo, no entra nada de afuera.
FKS_TABLAS_VIEJAS = [
    ('usuario',   'empresa_id',     'empresa'),
    ('categoria', 'usuario_id',     'usuario'),
    ('gasto',     'usuario_id',     'usuario'),
    ('gasto',     'categoria_id',   'categoria'),
    ('gasto',     'cuenta_pago_id', 'cuenta_cobro'),
    ('ingreso',   'usuario_id',     'usuario'),
    ('ingreso',   'categoria_id',   'categoria'),
    ('historial', 'usuario_id',     'usuario'),
]


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


class TestEstadoDeLaBaseYDeLosCanales(unittest.TestCase):
    """Que la base este al dia y que los canales sean coherentes.

    La clase se llamaba TestLaMigracionNoTocoDatosExistentes, por el test de
    conteos que ya no esta (ver el comentario de abajo). Sus dos tests
    sobrevivientes tambien congelaban estado puntual de produccion -- un hash
    de migracion escrito a mano y la lista exacta de canales sembrados -- y
    quedaron reescritos como invariantes: la base esta en la ultima migracion
    del repo, sea cual sea, y ningun canal esta prendido sin credencial.
    """

    # ------------------------------------------------------------------
    # RETIRADO: test_las_6_tablas_originales_tienen_las_mismas_filas_que_antes
    #
    # Que probaba: que aplicar la migracion 4a3c449fc7b6 (FASE2-S1, agosto/
    # septiembre de 2026) no borro, inserto ni duplico una sola fila de las 6
    # tablas que ya existian -- empresa, usuario, categoria, historial, gasto,
    # ingreso. Comparaba el conteo exacto de cada una contra
    # tests/baseline_pre_fase2_s1.json, capturado en vivo contra Supabase
    # inmediatamente antes de correr `flask db upgrade`.
    #
    # Cumplio su funcion: la slice se verifico aditiva en su momento y eso
    # quedo asentado en los commits de FASE2-S1 y en el JSON del baseline, que
    # se conserva como registro historico.
    #
    # Por que se retira: era una verificacion de un evento puntual disfrazada
    # de chequeo de regresion permanente. Los conteos son de la base VIVA, y
    # esas tablas crecen con el uso normal de la app -- historial suma una fila
    # por cada accion de Roman, y gasto/ingreso van a crecer apenas cargue
    # datos de verdad. Empezo a fallar en rojo sin que hubiera ninguna
    # regresion: la app simplemente se uso. Un test que se pone rojo por el uso
    # esperado del sistema no informa nada y entrena a ignorar la suite.
    #
    # Lo reemplaza TestIntegridadReferencial, mas abajo: invariantes que tienen
    # que valer sobre la base viva sin importar cuanto crezca.
    # ------------------------------------------------------------------

    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def test_la_base_esta_en_la_ultima_migracion_del_repo(self):
        """alembic_version de Supabase == la cabeza de migrations/versions/.

        Antes esto comparaba contra el hash de FASE2-S1 escrito a mano, o sea
        que congelaba "cual fue la ultima slice que corrio" y se iba a poner en
        rojo apenas alguna agregara una migracion nueva. Lo que importa no es
        cual es la revision sino si la base esta al dia con el repo, y eso se
        sigue cumpliendo pase lo que pase. La cabeza la resuelve Alembic
        leyendo migrations/versions/, asi que el test se actualiza solo.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from sqlalchemy import text
        from models import db

        config = Config(os.path.join(MIGRACIONES, 'alembic.ini'))
        config.set_main_option('script_location', MIGRACIONES)
        cabezas = ScriptDirectory.from_config(config).get_heads()

        # Mas de una cabeza es un arbol de migraciones ramificado: `flask db
        # upgrade` no sabria cual aplicar. Va aparte para que el mensaje diga
        # eso y no "la base esta atrasada", que seria enganioso.
        self.assertEqual(
            len(cabezas), 1,
            'migrations/versions tiene %d cabezas (%s): el arbol se ramifico'
            % (len(cabezas), ', '.join(cabezas)))

        with db.engine.connect() as conn:
            aplicada = conn.execute(
                text('select version_num from alembic_version')).scalar()

        self.assertEqual(
            aplicada, cabezas[0],
            'la base esta en la revision %s y la ultima del repo es %s: falta '
            'correr `flask db upgrade`' % (aplicada, cabezas[0]))

    def test_canal_activo_y_credencial_viva_van_siempre_juntos(self):
        """Un canal prendido tiene token, y uno apagado no deja token vivo.

        Este test venia afirmando ademas cuales eran los canales que existian
        ('mercadolibre' y 'tiendanube', exactamente esos dos) y en que estado
        estaba cada uno. Eso es una foto de produccion en un momento dado: se
        rompe si Roman conecta un canal mas, o si desconecta Tiendanube un
        rato. De hecho ya se habia tenido que reescribir una vez, cuando
        FASE3-S1 conecto Tiendanube de verdad y el "los dos estan inactivos"
        original dejo de valer.

        Lo que queda es la regla que si vale con cualquier cantidad de canales
        y en cualquier combinacion de estados, en los dos sentidos:

            canal activo    -> tiene que haber credencial activa con token
            canal inactivo  -> no puede quedar credencial activa

        Las dos mitades importan. Un canal prendido sin token rompe el sync en
        silencio; un canal apagado que se quedo con la credencial viva es un
        token que sigue existiendo sin que nadie lo use, que es justo lo que
        una desconexion tendria que haber dado de baja.
        """
        from models import CredencialCanal, db
        # Contexto propio: garantiza sesion nueva y no arrastra nada que haya
        # quedado en la de la clase.
        with app.app_context():
            canales = db.session.query(CanalVenta).order_by(CanalVenta.id).all()

            for canal in canales:
                with self.subTest(canal=canal.tipo, canal_id=canal.id):
                    credencial = (db.session.query(CredencialCanal)
                                  .filter_by(canal_id=canal.id, activo=True)
                                  .first())
                    if canal.activo:
                        self.assertIsNotNone(
                            credencial, 'el canal %s esta activo sin credencial' % canal.tipo)
                        self.assertTrue(credencial.access_token_cifrado,
                                        'el canal %s no tiene token guardado' % canal.tipo)
                    else:
                        self.assertIsNone(
                            credencial, 'el canal %s esta apagado pero tiene credencial viva'
                            % canal.tipo)


class TestIntegridadReferencial(unittest.TestCase):
    """Invariantes que valen sobre la base viva, crezca lo que crezca.

    Reemplaza al test de conteos que estaba antes en este archivo. La
    diferencia esta en la forma de la afirmacion: aquel decia "la tabla X tiene
    N filas", que deja de ser cierto en cuanto alguien usa la app; estos dicen
    "ninguna fila apunta a algo que no existe", que tiene que seguir siendo
    cierto con 0 filas, con 2 y con 200.000. Solo leen: no escriben nada.

    Las dos primeras familias de chequeos se apoyan una en la otra. Postgres
    hoy tiene declaradas todas las FK, asi que buscar huerfanos deberia dar
    cero por construccion -- pero eso vale mientras las FK sigan ahi, y una
    migracion que recree una tabla puede perderlas sin avisar. Por eso se
    verifica tambien que las restricciones sigan declaradas: un chequeo cuida
    al otro.

    Las dos ultimas familias son las que Postgres NO puede cuidar solo, y por
    eso son las que mas valen:

        un gasto usa una categoria de OTRO usuario   -> fuga entre usuarios
        un gasto usa una categoria de tipo 'ingreso' -> categoria mal asignada

    Las dos son imposibles por la via normal: los formularios de gasto e
    ingreso filtran las categorias por usuario_id y por tipo (app.py). Si
    alguna aparece, entro por un camino que no es la app.

    Nota honesta: con gasto, ingreso y categoria todavia en cero, esos dos
    ultimos chequeos pasan sin recorrer ninguna fila. Empiezan a tener dientes
    cuando Roman cargue datos, que es justo cuando hacen falta.
    """

    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def _filas(self, sql):
        from sqlalchemy import text
        from models import db
        with db.engine.connect() as conn:
            return conn.execute(text(sql)).fetchall()

    def test_ninguna_fila_apunta_a_un_padre_que_no_existe(self):
        """El huerfano clasico: un gasto de un usuario borrado, un historial
        que referencia a alguien que ya no esta."""
        for tabla, columna, referida in FKS_TABLAS_VIEJAS:
            with self.subTest(fk='%s.%s -> %s' % (tabla, columna, referida)):
                huerfanos = self._filas(
                    'select h.id from "%s" h '
                    'left join "%s" p on h."%s" = p.id '
                    'where h."%s" is not null and p.id is null '
                    'limit 10' % (tabla, referida, columna, columna))
                self.assertEqual(
                    [f.id for f in huerfanos], [],
                    'hay filas de %s cuyo %s no existe en %s (se muestran hasta 10)'
                    % (tabla, columna, referida))

    def test_las_fk_de_las_tablas_viejas_siguen_declaradas(self):
        """Que las FK existan es lo que hace barato al test de arriba.

        Si una migracion futura recrea una de estas tablas y se olvida la
        restriccion, los huerfanos pasan a ser posibles y nadie se entera hasta
        que algo se rompe en pantalla.
        """
        from sqlalchemy import inspect
        from models import db

        inspector = inspect(db.engine)
        for tabla, columna, referida in FKS_TABLAS_VIEJAS:
            with self.subTest(fk='%s.%s -> %s' % (tabla, columna, referida)):
                declaradas = [
                    (tuple(fk['constrained_columns']), fk['referred_table'])
                    for fk in inspector.get_foreign_keys(tabla)
                ]
                self.assertIn(
                    ((columna,), referida), declaradas,
                    'la base perdio la FK %s.%s -> %s' % (tabla, columna, referida))

    def test_las_6_tablas_originales_siguen_existiendo(self):
        """Lo unico del baseline viejo que si es permanente: que esten."""
        from sqlalchemy import inspect
        from models import db

        presentes = set(inspect(db.engine).get_table_names())
        for tabla in TABLAS_VIEJAS:
            with self.subTest(tabla=tabla):
                self.assertIn(tabla, presentes)

    def test_nadie_usa_una_categoria_de_otro_usuario(self):
        """Fuga entre usuarios. Postgres no la puede ver: la FK a categoria se
        cumple igual, aunque la categoria sea de otro."""
        for tabla in ('gasto', 'ingreso'):
            with self.subTest(tabla=tabla):
                cruzados = self._filas(
                    'select x.id from "%s" x '
                    'join categoria c on x.categoria_id = c.id '
                    'where c.usuario_id <> x.usuario_id '
                    'limit 10' % tabla)
                self.assertEqual(
                    [f.id for f in cruzados], [],
                    'hay filas de %s que usan una categoria de otro usuario' % tabla)

    def test_la_categoria_de_un_gasto_es_de_tipo_gasto(self):
        """Y la de un ingreso, de tipo ingreso.

        El nombre de la tabla y el valor de categoria.tipo son el mismo string
        ('gasto' / 'ingreso'), asi que la comparacion se arma sola.
        """
        for tabla in ('gasto', 'ingreso'):
            with self.subTest(tabla=tabla):
                mal_tipadas = self._filas(
                    "select x.id from \"%s\" x "
                    "join categoria c on x.categoria_id = c.id "
                    "where c.tipo <> '%s' "
                    "limit 10" % (tabla, tabla))
                self.assertEqual(
                    [f.id for f in mal_tipadas], [],
                    'hay filas de %s con una categoria que no es de tipo %s'
                    % (tabla, tabla))


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
