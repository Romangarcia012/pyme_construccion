# -*- coding: utf-8 -*-
"""Tests de FASE-CAJA-GENERAL-S3 (de que plata salio cada gasto).

    python -m unittest discover -s tests -v

LA PREGUNTA

Cada gasto tiene que decir con que plata se pago: la facturada -- y de cual de
las dos cuentas, la de Roman o la de Nachi -- o el capital que aportaron los
socios. Hasta esta slice `gasto.cuenta_pago_id` existia desde FASE2-S1 pero
ningun formulario lo escribia: la respuesta no estaba en ningun lado.

Lo que se prueba:

    el alta lo exige              -> un gasto nuevo sin origen no entra
    facturacion pide cuenta       -> 'de la facturacion' sin decir de cual
                                     cuenta se rechaza con un mensaje que se
                                     entiende
    capital LIMPIA la cuenta      -> no se rechaza: se descarta (ver abajo)
    la base tambien lo sabe       -> los dos CHECK, por si alguien escribe por
                                     fuera del formulario
    el gasto viejo no se fuerza   -> una fila con origen_fondo NULL se sigue
                                     pudiendo editar sin completarlo
    el saldo real resta           -> /reportes/caja-socio muestra facturado Y
                                     lo que queda, y son dos numeros distintos
    capital y sin clasificar NO   -> esos gastos no le bajan el saldo a ningun
                                     socio; se muestran aparte

CAPITAL + CUENTA: SE DESCARTA, NO SE RECHAZA

`test_gasto_capital_no_permite_cuenta` fija la decision explicitamente. El
<select> de cuenta sigue en el DOM cuando se elige Capital -- el formulario lo
oculta, no lo borra -- asi que el navegador manda igual lo que estuviera
seleccionado antes de cambiar de opcion. Devolverle un error a quien carga por
algo que hizo el formulario solo seria castigarlo por una decision de la
pantalla; el dato que importa ("salio de capital") ya quedo dicho sin
ambiguedad. Se guarda con cuenta_pago_id en NULL y listo.

Lo que NO se relaja por eso es la base: el CHECK sigue prohibiendo la
combinacion, y `test_check_de_base_rechaza_capital_con_cuenta` lo prueba
escribiendo derecho contra la tabla. La ruta es amable con el formulario; la
base no le cree a nadie.

LAS CUENTAS

En produccion son los ids 40 (Roman) y 41 (Nachi). Aca se siembran de cero, asi
que los ids son otros y los tests comparan contra `self.id_cuenta_roman` en vez
de contra el 40 literal: lo que se prueba es que se guarda LA cuenta de Roman,
no un numero que depende de en que orden se creo la base.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from tests.ayuda_auth import request_anonimo  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    ORIGEN_CAPITAL,
    ORIGEN_FACTURACION,
    CanalVenta,
    Categoria,
    CuentaCobro,
    Empresa,
    Gasto,
    Pedido,
    Usuario,
    db,
)

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


class BaseOrigen(unittest.TestCase):
    """Una empresa, un usuario, las dos cuentas de cobro y una categoria."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test CAJA-GENERAL-S3')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test',
                               email='cajageneral3@test.local',
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
            'monto': '1000.00',
            'categoria_id': str(self.id_categoria),
        }
        datos.update(extra)
        return datos

    def alta(self, **extra):
        return self.client.post('/gasto/nuevo', data=self.form(**extra),
                                follow_redirects=True)

    def gasto_directo(self, monto='500.00', origen_fondo=None, cuenta_pago_id=None,
                      descripcion='Gasto sembrado'):
        """Escribe un gasto sin pasar por el formulario.

        Sirve para sembrar el estado que un test quiere mirar (un gasto viejo
        sin origen, por ejemplo) sin depender de que la ruta lo permita.
        """
        fila = Gasto(empresa_id=self.empresa_id, usuario_id=self.usuario_id,
                     categoria_id=self.id_categoria,
                     descripcion=descripcion, monto=Decimal(monto),
                     fecha=date(2026, 9, 1),
                     origen_fondo=origen_fondo, cuenta_pago_id=cuenta_pago_id)
        db.session.add(fila)
        db.session.commit()
        return fila

    def gastos(self):
        return Gasto.query.order_by(Gasto.id).all()


class TestAltaDeGasto(BaseOrigen):
    """PARTE 3: el formulario de /gasto/nuevo."""

    def test_gasto_facturacion_requiere_cuenta(self):
        """'Salio de la facturacion' sin decir de cual cuenta no se guarda.

        Es la mitad del campo que le da sentido al campo entero: sin la cuenta
        no se sabe a quien restarle, y el gasto quedaria fuera de las dos
        columnas del reporte sin que nada lo delate.
        """
        respuesta = self.alta(origen_fondo=ORIGEN_FACTURACION,
                              cuenta_pago_id='')

        self.assertEqual(self.gastos(), [])
        texto = respuesta.get_data(as_text=True)
        # El mensaje tiene que decir QUE falta, no "error al agregar gasto".
        self.assertIn('cuenta', texto.lower())

    def test_gasto_facturacion_roman_se_guarda_bien(self):
        """Facturacion + Roman -> se guarda con la cuenta de Roman (id 40 en prod)."""
        self.alta(origen_fondo=ORIGEN_FACTURACION,
                  cuenta_pago_id=str(self.id_cuenta_roman))

        filas = self.gastos()
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].origen_fondo, ORIGEN_FACTURACION)
        self.assertEqual(filas[0].cuenta_pago_id, self.id_cuenta_roman)
        self.assertEqual(filas[0].monto, Decimal('1000.00'))

    def test_gasto_capital_se_guarda_sin_cuenta(self):
        """Capital + sin cuenta -> OK, y la cuenta queda en NULL."""
        self.alta(origen_fondo=ORIGEN_CAPITAL, cuenta_pago_id='')

        filas = self.gastos()
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].origen_fondo, ORIGEN_CAPITAL)
        self.assertIsNone(filas[0].cuenta_pago_id)

    def test_gasto_capital_no_permite_cuenta(self):
        """Capital + una cuenta seleccionada -> la cuenta se DESCARTA.

        La decision esta escrita en el docstring del modulo: no se rechaza. El
        <select> de cuenta sigue mandando lo que quedo seleccionado aunque el
        formulario lo oculte, y eso es cosa del formulario, no de quien carga.
        El gasto entra, y entra sin cuenta.
        """
        self.alta(origen_fondo=ORIGEN_CAPITAL,
                  cuenta_pago_id=str(self.id_cuenta_nachi))

        filas = self.gastos()
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].origen_fondo, ORIGEN_CAPITAL)
        self.assertIsNone(filas[0].cuenta_pago_id)

    def test_alta_sin_origen_se_rechaza(self):
        """En el alta el origen es obligatorio.

        Un gasto nuevo que no dice de que bolsillo salio deja el saldo real de
        los socios mintiendo por omision desde el primer dia, y completarlo
        despues -- cuando ya nadie se acuerda -- es adivinar.
        """
        respuesta = self.alta(origen_fondo='')

        self.assertEqual(self.gastos(), [])
        self.assertIn('plata', respuesta.get_data(as_text=True).lower())

    def test_alta_con_origen_inventado_se_rechaza(self):
        """Un valor fuera del vocabulario no entra ni escribiendolo a mano."""
        self.alta(origen_fondo='efectivo_del_cajon')
        self.assertEqual(self.gastos(), [])

    def test_no_se_puede_pagar_desde_la_cuenta_de_otra_empresa(self):
        """La cuenta se valida por EMPRESA, igual que la categoria."""
        otra = Empresa(nombre='Otra empresa')
        db.session.add(otra)
        db.session.flush()
        cuenta_ajena = CuentaCobro(empresa_id=otra.id, nombre='Ajena',
                                   tipo='mercadopago', socio='roman')
        db.session.add(cuenta_ajena)
        db.session.commit()

        self.alta(origen_fondo=ORIGEN_FACTURACION,
                  cuenta_pago_id=str(cuenta_ajena.id))

        self.assertEqual(self.gastos(), [])

    def test_el_formulario_ofrece_las_dos_cuentas_por_socio(self):
        """El selector nombra a los socios, no al texto libre de la cuenta."""
        texto = self.client.get('/gasto/nuevo').get_data(as_text=True)
        self.assertIn('Roman', texto)
        self.assertIn('Nachi', texto)
        self.assertIn('origen_fondo', texto)
        self.assertIn('cuenta_pago_id', texto)


class TestEdicionDeGasto(BaseOrigen):
    """PARTE 3: /gasto/editar, y el trato distinto que reciben las filas viejas."""

    def editar(self, gasto_id, **extra):
        return self.client.post('/gasto/editar/%d' % gasto_id,
                                data=self.form(**extra), follow_redirects=True)

    def test_gasto_viejo_sin_origen_no_se_fuerza(self):
        """Una fila con origen_fondo NULL se sigue editando sin completarlo.

        Mismo criterio que en todo el resto: no se le inventa a una fila vieja
        un dato que nadie registro en su momento. Editarle la descripcion no
        tiene por que obligar a adivinar de que bolsillo salio.
        """
        viejo = self.gasto_directo(origen_fondo=None)

        self.editar(viejo.id, descripcion='Descripcion corregida',
                    origen_fondo='')

        db.session.refresh(viejo)
        self.assertEqual(viejo.descripcion, 'Descripcion corregida')
        self.assertIsNone(viejo.origen_fondo)
        self.assertIsNone(viejo.cuenta_pago_id)

    def test_gasto_viejo_se_puede_clasificar_al_editar(self):
        """Y si quien edita SI sabe de donde salio, lo puede completar."""
        viejo = self.gasto_directo(origen_fondo=None)

        self.editar(viejo.id, origen_fondo=ORIGEN_FACTURACION,
                    cuenta_pago_id=str(self.id_cuenta_nachi))

        db.session.refresh(viejo)
        self.assertEqual(viejo.origen_fondo, ORIGEN_FACTURACION)
        self.assertEqual(viejo.cuenta_pago_id, self.id_cuenta_nachi)

    def test_un_gasto_ya_clasificado_no_vuelve_a_sin_dato(self):
        """Lo contrario del caso anterior: no se pierde un dato que si esta.

        `requerido` sale de lo que la fila YA dice, no de si es alta o edicion.
        """
        clasificado = self.gasto_directo(origen_fondo=ORIGEN_CAPITAL)

        self.editar(clasificado.id, origen_fondo='')

        db.session.refresh(clasificado)
        self.assertEqual(clasificado.origen_fondo, ORIGEN_CAPITAL)

    def test_editar_de_facturacion_a_capital_limpia_la_cuenta(self):
        """Cambiar de bolsillo tiene que soltar la cuenta, no arrastrarla."""
        gasto = self.gasto_directo(origen_fondo=ORIGEN_FACTURACION,
                                   cuenta_pago_id=self.id_cuenta_roman)

        self.editar(gasto.id, origen_fondo=ORIGEN_CAPITAL,
                    cuenta_pago_id=str(self.id_cuenta_roman))

        db.session.refresh(gasto)
        self.assertEqual(gasto.origen_fondo, ORIGEN_CAPITAL)
        self.assertIsNone(gasto.cuenta_pago_id)


class TestReglasDeLaBase(BaseOrigen):
    """PARTE 2: los dos CHECK, probados por fuera del formulario.

    La ruta es la puerta amable; estos tests entran por la ventana. Si alguna
    vez se escribe un gasto desde una migracion, un script o el shell, la
    combinacion imposible tiene que seguir siendo imposible.
    """

    def escribir_crudo(self, origen_fondo, cuenta_pago_id):
        db.session.execute(text(
            'INSERT INTO gasto (descripcion, monto, fecha, empresa_id,'
            ' categoria_id, origen_fondo, cuenta_pago_id)'
            ' VALUES (:d, :m, :f, :e, :c, :o, :cp)'),
            {'d': 'Escrito a mano', 'm': 100, 'f': '2026-09-01',
             'e': self.empresa_id, 'c': self.id_categoria,
             'o': origen_fondo, 'cp': cuenta_pago_id})
        db.session.commit()

    def test_check_de_base_rechaza_facturacion_sin_cuenta(self):
        with self.assertRaises(IntegrityError):
            self.escribir_crudo(ORIGEN_FACTURACION, None)
        db.session.rollback()

    def test_check_de_base_rechaza_capital_con_cuenta(self):
        with self.assertRaises(IntegrityError):
            self.escribir_crudo(ORIGEN_CAPITAL, self.id_cuenta_roman)
        db.session.rollback()

    def test_check_de_base_rechaza_vocabulario_invalido(self):
        with self.assertRaises(IntegrityError):
            self.escribir_crudo('efectivo_del_cajon', None)
        db.session.rollback()

    def test_null_sigue_siendo_valido(self):
        """NULL es "todavia no se dijo", y tiene que poder existir."""
        self.escribir_crudo(None, None)
        self.assertEqual(len(self.gastos()), 1)

    def test_el_modelo_rechaza_un_origen_inventado(self):
        """El @validates, antes de llegar a la base."""
        with self.assertRaises(ValueError):
            Gasto(empresa_id=self.empresa_id, descripcion='x',
                  monto=Decimal('1'), fecha=date(2026, 9, 1),
                  origen_fondo='Facturacion')  # mayuscula: no es la clave


class TestComoSeMuestra(BaseOrigen):
    """PARTE 4: el origen se lee en /gasto/listar y en /caja-general."""

    def test_origen_legible_arma_las_tres_etiquetas(self):
        facturado = self.gasto_directo(origen_fondo=ORIGEN_FACTURACION,
                                       cuenta_pago_id=self.id_cuenta_roman)
        capital = self.gasto_directo(origen_fondo=ORIGEN_CAPITAL)
        sin_dato = self.gasto_directo(origen_fondo=None)

        self.assertIn('Roman', facturado.origen_legible)
        self.assertIn('Facturaci', facturado.origen_legible)
        self.assertEqual(capital.origen_legible, 'Capital')
        self.assertEqual(sin_dato.origen_legible, 'sin dato')

    def test_la_etiqueta_no_depende_del_nombre_de_la_cuenta(self):
        """Renombrar la cuenta no cambia de quien dice que salio la plata.

        Es la misma leccion de FASE-CAJA-SOCIO-S1: el socio sale del campo, no
        del texto libre.
        """
        gasto = self.gasto_directo(origen_fondo=ORIGEN_FACTURACION,
                                   cuenta_pago_id=self.id_cuenta_roman)
        self.cuenta_roman.nombre = 'Cuenta vieja que nadie sabe de quien es'
        db.session.commit()
        db.session.refresh(gasto)

        self.assertIn('Roman', gasto.origen_legible)

    def test_listar_gastos_muestra_el_origen(self):
        self.gasto_directo(origen_fondo=ORIGEN_FACTURACION,
                           cuenta_pago_id=self.id_cuenta_nachi,
                           descripcion='Pagado por Nachi')
        self.gasto_directo(origen_fondo=ORIGEN_CAPITAL,
                           descripcion='Pagado con capital')
        self.gasto_directo(origen_fondo=None, descripcion='Nadie dijo nada')

        texto = self.client.get('/gasto/listar').get_data(as_text=True)

        self.assertIn('Nachi', texto)
        self.assertIn('Capital', texto)
        self.assertIn('sin dato', texto)

    def test_caja_general_muestra_el_origen(self):
        self.gasto_directo(origen_fondo=ORIGEN_FACTURACION,
                           cuenta_pago_id=self.id_cuenta_roman)
        self.gasto_directo(origen_fondo=None)

        texto = self.client.get('/caja-general').get_data(as_text=True)

        self.assertIn('Origen', texto)
        self.assertIn('Roman', texto)
        self.assertIn('sin dato', texto)


class TestSaldoRealPorSocio(BaseOrigen):
    """PARTE 5: /reportes/caja-socio deja de mostrar un solo numero.

    Se venden $10.000 por el canal de Roman y se pagan $3.000 desde su cuenta:
    facturado 10.000, gastado 3.000, tenes realmente 7.000. Los numeros son
    redondos a proposito -- la resta se tiene que poder verificar de un
    vistazo.
    """

    def setUp(self):
        super(TestSaldoRealPorSocio, self).setUp()

        self.canal_roman = CanalVenta(empresa_id=self.empresa_id,
                                      tipo='tiendanube', nombre='Korvo',
                                      activo=True, id_tienda_externo='9999',
                                      cuenta_cobro_id=self.id_cuenta_roman)
        self.canal_nachi = CanalVenta(empresa_id=self.empresa_id,
                                      tipo='mercadolibre', nombre='Mercado Libre',
                                      activo=False,
                                      cuenta_cobro_id=self.id_cuenta_nachi)
        db.session.add_all([self.canal_roman, self.canal_nachi])
        db.session.commit()

        db.session.add(Pedido(empresa_id=self.empresa_id,
                              canal_id=self.canal_roman.id,
                              fecha_pedido=datetime(2026, 9, 1, 12, 0),
                              estado='open', comprador_nombre='Camila',
                              total=Decimal('10000.00')))
        db.session.commit()

    def reporte(self):
        """Lo que /reportes/caja-socio le pasa a la plantilla."""
        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado['context'] = context

        template_rendered.connect(anotar, app)
        try:
            respuesta = self.client.get('/reportes/caja-socio')
        finally:
            template_rendered.disconnect(anotar, app)

        return respuesta, capturado.get('context', {})

    def socio(self, contexto, clave):
        for fila in contexto['socios']:
            if fila['clave'] == clave:
                return fila
        self.fail('no salio el socio %r en el reporte' % clave)

    def test_facturado_menos_gastado_es_el_saldo_real(self):
        self.gasto_directo(monto='3000.00', origen_fondo=ORIGEN_FACTURACION,
                           cuenta_pago_id=self.id_cuenta_roman)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('10000.00'))
        self.assertEqual(roman['gastado'], Decimal('3000.00'))
        self.assertEqual(roman['saldo_real'], Decimal('7000.00'))
        self.assertEqual(roman['gastos'], 1)

    def test_el_gasto_de_roman_no_le_baja_el_saldo_a_nachi(self):
        self.gasto_directo(monto='3000.00', origen_fondo=ORIGEN_FACTURACION,
                           cuenta_pago_id=self.id_cuenta_roman)

        _, contexto = self.reporte()
        nachi = self.socio(contexto, 'nachi')

        self.assertEqual(nachi['gastado'], Decimal('0.00'))
        self.assertEqual(nachi['saldo_real'], Decimal('0.00'))

    def test_el_gasto_de_capital_no_le_resta_a_nadie(self):
        """Salio de otro pool: no baja el saldo, se muestra aparte."""
        self.gasto_directo(monto='5000.00', origen_fondo=ORIGEN_CAPITAL)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['gastado'], Decimal('0.00'))
        self.assertEqual(roman['saldo_real'], Decimal('10000.00'))
        self.assertEqual(contexto['capital_monto'], Decimal('5000.00'))
        self.assertEqual(contexto['capital_cantidad'], 1)

    def test_el_gasto_sin_clasificar_no_resta_pero_se_cuenta(self):
        """No se adivina de que plata salio, y que falte se VE.

        Es la diferencia entre un saldo incompleto y un saldo que miente: el
        numero de arriba no se mueve, pero al pie dice que hay un gasto que
        todavia no se sabe de donde salio.
        """
        self.gasto_directo(monto='800.00', origen_fondo=None)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['gastado'], Decimal('0.00'))
        self.assertEqual(roman['saldo_real'], Decimal('10000.00'))
        self.assertEqual(contexto['sin_clasificar_monto'], Decimal('800.00'))
        self.assertEqual(contexto['sin_clasificar_cantidad'], 1)

    def test_el_saldo_puede_dar_negativo_y_se_muestra(self):
        """Gastar mas de lo facturado no se recorta a cero: se ve el rojo."""
        self.gasto_directo(monto='12000.00', origen_fondo=ORIGEN_FACTURACION,
                           cuenta_pago_id=self.id_cuenta_roman)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['saldo_real'], Decimal('-2000.00'))

    def test_la_pantalla_muestra_los_dos_numeros(self):
        """Facturado y "tenes realmente", uno debajo del otro y distinguibles."""
        self.gasto_directo(monto='3000.00', origen_fondo=ORIGEN_FACTURACION,
                           cuenta_pago_id=self.id_cuenta_roman)
        self.gasto_directo(monto='5000.00', origen_fondo=ORIGEN_CAPITAL)

        respuesta, _ = self.reporte()
        texto = respuesta.get_data(as_text=True)

        self.assertIn('Facturado', texto)
        self.assertIn('realmente', texto)
        self.assertIn('10000.00', texto)
        self.assertIn('7000.00', texto)
        # Y el bloque de lo que no resta, con su monto.
        self.assertIn('5000.00', texto)

    def test_un_socio_con_gastos_y_sin_ventas_sale_igual(self):
        """Nachi no vendio nada pero pago $500 de su cuenta: tiene que verse."""
        self.gasto_directo(monto='500.00', origen_fondo=ORIGEN_FACTURACION,
                           cuenta_pago_id=self.id_cuenta_nachi)

        _, contexto = self.reporte()
        nachi = self.socio(contexto, 'nachi')

        self.assertEqual(nachi['total'], Decimal('0.00'))
        self.assertEqual(nachi['gastado'], Decimal('500.00'))
        self.assertEqual(nachi['saldo_real'], Decimal('-500.00'))


class TestAuth(BaseOrigen):
    """Las pantallas de la slice siguen detras de @login_required."""

    def test_gasto_nuevo_pide_login(self):
        respuesta = request_anonimo(self.ctx, 'get', '/gasto/nuevo')
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers.get('Location', ''))

    def test_caja_socio_pide_login(self):
        respuesta = request_anonimo(self.ctx, 'get', '/reportes/caja-socio')
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers.get('Location', ''))


if __name__ == '__main__':
    unittest.main()
