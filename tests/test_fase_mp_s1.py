# -*- coding: utf-8 -*-
"""Tests de la FASE-MP-S1 (OAuth de Mercado Pago + sync de movimientos).

    python -m unittest discover -s tests -v

NINGUN test pega contra la API real de Mercado Pago: toda la capa HTTP esta
mockeada en `mp.requests`, y TestNoSePegaALaApiReal del final verifica que no
haya otra puerta de salida. Tampoco se toca la base productiva: el modulo
repunta la app a un SQLite en memoria mientras corre y la devuelve a
DATABASE_URL al terminar, igual que FASE3-S1 y S2.

El backfill se ejecuta SINCRONICAMENTE (llamando a correr_backfill directo,
sin thread): lo que se testea es que escribe bien y que no duplica, no que
threading.Thread funcione. Que la ruta no bloquee el request tiene su propio
test, que verifica que durante el POST no se le pega a la API.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('CREDENTIALS_ENCRYPTION_KEY',
                      'sO1mHTMYm4Rfy9ii1YV8dqmM1J4KrHnQPy_2xGx0nMk=')
os.environ.setdefault('SECRET_KEY', 'clave-de-test')

import cripto  # noqa: E402
import ingestor_mercadopago as ingestor  # noqa: E402
import integracion_mercadopago as mp  # noqa: E402
import sync_mercadopago  # noqa: E402
from app import app  # noqa: E402
from tests.ayuda_auth import request_anonimo  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    CredencialCuentaCobro,
    CuentaCobro,
    Empresa,
    MovimientoCuenta,
    SyncLog,
    Usuario,
    db,
)

ENGINE_PRODUCTIVO = None

TOKEN_ROMAN = 'APP_USR-token-de-prueba-de-roman-no-es-real'
TOKEN_NACHI = 'APP_USR-token-de-prueba-de-nachi-no-es-real'
REFRESH_ROMAN = 'TG-refresh-de-roman-no-es-real'
USER_ID_ROMAN = '111222333'
USER_ID_NACHI = '444555666'

CLIENT_ID_FALSO = '1234567890123456'
SECRET_FALSO = 'secreto-de-test'
REDIRECT_FALSO = 'https://pyme.test.local/integraciones/mercadopago/callback'

ENTORNO_MP = {
    'MERCADOPAGO_CLIENT_ID': CLIENT_ID_FALSO,
    'MERCADOPAGO_CLIENT_SECRET': SECRET_FALSO,
    'MERCADOPAGO_REDIRECT_URI': REDIRECT_FALSO,
}


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


# ---------------------------------------------------------------------------
# Payloads de Mercado Pago (recortados a los campos que mira el ingestor)
# ---------------------------------------------------------------------------

def pago(id_pago, monto, comision=None, estado='approved', fecha=None,
         fee_details=None, moneda='ARS'):
    """Un pago crudo como lo devuelve /v1/payments/search.

    `comision` arma el fee_details tipico (una sola linea, mercadopago_fee a
    cargo del collector). `fee_details` lo pisa entero, para los casos raros.
    """
    crudo = {
        'id': id_pago,
        'status': estado,
        'status_detail': 'accredited' if estado == 'approved' else 'pending',
        'date_created': fecha or '2026-03-10T09:00:00.000-03:00',
        'date_approved': fecha or '2026-03-10T09:00:00.000-03:00',
        'currency_id': moneda,
        'transaction_amount': monto,
        'payment_method_id': 'visa',
        'description': 'Venta mostrador',
    }
    if fee_details is not None:
        crudo['fee_details'] = fee_details
    elif comision is not None:
        crudo['fee_details'] = [
            {'type': 'mercadopago_fee', 'amount': comision, 'fee_payer': 'collector'},
        ]
    return crudo


# Tres pagos de la cuenta de Roman. El tercero esta pendiente: no es plata que
# entro y no tiene que generar movimiento.
PAGOS_ROMAN = [
    pago(70001, '12500.50', comision='875.04'),
    pago(70002, '4300.99', comision='301.07', fecha='2026-04-02T14:30:00.000-03:00'),
    pago(70003, '9999.00', comision='699.93', estado='pending'),
]

# Un pago de la cuenta de Nachi. Ids distintos: son cuentas distintas.
PAGOS_NACHI = [
    pago(80001, '55000.00', comision='3850.00'),
]


class RespuestaFalsa:
    """Lo minimo de requests.Response que usa integracion_mercadopago."""

    def __init__(self, status_code=200, datos=None, texto=None, headers=None):
        self.status_code = status_code
        self._datos = datos
        self.text = texto if texto is not None else str(datos)
        self.headers = headers or {}

    def json(self):
        if self._datos is None:
            raise ValueError('no es JSON')
        return self._datos


def respuesta_de_busqueda(resultados, total=None):
    """El sobre {paging, results} de /v1/payments/search."""
    return RespuestaFalsa(200, {
        'paging': {'total': len(resultados) if total is None else total,
                   'limit': mp.LIMITE_POR_PAGINA, 'offset': 0},
        'results': resultados,
    })


def _en_la_ventana(crudo, params):
    """Si el pago cae dentro del begin_date/end_date que pidio la llamada.

    El mock filtra de verdad por ventana en vez de devolver siempre todo: como
    traer_pagos barre ANIOS_HISTORICO en tramos de 364 dias, un mock que
    ignorara el rango devolveria los mismos pagos siete veces y los contadores
    de la corrida (leidos, actualizados) dejarian de significar nada.
    """
    aprobado = ingestor._fecha(crudo.get('date_approved'))
    if aprobado is None:
        return False
    desde = datetime.strptime(params['begin_date'], mp.FORMATO_FECHA)
    hasta = datetime.strptime(params['end_date'], mp.FORMATO_FECHA)
    return desde <= aprobado < hasta


def _servir(pagos, params):
    """Una pagina de la busqueda, filtrada por ventana y por offset."""
    de_la_ventana = [p for p in pagos if _en_la_ventana(p, params)]
    offset = params.get('offset') or 0
    return respuesta_de_busqueda(de_la_ventana[offset:], total=len(de_la_ventana))


def get_falso(pagos):
    """Un requests.get que sirve `pagos` respetando la ventana de fecha."""
    def _get(url, headers=None, params=None, timeout=None):
        return _servir(pagos, params or {})
    return _get


def get_por_token(mapa):
    """Un requests.get que devuelve pagos distintos segun el Bearer.

    Es el mock que hace verificable el aislamiento entre las dos cuentas: si el
    sync usara el token equivocado, traeria los pagos del otro.
    """
    def _get(url, headers=None, params=None, timeout=None):
        token = (headers or {}).get('Authorization', '').replace('Bearer ', '')
        return _servir(mapa.get(token, []), params or {})
    return _get


# ---------------------------------------------------------------------------
# Base comun
# ---------------------------------------------------------------------------

class BaseWeb(unittest.TestCase):
    """Una empresa, un usuario logueado y las dos cuentas de Mercado Pago.

    Reproduce lo que deja la migracion: dos filas de cuenta_cobro sin
    credencial, y los canales apuntando a la cuenta que les corresponde.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-MP-S1')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(
            nombre='Roman Test',
            email='fasemps1@test.local',
            empresa_id=self.empresa.id,
            rol='admin',
            verificado=True,
        )
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.cuenta_roman = CuentaCobro(
            empresa_id=self.empresa.id, nombre='Roman - Presencial y Tiendanube',
            tipo='mercadopago', metodo_ingesta='api')
        self.cuenta_nachi = CuentaCobro(
            empresa_id=self.empresa.id, nombre='Nachi - Mercado Libre',
            tipo='mercadopago', metodo_ingesta='api')
        db.session.add_all([self.cuenta_roman, self.cuenta_nachi])
        db.session.flush()

        db.session.add_all([
            CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                       nombre='Tiendanube', activo=False,
                       cuenta_cobro_id=self.cuenta_roman.id),
            CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                       nombre='Venta manual / presencial', activo=True,
                       cuenta_cobro_id=self.cuenta_roman.id),
            CanalVenta(empresa_id=self.empresa.id, tipo='mercadolibre',
                       nombre='Mercado Libre', activo=False,
                       cuenta_cobro_id=self.cuenta_nachi.id),
        ])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.id_roman = self.cuenta_roman.id
        self.id_nachi = self.cuenta_nachi.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers ------------------------------------------------------------

    def conectar(self, cuenta_id, token, refresh=None, expira_en=None):
        """Deja una credencial cifrada, como si el OAuth ya hubiera corrido."""
        credencial = CredencialCuentaCobro(
            cuenta_cobro_id=cuenta_id,
            access_token_cifrado=cripto.cifrar(token),
            refresh_token_cifrado=cripto.cifrar(refresh) if refresh else None,
            expira_en=expira_en,
            actualizado_en=datetime.utcnow(),
        )
        db.session.add(credencial)
        db.session.commit()
        return credencial

    def sembrar_state(self, state, cuenta_id):
        """Deja el state en la sesion, como lo dejaria GET /conectar."""
        with self.client.session_transaction() as sesion:
            sesion['mp_oauth_state'] = state
            sesion['mp_oauth_cuenta_id'] = cuenta_id

    def respuesta_token(self, access=TOKEN_ROMAN, refresh=REFRESH_ROMAN,
                        user_id=USER_ID_ROMAN, expires_in=15552000):
        return RespuestaFalsa(200, {
            'access_token': access,
            'refresh_token': refresh,
            'token_type': 'Bearer',
            'expires_in': expires_in,
            'scope': 'offline_access read',
            'user_id': int(user_id),
            'live_mode': True,
        })

    def correr_sync(self, cuenta_id):
        """Una corrida sincrona completa, con su fila de sync_log."""
        arranque = datetime.utcnow()
        db.session.add(SyncLog(
            cuenta_cobro_id=cuenta_id, entidad='movimiento',
            operacion=sync_mercadopago.OPERACION, estado='corriendo',
            fecha_inicio=arranque))
        db.session.commit()
        return sync_mercadopago.correr_backfill(cuenta_id, arranque)


# ---------------------------------------------------------------------------
# PARTE 1 - Esquema
# ---------------------------------------------------------------------------

class TestEsquema(BaseWeb):
    """Las columnas y la tabla nuevas existen y significan lo que dicen."""

    def test_cuenta_cobro_guarda_el_user_id_de_mercadopago(self):
        self.cuenta_roman.id_cuenta_externa = USER_ID_ROMAN
        db.session.commit()

        recargada = db.session.get(CuentaCobro, self.id_roman)
        self.assertEqual(recargada.id_cuenta_externa, USER_ID_ROMAN)

    def test_cada_canal_apunta_a_la_cuenta_que_le_corresponde(self):
        por_tipo = {c.tipo: c for c in CanalVenta.query.all()}

        self.assertEqual(por_tipo['tiendanube'].cuenta_cobro_id, self.id_roman)
        self.assertEqual(por_tipo['manual'].cuenta_cobro_id, self.id_roman)
        self.assertEqual(por_tipo['mercadolibre'].cuenta_cobro_id, self.id_nachi)

    def test_el_canal_mercadolibre_sigue_apagado(self):
        """Conectar la cuenta de cobro de Nachi no prende el canal de ventas.

        Son dos cosas independientes: la integracion de ventas de Mercado Libre
        no existe todavia. Prenderlo dejaria un canal activo sin credencial,
        que es justo lo que prohibe el test de invariantes de FASE2-S1.
        """
        self.conectar(self.id_nachi, TOKEN_NACHI)

        canal = CanalVenta.query.filter_by(tipo='mercadolibre').one()
        self.assertFalse(canal.activo)

    def test_una_cuenta_tiene_una_sola_credencial(self):
        from sqlalchemy.exc import IntegrityError

        self.conectar(self.id_roman, TOKEN_ROMAN)
        db.session.add(CredencialCuentaCobro(
            cuenta_cobro_id=self.id_roman,
            access_token_cifrado=cripto.cifrar('otro-token')))

        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()


# ---------------------------------------------------------------------------
# PARTE 2 - OAuth
# ---------------------------------------------------------------------------

class TestConectar(BaseWeb):
    """GET /integraciones/mercadopago/conectar/<id>."""

    def setUp(self):
        super().setUp()
        # El entorno de Mercado Pago se define para TODA la clase, no test por
        # test, por culpa de test_requiere_login: sin MERCADOPAGO_CLIENT_ID la
        # ruta le contesta lo mismo a cualquiera -- flashea "no esta
        # configurada" y redirige a /integraciones -- este o no logueado. El
        # 302 que afirma ese test se cumplia solo, y habria pasado igual sin el
        # @login_required en la ruta.
        #
        # Con la variable puesta, un request CON sesion se va a
        # auth.mercadopago.com, asi que el assertNotIn de abajo solo se cumple
        # si de verdad no hay sesion.
        #
        # Los valores son los mismos de siempre y no salen a ningun lado: un
        # client_id inventado y un redirect a pyme.test.local.
        entorno = mock.patch.dict(os.environ, ENTORNO_MP)
        entorno.start()
        self.addCleanup(entorno.stop)

    def test_redirige_a_mercadopago_con_los_parametros_del_flujo(self):
        from urllib.parse import parse_qs, urlparse

        with mock.patch.dict(os.environ, ENTORNO_MP):
            resp = self.client.get('/integraciones/mercadopago/conectar/%s' % self.id_roman)

        self.assertEqual(resp.status_code, 302)
        destino = urlparse(resp.headers['Location'])
        self.assertEqual(destino.netloc, 'auth.mercadopago.com')

        params = parse_qs(destino.query)
        self.assertEqual(params['client_id'], [CLIENT_ID_FALSO])
        self.assertEqual(params['response_type'], ['code'])
        self.assertEqual(params['redirect_uri'], [REDIRECT_FALSO])
        # offline_access es lo que hace que la respuesta traiga refresh_token.
        self.assertIn('offline_access', params['scope'][0])
        self.assertIn('read', params['scope'][0])
        self.assertTrue(params['state'][0])

    def test_no_escribe_nada_en_la_base(self):
        with mock.patch.dict(os.environ, ENTORNO_MP):
            self.client.get('/integraciones/mercadopago/conectar/%s' % self.id_roman)

        self.assertEqual(CredencialCuentaCobro.query.count(), 0)
        self.assertIsNone(db.session.get(CuentaCobro, self.id_roman).id_cuenta_externa)

    def test_guarda_el_state_y_la_cuenta_en_la_sesion(self):
        from urllib.parse import parse_qs, urlparse

        with mock.patch.dict(os.environ, ENTORNO_MP):
            resp = self.client.get('/integraciones/mercadopago/conectar/%s' % self.id_nachi)

        enviado = parse_qs(urlparse(resp.headers['Location']).query)['state'][0]
        with self.client.session_transaction() as sesion:
            self.assertEqual(sesion['mp_oauth_state'], enviado)
            self.assertEqual(sesion['mp_oauth_cuenta_id'], self.id_nachi)

    def test_cuenta_de_otra_empresa_no_se_puede_conectar(self):
        otra = Empresa(nombre='Empresa Ajena')
        db.session.add(otra)
        db.session.flush()
        ajena = CuentaCobro(empresa_id=otra.id, nombre='MP Ajena',
                            tipo='mercadopago', metodo_ingesta='api')
        db.session.add(ajena)
        db.session.commit()

        with mock.patch.dict(os.environ, ENTORNO_MP):
            resp = self.client.get('/integraciones/mercadopago/conectar/%s' % ajena.id)

        self.assertEqual(resp.status_code, 302)
        self.assertIn('/integraciones', resp.headers['Location'])
        with self.client.session_transaction() as sesion:
            self.assertNotIn('mp_oauth_state', sesion)

    def test_sin_client_id_configurado_no_manda_a_ningun_lado(self):
        entorno = dict(os.environ)
        entorno.pop('MERCADOPAGO_CLIENT_ID', None)

        with mock.patch.dict(os.environ, entorno, clear=True):
            resp = self.client.get('/integraciones/mercadopago/conectar/%s' % self.id_roman)

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('mercadopago.com', resp.headers['Location'])
        with self.client.session_transaction() as sesion:
            self.assertNotIn('mp_oauth_state', sesion)

    def test_requiere_login(self):
        # El entorno de MP esta puesto por el setUp: con sesion, esta misma
        # ruta redirige a auth.mercadopago.com. Que NO lo haga es lo que
        # prueba que el @login_required corto antes.
        resp = request_anonimo(self.ctx, 'get',
                               '/integraciones/mercadopago/conectar/%s' % self.id_roman)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('mercadopago.com', resp.headers['Location'])
        self.assertIn('/login', resp.headers['Location'])


class TestCallbackExitoso(BaseWeb):
    """El camino feliz: state valido + code valido -> tokens cifrados."""

    def _callback(self, cuenta_id, state='state-valido', code='code-valido',
                  respuesta=None):
        self.sembrar_state(state, cuenta_id)
        respuesta = respuesta or self.respuesta_token()
        with mock.patch.object(mp.requests, 'post', return_value=respuesta) as post, \
             mock.patch.dict(os.environ, ENTORNO_MP):
            resp = self.client.get(
                '/integraciones/mercadopago/callback?code=%s&state=%s' % (code, state))
        return resp, post

    def test_guarda_los_dos_tokens_cifrados_y_el_user_id(self):
        resp, post = self._callback(self.id_roman)

        self.assertEqual(resp.status_code, 302)

        cuenta = db.session.get(CuentaCobro, self.id_roman)
        self.assertEqual(cuenta.id_cuenta_externa, USER_ID_ROMAN)

        credencial = CredencialCuentaCobro.query.filter_by(
            cuenta_cobro_id=self.id_roman).one()
        self.assertEqual(cripto.descifrar(credencial.access_token_cifrado), TOKEN_ROMAN)
        self.assertEqual(cripto.descifrar(credencial.refresh_token_cifrado), REFRESH_ROMAN)

        # Se pidio el token con los cinco campos del grant, redirect_uri incluido
        # (Mercado Pago lo exige y Tiendanube no).
        cuerpo = post.call_args.kwargs['json']
        self.assertEqual(cuerpo['grant_type'], 'authorization_code')
        self.assertEqual(cuerpo['code'], 'code-valido')
        self.assertEqual(cuerpo['client_id'], CLIENT_ID_FALSO)
        self.assertEqual(cuerpo['client_secret'], SECRET_FALSO)
        self.assertEqual(cuerpo['redirect_uri'], REDIRECT_FALSO)

    def test_ninguna_columna_guarda_un_token_en_texto_plano(self):
        """Lo importante de la slice: leer las columnas crudas no da los tokens."""
        self._callback(self.id_roman)

        from sqlalchemy import text
        fila = db.session.execute(text(
            'SELECT access_token_cifrado, refresh_token_cifrado '
            'FROM credencial_cuenta_cobro')).one()

        for guardado, original in zip(fila, (TOKEN_ROMAN, REFRESH_ROMAN)):
            self.assertIsNotNone(guardado)
            self.assertNotIn(original, guardado)
            # Es un token Fernet: prefijo de version 0x80 en base64url.
            self.assertTrue(guardado.startswith('gAAAAA'), guardado[:20])
            self.assertEqual(cripto.descifrar(guardado), original)

    def test_calcula_el_vencimiento_a_partir_de_expires_in(self):
        antes = datetime.utcnow()
        self._callback(self.id_roman)

        credencial = CredencialCuentaCobro.query.one()
        esperado = antes + timedelta(seconds=15552000)
        self.assertIsNotNone(credencial.expira_en)
        self.assertLess(abs((credencial.expira_en - esperado).total_seconds()), 60)

    def test_sin_refresh_token_la_conexion_igual_se_guarda(self):
        """Si la app no tiene offline_access no viene refresh_token.

        No es motivo para abortar: el access_token sirve igual por 180 dias.
        Lo que no puede pasar es que la cuenta quede a medias.
        """
        sin_refresh = self.respuesta_token(refresh=None)
        self._callback(self.id_roman, respuesta=sin_refresh)

        credencial = CredencialCuentaCobro.query.one()
        self.assertIsNotNone(credencial.access_token_cifrado)
        self.assertIsNone(credencial.refresh_token_cifrado)

    def test_reconectar_reutiliza_la_fila_de_credencial(self):
        self._callback(self.id_roman)
        self._callback(self.id_roman,
                       respuesta=self.respuesta_token(access='APP_USR-token-nuevo'))

        credencial = CredencialCuentaCobro.query.filter_by(
            cuenta_cobro_id=self.id_roman).one()
        self.assertEqual(cripto.descifrar(credencial.access_token_cifrado),
                         'APP_USR-token-nuevo')

    def test_el_state_es_de_un_solo_uso(self):
        """Reusar el mismo state con otro code no vuelve a escribir.

        Sin esto, un code viejo interceptado podria reciclar un state que ya
        cumplio su funcion.
        """
        self._callback(self.id_roman, state='state-unico')

        with mock.patch.object(mp.requests, 'post') as post, \
             mock.patch.dict(os.environ, ENTORNO_MP):
            resp = self.client.get(
                '/integraciones/mercadopago/callback?code=otro&state=state-unico')

        post.assert_not_called()
        self.assertEqual(resp.status_code, 302)


class TestCallbackState(BaseWeb):
    """El state guardado en sesion es lo unico que decide en que cuenta escribir.

    Es la parte que reemplaza lo que quedo pendiente de Tiendanube. Con DOS
    cuentas de DOS personas, un callback que escriba en la fila equivocada no
    es un problema teorico de CSRF: es la plata de Nachi apareciendo como la de
    Roman, o el token de uno guardado donde el otro lo va a usar.
    """

    def _assert_no_escribio_nada(self, resp):
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/integraciones', resp.headers['Location'])
        self.assertEqual(CredencialCuentaCobro.query.count(), 0)
        for cuenta_id in (self.id_roman, self.id_nachi):
            self.assertIsNone(db.session.get(CuentaCobro, cuenta_id).id_cuenta_externa)

    def test_state_distinto_al_de_la_sesion_no_escribe_en_ninguna_cuenta(self):
        self.sembrar_state('el-state-bueno', self.id_roman)

        with mock.patch.object(mp.requests, 'post') as post, \
             mock.patch.dict(os.environ, ENTORNO_MP):
            resp = self.client.get(
                '/integraciones/mercadopago/callback?code=code-valido&state=el-state-malo')

        # Ni siquiera se intenta canjear el code: se corta antes.
        post.assert_not_called()
        self._assert_no_escribio_nada(resp)

    def test_callback_sin_state_no_escribe_en_ninguna_cuenta(self):
        self.sembrar_state('el-state-bueno', self.id_roman)

        with mock.patch.object(mp.requests, 'post') as post, \
             mock.patch.dict(os.environ, ENTORNO_MP):
            resp = self.client.get('/integraciones/mercadopago/callback?code=code-valido')

        post.assert_not_called()
        self._assert_no_escribio_nada(resp)

    def test_callback_sin_nada_en_la_sesion_no_escribe_en_ninguna_cuenta(self):
        """El caso del link pegado en el navegador: no hay flujo empezado."""
        with mock.patch.object(mp.requests, 'post') as post, \
             mock.patch.dict(os.environ, ENTORNO_MP):
            resp = self.client.get(
                '/integraciones/mercadopago/callback?code=code-valido&state=inventado')

        post.assert_not_called()
        self._assert_no_escribio_nada(resp)

    def test_el_state_manda_sobre_el_querystring(self):
        """No se puede redirigir el token a otra cuenta pasando su id por la URL.

        La cuenta de destino sale SOLO de lo que quedo en sesion; nada de lo
        que venga en el callback puede cambiarla.
        """
        self.sembrar_state('state-de-nachi', self.id_nachi)

        with mock.patch.object(mp.requests, 'post',
                               return_value=self.respuesta_token(access=TOKEN_NACHI,
                                                                 user_id=USER_ID_NACHI)), \
             mock.patch.dict(os.environ, ENTORNO_MP):
            self.client.get('/integraciones/mercadopago/callback'
                            '?code=code-valido&state=state-de-nachi'
                            '&cuenta_cobro_id=%s' % self.id_roman)

        credencial = CredencialCuentaCobro.query.one()
        self.assertEqual(credencial.cuenta_cobro_id, self.id_nachi)

    def test_el_error_que_ve_el_usuario_no_es_un_stack_trace(self):
        with mock.patch.dict(os.environ, ENTORNO_MP):
            self.client.get('/integraciones/mercadopago/callback?code=x&state=y')
            pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('No se pudo validar la vuelta de Mercado Pago', pagina)
        self.assertNotIn('Traceback', pagina)


class TestCallbackFallido(BaseWeb):
    """Los caminos de error del canje: no se guarda nada a medias."""

    def _con_state(self, respuesta=None, side_effect=None, query='code=code-valido'):
        self.sembrar_state('state-valido', self.id_roman)
        kwargs = {'side_effect': side_effect} if side_effect else {'return_value': respuesta}
        with mock.patch.object(mp.requests, 'post', **kwargs), \
             mock.patch.dict(os.environ, ENTORNO_MP):
            return self.client.get(
                '/integraciones/mercadopago/callback?%s&state=state-valido' % query)

    def _assert_no_conecto(self, resp):
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(CredencialCuentaCobro.query.count(), 0)
        self.assertIsNone(db.session.get(CuentaCobro, self.id_roman).id_cuenta_externa)

    def test_code_rechazado_por_mercadopago(self):
        rechazo = RespuestaFalsa(400, {'error': 'invalid_grant'},
                                 texto='{"error":"invalid_grant"}')
        self._assert_no_conecto(self._con_state(respuesta=rechazo))

    def test_respuesta_200_con_cuerpo_de_error(self):
        rechazo = RespuestaFalsa(200, {'error': 'invalid_client',
                                       'message': 'client_secret no coincide'})
        self._assert_no_conecto(self._con_state(respuesta=rechazo))

    def test_respuesta_sin_access_token(self):
        incompleta = RespuestaFalsa(200, {'user_id': 123, 'token_type': 'Bearer'})
        self._assert_no_conecto(self._con_state(respuesta=incompleta))

    def test_red_caida(self):
        import requests as requests_real
        self._assert_no_conecto(
            self._con_state(side_effect=requests_real.ConnectionError('sin red')))

    def test_usuario_cancela_la_autorizacion(self):
        self.sembrar_state('state-valido', self.id_roman)
        with mock.patch.object(mp.requests, 'post') as post, \
             mock.patch.dict(os.environ, ENTORNO_MP):
            resp = self.client.get('/integraciones/mercadopago/callback'
                                   '?error=access_denied&state=state-valido')
        post.assert_not_called()
        self._assert_no_conecto(resp)

    def test_sin_client_secret_configurado_en_el_servidor(self):
        entorno = dict(os.environ)
        entorno.update(ENTORNO_MP)
        entorno.pop('MERCADOPAGO_CLIENT_SECRET')

        self.sembrar_state('state-valido', self.id_roman)
        with mock.patch.dict(os.environ, entorno, clear=True), \
             mock.patch.object(mp.requests, 'post') as post:
            resp = self.client.get('/integraciones/mercadopago/callback'
                                   '?code=code-valido&state=state-valido')

        post.assert_not_called()
        self._assert_no_conecto(resp)


class TestAislamientoEntreCuentas(BaseWeb):
    """Conectar una cuenta no toca ni lee nada de la otra."""

    def _conectar_por_oauth(self, cuenta_id, token, user_id):
        self.sembrar_state('state-%s' % cuenta_id, cuenta_id)
        with mock.patch.object(mp.requests, 'post',
                               return_value=self.respuesta_token(access=token,
                                                                 user_id=user_id)), \
             mock.patch.dict(os.environ, ENTORNO_MP):
            self.client.get('/integraciones/mercadopago/callback'
                            '?code=code-valido&state=state-%s' % cuenta_id)

    def test_conectar_roman_no_crea_ni_toca_la_credencial_de_nachi(self):
        self._conectar_por_oauth(self.id_roman, TOKEN_ROMAN, USER_ID_ROMAN)

        self.assertEqual(CredencialCuentaCobro.query.count(), 1)
        self.assertEqual(CredencialCuentaCobro.query.one().cuenta_cobro_id, self.id_roman)
        self.assertIsNone(db.session.get(CuentaCobro, self.id_nachi).id_cuenta_externa)

    def test_conectar_nachi_no_pisa_la_credencial_de_roman(self):
        self._conectar_por_oauth(self.id_roman, TOKEN_ROMAN, USER_ID_ROMAN)
        self._conectar_por_oauth(self.id_nachi, TOKEN_NACHI, USER_ID_NACHI)

        credenciales = {c.cuenta_cobro_id: c for c in CredencialCuentaCobro.query.all()}
        self.assertEqual(len(credenciales), 2)
        self.assertEqual(cripto.descifrar(credenciales[self.id_roman].access_token_cifrado),
                         TOKEN_ROMAN)
        self.assertEqual(cripto.descifrar(credenciales[self.id_nachi].access_token_cifrado),
                         TOKEN_NACHI)

        self.assertEqual(db.session.get(CuentaCobro, self.id_roman).id_cuenta_externa,
                         USER_ID_ROMAN)
        self.assertEqual(db.session.get(CuentaCobro, self.id_nachi).id_cuenta_externa,
                         USER_ID_NACHI)

    def test_cada_sync_usa_el_token_de_su_cuenta_y_trae_solo_sus_pagos(self):
        """El aislamiento no depende de acordarse de filtrar.

        Cada corrida arranca de un cuenta_cobro_id, usa el token de ESA fila y
        la API le devuelve solo los pagos de esa cuenta. El mock devuelve
        payloads distintos segun el Bearer: si el sync usara el token
        equivocado, los movimientos aparecerian en la cuenta que no es.
        """
        self.conectar(self.id_roman, TOKEN_ROMAN)
        self.conectar(self.id_nachi, TOKEN_NACHI)

        falso = get_por_token({TOKEN_ROMAN: PAGOS_ROMAN, TOKEN_NACHI: PAGOS_NACHI})
        with mock.patch.object(mp.requests, 'get', side_effect=falso):
            self.correr_sync(self.id_roman)
            self.correr_sync(self.id_nachi)

        de_roman = MovimientoCuenta.query.filter_by(cuenta_id=self.id_roman).all()
        de_nachi = MovimientoCuenta.query.filter_by(cuenta_id=self.id_nachi).all()

        # Dos de los tres pagos de Roman: el tercero esta pendiente.
        self.assertEqual({m.id_externo_procesador for m in de_roman}, {'70001', '70002'})
        self.assertEqual({m.id_externo_procesador for m in de_nachi}, {'80001'})

    def test_sincronizar_una_cuenta_no_borra_los_movimientos_de_la_otra(self):
        self.conectar(self.id_roman, TOKEN_ROMAN)
        self.conectar(self.id_nachi, TOKEN_NACHI)

        falso = get_por_token({TOKEN_ROMAN: PAGOS_ROMAN, TOKEN_NACHI: PAGOS_NACHI})
        with mock.patch.object(mp.requests, 'get', side_effect=falso):
            self.correr_sync(self.id_nachi)
            self.correr_sync(self.id_roman)
            self.correr_sync(self.id_roman)

        self.assertEqual(
            MovimientoCuenta.query.filter_by(cuenta_id=self.id_nachi).count(), 1)


# ---------------------------------------------------------------------------
# PARTE 3 - Movimientos
# ---------------------------------------------------------------------------

class TestMontoNeto(unittest.TestCase):
    """Como se calcula lo que REALMENTE entro. Funciones puras, sin base."""

    def test_resta_la_comision_del_cobrador(self):
        neto, aviso = ingestor.monto_neto(pago(1, '12500.50', comision='875.04'))
        self.assertEqual(neto, Decimal('11625.46'))
        self.assertIsNone(aviso)

    def test_el_neto_es_decimal_nunca_float(self):
        neto, _ = ingestor.monto_neto(pago(1, '12500.50', comision='875.04'))
        self.assertIsInstance(neto, Decimal)

    def test_suma_varias_comisiones(self):
        detalles = [
            {'type': 'mercadopago_fee', 'amount': '875.04', 'fee_payer': 'collector'},
            {'type': 'application_fee', 'amount': '100.00', 'fee_payer': 'collector'},
        ]
        neto, aviso = ingestor.monto_neto(pago(1, '12500.50', fee_details=detalles))
        self.assertEqual(neto, Decimal('11525.46'))
        self.assertIsNone(aviso)

    def test_no_resta_las_comisiones_que_paga_el_comprador(self):
        """El costo de financiacion en cuotas lo paga el comprador.

        Restarlo subestimaria lo que entro a la cuenta: esa plata nunca salio
        de nuestro bolsillo.
        """
        detalles = [
            {'type': 'mercadopago_fee', 'amount': '875.04', 'fee_payer': 'collector'},
            {'type': 'financing_fee', 'amount': '2000.00', 'fee_payer': 'payer'},
        ]
        neto, _ = ingestor.monto_neto(pago(1, '12500.50', fee_details=detalles))
        self.assertEqual(neto, Decimal('11625.46'))

    def test_sin_fee_payer_se_asume_el_cobrador(self):
        detalles = [{'type': 'mercadopago_fee', 'amount': '875.04'}]
        neto, aviso = ingestor.monto_neto(pago(1, '12500.50', fee_details=detalles))
        self.assertEqual(neto, Decimal('11625.46'))
        self.assertIsNone(aviso)

    def test_fee_details_vacio_cae_a_monto_bruto_y_no_es_un_aviso(self):
        """Un cobro sin comision es legitimo (efectivo, dinero en cuenta)."""
        neto, aviso = ingestor.monto_neto(pago(1, '12500.50', fee_details=[]))
        self.assertEqual(neto, Decimal('12500.50'))
        self.assertIsNone(aviso)

    def test_sin_fee_details_cae_a_monto_bruto(self):
        neto, aviso = ingestor.monto_neto(pago(1, '12500.50'))
        self.assertEqual(neto, Decimal('12500.50'))
        self.assertIsNone(aviso)

    def test_fee_details_ilegible_descarta_esa_linea_y_avisa(self):
        """Se guarda el pago con la comision de menos, y queda el aviso.

        El criterio esta documentado en ingestor_mercadopago: descartar el pago
        lo haria faltar entero (un error de miles), guardarlo con una comision
        de menos lo deja corto por unos cientos y el aviso queda en sync_log.
        """
        detalles = [
            {'type': 'mercadopago_fee', 'amount': 'no-es-un-numero', 'fee_payer': 'collector'},
            {'type': 'application_fee', 'amount': '100.00', 'fee_payer': 'collector'},
        ]
        neto, aviso = ingestor.monto_neto(pago(1, '12500.50', fee_details=detalles))
        self.assertEqual(neto, Decimal('12400.50'))
        self.assertIsNotNone(aviso)
        self.assertIn('no se pudieron leer', aviso)

    def test_fee_details_con_forma_inesperada_cae_a_bruto_y_avisa(self):
        crudo = pago(1, '12500.50')
        crudo['fee_details'] = {'amount': '875.04'}  # dict, no lista
        neto, aviso = ingestor.monto_neto(crudo)
        self.assertEqual(neto, Decimal('12500.50'))
        self.assertIn('formato inesperado', aviso)

    def test_sin_transaction_amount_el_pago_se_saltea(self):
        crudo = pago(1, None)
        with self.assertRaises(ingestor.PagoIgnorado):
            ingestor.monto_neto(crudo)


class TestNormalizacion(unittest.TestCase):
    """El mapeo de un pago a los campos de movimiento_cuenta."""

    def test_usa_la_fecha_de_aprobacion_en_utc_naive(self):
        crudo = pago(1, '100.00', fecha='2026-08-14T10:20:30.000-03:00')
        datos = ingestor.normalizar_movimiento(crudo)
        self.assertEqual(datos['fecha'], datetime(2026, 8, 14, 13, 20, 30))
        self.assertIsNone(datos['fecha'].tzinfo)

    def test_acepta_offset_sin_dos_puntos(self):
        crudo = pago(1, '100.00', fecha='2026-08-14T10:20:30.000-0300')
        datos = ingestor.normalizar_movimiento(crudo)
        self.assertEqual(datos['fecha'], datetime(2026, 8, 14, 13, 20, 30))

    def test_acepta_la_z_de_utc(self):
        crudo = pago(1, '100.00', fecha='2026-08-14T10:20:30.000Z')
        datos = ingestor.normalizar_movimiento(crudo)
        self.assertEqual(datos['fecha'], datetime(2026, 8, 14, 10, 20, 30))

    def test_un_pago_no_aprobado_no_es_un_movimiento(self):
        for estado in ('pending', 'rejected', 'cancelled', 'in_process'):
            with self.subTest(estado=estado):
                with self.assertRaises(ingestor.PagoIgnorado):
                    ingestor.normalizar_movimiento(pago(1, '100.00', estado=estado))

    def test_el_hash_se_deriva_solo_del_id_del_pago(self):
        """Estable aunque cambien monto, fecha o comision.

        Si el hash dependiera del monto, un pago cuya comision se ajusta
        despues entraria dos veces, que es justo lo que la columna impide.
        """
        uno = ingestor.hash_movimiento(pago(70001, '100.00', comision='7.00'))
        otro = ingestor.hash_movimiento(
            pago(70001, '999.99', comision='70.00', fecha='2027-01-01T00:00:00.000Z'))
        self.assertEqual(uno, otro)
        self.assertEqual(len(uno), 64)

    def test_ids_distintos_dan_hashes_distintos(self):
        self.assertNotEqual(ingestor.hash_movimiento(pago(70001, '100.00')),
                            ingestor.hash_movimiento(pago(70002, '100.00')))

    def test_un_pago_sin_id_se_saltea(self):
        crudo = pago(1, '100.00')
        crudo['id'] = None
        with self.assertRaises(ingestor.PagoIgnorado):
            ingestor.normalizar_movimiento(crudo)

    def test_el_tipo_del_movimiento_es_cobro(self):
        datos = ingestor.normalizar_movimiento(pago(1, '100.00', comision='7.00'))
        self.assertEqual(datos['tipo'], 'cobro')


class TestPaginacion(unittest.TestCase):
    """Ventanas de fecha y offset. La API limita el rango a 365 dias."""

    def test_parte_el_historico_en_ventanas_que_no_superan_el_limite(self):
        desde = datetime(2020, 1, 1)
        hasta = datetime(2026, 9, 1)
        tramos = mp.ventanas(desde, hasta)

        self.assertGreater(len(tramos), 1)
        self.assertEqual(tramos[0][0], desde)
        self.assertEqual(tramos[-1][1], hasta)
        for inicio, fin in tramos:
            self.assertLessEqual((fin - inicio).days, 365)

    def test_las_ventanas_son_contiguas_y_no_dejan_agujeros(self):
        tramos = mp.ventanas(datetime(2024, 1, 1), datetime(2026, 9, 1))
        for anterior, siguiente in zip(tramos, tramos[1:]):
            self.assertEqual(anterior[1], siguiente[0])

    def test_rango_vacio_no_genera_ventanas(self):
        momento = datetime(2026, 1, 1)
        self.assertEqual(mp.ventanas(momento, momento), [])

    def test_avanza_el_offset_con_lo_que_devolvio_la_api_no_con_lo_que_pidio(self):
        """La doc de Mercado Pago no es consistente sobre el maximo de `limit`.

        Si la API ignora el limit pedido y devuelve menos, avanzar el offset de
        a LIMITE_POR_PAGINA saltearia pagos. Por eso se avanza con len(results).
        """
        llamadas = []
        # 5 pagos en total, la API los entrega de a 2 aunque se pidan 100.
        todos = [pago(90000 + i, '100.00') for i in range(5)]

        def _get(url, headers=None, params=None, timeout=None):
            offset = params['offset']
            llamadas.append(offset)
            # Solo la primera ventana tiene datos.
            if params['begin_date'] > '2021':
                return respuesta_de_busqueda([], total=0)
            return RespuestaFalsa(200, {
                'paging': {'total': 5, 'limit': 2, 'offset': offset},
                'results': todos[offset:offset + 2],
            })

        with mock.patch.object(mp.requests, 'get', side_effect=_get):
            pagos = mp.traer_pagos('token', desde=datetime(2020, 1, 1),
                                   hasta=datetime(2020, 6, 1))

        self.assertEqual(len(pagos), 5)
        self.assertEqual(llamadas, [0, 2, 4])

    def test_una_pagina_vacia_corta_aunque_el_total_diga_otra_cosa(self):
        """Sin esta guarda, un paging.total mentiroso gira hasta MAX_PAGINAS."""
        def _get(url, headers=None, params=None, timeout=None):
            return RespuestaFalsa(200, {
                'paging': {'total': 999, 'limit': 100, 'offset': params['offset']},
                'results': [],
            })

        with mock.patch.object(mp.requests, 'get', side_effect=_get) as get:
            pagos = mp.traer_pagos('token', desde=datetime(2020, 1, 1),
                                   hasta=datetime(2020, 6, 1))

        self.assertEqual(pagos, [])
        self.assertEqual(get.call_count, 1)

    def test_filtra_por_fecha_de_aprobacion_y_no_de_creacion(self):
        """Lo que se reconstruye es cuando entro la plata, no cuando se pidio."""
        with mock.patch.object(mp.requests, 'get',
                               return_value=respuesta_de_busqueda([])) as get:
            mp.traer_pagos('token', desde=datetime(2026, 1, 1),
                           hasta=datetime(2026, 6, 1))

        params = get.call_args.kwargs['params']
        self.assertEqual(params['range'], 'date_approved')
        self.assertEqual(params['sort'], 'date_approved')
        self.assertIn('begin_date', params)
        self.assertIn('end_date', params)

    def test_la_primera_corrida_barre_todo_el_historico(self):
        """Sin `desde` va varios anios para atras, no solo lo reciente."""
        with mock.patch.object(mp.requests, 'get',
                               return_value=respuesta_de_busqueda([])) as get:
            mp.traer_pagos('token')

        primeras = [llamada.kwargs['params']['begin_date'] for llamada in get.call_args_list]
        anio_mas_viejo = int(min(primeras)[:4])
        self.assertLessEqual(anio_mas_viejo, datetime.utcnow().year - 5)


class TestRateLimit(unittest.TestCase):
    """429 con backoff, sin dormir de verdad en los tests."""

    def test_reintenta_el_429_y_respeta_retry_after(self):
        respuestas = [
            RespuestaFalsa(429, {'message': 'too many requests'},
                           headers={'Retry-After': '3'}),
            respuesta_de_busqueda([]),
        ]
        with mock.patch.object(mp.requests, 'get', side_effect=respuestas), \
             mock.patch.object(mp.time, 'sleep') as dormir:
            mp._get_api('v1/payments/search', 'token')

        dormir.assert_called_once_with(3.0)

    def test_sin_retry_after_usa_backoff_exponencial(self):
        respuestas = [
            RespuestaFalsa(429, {'message': 'too many requests'}),
            RespuestaFalsa(429, {'message': 'too many requests'}),
            respuesta_de_busqueda([]),
        ]
        with mock.patch.object(mp.requests, 'get', side_effect=respuestas), \
             mock.patch.object(mp.time, 'sleep') as dormir:
            mp._get_api('v1/payments/search', 'token')

        self.assertEqual([c.args[0] for c in dormir.call_args_list], [1.0, 2.0])

    def test_un_429_que_no_afloja_termina_en_error_legible(self):
        siempre_429 = RespuestaFalsa(429, {'message': 'too many requests'})
        with mock.patch.object(mp.requests, 'get', return_value=siempre_429), \
             mock.patch.object(mp.time, 'sleep'):
            with self.assertRaises(mp.ErrorMercadoPago) as caja:
                mp._get_api('v1/payments/search', 'token')

        self.assertIn('limitando las consultas', str(caja.exception))

    def test_la_espera_tiene_techo(self):
        absurdo = RespuestaFalsa(429, {}, headers={'Retry-After': '99999'})
        self.assertEqual(mp._espera_tras_429(absurdo, 0), mp.ESPERA_MAXIMA)


class TestSyncDeMovimientos(BaseWeb):
    """La corrida que escribe en movimiento_cuenta."""

    def setUp(self):
        super().setUp()
        self.conectar(self.id_roman, TOKEN_ROMAN)

    def _sync(self, pagos=None):
        with mock.patch.object(mp.requests, 'get',
                               side_effect=get_falso(PAGOS_ROMAN if pagos is None else pagos)):
            return self.correr_sync(self.id_roman)

    def test_escribe_un_movimiento_por_pago_aprobado(self):
        resultado = self._sync()

        movimientos = MovimientoCuenta.query.filter_by(cuenta_id=self.id_roman).all()
        self.assertEqual(len(movimientos), 2)
        self.assertEqual(resultado['nuevos'], 2)
        # El pendiente se saltea, no cuenta como error.
        self.assertEqual(resultado['salteados'], 1)
        self.assertEqual(resultado['error'], 0)

    def test_el_monto_guardado_es_el_neto(self):
        self._sync()

        movimiento = MovimientoCuenta.query.filter_by(
            id_externo_procesador='70001').one()
        self.assertEqual(movimiento.monto, Decimal('11625.46'))
        self.assertEqual(movimiento.tipo, 'cobro')
        self.assertEqual(movimiento.moneda, 'ARS')

    def test_guarda_el_id_del_pago_y_un_hash_derivado_de_el(self):
        self._sync()

        movimiento = MovimientoCuenta.query.filter_by(
            id_externo_procesador='70002').one()
        self.assertEqual(movimiento.hash_dedup,
                         ingestor.hash_movimiento({'id': 70002}))

    def test_guarda_el_payload_crudo_para_poder_reprocesar(self):
        self._sync()

        movimiento = MovimientoCuenta.query.filter_by(
            id_externo_procesador='70001').one()
        self.assertEqual(movimiento.raw_payload['id'], 70001)

    def test_resincronizar_dos_veces_no_duplica_movimientos(self):
        """Lo que hace idempotente al boton."""
        self._sync()
        resultado = self._sync()

        self.assertEqual(
            MovimientoCuenta.query.filter_by(cuenta_id=self.id_roman).count(), 2)
        self.assertEqual(resultado['nuevos'], 0)
        self.assertEqual(resultado['actualizados'], 2)

    def test_resincronizar_tres_veces_tampoco(self):
        self._sync()
        self._sync()
        self._sync()
        self.assertEqual(
            MovimientoCuenta.query.filter_by(cuenta_id=self.id_roman).count(), 2)

    def test_un_pago_que_cambio_de_comision_se_actualiza_en_su_fila(self):
        self._sync()
        # Mercado Pago ajusta la comision despues de acreditar.
        ajustado = [pago(70001, '12500.50', comision='1000.00')]
        self._sync(pagos=ajustado)

        movimiento = MovimientoCuenta.query.filter_by(
            id_externo_procesador='70001').one()
        self.assertEqual(movimiento.monto, Decimal('11500.50'))

    def test_resincronizar_no_deshace_una_conciliacion(self):
        """Si una slice futura ya concilio el movimiento, el sync no lo pisa."""
        self._sync()
        movimiento = MovimientoCuenta.query.filter_by(
            id_externo_procesador='70001').one()
        movimiento.conciliado = True
        db.session.commit()

        self._sync()

        recargado = MovimientoCuenta.query.filter_by(
            id_externo_procesador='70001').one()
        self.assertTrue(recargado.conciliado)

    def test_un_pago_con_fee_details_roto_no_voltea_la_corrida(self):
        pagos = [
            pago(70001, '12500.50', comision='875.04'),
            pago(70002, '4300.99', fee_details=[{'type': 'mercadopago_fee',
                                                 'amount': 'ilegible',
                                                 'fee_payer': 'collector'}]),
            pago(70004, '1000.00', comision='70.00'),
        ]
        resultado = self._sync(pagos=pagos)

        # Los tres se guardaron; el del medio con el bruto.
        self.assertEqual(resultado['nuevos'], 3)
        self.assertEqual(resultado['error'], 0)
        roto = MovimientoCuenta.query.filter_by(id_externo_procesador='70002').one()
        self.assertEqual(roto.monto, Decimal('4300.99'))

    def test_el_fee_details_roto_queda_registrado_en_el_sync_log(self):
        pagos = [pago(70002, '4300.99', fee_details=[{'amount': 'ilegible'}])]
        self._sync(pagos=pagos)

        fila = SyncLog.query.filter_by(cuenta_cobro_id=self.id_roman).one()
        self.assertEqual(fila.estado, 'parcial')
        self.assertIn('70002', fila.mensaje_error)

    def test_una_corrida_limpia_cierra_el_sync_log_en_ok(self):
        self._sync()

        fila = SyncLog.query.filter_by(cuenta_cobro_id=self.id_roman).one()
        self.assertEqual(fila.estado, 'ok')
        self.assertEqual(fila.entidad, 'movimiento')
        self.assertEqual(fila.registros_nuevos, 2)
        self.assertIsNotNone(fila.fecha_fin)

    def test_marca_la_fecha_de_ultima_sync_en_la_cuenta(self):
        self._sync()
        self.assertIsNotNone(db.session.get(CuentaCobro, self.id_roman).fecha_ultima_sync)

    def test_no_toca_pedidos_ni_pagos(self):
        """La conciliacion es la proxima slice: esta no escribe ahi."""
        from models import Pago, Pedido

        self._sync()

        self.assertEqual(Pedido.query.count(), 0)
        self.assertEqual(Pago.query.count(), 0)


class TestTokenVencido(BaseWeb):
    """El mensaje tiene que decir que hacer, no mostrar un stack trace."""

    def test_expira_en_pasado_corta_antes_de_pegarle_a_la_api(self):
        self.conectar(self.id_roman, TOKEN_ROMAN,
                      expira_en=datetime.utcnow() - timedelta(days=1))

        with mock.patch.object(mp.requests, 'get') as get:
            with self.assertRaises(mp.ErrorMercadoPago) as caja:
                self.correr_sync(self.id_roman)

        get.assert_not_called()
        self.assertTrue(caja.exception.reconectar)
        self.assertIn('Reconecta esta cuenta', str(caja.exception))

    def test_un_401_de_la_api_se_traduce_a_reconectar(self):
        self.conectar(self.id_roman, TOKEN_ROMAN)
        rechazo = RespuestaFalsa(401, {'message': 'invalid access token',
                                       'status': 401})

        with mock.patch.object(mp.requests, 'get', return_value=rechazo):
            with self.assertRaises(mp.ErrorMercadoPago) as caja:
                self.correr_sync(self.id_roman)

        self.assertTrue(caja.exception.reconectar)
        self.assertIn('Reconecta esta cuenta', str(caja.exception))

    def test_el_thread_deja_el_mensaje_legible_en_el_sync_log(self):
        """Lo que Roman ve en la pantalla cuando el token vencio."""
        self.conectar(self.id_roman, TOKEN_ROMAN,
                      expira_en=datetime.utcnow() - timedelta(days=1))

        arranque = datetime.utcnow()
        db.session.add(SyncLog(
            cuenta_cobro_id=self.id_roman, entidad='movimiento',
            operacion=sync_mercadopago.OPERACION, estado='corriendo',
            fecha_inicio=arranque))
        db.session.commit()

        try:
            sync_mercadopago.correr_backfill(self.id_roman, arranque)
        except mp.ErrorMercadoPago as exc:
            sync_mercadopago._cerrar_con_error(self.id_roman, arranque, exc)

        fila = SyncLog.query.filter_by(cuenta_cobro_id=self.id_roman).one()
        self.assertEqual(fila.estado, 'error')
        self.assertIn('Reconecta esta cuenta', fila.mensaje_error)
        self.assertNotIn('Traceback', fila.mensaje_error)

        pagina = self.client.get('/integraciones').get_data(as_text=True)
        self.assertIn('Reconecta esta cuenta', pagina)
        self.assertNotIn('Traceback', pagina)

    def test_una_cuenta_sin_credencial_dice_que_hay_que_conectarla(self):
        with mock.patch.object(mp.requests, 'get') as get:
            with self.assertRaises(mp.ErrorMercadoPago) as caja:
                self.correr_sync(self.id_nachi)

        get.assert_not_called()
        self.assertIn('no esta conectada', str(caja.exception))

    def test_un_token_que_no_descifra_pide_reconectar(self):
        """Pasa si se rota CREDENTIALS_ENCRYPTION_KEY sin recifrar."""
        credencial = self.conectar(self.id_roman, TOKEN_ROMAN)
        credencial.access_token_cifrado = 'gAAAAA-esto-no-es-un-token-valido'
        db.session.commit()

        with self.assertRaises(mp.ErrorMercadoPago) as caja:
            self.correr_sync(self.id_roman)

        self.assertTrue(caja.exception.reconectar)
        self.assertIn('Reconecta esta cuenta', str(caja.exception))


# ---------------------------------------------------------------------------
# PARTE 4 - Vista y ruta de sincronizacion
# ---------------------------------------------------------------------------

class TestRutaSincronizar(BaseWeb):
    """POST /integraciones/mercadopago/sincronizar/<id>."""

    def test_no_le_pega_a_la_api_dentro_del_request(self):
        """El backfill corre en un thread: el request vuelve enseguida.

        Se corta el thread para que el test no dependa de su timing; lo que se
        verifica es que durante el ciclo request/response no se salio a la red.
        """
        self.conectar(self.id_roman, TOKEN_ROMAN)

        with mock.patch.object(mp.requests, 'get') as get, \
             mock.patch.object(sync_mercadopago.threading, 'Thread') as hilo:
            resp = self.client.post(
                '/integraciones/mercadopago/sincronizar/%s' % self.id_roman)

        self.assertEqual(resp.status_code, 302)
        get.assert_not_called()
        hilo.assert_called_once()

    def test_deja_la_corrida_marcada_como_corriendo(self):
        self.conectar(self.id_roman, TOKEN_ROMAN)

        with mock.patch.object(sync_mercadopago.threading, 'Thread'):
            self.client.post('/integraciones/mercadopago/sincronizar/%s' % self.id_roman)

        fila = SyncLog.query.filter_by(cuenta_cobro_id=self.id_roman).one()
        self.assertEqual(fila.estado, 'corriendo')
        self.assertEqual(fila.entidad, 'movimiento')

    def test_una_cuenta_sin_conectar_no_dispara_nada(self):
        with mock.patch.object(sync_mercadopago.threading, 'Thread') as hilo:
            self.client.post('/integraciones/mercadopago/sincronizar/%s' % self.id_roman)

        hilo.assert_not_called()
        self.assertEqual(SyncLog.query.count(), 0)

    def test_no_arranca_una_segunda_corrida_si_ya_hay_una(self):
        self.conectar(self.id_roman, TOKEN_ROMAN)

        with mock.patch.object(sync_mercadopago.threading, 'Thread') as hilo:
            self.client.post('/integraciones/mercadopago/sincronizar/%s' % self.id_roman)
            self.client.post('/integraciones/mercadopago/sincronizar/%s' % self.id_roman)

        self.assertEqual(hilo.call_count, 1)
        self.assertEqual(SyncLog.query.count(), 1)

    def test_no_se_puede_sincronizar_la_cuenta_de_otra_empresa(self):
        otra = Empresa(nombre='Empresa Ajena')
        db.session.add(otra)
        db.session.flush()
        ajena = CuentaCobro(empresa_id=otra.id, nombre='MP Ajena',
                            tipo='mercadopago', metodo_ingesta='api')
        db.session.add(ajena)
        db.session.flush()
        db.session.add(CredencialCuentaCobro(
            cuenta_cobro_id=ajena.id,
            access_token_cifrado=cripto.cifrar('token-ajeno')))
        db.session.commit()

        with mock.patch.object(sync_mercadopago.threading, 'Thread') as hilo:
            self.client.post('/integraciones/mercadopago/sincronizar/%s' % ajena.id)

        hilo.assert_not_called()
        self.assertEqual(SyncLog.query.count(), 0)

    def test_requiere_login(self):
        # La cuenta se conecta a proposito ANTES del request. Sin credencial,
        # lanzar_backfill corta en "primero conecta la cuenta" y no escribe
        # ningun sync_log, asi que el assertEqual(count, 0) de abajo se cumplia
        # con sesion y sin sesion: el test pasaba igual sin el
        # @login_required. Con la cuenta conectada, este mismo POST logueado
        # deja una fila en 'corriendo' (lo prueba
        # test_deja_la_corrida_marcada_como_corriendo), asi que ahora el 0 solo
        # puede venir de que no habia sesion.
        self.conectar(self.id_roman, TOKEN_ROMAN)

        with mock.patch.object(sync_mercadopago.threading, 'Thread') as hilo:
            resp = request_anonimo(
                self.ctx, 'post',
                '/integraciones/mercadopago/sincronizar/%s' % self.id_roman)

        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])
        hilo.assert_not_called()
        self.assertEqual(SyncLog.query.count(), 0)

    def test_no_responde_a_get(self):
        resp = self.client.get('/integraciones/mercadopago/sincronizar/%s' % self.id_roman)
        self.assertEqual(resp.status_code, 405)


class TestVista(BaseWeb):
    """GET /integraciones, la parte de cuentas de cobro."""

    def test_lista_las_dos_cuentas_con_su_alias(self):
        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('Roman - Presencial y Tiendanube', pagina)
        self.assertIn('Nachi - Mercado Libre', pagina)
        self.assertIn('Sin conectar', pagina)

    def test_muestra_boton_conectar_por_cada_cuenta(self):
        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('/integraciones/mercadopago/conectar/%s' % self.id_roman, pagina)
        self.assertIn('/integraciones/mercadopago/conectar/%s' % self.id_nachi, pagina)

    def test_la_cuenta_conectada_muestra_su_id_externo_y_el_boton_sincronizar(self):
        self.conectar(self.id_roman, TOKEN_ROMAN)
        cuenta = db.session.get(CuentaCobro, self.id_roman)
        cuenta.id_cuenta_externa = USER_ID_ROMAN
        db.session.commit()

        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('Conectada', pagina)
        self.assertIn(USER_ID_ROMAN, pagina)
        self.assertIn('/integraciones/mercadopago/sincronizar/%s' % self.id_roman, pagina)

    def test_muestra_el_total_y_la_fecha_del_movimiento_mas_reciente(self):
        """El numero que Roman quiere ver."""
        self.conectar(self.id_roman, TOKEN_ROMAN)
        with mock.patch.object(mp.requests, 'get', side_effect=get_falso(PAGOS_ROMAN)):
            self.correr_sync(self.id_roman)

        pagina = self.client.get('/integraciones').get_data(as_text=True)

        # 11625.46 + 3999.92
        self.assertIn('15625.38', pagina)
        self.assertIn('02/04/2026', pagina)

    def test_una_cuenta_con_token_vencido_ofrece_reconectar(self):
        self.conectar(self.id_roman, TOKEN_ROMAN,
                      expira_en=datetime.utcnow() - timedelta(days=1))

        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('Acceso vencido', pagina)
        self.assertIn('Reconectar', pagina)

    def test_no_muestra_cuentas_de_otra_empresa(self):
        otra = Empresa(nombre='Empresa Ajena')
        db.session.add(otra)
        db.session.flush()
        db.session.add(CuentaCobro(empresa_id=otra.id, nombre='Cuenta Ajena SRL',
                                   tipo='mercadopago', metodo_ingesta='api',
                                   id_cuenta_externa='999888777'))
        db.session.commit()

        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertNotIn('Cuenta Ajena SRL', pagina)
        self.assertNotIn('999888777', pagina)

    def test_los_canales_de_venta_siguen_apareciendo(self):
        """La seccion nueva no se comio la de FASE3-S1/S2."""
        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('Tiendanube', pagina)
        self.assertIn('Mercado Libre', pagina)
        self.assertIn('/integraciones/tiendanube/conectar', pagina)


class TestNoSePegaALaApiReal(unittest.TestCase):
    """Que no haya otra puerta de salida a la red que la que se mockea."""

    def test_todas_las_urls_son_de_mercadopago(self):
        for url in (mp.URL_AUTORIZAR, mp.URL_TOKEN, mp.URL_API):
            self.assertTrue(url.startswith('https://'), url)
            self.assertIn('mercadopago.com', url)

    def test_el_unico_cliente_http_es_requests_en_integracion_mercadopago(self):
        """sync_ e ingestor_ no importan requests: si lo hicieran, un test
        podria mockear mp.requests y estar dejando una llamada real viva."""
        import ingestor_mercadopago
        import sync_mercadopago as sm

        for modulo in (ingestor_mercadopago, sm):
            with self.subTest(modulo=modulo.__name__):
                self.assertFalse(hasattr(modulo, 'requests'),
                                 '%s importa requests por su cuenta' % modulo.__name__)

    def test_mockear_requests_deja_al_cliente_sin_salida(self):
        with mock.patch.object(mp.requests, 'get') as get, \
             mock.patch.object(mp.requests, 'post') as post:
            get.side_effect = AssertionError('salio a la red')
            post.side_effect = AssertionError('salio a la red')

            with self.assertRaises(AssertionError):
                mp._get_api('v1/payments/search', 'token')
