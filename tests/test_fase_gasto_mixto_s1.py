# -*- coding: utf-8 -*-
"""Tests de FASE-GASTO-MIXTO-S1 (un gasto pagado entre varias cuentas).

    python -m unittest discover -s tests -v

LA PREGUNTA

Un gasto de $68.000 pagado 53.000 de la cuenta de Roman y 15.000 de la de
Nachi es UN pago real. Hasta esta slice el esquema no lo podia representar --
`origen_fondo` era 'facturacion' (una cuenta) o 'capital' (ninguna) -- y habia
que cargarlo como DOS gastos, que lo duplicaba en Caja General.

Lo que se prueba:

    el mixto se guarda         -> un gasto, dos filas de reparto
    la suma tiene que cerrar   -> 53.000 + 15.000 != 67.000 se rechaza con un
                                  mensaje que se entiende, y NO guarda nada
    el cero es un dato         -> una cuenta con 0 es valido y se guarda
    caja-socio lo reparte      -> el ejemplo real le resta 53.000 a Roman y
                                  15.000 a Nachi, no 68.000 a ninguno de los
                                  dos ni a los dos
    facturacion sigue igual    -> el cambio de esquema no toca el camino viejo

EL EJEMPLO ES EL REAL

$68.000 dividido 53.000 / 15.000 no es un numero de adorno: es el caso que
motivo la slice, y aparece tal cual en los tests para que si alguna vez se
rompe, se rompa con los numeros que Roman reconoce.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.ayuda_auth import request_anonimo  # noqa: E402,F401
from app import app  # noqa: E402
from models import (  # noqa: E402
    ORIGEN_CAPITAL,
    ORIGEN_FACTURACION,
    ORIGEN_MIXTO,
    Categoria,
    CuentaCobro,
    Empresa,
    Gasto,
    GastoOrigenMixto,
    Usuario,
    db,
)
from rutas_reportes import _gastado_por_socio  # noqa: E402


ENGINE_PRODUCTIVO = None

# El ejemplo real que motivo la slice.
TOTAL = Decimal('68000.00')
PARTE_ROMAN = Decimal('53000.00')
PARTE_NACHI = Decimal('15000.00')


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


class BaseMixto(unittest.TestCase):
    """Una empresa, un usuario, las dos cuentas de cobro y una categoria."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test GASTO-MIXTO-S1')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test',
                               email='gastomixto1@test.local',
                               empresa_id=self.empresa.id, rol='admin',
                               verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.cuenta_roman = CuentaCobro(empresa_id=self.empresa.id,
                                        nombre='Roman - Presencial y Tiendanube',
                                        tipo='mercadopago', socio='roman')
        self.cuenta_nachi = CuentaCobro(empresa_id=self.empresa.id,
                                        nombre='Nachi - Mercado Libre',
                                        tipo='mercadopago', socio='nachi')
        db.session.add_all([self.cuenta_roman, self.cuenta_nachi])

        self.categoria = Categoria(nombre='Materiales', tipo='gasto',
                                   empresa_id=self.empresa.id)
        db.session.add(self.categoria)
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.id_cuenta_roman = self.cuenta_roman.id
        self.id_cuenta_nachi = self.cuenta_nachi.id
        self.id_categoria = self.categoria.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def form(self, **extra):
        """Los campos que /gasto/nuevo siempre pide, mas lo que pruebe el test."""
        datos = {
            'descripcion': 'Compra de materiales',
            'fecha': '2026-09-01',
            'monto': str(TOTAL),
            'categoria_id': str(self.id_categoria),
        }
        datos.update(extra)
        return datos

    def alta(self, **extra):
        return self.client.post('/gasto/nuevo', data=self.form(**extra),
                                follow_redirects=True)

    def alta_mixta(self, roman=PARTE_ROMAN, nachi=PARTE_NACHI, **extra):
        """El alta del ejemplo real, con las partes por defecto."""
        return self.alta(origen_fondo=ORIGEN_MIXTO,
                         **dict({'parte_%d' % self.id_cuenta_roman: str(roman),
                                 'parte_%d' % self.id_cuenta_nachi: str(nachi)},
                                **extra))

    def gastos(self):
        return Gasto.query.order_by(Gasto.id).all()

    def partes(self):
        return GastoOrigenMixto.query.order_by(GastoOrigenMixto.id).all()

    def texto(self, respuesta):
        return respuesta.get_data(as_text=True)


class TestAltaMixta(BaseMixto):
    """PARTE 3: la validacion al guardar."""

    def test_gasto_mixto_se_guarda_con_detalle(self):
        """$68.000 dividido 53.000 / 15.000: UN gasto y DOS filas de reparto.

        El punto de la slice en una sola assertion: donde antes hacian falta
        dos gastos -- y el total aparecia dos veces en Caja General -- ahora
        hay uno solo, con el detalle al costado.
        """
        self.alta_mixta()

        gastos = self.gastos()
        self.assertEqual(len(gastos), 1, 'un pago real es UN gasto')
        gasto = gastos[0]
        self.assertEqual(gasto.origen_fondo, ORIGEN_MIXTO)
        self.assertEqual(gasto.monto, TOTAL)
        # Sin cuenta: no hay UNA cuenta de la que haya salido, y poner
        # cualquiera de las dos diria que salio entero de esa.
        self.assertIsNone(gasto.cuenta_pago_id)

        reparto = {p.cuenta_cobro_id: p.monto for p in self.partes()}
        self.assertEqual(reparto, {self.id_cuenta_roman: PARTE_ROMAN,
                                   self.id_cuenta_nachi: PARTE_NACHI})
        # Y el reparto suma el gasto, que es la regla que lo vuelve creible.
        self.assertEqual(sum(reparto.values()), TOTAL)

    def test_gasto_mixto_rechaza_suma_que_no_cierra(self):
        """53.000 + 14.000 = 67.000 contra un gasto de 68.000: no entra nada.

        "No entra NADA" es la mitad importante: si el gasto se guardara y el
        reparto no, quedaria un mixto sin detalle -- un gasto que dice haber
        salido de varias cuentas sin decir de cuales ni cuanto, que es peor
        que el problema original.
        """
        respuesta = self.alta_mixta(nachi=Decimal('14000.00'))

        self.assertEqual(self.gastos(), [])
        self.assertEqual(self.partes(), [])
        cuerpo = self.texto(respuesta)
        self.assertIn('67000', cuerpo.replace('.', '').replace(',', ''))
        self.assertIn('no cierra', cuerpo)

    def test_gasto_mixto_rechaza_parte_vacia(self):
        """Vacio no es cero: se pregunta, no se asume.

        Un campo en blanco puede ser "de aca no salio nada" o "me olvide de
        completarlo", y las dos cosas no se pueden distinguir. Se pide el 0
        explicito para que el cero que quede guardado sea el que alguien
        escribio.
        """
        respuesta = self.alta_mixta(**{'parte_%d' % self.id_cuenta_nachi: ''})

        self.assertEqual(self.gastos(), [])
        self.assertEqual(self.partes(), [])
        self.assertIn('poné 0', self.texto(respuesta))

    def test_gasto_mixto_acepta_parte_en_cero(self):
        """Una cuenta con 0 es valido, y el 0 SE GUARDA.

        Es un dato: "de esta cuenta no salio nada en este gasto", distinto de
        no haber elegido mixto. Por eso el CHECK de la tabla es `monto >= 0` y
        no `> 0`.
        """
        self.alta_mixta(roman=TOTAL, nachi=Decimal('0'))

        self.assertEqual(len(self.gastos()), 1)
        reparto = {p.cuenta_cobro_id: p.monto for p in self.partes()}
        self.assertEqual(reparto, {self.id_cuenta_roman: TOTAL,
                                   self.id_cuenta_nachi: Decimal('0')})

    def test_gasto_mixto_no_guarda_cuenta_de_pago(self):
        """La cuenta que mande el formulario se descarta, no rompe el alta.

        El <select> de cuenta sigue en el DOM cuando se elige Mixto -- se
        oculta, no se borra -- asi que el navegador manda igual lo que
        estuviera seleccionado. Mismo criterio que Capital en S3.
        """
        self.alta_mixta(cuenta_pago_id=str(self.id_cuenta_roman))

        self.assertEqual(len(self.gastos()), 1)
        self.assertIsNone(self.gastos()[0].cuenta_pago_id)

    def test_gasto_mixto_rechaza_parte_negativa(self):
        """Una cuenta no puede aportar plata en contra."""
        respuesta = self.alta_mixta(roman=Decimal('70000.00'),
                                    nachi=Decimal('-2000.00'))

        self.assertEqual(self.gastos(), [])
        self.assertIn('negativo', self.texto(respuesta))

    def test_gasto_mixto_reparte_centavos_sin_redondeo(self):
        """La suma se compara en Decimal, no en float.

        53000.10 + 14999.90 da exactamente 68000 en Decimal. En float da
        68000.00000000001 y el gasto se rechazaria por un error que no cometio
        nadie.
        """
        self.alta_mixta(roman=Decimal('53000.10'), nachi=Decimal('14999.90'))

        self.assertEqual(len(self.gastos()), 1)
        self.assertEqual(sum(p.monto for p in self.partes()), TOTAL)


class TestCajaSocio(BaseMixto):
    """PARTE 5: /reportes/caja-socio usa el detalle."""

    def test_caja_socio_reparte_el_mixto_entre_los_dos(self):
        """El ejemplo real: 53.000 a Roman y 15.000 a Nachi.

        Las tres formas de equivocarse que este test descarta:
          - 68.000 a Roman (o a Nachi): imputar el gasto entero a uno solo.
          - 68.000 a los DOS: restar $136.000 de una caja de la que salieron
            $68.000.
          - 34.000 a cada uno: promediar un dato que se tiene exacto.
        """
        self.alta_mixta()

        gastado = _gastado_por_socio(self.empresa_id)

        self.assertEqual(gastado['roman'][0], PARTE_ROMAN)
        self.assertEqual(gastado['nachi'][0], PARTE_NACHI)
        # Y entre los dos suman el gasto: ni de mas ni de menos.
        self.assertEqual(gastado['roman'][0] + gastado['nachi'][0], TOTAL)

    def test_caja_socio_suma_el_mixto_al_gasto_de_una_sola_cuenta(self):
        """Un mixto y un gasto comun de la misma cuenta se acumulan.

        Son dos caminos distintos (`gasto.monto` por `cuenta_pago_id`, y
        `gasto_origen_mixto.monto`) y la pantalla muestra UN numero: si se
        acumularan mal, uno de los dos pisaria al otro y el saldo saldria
        entero pero equivocado.
        """
        self.alta_mixta()
        self.alta(monto='1000.00', origen_fondo=ORIGEN_FACTURACION,
                  cuenta_pago_id=str(self.id_cuenta_roman))

        gastado = _gastado_por_socio(self.empresa_id)

        self.assertEqual(gastado['roman'][0], PARTE_ROMAN + Decimal('1000.00'))
        self.assertEqual(gastado['roman'][1], 2, 'los dos gastos se cuentan')
        self.assertEqual(gastado['nachi'][0], PARTE_NACHI)

    def test_caja_socio_muestra_el_saldo_con_el_mixto_restado(self):
        """La pantalla entera se dibuja y el numero que muestra es el repartido.

        No alcanza con que la funcion sume bien: el template tiene que llegar
        a renderizarse con el gasto mixto adentro.
        """
        self.alta_mixta()

        respuesta = self.client.get('/reportes/caja-socio')

        self.assertEqual(respuesta.status_code, 200)
        cuerpo = self.texto(respuesta).replace('.', '').replace(',', '')
        self.assertIn('53000', cuerpo)
        self.assertIn('15000', cuerpo)

    def test_capital_y_sin_clasificar_siguen_sin_restarle_a_nadie(self):
        """El mixto no cambia lo que S3 decidio para los otros dos casos."""
        self.alta(origen_fondo=ORIGEN_CAPITAL)
        db.session.add(Gasto(empresa_id=self.empresa_id,
                             usuario_id=self.usuario_id,
                             categoria_id=self.id_categoria,
                             descripcion='Viejo sin origen',
                             monto=Decimal('500.00'), fecha=date(2026, 9, 1)))
        db.session.commit()

        self.assertEqual(_gastado_por_socio(self.empresa_id), {})


class TestElCaminoViejoNoSeToca(BaseMixto):
    """CONSTRAINTS: 'facturacion' y 'capital' funcionan exacto igual."""

    def test_gasto_facturacion_sigue_igual(self):
        """Un gasto de una sola cuenta no se ve afectado por el esquema nuevo.

        Se guarda con su cuenta, sin ninguna fila de reparto, y caja-socio le
        resta el monto ENTERO a ese socio -- que es lo correcto: salio entero
        de ahi.
        """
        self.alta(monto='1000.00', origen_fondo=ORIGEN_FACTURACION,
                  cuenta_pago_id=str(self.id_cuenta_roman))

        gastos = self.gastos()
        self.assertEqual(len(gastos), 1)
        self.assertEqual(gastos[0].origen_fondo, ORIGEN_FACTURACION)
        self.assertEqual(gastos[0].cuenta_pago_id, self.id_cuenta_roman)
        self.assertEqual(gastos[0].partes_mixto, [],
                         'un gasto de una sola cuenta no tiene reparto')

        gastado = _gastado_por_socio(self.empresa_id)
        self.assertEqual(gastado['roman'], (Decimal('1000.00'), 1))
        self.assertNotIn('nachi', gastado)

    def test_gasto_capital_sigue_igual(self):
        """'Capital' sigue guardandose sin cuenta y sin reparto."""
        self.alta(origen_fondo=ORIGEN_CAPITAL)

        gastos = self.gastos()
        self.assertEqual(len(gastos), 1)
        self.assertEqual(gastos[0].origen_fondo, ORIGEN_CAPITAL)
        self.assertIsNone(gastos[0].cuenta_pago_id)
        self.assertEqual(self.partes(), [])

    def test_el_alta_sin_origen_se_sigue_rechazando(self):
        """El tercer origen no relaja la obligacion de decir cual es."""
        respuesta = self.alta()

        self.assertEqual(self.gastos(), [])
        self.assertIn('Decí con qué plata se pagó', self.texto(respuesta))


class TestEdicion(BaseMixto):
    """El reparto se reescribe entero o se va: nunca queda a medias."""

    def _id_del_mixto(self):
        self.alta_mixta()
        return self.gastos()[0].id

    def form_edicion(self, **extra):
        datos = {
            'descripcion': 'Compra de materiales',
            'fecha': '2026-09-01',
            'monto': str(TOTAL),
            'categoria_id': str(self.id_categoria),
        }
        datos.update(extra)
        return datos

    def editar(self, gasto_id, **extra):
        return self.client.post('/gasto/editar/%d' % gasto_id,
                                data=self.form_edicion(**extra),
                                follow_redirects=True)

    def test_dejar_de_ser_mixto_borra_el_reparto(self):
        """Si el gasto pasa a 'capital', el reparto viejo NO puede sobrevivir.

        Dejarlo colgado le seguiria restando plata a los socios por un gasto
        que ahora dice haber salido de otro lado.
        """
        gasto_id = self._id_del_mixto()

        self.editar(gasto_id, origen_fondo=ORIGEN_CAPITAL)

        self.assertEqual(Gasto.query.get(gasto_id).origen_fondo, ORIGEN_CAPITAL)
        self.assertEqual(self.partes(), [])
        self.assertEqual(_gastado_por_socio(self.empresa_id), {})

    def test_reeditar_el_reparto_lo_reemplaza_no_lo_acumula(self):
        """Dos filas antes, dos filas despues -- con los montos nuevos."""
        gasto_id = self._id_del_mixto()

        self.editar(gasto_id, origen_fondo=ORIGEN_MIXTO,
                    **{'parte_%d' % self.id_cuenta_roman: '40000.00',
                       'parte_%d' % self.id_cuenta_nachi: '28000.00'})

        reparto = {p.cuenta_cobro_id: p.monto for p in self.partes()}
        self.assertEqual(reparto, {self.id_cuenta_roman: Decimal('40000.00'),
                                   self.id_cuenta_nachi: Decimal('28000.00')})

    def test_editar_el_monto_sin_rehacer_el_reparto_se_rechaza(self):
        """El reparto viejo tiene que seguir cerrando contra el monto NUEVO.

        Es el unico camino por el que un mixto podria quedar con un reparto
        que no suma el total: el formulario no manda el origen, se conserva el
        que la fila tenia, y el monto cambio abajo.
        """
        gasto_id = self._id_del_mixto()

        respuesta = self.editar(gasto_id, monto='99000.00')

        gasto = Gasto.query.get(gasto_id)
        self.assertEqual(gasto.monto, TOTAL, 'no se guardo el monto nuevo')
        self.assertIn('volvé a repartirlo', self.texto(respuesta))

    def test_borrar_el_gasto_se_lleva_el_reparto(self):
        """Una fila de reparto no significa nada sin su gasto."""
        gasto_id = self._id_del_mixto()

        self.client.post('/gasto/eliminar/%d' % gasto_id,
                         follow_redirects=True)

        self.assertEqual(self.gastos(), [])
        self.assertEqual(self.partes(), [])


class TestLaBaseTambienLoSabe(BaseMixto):
    """El formulario no es el unico camino de escritura."""

    def test_check_acepta_mixto_sin_cuenta(self):
        """'mixto' con cuenta_pago_id NULL pasa el CHECK."""
        gasto = Gasto(empresa_id=self.empresa_id, usuario_id=self.usuario_id,
                      categoria_id=self.id_categoria, descripcion='Mixto crudo',
                      monto=TOTAL, fecha=date(2026, 9, 1),
                      origen_fondo=ORIGEN_MIXTO, cuenta_pago_id=None)
        db.session.add(gasto)
        db.session.commit()

        self.assertEqual(Gasto.query.get(gasto.id).origen_fondo, ORIGEN_MIXTO)

    def test_el_validates_acepta_mixto_y_sigue_rechazando_typos(self):
        """El vocabulario crecio en uno, no se abrio."""
        gasto = Gasto()
        gasto.origen_fondo = ORIGEN_MIXTO
        self.assertEqual(gasto.origen_fondo, ORIGEN_MIXTO)

        with self.assertRaises(ValueError):
            gasto.origen_fondo = 'mixta'


if __name__ == '__main__':
    unittest.main(verbosity=2)
