# -*- coding: utf-8 -*-
"""Tests de FASE-AUDITORIA-S3 (quien disparo el sync).

    python -m unittest discover -s tests -v

Lo unico que agrega esta slice es una columna: sync_log.usuario_id. La
pregunta que responde es "quien apreto Sincronizar", y tiene exactamente dos
respuestas validas:

    boton manual (hay sesion)         -> el id del que hizo clic
    cron (token o script, sin sesion) -> NULL

Los dos casos se prueban de punta a punta hasta la fila en sync_log, no hasta
el argumento que recibio la funcion: un `usuario_id=` que se pasa pero no se
persiste dejaria el test verde y la bitacora vacia.

NO se prueba aca que el sync siga fuera de la auditoria (TABLAS_AUDITADAS):
eso lo fija FASE-AUDITORIA-S1 y esta slice no lo toca. Pero si se afirma que
la corrida manual no escribe historial, porque el riesgo de esta slice es
justamente que alguien confunda "anotar el clic" con "auditar la escritura".

Nada sale a internet: `correr_backfill` -- el cuerpo que le pega a la API --
se reemplaza por un doble, y lo que se ejecuta de verdad es la reserva, que es
donde nace la fila.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_tiendanube  # noqa: E402
from models import CanalVenta, Empresa, Historial, SyncLog, Usuario, db  # noqa: E402
from app import app  # noqa: E402

RUTA_MANUAL = '/integraciones/tiendanube/sincronizar'
RUTA_CRON = '/integraciones/tiendanube/sync-externo'

TOKEN_TEST = 'token-de-prueba-fase-auditoria-s3-pQ4mZ9'

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


class BaseSync(unittest.TestCase):
    """Una empresa, un usuario y un canal de Tiendanube conectado.

    El thread del backfill se neutraliza reemplazando `correr_backfill`: la
    reserva (que es lo que escribe sync_log) corre de verdad, el cuerpo no.
    Sin esto el hilo saldria a internet y, peor, escribiria sobre la sesion de
    SQLAlchemy mientras el test la esta leyendo.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-AUDITORIA-S3')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='auditorias3@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.canal = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                nombre='Ferreteria Roman', activo=True,
                                id_tienda_externo='9999')
        db.session.add(self.canal)
        db.session.commit()

        self.usuario_id = self.usuario.id
        self.canal_id = self.canal.id

        self.backfill_original = sync_tiendanube.correr_backfill
        sync_tiendanube.correr_backfill = lambda canal_id, arranque: None

        self.token_original = os.environ.get('SYNC_CRON_TOKEN')
        os.environ['SYNC_CRON_TOKEN'] = TOKEN_TEST

        self.cliente = app.test_client()

    def tearDown(self):
        sync_tiendanube.correr_backfill = self.backfill_original
        if self.token_original is None:
            os.environ.pop('SYNC_CRON_TOKEN', None)
        else:
            os.environ['SYNC_CRON_TOKEN'] = self.token_original
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def loguear(self):
        with self.cliente.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def filas_sync(self):
        """Las filas de sync_log de este canal, recien leidas de la base."""
        db.session.expire_all()
        return SyncLog.query.filter_by(canal_id=self.canal_id).all()


class TestDisparoManual(BaseSync):
    """El clic tiene dueno y queda anotado."""

    def test_sync_manual_registra_usuario(self):
        """POST logueado -> las dos filas de sync_log llevan ese usuario."""
        self.loguear()

        respuesta = self.cliente.post(RUTA_MANUAL)
        self.assertEqual(respuesta.status_code, 302)

        filas = self.filas_sync()
        self.assertEqual(len(filas), 2,
                         'una corrida son dos filas: productos y pedidos')
        for fila in filas:
            self.assertEqual(fila.usuario_id, self.usuario_id,
                             'la fila tiene que decir quien apreto el boton')

    def test_el_nombre_llega_a_la_pantalla(self):
        """`ultimo_sync` expone el nombre, que es lo que lee el template.

        Se afirma sobre el nombre y no sobre el id porque lo que se rompe en
        silencio es el renglon de la UI: la columna puede estar bien poblada y
        la pantalla igual no mostrar nada.
        """
        self.loguear()
        self.cliente.post(RUTA_MANUAL)

        resumen = sync_tiendanube.ultimo_sync(self.canal_id)
        self.assertEqual(resumen['disparada_por'], 'Roman Test')

    def test_el_boton_no_escribe_historial(self):
        """Anotar el clic no es auditar la escritura.

        El sync sigue afuera de TABLAS_AUDITADAS (FASE-AUDITORIA-S1). Si
        alguien lo metiera ahi, cada corrida vomitaria cientos de filas de
        historial por productos que nadie edito a mano.
        """
        self.loguear()
        self.cliente.post(RUTA_MANUAL)

        self.assertEqual(Historial.query.count(), 0)


class TestDisparoAutomatico(BaseSync):
    """El cron no tiene a quien atribuirle nada, y eso se guarda como NULL."""

    def test_sync_cron_usuario_null(self):
        """El endpoint con token deja usuario_id en NULL."""
        respuesta = self.cliente.post(RUTA_CRON, headers={'X-Sync-Token': TOKEN_TEST})
        self.assertEqual(respuesta.status_code, 202)

        filas = self.filas_sync()
        self.assertEqual(len(filas), 2)
        for fila in filas:
            self.assertIsNone(fila.usuario_id,
                              'el cron no tiene usuario: la columna va NULL')

    def test_sync_cron_con_sesion_abierta_sigue_siendo_null(self):
        """Aunque el navegador tenga sesion, el endpoint de token no la usa.

        Es el caso que atrapa el error real: `sync_externo_tiendanube` no tiene
        @login_required, pero corre en el mismo proceso y con el mismo
        `current_user` disponible si el que llama trae cookie. Si alguien
        copiara el `usuario_id=current_user.id` a esa ruta, una corrida
        automatica quedaria firmada por quien casualmente tenia el navegador
        abierto.
        """
        self.loguear()

        respuesta = self.cliente.post(RUTA_CRON, headers={'X-Sync-Token': TOKEN_TEST})
        self.assertEqual(respuesta.status_code, 202)

        for fila in self.filas_sync():
            self.assertIsNone(fila.usuario_id)

    def test_script_periodico_usuario_null(self):
        """La tercera puerta: `correr_sync_ahora`, sin request context.

        No pasa por `lanzar_backfill` -- llama `_reservar_corrida` directo --
        asi que tiene que probarse aparte: el default de una funcion no dice
        nada del default de la otra.
        """
        estado, _ = sync_tiendanube.correr_sync_ahora(self.canal_id)
        self.assertEqual(estado, 'ok')

        filas = self.filas_sync()
        self.assertEqual(len(filas), 2)
        for fila in filas:
            self.assertIsNone(fila.usuario_id)

    def test_ultimo_sync_no_inventa_nombre(self):
        """Sin usuario, `disparada_por` es None (el template dice automatico)."""
        self.cliente.post(RUTA_CRON, headers={'X-Sync-Token': TOKEN_TEST})

        resumen = sync_tiendanube.ultimo_sync(self.canal_id)
        self.assertIsNone(resumen['disparada_por'])


class TestBitacoraSobreviveAlUsuario(BaseSync):
    """Borrar la cuenta no puede borrar la constancia de la corrida.

    Mismo criterio que `historial` y que la caja: la fila queda, con la FK en
    NULL. Sin esto, eliminar un usuario se llevaria puesto el registro de todo
    lo que sincronizo -- que es justo lo que esta slice existe para conservar.
    """

    def test_al_borrar_el_usuario_la_fila_queda_con_null(self):
        self.loguear()
        self.cliente.post(RUTA_MANUAL)

        usuario = db.session.get(Usuario, self.usuario_id)
        db.session.delete(usuario)
        db.session.commit()

        filas = self.filas_sync()
        self.assertEqual(len(filas), 2, 'la bitacora no se borra con la cuenta')
        for fila in filas:
            self.assertIsNone(fila.usuario_id)


if __name__ == '__main__':
    unittest.main(verbosity=2)
