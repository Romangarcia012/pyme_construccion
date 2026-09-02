# -*- coding: utf-8 -*-
"""Tests de FASE-SYNC-CRON-S1 (sincronizacion periodica por Cron Job).

    python -m unittest discover -s tests -v

Lo que se prueba no es el sync (eso ya lo cubre FASE3-S2) sino las tres cosas
que el cron agrega y que, si fallan, fallan en silencio:

    reusa la funcion existente  -> el script y el boton no pueden divergir
    error -> log + exit != 0    -> una corrida rota se ve en Render
    guarda de concurrencia      -> el cron no pisa un sync ya en curso

Ninguna llamada sale a internet: `correr_backfill` se reemplaza por un doble.
Lo que se verifica es el cableado alrededor, no la ingesta.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import io
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_tiendanube  # noqa: E402
from ingestor_canal import ENTIDAD_PEDIDO, ENTIDAD_PRODUCTO  # noqa: E402
from models import CanalVenta, Empresa, SyncLog, db  # noqa: E402
from app import app  # noqa: E402
from scripts import sync_periodico  # noqa: E402

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


class BaseCron(unittest.TestCase):
    """Una empresa con un canal de Tiendanube conectado y nada mas.

    No hace falta catalogo ni pedidos: en todos los tests el backfill real
    esta reemplazado por un doble, porque lo que se mide es que se lo llame (o
    que no se lo llame) y que pasa con el resultado.

    stderr se captura durante cada test: el script no devuelve texto, loguea, y
    varios de estos tests afirman justamente sobre lo que quedo logueado.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-SYNC-CRON-S1')
        db.session.add(self.empresa)
        db.session.flush()

        self.canal = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                nombre='Korvo', activo=True, id_tienda_externo='9999')
        db.session.add(self.canal)
        db.session.commit()
        self.canal_id = self.canal.id

        self.backfill_original = sync_tiendanube.correr_backfill
        self.stderr_original = sys.stderr
        sys.stderr = io.StringIO()

    def tearDown(self):
        sys.stderr = self.stderr_original
        sync_tiendanube.correr_backfill = self.backfill_original
        db.session.remove()
        self.ctx.pop()

    def _log(self):
        """Lo que se escribio a stderr durante el test."""
        return sys.stderr.getvalue()


class TestReusoDeLaFuncionExistente(BaseCron):
    """El script no puede tener su propia copia de la logica de sync."""

    def test_sync_periodico_reusa_funcion_existente(self):
        """El script termina llamando a `correr_backfill`, la misma funcion que
        corre el boton manual.

        Es la prueba de que las dos rutas no van a divergir: si alguien
        reimplementa el sync adentro del script, el doble no se llama y esto se
        pone rojo. Se afirma tambien sobre el sync_log porque la reserva
        (`_reservar_corrida`) tambien es compartida -- el cron deja el mismo
        rastro que el boton, no uno propio.
        """
        llamadas = []

        def backfill_falso(canal_id, arranque):
            llamadas.append((canal_id, arranque))
            return {'productos': {'leidos': 3, 'nuevos': 1, 'actualizados': 2, 'error': 0},
                    'pedidos': {'leidos': 2, 'nuevos': 2, 'actualizados': 0, 'error': 0}}

        sync_tiendanube.correr_backfill = backfill_falso

        fallidos = sync_periodico.sincronizar_canales()

        self.assertEqual(fallidos, 0, 'ningun canal tenia que fallar')
        self.assertEqual(len(llamadas), 1,
                         'el script tenia que llamar a correr_backfill una vez')
        self.assertEqual(llamadas[0][0], self.canal_id)

        # La reserva es la misma que la del boton: dos filas de sync_log, una
        # por entidad, compartiendo fecha_inicio.
        filas = SyncLog.query.filter_by(canal_id=self.canal_id,
                                        operacion=sync_tiendanube.OPERACION).all()
        self.assertEqual(len(filas), 2)
        self.assertEqual({fila.entidad for fila in filas},
                         {ENTIDAD_PRODUCTO, ENTIDAD_PEDIDO})
        self.assertEqual({fila.fecha_inicio for fila in filas}, {llamadas[0][1]})

    def test_el_script_no_reimplementa_el_sync(self):
        """El modulo del script no importa el ingestor ni el cliente HTTP.

        Complemento del test de arriba, por el lado del codigo fuente: si
        `sync_periodico` empieza a hablarle a Tiendanube por su cuenta, dejo de
        ser un disparador y paso a ser una segunda implementacion.
        """
        ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'scripts', 'sync_periodico.py')
        fuente = io.open(ruta, encoding='utf-8').read()
        for prohibido in ('ingestor_tiendanube', 'integracion_tiendanube', 'import requests'):
            self.assertNotIn(prohibido, fuente,
                             'el script no deberia hablarle a Tiendanube por su cuenta')


class TestErrorNoQuedaEnSilencio(BaseCron):
    """Una corrida que falla tiene que dejar rastro y ponerse roja."""

    def test_sync_periodico_loguea_error_sin_crashear_silencioso(self):
        """Si el sync interno revienta: traceback en stderr y exit != 0.

        Los dos importan por separado. Sin el codigo != 0 Render marca la
        corrida como exitosa y nadie se entera; sin el traceback la corrida
        sale roja pero no dice que se rompio.
        """
        def backfill_explota(canal_id, arranque):
            raise RuntimeError('la API de Tiendanube devolvio 500')

        sync_tiendanube.correr_backfill = backfill_explota

        codigo = sync_periodico.main()

        self.assertEqual(codigo, 1,
                         'una corrida fallida tiene que salir con codigo != 0')

        log = self._log()
        self.assertIn('Traceback', log, 'falta el traceback del error')
        self.assertIn('la API de Tiendanube devolvio 500', log,
                      'falta el mensaje original de la excepcion')
        self.assertIn('FALLO', log)

    def test_el_error_no_deja_el_sync_log_trabado_en_corriendo(self):
        """El fallo cierra las filas de sync_log en 'error', no en 'corriendo'.

        Si quedaran en 'corriendo', el boton manual aparece trabado durante los
        30 minutos del TTL y la UI miente diciendo que hay un sync en curso.
        """
        def backfill_explota(canal_id, arranque):
            raise RuntimeError('se corto la conexion')

        sync_tiendanube.correr_backfill = backfill_explota

        sync_periodico.sincronizar_canales()

        filas = SyncLog.query.filter_by(canal_id=self.canal_id).all()
        self.assertEqual(len(filas), 2)
        for fila in filas:
            self.assertEqual(fila.estado, 'error')
            self.assertIn('se corto la conexion', fila.mensaje_error or '')
            self.assertIsNotNone(fila.fecha_fin)

    def test_un_canal_que_falla_no_frena_a_los_demas(self):
        """Dos empresas, la primera revienta: la segunda igual se sincroniza.

        Son datos de empresas distintas; el error de una no tiene por que
        costarle el sync a la otra.
        """
        otra = Empresa(nombre='Otra empresa')
        db.session.add(otra)
        db.session.flush()
        canal_2 = CanalVenta(empresa_id=otra.id, tipo='tiendanube',
                             nombre='Otra tienda', activo=True, id_tienda_externo='8888')
        db.session.add(canal_2)
        db.session.commit()

        vistos = []

        def backfill_selectivo(canal_id, arranque):
            vistos.append(canal_id)
            if canal_id == self.canal_id:
                raise RuntimeError('esta empresa falla')
            return {'productos': {}, 'pedidos': {}}

        sync_tiendanube.correr_backfill = backfill_selectivo

        fallidos = sync_periodico.sincronizar_canales()

        self.assertEqual(vistos, [self.canal_id, canal_2.id],
                         'el segundo canal tenia que sincronizarse igual')
        self.assertEqual(fallidos, 1)


class TestGuardaDeConcurrencia(BaseCron):
    """El cron no puede pisar un sync que ya esta corriendo."""

    def _marcar_en_curso(self, hace=None):
        """Deja el canal con una corrida viva, como la que deja el boton."""
        arranque = datetime.utcnow() - (hace or timedelta(seconds=5))
        for entidad in (ENTIDAD_PRODUCTO, ENTIDAD_PEDIDO):
            db.session.add(SyncLog(
                canal_id=self.canal_id, entidad=entidad,
                operacion=sync_tiendanube.OPERACION,
                estado='corriendo', fecha_inicio=arranque))
        db.session.commit()
        return arranque

    def test_guarda_concurrencia_bloquea_corrida_simultanea(self):
        """Con un sync ya en curso, el cron no ejecuta el backfill de nuevo y
        lo deja logueado como saltado por concurrencia.

        El caso real: alguien aprieta "Sincronizar" y treinta segundos despues
        entra el cron. Sin la guarda serian dos corridas contra la misma API
        gastando el mismo rate limit y escribiendo las mismas filas.
        """
        self._marcar_en_curso()

        llamadas = []
        sync_tiendanube.correr_backfill = (
            lambda canal_id, arranque: llamadas.append(canal_id))

        codigo = sync_periodico.main()

        self.assertEqual(llamadas, [], 'el backfill NO tenia que ejecutarse')
        self.assertEqual(codigo, 0, 'saltar por concurrencia no es un error')
        self.assertIn('saltado por concurrencia', self._log())

        # Y no ensucia la bitacora: siguen siendo las dos filas de la corrida
        # que ya estaba, sin una tercera reservada al pedo.
        self.assertEqual(SyncLog.query.filter_by(canal_id=self.canal_id).count(), 2)

    def test_una_corrida_huerfana_no_traba_el_cron_para_siempre(self):
        """Pasado el TTL, una fila en 'corriendo' se da por perdida y el cron
        vuelve a arrancar.

        Sin esto, un deploy de Render en el medio de un sync dejaria el canal
        sin sincronizar hasta que alguien lo notara a mano.
        """
        self._marcar_en_curso(hace=sync_tiendanube.TTL_CORRIENDO + timedelta(minutes=1))

        llamadas = []
        sync_tiendanube.correr_backfill = (
            lambda canal_id, arranque: llamadas.append(canal_id))

        sync_periodico.sincronizar_canales()

        self.assertEqual(llamadas, [self.canal_id],
                         'la corrida huerfana tenia que darse por perdida')
        huerfanas = SyncLog.query.filter_by(canal_id=self.canal_id, estado='error').all()
        self.assertEqual(len(huerfanas), 2)

    def test_canal_desconectado_no_se_sincroniza(self):
        """Un canal inactivo ni siquiera entra en la lista del cron."""
        self.canal.activo = False
        db.session.commit()

        llamadas = []
        sync_tiendanube.correr_backfill = (
            lambda canal_id, arranque: llamadas.append(canal_id))

        codigo = sync_periodico.main()

        self.assertEqual(llamadas, [])
        self.assertEqual(codigo, 0)
        self.assertIn('no hay canales', self._log())


if __name__ == '__main__':
    unittest.main(verbosity=2)
