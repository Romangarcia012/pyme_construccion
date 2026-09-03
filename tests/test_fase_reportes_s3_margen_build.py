# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S3-MARGEN-BUILD (margen por producto y canal).

    python -m unittest discover -s tests -v

Esta pantalla es la primera del sistema que afirma cuanta plata quedo. Un
reporte de stock que agrupa mal se nota mirandolo; uno de margen no: el numero
sale igual de plausible este bien o mal, y encima se usa para decidir precios.
Por eso los tests son sobre la aritmetica y sobre que entra y que no, mucho mas
que sobre la pantalla.

    pedido completo          -> las tres columnas dan lo que da la cuenta a mano
    falta un componente      -> no se calcula Y no suma en ningun grupo
    un 0 cargado a mano      -> es un dato, no un faltante: entra al calculo
    mismo producto, 2 canales-> una sola fila (a diferencia de S1)
    pedido de varias lineas  -> bloque aparte, sin descomponer
    grupo con 2+ pedidos     -> el % sale de los totales, no del promedio
    ingreso != total         -> se marca, pero se sigue contando

El numero de referencia del primer test es el del pedido real #100 de Korvo,
calculado a mano en la investigacion de la slice: 7490.00 - 1423.10 + 7630.00
de ingreso, 3994.18 de costo congelado, 248.59 de comision y 7630.00 de flete
-> 1824.13 de ganancia, 13.3% y 30.1%.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    Empresa,
    MapeoProductoCanal,
    Pedido,
    PedidoItem,
    Producto,
    Usuario,
    db,
)
from tests.ayuda_auth import request_anonimo  # noqa: E402

ENGINE_PRODUCTIVO = None

# El valor que manda Tiendanube cuando el pedido se da de baja. Igual que en
# FASE-REPORTES-S1: el ingestor guarda el status crudo, sin traducirlo.
ESTADO_CANCELADO = 'cancelled'


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


class BaseMargen(unittest.TestCase):
    """Una empresa con dos canales y dos productos, sin ningun pedido todavia.

    Cada test arma las ventas que necesita: lo que se prueba aca es como se
    comporta la cuenta ante cada forma de pedido, y un escenario compartido
    haria que cada test tuviera que descontar el ruido de los demas.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test MARGEN')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='margen@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True, id_tienda_externo='9999')
        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial', activo=True)
        db.session.add_all([self.canal_tn, self.canal_manual])
        db.session.flush()

        self.tarjetero = Producto(empresa_id=self.empresa.id,
                                  sku='TN-360354459-1574653133',
                                  nombre='Tarjetero Minimalista de Aluminio (Negro)',
                                  stock=170, costo_unitario=Decimal('3994.18'))
        self.micro = Producto(empresa_id=self.empresa.id,
                              sku='TN-363274370-1584388030',
                              nombre='Micrófono Inalámbrico Korvo',
                              stock=100, costo_unitario=Decimal('20000.00'))
        db.session.add_all([self.tarjetero, self.micro])
        db.session.flush()

        # El mapeo es lo que decide que dos filas de `producto` son el mismo
        # producto padre. Sin el, cada una seria su propio grupo.
        for producto, id_padre, id_variante in (
            (self.tarjetero, '360354459', '1574653133'),
            (self.micro, '363274370', '1584388030'),
        ):
            db.session.add(MapeoProductoCanal(
                producto_id=producto.id, canal_id=self.canal_tn.id,
                id_producto_externo=id_padre, id_variante_externo=id_variante,
                sku_externo=producto.sku))

        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.canal_tn_id = self.canal_tn.id
        self.canal_manual_id = self.canal_manual.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def registrar_pedido(self, canal_id=None, lineas=(), bruto='0.00',
                         descuentos='0.00', envio='0.00', total=None,
                         comision='0.00', envio_vendedor='0.00',
                         estado='closed', numero=None, empresa_id=None,
                         fecha=None):
        """Un pedido con sus lineas y sus tres componentes de costo.

        `lineas`: (producto|None, cantidad, snapshot|None). El snapshot se pasa
        explicito y NO se copia del producto: la mitad de los tests son sobre
        que pasa cuando falta, y tomarlo del producto los volveria imposibles
        de escribir.

        `comision` y `envio_vendedor` aceptan None, que es el NULL de la base
        -- "no lo cargue" -- distinto del '0.00', que es un cero cargado.
        """
        bruto = Decimal(bruto)
        envio = Decimal(envio)
        descuentos = Decimal(descuentos)

        pedido = Pedido(
            empresa_id=empresa_id or self.empresa_id,
            canal_id=canal_id if canal_id is not None else self.canal_tn_id,
            numero_externo=numero,
            fecha_pedido=fecha or datetime(2026, 8, 31, 17, 55, 12),
            estado=estado,
            total_bruto=bruto,
            total_descuentos=descuentos,
            total_envio=envio,
            total_impuestos=Decimal('0.00'),
            # Por defecto el total cierra con la cuenta: el descuadre es un
            # caso que se pide a proposito, no el estado normal.
            total=Decimal(total) if total is not None else bruto - descuentos + envio,
            comision_plataforma=None if comision is None else Decimal(comision),
            costo_envio_vendedor=None if envio_vendedor is None else Decimal(envio_vendedor),
        )
        db.session.add(pedido)
        db.session.flush()

        for producto, cantidad, snapshot in lineas:
            precio = bruto / cantidad if cantidad else bruto
            db.session.add(PedidoItem(
                pedido_id=pedido.id,
                producto_id=producto.id if producto is not None else None,
                descripcion=producto.nombre if producto is not None else 'Ítem suelto',
                cantidad=cantidad,
                precio_unitario=precio,
                descuento_unitario=Decimal('0.00'),
                costo_unitario_snapshot=None if snapshot is None else Decimal(snapshot),
                subtotal=precio * cantidad))

        db.session.commit()
        return pedido

    def pedido_real_100(self, **extra):
        """El pedido #100 de Korvo, tal como esta hoy en Supabase."""
        datos = dict(lineas=[(self.tarjetero, 1, '3994.18')],
                     bruto='7490.00', descuentos='1423.10', envio='7630.00',
                     comision='248.59', envio_vendedor='7630.00', numero='100')
        datos.update(extra)
        return self.registrar_pedido(**datos)

    def reporte(self):
        """Lo que la ruta le pasa a la plantilla, sin pasar por el HTML.

        Afirmar sobre el contexto y no sobre el markup es lo que hace que
        estos tests hablen de los numeros -- que es lo que importa -- y no se
        rompan el dia que alguien cambie una clase de CSS.
        """
        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado['context'] = context

        template_rendered.connect(anotar, app)
        try:
            respuesta = self.client.get('/reportes/margen')
        finally:
            template_rendered.disconnect(anotar, app)

        return respuesta, capturado.get('context', {})

    def fila_llamada(self, filas, nombre):
        for fila in filas:
            if fila['nombre'] == nombre:
                return fila
        self.fail('no hay ninguna fila llamada %r; hay %r'
                  % (nombre, [fila['nombre'] for fila in filas]))


class TestPedidoCompleto(BaseMargen):
    """El caso central: un pedido con todo cargado da los tres numeros."""

    def setUp(self):
        super().setUp()
        self.pedido_real_100()
        self.respuesta, self.contexto = self.reporte()

    def test_responde_ok(self):
        self.assertEqual(self.respuesta.status_code, 200)

    def test_pedido_completo_calcula_las_tres_columnas(self):
        fila = self.fila_llamada(self.contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')

        # 7490.00 - 1423.10 + 7630.00
        self.assertEqual(fila['ingreso_neto'], Decimal('13696.90'))
        # 3994.18 de mercaderia + 248.59 de comision + 7630.00 de flete
        self.assertEqual(fila['costo_total'], Decimal('11872.77'))
        self.assertEqual(fila['ganancia'], Decimal('1824.13'))
        self.assertEqual(fila['margen_pct'], Decimal('13.3'))
        # Sacando el envio de los dos lados: 1824.13 / 6066.90
        self.assertEqual(fila['margen_mercaderia_pct'], Decimal('30.1'))

    def test_las_dos_columnas_de_margen_no_son_la_misma(self):
        """Con envio de por medio, medir sobre el total esconde el margen real.

        Es el motivo de que la pantalla tenga las dos: el flete infla el
        denominador de la primera sin aportar un peso de ganancia.
        """
        fila = self.fila_llamada(self.contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')
        self.assertLess(fila['margen_pct'], fila['margen_mercaderia_pct'])

    def test_el_mismo_pedido_suma_igual_en_el_corte_por_canal(self):
        canal = self.fila_llamada(self.contexto['canales'], 'Korvo')
        producto = self.fila_llamada(self.contexto['productos'],
                                     'Tarjetero Minimalista de Aluminio')
        self.assertEqual(canal['ganancia'], producto['ganancia'])
        self.assertEqual(canal['margen_pct'], producto['margen_pct'])

    def test_no_hay_incompletos_ni_descuadres(self):
        self.assertEqual(self.contexto['incompletos'], [])
        self.assertEqual(self.contexto['descuadres'], [])

    def test_el_costo_sale_del_snapshot_y_no_del_producto(self):
        """Cambiar el costo vigente no reescribe el margen de lo ya vendido.

        Es la razon de existir del snapshot (FASE-REPORTES-S3-SNAPSHOT-FIX) y
        se verifica desde este lado: el reporte tiene que leer la linea, no el
        producto.
        """
        self.tarjetero.costo_unitario = Decimal('9999.99')
        db.session.commit()

        _, contexto = self.reporte()
        fila = self.fila_llamada(contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(fila['ganancia'], Decimal('1824.13'))


class TestPedidoIncompleto(BaseMargen):
    """Falta un componente -> no hay margen, y no contamina ninguna suma."""

    def test_pedido_incompleto_no_calcula_margen(self):
        self.pedido_real_100(comision=None)
        _, contexto = self.reporte()

        self.assertEqual(len(contexto['incompletos']), 1)
        incompleto = contexto['incompletos'][0]
        self.assertEqual(incompleto['etiqueta'], '#100')
        self.assertIn('comisión', ' '.join(f['que'] for f in incompleto['faltan']))

        # Y no suma en ningun grupo: la fila del producto existe -- el pedido
        # ocurrio -- pero sin un solo peso adentro.
        fila = self.fila_llamada(contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(fila['pedidos'], 0)
        self.assertEqual(fila['ganancia'], Decimal('0.00'))
        self.assertEqual(fila['ingreso_neto'], Decimal('0.00'))
        self.assertIsNone(fila['margen_pct'])

        canal = self.fila_llamada(contexto['canales'], 'Korvo')
        self.assertEqual(canal['pedidos'], 0)
        self.assertEqual(canal['ganancia'], Decimal('0.00'))

    def test_el_faltante_dice_cual_es_y_donde_se_carga(self):
        self.pedido_real_100(lineas=[(self.tarjetero, 1, None)])
        _, contexto = self.reporte()

        faltan = contexto['incompletos'][0]['faltan']
        self.assertEqual(len(faltan), 1)
        self.assertIn('costo', faltan[0]['que'])
        self.assertIn('Tarjetero', faltan[0]['que'])
        self.assertEqual(faltan[0]['url'], '/productos/listar')

    def test_faltan_los_tres_a_la_vez_y_se_listan_los_tres(self):
        self.pedido_real_100(lineas=[(self.tarjetero, 1, None)],
                             comision=None, envio_vendedor=None)
        _, contexto = self.reporte()
        self.assertEqual(len(contexto['incompletos'][0]['faltan']), 3)

    def test_el_contador_dice_cuantos_quedaron_afuera(self):
        """'X de Y pedidos sin datos', por grupo."""
        self.pedido_real_100(numero='100')
        self.pedido_real_100(numero='101', comision=None)
        _, contexto = self.reporte()

        fila = self.fila_llamada(contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(fila['pedidos_totales'], 2)
        self.assertEqual(fila['incompletos'], 1)
        self.assertEqual(fila['pedidos'], 1)

        canal = self.fila_llamada(contexto['canales'], 'Korvo')
        self.assertEqual(canal['pedidos_totales'], 2)
        self.assertEqual(canal['incompletos'], 1)

    def test_un_pedido_incompleto_no_arrastra_al_completo(self):
        """La suma del grupo es exactamente la del pedido que si cerraba."""
        self.pedido_real_100(numero='100')
        self.pedido_real_100(numero='101', envio_vendedor=None)
        _, contexto = self.reporte()

        fila = self.fila_llamada(contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(fila['ganancia'], Decimal('1824.13'))
        self.assertEqual(fila['margen_pct'], Decimal('13.3'))


class TestCeroCargado(BaseMargen):
    """NULL no es 0, y el 0 tampoco es NULL: el reporte los separa."""

    def test_cero_cargado_no_es_incompleto(self):
        """Comision y flete en 0 explicito: se usan, no disparan la marca.

        Es el caso de la venta de mostrador -- no paga comision de plataforma
        ni flete -- y el del envio gratis que el canal no bonifico. Tratar ese
        cero como faltante sacaria del reporte a todas las ventas presenciales.
        """
        self.registrar_pedido(canal_id=self.canal_manual_id,
                              lineas=[(self.tarjetero, 1, '3994.18')],
                              bruto='7490.00', comision='0.00',
                              envio_vendedor='0.00', estado='completado')
        _, contexto = self.reporte()

        self.assertEqual(contexto['incompletos'], [])

        fila = self.fila_llamada(contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(fila['pedidos'], 1)
        self.assertEqual(fila['ingreso_neto'], Decimal('7490.00'))
        self.assertEqual(fila['ganancia'], Decimal('3495.82'))
        # Sin envio, las dos columnas de margen son la misma pregunta.
        self.assertEqual(fila['margen_pct'], fila['margen_mercaderia_pct'])

    def test_un_snapshot_en_cero_tampoco_es_faltante(self):
        """Un producto que de verdad salio $0 (una muestra, un regalo)."""
        self.registrar_pedido(lineas=[(self.tarjetero, 1, '0.00')],
                              bruto='7490.00', comision='100.00',
                              envio_vendedor='0.00')
        _, contexto = self.reporte()

        self.assertEqual(contexto['incompletos'], [])
        fila = self.fila_llamada(contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(fila['ganancia'], Decimal('7390.00'))


class TestCortePorProducto(BaseMargen):
    """La diferencia de fondo con S1: el canal no parte la fila."""

    def test_corte_por_producto_ignora_canal(self):
        self.registrar_pedido(canal_id=self.canal_tn_id,
                              lineas=[(self.tarjetero, 1, '3994.18')],
                              bruto='7490.00', comision='100.00',
                              envio_vendedor='0.00', numero='100')
        self.registrar_pedido(canal_id=self.canal_manual_id,
                              lineas=[(self.tarjetero, 1, '3994.18')],
                              bruto='7490.00', comision='0.00',
                              envio_vendedor='0.00', estado='completado')
        _, contexto = self.reporte()

        # Una sola fila para el producto, con los dos pedidos adentro...
        fila = self.fila_llamada(contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(fila['pedidos'], 2)
        self.assertEqual(fila['unidades'], 2)
        self.assertEqual(fila['ingreso_neto'], Decimal('14980.00'))
        self.assertEqual(fila['ganancia'], Decimal('6891.64'))

        # ...y dos filas en el corte por canal, que es donde se separan.
        self.assertEqual(len(contexto['canales']), 2)

    def test_dos_productos_distintos_son_dos_filas(self):
        self.registrar_pedido(lineas=[(self.tarjetero, 1, '3994.18')],
                              bruto='7490.00', comision='0.00', envio_vendedor='0.00')
        self.registrar_pedido(lineas=[(self.micro, 1, '20000.00')],
                              bruto='45000.00', comision='0.00', envio_vendedor='0.00')
        _, contexto = self.reporte()

        self.assertEqual(len(contexto['productos']), 2)
        micro = self.fila_llamada(contexto['productos'], 'Micrófono Inalámbrico Korvo')
        self.assertEqual(micro['ganancia'], Decimal('25000.00'))

    def test_un_item_sin_producto_no_se_pierde(self):
        """Una linea que ningun mapeo pudo atar: renglon propio, no descarte."""
        self.registrar_pedido(lineas=[(None, 1, '1000.00')],
                              bruto='5000.00', comision='0.00', envio_vendedor='0.00')
        _, contexto = self.reporte()

        fila = self.fila_llamada(contexto['productos'], 'Sin identificar')
        self.assertEqual(fila['ganancia'], Decimal('4000.00'))


class TestPedidoMultilinea(BaseMargen):
    """Dos productos en un pedido: no hay forma de repartir, no se reparte."""

    def setUp(self):
        super().setUp()
        self.registrar_pedido(
            lineas=[(self.tarjetero, 1, '3994.18'), (self.micro, 1, '20000.00')],
            bruto='52490.00', comision='1000.00', envio_vendedor='0.00',
            numero='200')
        self.respuesta, self.contexto = self.reporte()

    def test_pedido_multilinea_no_se_descompone(self):
        # No aparece en el corte por producto...
        self.assertEqual(self.contexto['productos'], [])
        # ...sino entero, en su propio bloque.
        self.assertEqual(len(self.contexto['multilinea']), 1)
        fila = self.contexto['multilinea'][0]
        self.assertEqual(fila['etiqueta'], '#200')
        self.assertEqual(fila['ingreso_neto'], Decimal('52490.00'))
        self.assertEqual(fila['ganancia'], Decimal('27495.82'))

    def test_el_corte_por_canal_si_lo_cuenta(self):
        """Ahi la unidad es el pedido y no hay nada que repartir."""
        canal = self.fila_llamada(self.contexto['canales'], 'Korvo')
        self.assertEqual(canal['pedidos'], 1)
        self.assertEqual(canal['ganancia'], Decimal('27495.82'))


class TestCortePorCanal(BaseMargen):
    """El porcentaje del grupo sale de los totales, nunca del promedio."""

    def test_canal_recalcula_pct_sobre_totales_no_promedia(self):
        # Una venta chica con margen altisimo (90%) y una grande con margen
        # flaco (2%). Promediar los dos porcentajes daria 46%; la respuesta
        # correcta, que es la plata sobre la plata, es 3.7%.
        self.registrar_pedido(lineas=[(self.tarjetero, 1, '100.00')],
                              bruto='1000.00', comision='0.00',
                              envio_vendedor='0.00', numero='100')
        self.registrar_pedido(lineas=[(self.micro, 1, '48000.00')],
                              bruto='50000.00', comision='1000.00',
                              envio_vendedor='0.00', numero='101')
        _, contexto = self.reporte()

        canal = self.fila_llamada(contexto['canales'], 'Korvo')
        self.assertEqual(canal['ingreso_neto'], Decimal('51000.00'))
        self.assertEqual(canal['ganancia'], Decimal('1900.00'))

        promedio_ingenuo = (Decimal('90.0') + Decimal('2.0')) / 2
        self.assertEqual(canal['margen_pct'], Decimal('3.7'))
        self.assertNotEqual(canal['margen_pct'], promedio_ingenuo)

    def test_el_margen_de_mercaderia_del_grupo_tambien_sale_de_los_totales(self):
        """El envio se resta una sola vez, sumado, no pedido por pedido."""
        self.registrar_pedido(lineas=[(self.tarjetero, 1, '3994.18')],
                              bruto='7490.00', envio='1000.00',
                              comision='0.00', envio_vendedor='1000.00',
                              numero='100')
        self.registrar_pedido(lineas=[(self.tarjetero, 1, '3994.18')],
                              bruto='7490.00', envio='500.00',
                              comision='0.00', envio_vendedor='500.00',
                              numero='101')
        _, contexto = self.reporte()

        canal = self.fila_llamada(contexto['canales'], 'Korvo')
        # ingreso 16480.00, envio 1500.00 -> base de mercaderia 14980.00
        self.assertEqual(canal['ingreso_neto'], Decimal('16480.00'))
        self.assertEqual(canal['ganancia'], Decimal('6991.64'))
        self.assertEqual(canal['margen_mercaderia_pct'], Decimal('46.7'))

    def test_un_canal_sin_ventas_no_aparece(self):
        """A diferencia de S1: una fila sin pedidos es un renglon de guiones."""
        self.pedido_real_100()
        _, contexto = self.reporte()
        self.assertEqual([fila['nombre'] for fila in contexto['canales']], ['Korvo'])


class TestCruceYFiltros(BaseMargen):
    """Los controles que impiden que el reporte mienta en silencio."""

    def test_cross_check_marca_pedido_que_no_cierra(self):
        """bruto - descuentos + envio != total -> alerta visible, no descarte."""
        self.pedido_real_100(total='99999.00')
        _, contexto = self.reporte()

        self.assertEqual(len(contexto['descuadres']), 1)
        descuadre = contexto['descuadres'][0]
        self.assertEqual(descuadre['etiqueta'], '#100')
        self.assertEqual(descuadre['ingreso_neto'], Decimal('13696.90'))
        self.assertEqual(descuadre['total'], Decimal('99999.00'))

        # Y se sigue contando: el aviso es sobre el dato de origen, no sobre
        # la cuenta. Esconderlo dejaria un reporte que no suma todo lo vendido.
        fila = self.fila_llamada(contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(fila['pedidos'], 1)
        self.assertEqual(fila['ganancia'], Decimal('1824.13'))
        self.assertEqual(fila['descuadres'], 1)

    def test_un_pedido_cancelado_no_entra(self):
        self.pedido_real_100(numero='100', estado=ESTADO_CANCELADO)
        _, contexto = self.reporte()

        self.assertEqual(contexto['productos'], [])
        self.assertEqual(contexto['canales'], [])
        self.assertEqual(contexto['total_pedidos'], 0)

    def test_los_pedidos_de_otra_empresa_no_se_filtran(self):
        otra = Empresa(nombre='Empresa Vecina')
        db.session.add(otra)
        db.session.flush()
        canal_ajeno = CanalVenta(empresa_id=otra.id, tipo='tiendanube',
                                 nombre='Canal Ajeno', activo=True)
        db.session.add(canal_ajeno)
        db.session.flush()
        self.registrar_pedido(canal_id=canal_ajeno.id, empresa_id=otra.id,
                              lineas=[(self.tarjetero, 1, '1.00')],
                              bruto='999999.00', comision='0.00',
                              envio_vendedor='0.00')
        _, contexto = self.reporte()

        self.assertEqual(contexto['total_pedidos'], 0)
        self.assertEqual(contexto['canales'], [])

    def test_ingreso_cero_no_rompe_el_porcentaje(self):
        """Un pedido regalado: sin base no hay margen que calcular."""
        self.registrar_pedido(lineas=[(self.tarjetero, 1, '3994.18')],
                              bruto='0.00', comision='0.00', envio_vendedor='0.00')
        respuesta, contexto = self.reporte()

        self.assertEqual(respuesta.status_code, 200)
        fila = self.fila_llamada(contexto['productos'],
                                 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(fila['ganancia'], Decimal('-3994.18'))
        self.assertIsNone(fila['margen_pct'])


class TestAuth(BaseMargen):
    """La pantalla dice cuanta plata deja el negocio: no se mira sin sesion."""

    def test_sin_sesion_no_se_ve_el_reporte(self):
        respuesta = request_anonimo(self.ctx, 'get', '/reportes/margen')
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers.get('Location', ''))


if __name__ == '__main__':
    unittest.main()
