# -*- coding: utf-8 -*-
"""Tests de FASE-SYNC-CRON-S2 (endpoint HTTP protegido por token).

    python -m unittest discover -s tests -v

El endpoint no tiene @login_required: lo unico que lo separa de una ruta
publica es el header. Por eso lo que se prueba aca es sobre todo la puerta, no
el sync (eso ya lo cubren FASE3-S2 y FASE-SYNC-CRON-S1):

    sin token / token malo -> 401 Y ademas nadie disparo nada
    token bueno            -> lanzar_backfill por cada canal activo
    ya hay una corrida     -> reporta "saltado", no arranca una segunda
    algo revienta          -> el token no queda escrito en el log

Los dos primeros afirman tambien sobre que NO se llamo al sync, no solo sobre
el codigo de respuesta. Si alguien moviera el chequeo del token abajo del
lanzamiento, el codigo seguiria siendo 401 y el endpoint estaria igual de
abierto; el assert sobre las llamadas es lo unico que atrapa eso.

Nada sale a internet: `lanzar_backfill` se reemplaza por un doble salvo en el
test de concurrencia, que necesita la reserva real.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_tiendanube  # noqa: E402
from models import CanalVenta, Empresa, SyncLog, db  # noqa: E402
from app import app  # noqa: E402

RUTA = '/integraciones/tiendanube/sync-externo'

# Token de juguete, solo para los tests. El real vive en la variable de entorno
# SYNC_CRON_TOKEN de Render y no esta en el repo.
TOKEN_TEST = 'token-de-prueba-fase-sync-cron-s2-nO7xQ2'

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


class BaseEndpoint(unittest.TestCase):
    """Una empresa con un canal de Tiendanube conectado, y el token puesto.

    El token se setea y se restaura por test: dejarlo pegado en os.environ
    contaminaria a los otros modulos de la suite, que corren en el mismo
    proceso.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-SYNC-CRON-S2')
        db.session.add(self.empresa)
        db.session.flush()

        self.canal = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                nombre='Korvo', activo=True, id_tienda_externo='9999')
        db.session.add(self.canal)
        db.session.commit()
        self.canal_id = self.canal.id

        self.token_original = os.environ.get('SYNC_CRON_TOKEN')
        os.environ['SYNC_CRON_TOKEN'] = TOKEN_TEST

        self.lanzar_original = sync_tiendanube.lanzar_backfill
        self.backfill_original = sync_tiendanube.correr_backfill
        self.stderr_original = sys.stderr
        sys.stderr = io.StringIO()

        self.cliente = app.test_client()

    def tearDown(self):
        sys.stderr = self.stderr_original
        sync_tiendanube.lanzar_backfill = self.lanzar_original
        sync_tiendanube.correr_backfill = self.backfill_original
        if self.token_original is None:
            os.environ.pop('SYNC_CRON_TOKEN', None)
        else:
            os.environ['SYNC_CRON_TOKEN'] = self.token_original
        db.session.remove()
        self.ctx.pop()

    def _log(self):
        """Lo que se escribio a stderr durante el test."""
        return sys.stderr.getvalue()

    def _espiar_lanzamientos(self):
        """Reemplaza lanzar_backfill por un doble y devuelve la lista de ids.

        La ruta llama `sync_tiendanube.lanzar_backfill`, o sea que resuelve el
        atributo del modulo en cada request; con parchear el modulo alcanza. Si
        manana alguien la cambia por un `from sync_tiendanube import ...`,
        estos tests se ponen rojos, que es lo correcto: seria una segunda
        referencia de la que nadie se va a acordar.
        """
        llamadas = []

        def lanzar_falso(app_obj, canal_id):
            llamadas.append(canal_id)
            return True, 'Sincronización iniciada.'

        sync_tiendanube.lanzar_backfill = lanzar_falso
        return llamadas


class TestPuertaCerrada(BaseEndpoint):
    """Sin el token correcto no pasa nada. Literalmente nada."""

    def test_endpoint_sin_token_devuelve_401(self):
        """Request pelado: 401 y ningun sync disparado.

        Lo segundo es lo que importa de verdad; el codigo de respuesta solo no
        prueba que el endpoint este cerrado.
        """
        llamadas = self._espiar_lanzamientos()

        respuesta = self.cliente.post(RUTA)

        self.assertEqual(respuesta.status_code, 401)
        self.assertEqual(llamadas, [],
                         'un request sin token no puede disparar ningun sync')
        self.assertEqual(SyncLog.query.count(), 0,
                         'tampoco puede dejar una corrida reservada')

    def test_endpoint_con_token_incorrecto_devuelve_401(self):
        """Token equivocado: mismo 401, mismo silencio.

        Ademas se controla que la respuesta no cuente nada del estado interno:
        sin token, el que llama no tiene por que saber si el canal existe, si
        esta conectado o si hay algo corriendo.
        """
        llamadas = self._espiar_lanzamientos()

        respuesta = self.cliente.post(RUTA, headers={'X-Sync-Token': 'token-equivocado'})

        self.assertEqual(respuesta.status_code, 401)
        self.assertEqual(llamadas, [])
        self.assertEqual(SyncLog.query.count(), 0)

        cuerpo = respuesta.get_data(as_text=True).lower()
        for filtrado in ('canal', 'tiendanube', 'korvo'):
            self.assertNotIn(filtrado, cuerpo,
                             'el 401 no deberia filtrar estado interno')

    def test_endpoint_sin_variable_configurada_devuelve_401(self):
        """Si SYNC_CRON_TOKEN no esta seteada, NADIE pasa.

        Es el caso real de "desplegue el codigo antes que la variable". Un
        endpoint que interpretara "no hay token configurado" como "no pido
        token" quedaria abierto justo en esa ventana.
        """
        os.environ.pop('SYNC_CRON_TOKEN', None)
        llamadas = self._espiar_lanzamientos()

        respuesta = self.cliente.post(RUTA, headers={'X-Sync-Token': 'lo-que-sea'})

        self.assertEqual(respuesta.status_code, 401)
        self.assertEqual(llamadas, [])


class TestPuertaAbierta(BaseEndpoint):
    """Con el token correcto corre el mismo sync que el boton manual."""

    def test_endpoint_con_token_correcto_dispara_sync(self):
        """Token valido: 202 y un lanzar_backfill por cada canal activo.

        Se afirma sobre `lanzar_backfill` y no sobre `correr_backfill` porque
        ese es el limite del endpoint: de ahi para adentro es codigo de S1 que
        ya tiene sus propios tests.
        """
        llamadas = self._espiar_lanzamientos()

        respuesta = self.cliente.post(RUTA, headers={'X-Sync-Token': TOKEN_TEST})

        self.assertEqual(respuesta.status_code, 202)
        self.assertEqual(llamadas, [self.canal_id])

        cuerpo = respuesta.get_json()
        self.assertEqual(len(cuerpo['canales']), 1)
        self.assertEqual(cuerpo['canales'][0]['canal_id'], self.canal_id)
        self.assertEqual(cuerpo['canales'][0]['estado'], 'arrancado')

    def test_recorre_todos_los_canales_activos_y_saltea_los_inactivos(self):
        """Dos canales conectados y uno desconectado: se disparan dos.

        El endpoint no puede hardcodear un id: el cron no tiene empresa
        "actual". Y un canal desconectado no tiene token para hablarle a
        Tiendanube, asi que arrancarlo seria una corrida fallida garantizada.
        """
        # Una empresa por canal: canal_venta tiene UNIQUE (empresa_id, tipo),
        # o sea que una empresa no puede tener dos canales de Tiendanube.
        otra = Empresa(nombre='Segunda Empresa')
        tercera = Empresa(nombre='Tercera Empresa (desconectada)')
        db.session.add_all([otra, tercera])
        db.session.flush()
        segundo = CanalVenta(empresa_id=otra.id, tipo='tiendanube',
                             nombre='Otra tienda', activo=True, id_tienda_externo='8888')
        apagado = CanalVenta(empresa_id=tercera.id, tipo='tiendanube',
                             nombre='Vieja', activo=False, id_tienda_externo='7777')
        db.session.add_all([segundo, apagado])
        db.session.commit()

        llamadas = self._espiar_lanzamientos()

        respuesta = self.cliente.post(RUTA, headers={'X-Sync-Token': TOKEN_TEST})

        self.assertEqual(respuesta.status_code, 202)
        self.assertEqual(sorted(llamadas), sorted([self.canal_id, segundo.id]))
        self.assertNotIn(apagado.id, llamadas)

    def test_endpoint_no_pisa_sync_en_curso(self):
        """Si el canal ya tiene una corrida reservada, la reporta como saltada.

        Aca NO se parchea `lanzar_backfill`: la guarda que se esta probando es
        la de S1 (`_reservar_corrida`), y parchearla seria probar el doble. Lo
        que se reemplaza es `correr_backfill`, para que el thread que igual se
        dispara en la primera llamada no salga a internet.

        Es el caso que se va a dar solo: cron cada 20 minutos, backfill que se
        pasa de 20 minutos, dos requests encimados.
        """
        sync_tiendanube.correr_backfill = lambda canal_id, arranque: {}

        # Primera llamada: reserva de verdad y deja el canal ocupado.
        primera = self.cliente.post(RUTA, headers={'X-Sync-Token': TOKEN_TEST})
        self.assertEqual(primera.get_json()['canales'][0]['estado'], 'arrancado')

        segunda = self.cliente.post(RUTA, headers={'X-Sync-Token': TOKEN_TEST})

        self.assertEqual(segunda.status_code, 202)
        canal = segunda.get_json()['canales'][0]
        self.assertEqual(canal['estado'], 'saltado',
                         'la segunda llamada no puede arrancar un sync paralelo')

        # La guarda es la de S1: no se abrio un segundo par de filas de
        # sync_log, siguen siendo las dos de la primera corrida.
        filas = SyncLog.query.filter_by(canal_id=self.canal_id,
                                        operacion=sync_tiendanube.OPERACION).all()
        self.assertEqual(len(filas), 2)


class TestTokenNoSeFiltra(BaseEndpoint):
    """El valor del token no puede terminar escrito en ningun lado."""

    def test_token_no_aparece_en_logs_de_error(self):
        """Si el lanzamiento revienta, el log no puede traer eco del token.

        Los logs de Render los ve cualquiera con acceso al panel, y se pegan en
        tickets y en chats sin pensarlo. Un token filtrado ahi es un token que
        hay que rotar.
        """
        def lanzar_explota(app_obj, canal_id):
            raise RuntimeError('la API de Tiendanube devolvio 500')

        sync_tiendanube.lanzar_backfill = lanzar_explota

        respuesta = self.cliente.post(RUTA, headers={'X-Sync-Token': TOKEN_TEST})

        # El canal fallado se reporta, pero el endpoint no se cae entero.
        self.assertEqual(respuesta.status_code, 202)
        self.assertEqual(respuesta.get_json()['canales'][0]['estado'], 'error')

        log = self._log()
        self.assertIn('la API de Tiendanube devolvio 500', log,
                      'el error real si tiene que quedar logueado')
        self.assertNotIn(TOKEN_TEST, log,
                         'el token no puede aparecer en el log')
        self.assertNotIn(TOKEN_TEST, respuesta.get_data(as_text=True),
                         'el token tampoco puede volver en la respuesta')

    def test_token_no_aparece_en_el_log_del_rechazo(self):
        """El 401 tampoco puede loguear el token que le mandaron.

        Un cron mal configurado que le manda el token bueno a la ruta
        equivocada no tiene que dejarlo escrito en el log del servidor.
        """
        self.cliente.post(RUTA, headers={'X-Sync-Token': TOKEN_TEST + '-mal'})

        log = self._log()
        self.assertNotIn(TOKEN_TEST, log)

    def test_el_token_no_esta_en_el_repo(self):
        """El codigo lee la variable de entorno, no un literal.

        Es la regresion que mas duele y la que menos se nota en un diff: un
        token de fallback "para probar local" queda commiteado para siempre.
        """
        ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'rutas_integraciones.py')
        fuente = io.open(ruta, encoding='utf-8').read()
        self.assertIn('os.environ.get(VAR_TOKEN_SYNC)', fuente)
        self.assertNotIn("SYNC_CRON_TOKEN', '", fuente,
                         'no puede haber un default hardcodeado para el token')


if __name__ == '__main__':
    unittest.main()
