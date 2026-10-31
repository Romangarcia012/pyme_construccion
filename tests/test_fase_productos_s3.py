# -*- coding: utf-8 -*-
"""Tests de FASE-PRODUCTOS-S3 (editar, archivar y eliminar productos).

    python -m unittest discover -s tests -v

Lo que se prueba:

    editar uno manual             -> nombre, SKU, stock (None vs 0), activo, costo
    SKU repetido al editar        -> rechazado; el propio SKU no cuenta
    editar uno con mapeo          -> solo el costo, aunque el POST mande mas
    archivar                      -> sale de la venta manual, la venta pasada
                                     sigue en el margen; con mapeo, rechazado
    eliminar sin ventas           -> borra el producto y sus mapeos
    eliminar con ventas           -> rechazado, se ofrece archivar
    producto de otra empresa      -> 404 en todas las rutas
    auditoria                     -> el hook registra editar y eliminar solo
    listado                       -> no ofrece lo que la ruta rechaza

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_tiendanube  # noqa: E402
import sync_tiendanube  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    Empresa,
    Historial,
    MapeoProductoCanal,
    PedidoItem,
    Producto,
    Usuario,
    db,
)
from tests.ayuda_auth import request_anonimo  # noqa: E402

ENGINE_PRODUCTIVO = None

RUTA_VENTA = '/pedidos/manual/nuevo'
RUTA_LISTADO = '/productos/listar'


def ruta_editar(producto_id):
    return '/productos/editar/%d' % producto_id


def ruta_eliminar(producto_id):
    return '/productos/eliminar/%d' % producto_id


def ruta_archivar(producto_id):
    return '/productos/archivar/%d' % producto_id


def ruta_reactivar(producto_id):
    return '/productos/reactivar/%d' % producto_id


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


class Base(unittest.TestCase):
    """Una empresa con un producto del sync (con mapeo) y uno manual, y una
    segunda empresa con un producto propio."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-PRODUCTOS-S3')
        self.otra_empresa = Empresa(nombre='Empresa Ajena')
        db.session.add_all([self.empresa, self.otra_empresa])
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='faseproductos3@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial', activo=True)
        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True, id_tienda_externo='9999')
        db.session.add_all([self.canal_manual, self.canal_tn])
        db.session.flush()

        self.del_sync = Producto(empresa_id=self.empresa.id, sku='TARJ-NEGRO',
                                 nombre='Tarjetero Minimalista de Aluminio (Negro)',
                                 costo_unitario=Decimal('3994.18'), stock=93)
        self.manual = Producto(empresa_id=self.empresa.id, sku='CINTA-19',
                               nombre='Cinta aisladora 19mm',
                               costo_unitario=Decimal('300.00'), stock=5)
        self.ajeno = Producto(empresa_id=self.otra_empresa.id, sku='LIJA-120',
                              nombre='Lija de otra empresa', stock=7)
        db.session.add_all([self.del_sync, self.manual, self.ajeno])
        db.session.flush()

        db.session.add(MapeoProductoCanal(
            producto_id=self.del_sync.id, canal_id=self.canal_tn.id,
            id_producto_externo='360354459', id_variante_externo='1574653133',
            sku_externo='TARJ-NEGRO'))
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.canal_tn_id = self.canal_tn.id
        self.del_sync_id = self.del_sync.id
        self.manual_id = self.manual.id
        self.ajeno_id = self.ajeno.id

        # La venta manual empuja stock a Tiendanube: se anula para no salir a
        # internet.
        self.original_push = stock_tiendanube.empujar_stock
        stock_tiendanube.empujar_stock = lambda empresa_id, producto_ids: []

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        stock_tiendanube.empujar_stock = self.original_push
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def get(self, producto_id):
        db.session.expire_all()
        return db.session.get(Producto, producto_id)

    def texto(self, respuesta):
        return respuesta.get_data(as_text=True)

    def editar(self, producto_id, **campos):
        return self.client.post(ruta_editar(producto_id), data=campos,
                                follow_redirects=True)

    def editar_manual(self, **cambios):
        """POST de edicion del producto manual con todos sus campos, como lo
        manda el navegador; `cambios` pisa los que se quieran cambiar."""
        campos = {'sku': 'CINTA-19', 'nombre': 'Cinta aisladora 19mm',
                  'costo_unitario': '300.00', 'control_stock': 'si',
                  'stock': '5', 'activo': 'si'}
        campos.update(cambios)
        return self.editar(self.manual_id, **campos)

    def eliminar(self, producto_id, confirmar=True):
        datos = {'confirmar': 'si'} if confirmar else {}
        return self.client.post(ruta_eliminar(producto_id), data=datos,
                                follow_redirects=True)

    def vender(self, sku, precio='500.00', cantidad='1'):
        return self.client.post(RUTA_VENTA, data={
            'fecha': '2026-09-01', 'medio': 'efectivo',
            'sku': [sku], 'cantidad': [cantidad], 'precio_unitario': [precio],
        }, follow_redirects=True)

    def historial(self, accion):
        return (Historial.query
                .filter_by(empresa_id=self.empresa_id, tipo='producto', accion=accion)
                .all())


class TestEditarManual(Base):

    def test_get_muestra_el_formulario_con_lo_guardado(self):
        respuesta = self.client.get(ruta_editar(self.manual_id))
        self.assertEqual(respuesta.status_code, 200)
        texto = self.texto(respuesta)
        self.assertIn('value="CINTA-19"', texto)
        self.assertIn('value="Cinta aisladora 19mm"', texto)
        self.assertIn('value="300.00"', texto)

    def test_editar_nombre_y_stock(self):
        respuesta = self.editar_manual(nombre='Cinta aisladora 19mm negra', stock='12')
        self.assertEqual(respuesta.status_code, 200)
        producto = self.get(self.manual_id)
        self.assertEqual(producto.nombre, 'Cinta aisladora 19mm negra')
        self.assertEqual(producto.stock, 12)
        self.assertIn('Se guardo', self.texto(respuesta))

    def test_no_llevo_la_cuenta_guarda_none(self):
        self.editar_manual(control_stock='no', stock='')
        self.assertIsNone(self.get(self.manual_id).stock)

    def test_llevo_la_cuenta_con_cero_guarda_cero(self):
        self.editar_manual(control_stock='si', stock='0')
        stock = self.get(self.manual_id).stock
        self.assertIsNotNone(stock, '0 no es "no llevo la cuenta"')
        self.assertEqual(stock, 0)

    def test_de_none_se_vuelve_a_llevar_la_cuenta(self):
        self.editar_manual(control_stock='no', stock='')
        self.editar_manual(control_stock='si', stock='3')
        self.assertEqual(self.get(self.manual_id).stock, 3)

    def test_el_formulario_arranca_en_no_llevo_la_cuenta_si_es_none(self):
        self.editar_manual(control_stock='no', stock='')
        texto = self.texto(self.client.get(ruta_editar(self.manual_id)))
        # El radio "no" es el marcado, y el input de cantidad queda vacio.
        self.assertRegex(texto, r'value="no"\s+checked')
        self.assertNotRegex(texto, r'name="control_stock" value="si"\s+checked')

    def test_llevo_la_cuenta_sin_numero_se_rechaza(self):
        self.editar_manual(control_stock='si', stock='')
        self.assertEqual(self.get(self.manual_id).stock, 5)

    def test_stock_negativo_se_rechaza(self):
        self.editar_manual(stock='-1')
        self.assertEqual(self.get(self.manual_id).stock, 5)

    def test_editar_el_costo(self):
        self.editar_manual(costo_unitario='350,50')
        self.assertEqual(self.get(self.manual_id).costo_unitario, Decimal('350.50'))

    def test_costo_vacio_vuelve_a_null(self):
        self.editar_manual(costo_unitario='')
        self.assertIsNone(self.get(self.manual_id).costo_unitario)

    def test_costo_invalido_no_guarda_nada(self):
        respuesta = self.editar_manual(nombre='Otro nombre', costo_unitario='abc')
        producto = self.get(self.manual_id)
        self.assertEqual(producto.nombre, 'Cinta aisladora 19mm')
        self.assertEqual(producto.costo_unitario, Decimal('300.00'))
        # Lo tipeado vuelve al formulario.
        self.assertIn('value="Otro nombre"', self.texto(respuesta))

    def test_nombre_vacio_se_rechaza(self):
        self.editar_manual(nombre='')
        self.assertEqual(self.get(self.manual_id).nombre, 'Cinta aisladora 19mm')

    def test_sin_cambios_lo_avisa(self):
        respuesta = self.editar_manual()
        self.assertIn('No hubo cambios', self.texto(respuesta))

    def test_un_producto_manual_ofrece_todos_los_campos(self):
        texto = self.texto(self.client.get(ruta_editar(self.manual_id)))
        for campo in ('name="sku"', 'name="nombre"', 'name="control_stock"',
                      'name="activo"', 'name="costo_unitario"'):
            self.assertIn(campo, texto)


class TestSkuAlEditar(Base):

    def test_sku_duplicado_rechazado(self):
        respuesta = self.editar_manual(sku='TARJ-NEGRO')
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self.get(self.manual_id).sku, 'CINTA-19')
        # Jinja escapa las comillas del flash como &#34;.
        self.assertIn('Ya existe un producto con el codigo &#34;TARJ-NEGRO&#34;',
                      self.texto(respuesta))

    def test_guardar_con_su_propio_sku_no_es_duplicado(self):
        respuesta = self.editar_manual(nombre='Cinta 19mm')
        self.assertNotIn('Ya existe', self.texto(respuesta))
        self.assertEqual(self.get(self.manual_id).nombre, 'Cinta 19mm')

    def test_cambiar_a_un_sku_libre(self):
        self.editar_manual(sku='CINTA-19-N')
        self.assertEqual(self.get(self.manual_id).sku, 'CINTA-19-N')

    def test_el_sku_de_otra_empresa_no_choca(self):
        self.editar_manual(sku='LIJA-120')
        self.assertEqual(self.get(self.manual_id).sku, 'LIJA-120')
        self.assertEqual(self.get(self.ajeno_id).sku, 'LIJA-120')


class TestEditarConMapeo(Base):
    """El sync pisa nombre, SKU (por identidad), stock y activo: solo el costo."""

    def test_la_pantalla_avisa_que_se_cambia_en_el_canal(self):
        texto = self.texto(self.client.get(ruta_editar(self.del_sync_id)))
        self.assertIn('Viene de Tiendanube: se cambia allá', texto)
        # Solo el costo es un input.
        self.assertIn('name="costo_unitario"', texto)
        for campo in ('name="sku"', 'name="nombre"', 'name="control_stock"',
                      'name="stock"', 'name="activo"'):
            self.assertNotIn(campo, texto)

    def test_post_directo_no_cambia_nombre_sku_stock_ni_activo(self):
        self.editar(self.del_sync_id, sku='OTRO-SKU', nombre='Nombre inventado',
                    control_stock='no', stock='', activo='no',
                    costo_unitario='3994.18')
        producto = self.get(self.del_sync_id)
        self.assertEqual(producto.sku, 'TARJ-NEGRO')
        self.assertEqual(producto.nombre, 'Tarjetero Minimalista de Aluminio (Negro)')
        self.assertEqual(producto.stock, 93)
        self.assertTrue(producto.activo)

    def test_el_costo_si_se_edita(self):
        self.editar(self.del_sync_id, costo_unitario='4100.00', nombre='ignorado')
        producto = self.get(self.del_sync_id)
        self.assertEqual(producto.costo_unitario, Decimal('4100.00'))
        self.assertEqual(producto.nombre, 'Tarjetero Minimalista de Aluminio (Negro)')

    def test_archivar_con_mapeo_se_rechaza(self):
        respuesta = self.client.post(ruta_archivar(self.del_sync_id),
                                     follow_redirects=True)
        self.assertTrue(self.get(self.del_sync_id).activo)
        self.assertIn('se cambia alla', self.texto(respuesta))


class TestArchivar(Base):

    def setUp(self):
        super().setUp()
        self.vender('CINTA-19', precio='500.00')

    def test_archivar_lo_saca_de_venta_manual(self):
        self.assertIn('CINTA-19', self.texto(self.client.get(RUTA_VENTA)))
        self.client.post(ruta_archivar(self.manual_id), follow_redirects=True)
        self.assertFalse(self.get(self.manual_id).activo)
        self.assertNotIn('CINTA-19', self.texto(self.client.get(RUTA_VENTA)))

    def test_archivado_no_se_puede_vender_por_post(self):
        self.client.post(ruta_archivar(self.manual_id))
        self.vender('CINTA-19')
        self.assertEqual(PedidoItem.query.filter_by(producto_id=self.manual_id).count(),
                         1, 'solo la venta de antes de archivar')

    def test_la_venta_pasada_sigue_en_el_margen(self):
        self.client.post(ruta_archivar(self.manual_id))
        texto = self.texto(self.client.get('/reportes/margen'))
        self.assertIn('Cinta aisladora 19mm', texto)
        # 500 - 300 de costo = 200 de ganancia.
        self.assertIn('>200.00<', texto.replace('\n', '').replace(' ', ''))

    def test_en_stock_aparece_marcado_archivado(self):
        self.client.post(ruta_archivar(self.manual_id))
        texto = self.texto(self.client.get(RUTA_LISTADO))
        self.assertIn('Cinta aisladora 19mm', texto)
        self.assertIn('(archivado)', texto)

    def test_reactivar(self):
        self.client.post(ruta_archivar(self.manual_id))
        self.client.post(ruta_reactivar(self.manual_id))
        self.assertTrue(self.get(self.manual_id).activo)
        self.assertIn('CINTA-19', self.texto(self.client.get(RUTA_VENTA)))

    def test_archivar_desde_la_edicion(self):
        self.editar_manual(activo='no')
        self.assertFalse(self.get(self.manual_id).activo)
        self.editar_manual(activo='si')
        self.assertTrue(self.get(self.manual_id).activo)

    def test_archivar_es_solo_por_post(self):
        self.assertEqual(self.client.get(ruta_archivar(self.manual_id)).status_code, 405)
        self.assertTrue(self.get(self.manual_id).activo)


class TestEliminar(Base):

    def test_el_post_sin_confirmar_muestra_la_confirmacion_y_no_borra(self):
        respuesta = self.eliminar(self.manual_id, confirmar=False)
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('Esto no se puede deshacer', self.texto(respuesta))
        self.assertIsNotNone(self.get(self.manual_id))

    def test_el_get_tampoco_borra(self):
        self.client.get(ruta_eliminar(self.manual_id))
        self.assertIsNotNone(self.get(self.manual_id))

    def test_eliminar_sin_ventas(self):
        respuesta = self.eliminar(self.manual_id)
        self.assertEqual(respuesta.status_code, 200)
        self.assertIsNone(self.get(self.manual_id))
        self.assertIn('Se elimino', self.texto(respuesta))

    def test_eliminar_con_mapeo_borra_sus_mapeos(self):
        respuesta = self.eliminar(self.del_sync_id)
        self.assertIsNone(self.get(self.del_sync_id))
        self.assertEqual(MapeoProductoCanal.query.filter_by(
            producto_id=self.del_sync_id).count(), 0)
        self.assertEqual(MapeoProductoCanal.query.count(), 0)
        self.assertIn('proxima sincronizacion', self.texto(respuesta))

    def test_la_confirmacion_con_mapeo_avisa_que_vuelve(self):
        texto = self.texto(self.client.get(ruta_eliminar(self.del_sync_id)))
        self.assertIn('vuelve en la próxima sincronización', texto)

    def test_el_aviso_es_cierto_el_sync_lo_trae_sin_costo(self):
        """Lo que dice la confirmacion, verificado contra el sync de verdad."""
        self.eliminar(self.del_sync_id)
        canal = db.session.get(CanalVenta, self.canal_tn_id)
        producto, es_nuevo = sync_tiendanube._upsert_producto_y_mapeo(canal, {
            'sku': 'TARJ-NEGRO',
            'nombre': 'Tarjetero Minimalista de Aluminio (Negro)',
            'activo': True, 'stock': 93, 'precio_lista': None,
            'id_producto_externo': '360354459',
            'id_variante_externo': '1574653133', 'sku_externo': 'TARJ-NEGRO',
        })
        db.session.commit()
        self.assertTrue(es_nuevo)
        self.assertIsNone(producto.costo_unitario)

    def test_con_ventas_se_rechaza(self):
        self.vender('CINTA-19')
        respuesta = self.eliminar(self.manual_id)
        self.assertIsNotNone(self.get(self.manual_id))
        texto = self.texto(respuesta)
        self.assertIn('no se puede eliminar', texto)
        # Sin mapeo y activo: se ofrece archivar, y no el boton de borrar.
        self.assertIn(ruta_archivar(self.manual_id), texto)
        self.assertNotIn('name="confirmar"', texto)

    def test_con_ventas_la_venta_queda_intacta(self):
        self.vender('CINTA-19')
        self.eliminar(self.manual_id)
        item = PedidoItem.query.filter_by(producto_id=self.manual_id).first()
        self.assertIsNotNone(item, 'la linea sigue atada al producto')

    def test_con_ventas_y_mapeo_no_ofrece_archivar(self):
        db.session.add(PedidoItem(pedido_id=self._pedido_tn(), producto_id=self.del_sync_id,
                                  descripcion='Tarjetero', cantidad=1,
                                  precio_unitario=Decimal('1000'),
                                  subtotal=Decimal('1000')))
        db.session.commit()
        texto = self.texto(self.client.get(ruta_eliminar(self.del_sync_id)))
        self.assertIn('no se puede eliminar', texto)
        self.assertNotIn(ruta_archivar(self.del_sync_id), texto)

    def _pedido_tn(self):
        from datetime import datetime
        from models import Pedido
        pedido = Pedido(empresa_id=self.empresa_id, canal_id=self.canal_tn_id,
                        id_externo='1', fecha_pedido=datetime(2026, 9, 1),
                        estado='open', moneda='ARS', total_bruto=Decimal('1000'),
                        total=Decimal('1000'))
        db.session.add(pedido)
        db.session.flush()
        return pedido.id


class TestOtraEmpresa(Base):

    def test_editar_ajeno_es_404(self):
        self.assertEqual(self.client.get(ruta_editar(self.ajeno_id)).status_code, 404)
        respuesta = self.client.post(ruta_editar(self.ajeno_id),
                                     data={'sku': 'X', 'nombre': 'Robado',
                                           'activo': 'no'})
        self.assertEqual(respuesta.status_code, 404)
        producto = self.get(self.ajeno_id)
        self.assertEqual(producto.nombre, 'Lija de otra empresa')
        self.assertTrue(producto.activo)

    def test_eliminar_ajeno_es_404(self):
        respuesta = self.client.post(ruta_eliminar(self.ajeno_id),
                                     data={'confirmar': 'si'})
        self.assertEqual(respuesta.status_code, 404)
        self.assertIsNotNone(self.get(self.ajeno_id))

    def test_archivar_ajeno_es_404(self):
        self.assertEqual(self.client.post(ruta_archivar(self.ajeno_id)).status_code, 404)
        self.assertEqual(self.client.post(ruta_reactivar(self.ajeno_id)).status_code, 404)
        self.assertTrue(self.get(self.ajeno_id).activo)

    def test_inexistente_es_404(self):
        self.assertEqual(self.client.get(ruta_editar(99999)).status_code, 404)


class TestAuditoria(Base):
    """`Producto` esta en TABLAS_AUDITADAS: el hook registra todo solo."""

    def test_editar_queda_registrado(self):
        self.editar_manual(nombre='Cinta negra')
        filas = self.historial('editar')
        self.assertEqual(len(filas), 1, 'una fila por campo cambiado: solo el nombre')
        fila = filas[0]
        self.assertEqual(fila.usuario_id, self.usuario_id)
        self.assertEqual(fila.id_registro, self.manual_id)
        self.assertIn('nombre', fila.descripcion)
        self.assertEqual(fila.valor_anterior, 'Cinta aisladora 19mm')
        self.assertEqual(fila.valor_nuevo, 'Cinta negra')

    def test_eliminar_queda_registrado(self):
        self.eliminar(self.manual_id)
        filas = self.historial('eliminar')
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].id_registro, self.manual_id)
        self.assertEqual(filas[0].usuario_id, self.usuario_id)
        self.assertIn('eliminado', filas[0].descripcion)

    def test_archivar_queda_registrado(self):
        self.client.post(ruta_archivar(self.manual_id))
        filas = self.historial('editar')
        self.assertEqual(len(filas), 1)
        self.assertIn('activo', filas[0].descripcion)

    def test_un_rechazo_no_deja_historial(self):
        self.editar_manual(sku='TARJ-NEGRO', nombre='Otro')
        self.assertEqual(self.historial('editar'), [])


class TestListado(Base):
    """La pantalla no ofrece lo que la ruta despues rechaza."""

    def test_cada_fila_tiene_editar(self):
        texto = self.texto(self.client.get(RUTA_LISTADO))
        self.assertIn('href="%s"' % ruta_editar(self.manual_id), texto)
        self.assertIn('href="%s"' % ruta_editar(self.del_sync_id), texto)

    def test_sin_ventas_ofrece_eliminar_con_form_aparte(self):
        texto = self.texto(self.client.get(RUTA_LISTADO))
        self.assertIn('form="form-eliminar-%d"' % self.manual_id, texto)
        self.assertIn('id="form-eliminar-%d"' % self.manual_id, texto)

    def test_con_ventas_no_ofrece_eliminar(self):
        self.vender('CINTA-19')
        texto = self.texto(self.client.get(RUTA_LISTADO))
        self.assertNotIn('form-eliminar-%d"' % self.manual_id, texto)
        self.assertIn('con ventas', texto)

    def test_el_form_de_eliminar_no_esta_adentro_del_de_costos(self):
        texto = self.texto(self.client.get(RUTA_LISTADO))
        cierre_costos = texto.index('</form>')
        self.assertGreater(texto.index('id="form-eliminar-%d"' % self.manual_id),
                           cierre_costos)

    def test_el_guardado_de_costos_sigue_andando(self):
        """Los botones nuevos viven dentro del form de costos: no lo rompen."""
        self.client.post('/productos/costos', data={
            'sku': ['CINTA-19', 'TARJ-NEGRO'],
            'costo_unitario': ['310.00', '3994.18']})
        self.assertEqual(self.get(self.manual_id).costo_unitario, Decimal('310.00'))

    def test_ajeno_no_aparece(self):
        texto = self.texto(self.client.get(RUTA_LISTADO))
        self.assertNotIn(ruta_editar(self.ajeno_id), texto)


class TestSinSesion(Base):

    def test_las_rutas_piden_login(self):
        for metodo, ruta in (('get', ruta_editar(self.manual_id)),
                             ('post', ruta_editar(self.manual_id)),
                             ('post', ruta_eliminar(self.manual_id)),
                             ('post', ruta_archivar(self.manual_id)),
                             ('post', ruta_reactivar(self.manual_id))):
            respuesta = request_anonimo(self.ctx, metodo, ruta,
                                        data={'confirmar': 'si', 'activo': 'no'})
            self.assertEqual(respuesta.status_code, 302, ruta)
        producto = self.get(self.manual_id)
        self.assertIsNotNone(producto)
        self.assertTrue(producto.activo)


if __name__ == '__main__':
    unittest.main()
