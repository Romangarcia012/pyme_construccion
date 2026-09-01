"""Rutas de integraciones con canales de venta (FASE3-S1).

Solo autenticacion: conectar la tienda y guardar el token cifrado. Traer
pedidos y pagos es FASE3-S2.

El blueprint se registra en app.py; ninguna ruta existente se toca.
"""

import sys
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

import cripto
import integracion_tiendanube as tn
from models import CanalVenta, CredencialCanal, db

integraciones_bp = Blueprint('integraciones', __name__, url_prefix='/integraciones')

TIPO_TIENDANUBE = 'tiendanube'

# Canales que la UI muestra siempre, esten o no en la base. El endpoint es
# None mientras el canal no tenga flujo de conexion implementado.
CANALES_CONOCIDOS = [
    {'tipo': TIPO_TIENDANUBE, 'nombre': 'Tiendanube',
     'endpoint_conectar': 'integraciones.conectar_tiendanube'},
    {'tipo': 'mercadolibre', 'nombre': 'Mercado Libre',
     'endpoint_conectar': None},
]


def _log(mensaje):
    """Detalle tecnico al log del servidor. Nunca al navegador: los detalles
    de error de OAuth pueden traer eco de credenciales."""
    sys.stderr.write(f'[integraciones] {mensaje}\n')
    sys.stderr.flush()


def _canal(tipo, crear=False):
    """El canal_venta de la empresa del usuario actual.

    La migracion de FASE2-S1 sembro las filas para las empresas que existian
    en ese momento; una empresa creada despues no las tiene, asi que el
    callback las crea al vuelo en vez de fallar.
    """
    canal = CanalVenta.query.filter_by(
        empresa_id=current_user.empresa_id, tipo=tipo
    ).first()
    if canal is None and crear:
        nombre = next((c['nombre'] for c in CANALES_CONOCIDOS if c['tipo'] == tipo), tipo)
        canal = CanalVenta(
            empresa_id=current_user.empresa_id, tipo=tipo, nombre=nombre, activo=False
        )
        db.session.add(canal)
        db.session.flush()
    return canal


@integraciones_bp.route('')
@login_required
def listar():
    """Estado de cada canal: conectado o no, y con que cuenta."""
    por_tipo = {
        c.tipo: c
        for c in CanalVenta.query.filter_by(empresa_id=current_user.empresa_id).all()
    }

    canales = []
    for conocido in CANALES_CONOCIDOS:
        fila = por_tipo.get(conocido['tipo'])
        canales.append({
            'tipo': conocido['tipo'],
            # Si esta conectado, el nombre guardado es el de la tienda real.
            'nombre': fila.nombre if fila else conocido['nombre'],
            'activo': bool(fila and fila.activo),
            'cuenta_externa': fila.id_tienda_externo if fila else None,
            'ultima_sync': fila.fecha_ultima_sync if fila else None,
            'endpoint_conectar': conocido['endpoint_conectar'],
        })

    return render_template('integraciones.html', canales=canales)


@integraciones_bp.route('/tiendanube/conectar')
@login_required
def conectar_tiendanube():
    """Manda al usuario a aprobar la instalacion en Tiendanube.

    No escribe nada: hasta que Tiendanube no redirija al callback con un code
    valido, para esta app no paso nada.
    """
    return redirect(tn.url_autorizacion())


@integraciones_bp.route('/tiendanube/callback')
@login_required
def callback_tiendanube():
    """Vuelta de Tiendanube con ?code=.

    Todo lo que puede fallar (intercambio del code, prueba del token) pasa
    ANTES de tocar la base. Recien con las dos llamadas OK se escribe, y en un
    solo commit: no existe el estado "canal activo sin token" ni al reves.
    """
    if request.args.get('error'):
        _log(f"Tiendanube devolvio error en el callback: {request.args.get('error')}")
        flash('No se completo la conexion con Tiendanube: se cancelo la autorizacion.', 'danger')
        return redirect(url_for('integraciones.listar'))

    code = request.args.get('code')
    if not code:
        flash('Tiendanube no devolvio el codigo de autorizacion. Proba conectar de nuevo.', 'danger')
        return redirect(url_for('integraciones.listar'))

    try:
        token = tn.intercambiar_code(code)
        store = tn.traer_tienda(token['user_id'], token['access_token'])
    except tn.ErrorTiendanube as exc:
        _log(f'fallo la conexion con Tiendanube: {exc.detalle or exc}')
        flash(str(exc), 'danger')
        return redirect(url_for('integraciones.listar'))

    nombre_tienda = tn.nombre_de_tienda(store)

    try:
        canal = _canal(TIPO_TIENDANUBE, crear=True)
        canal.nombre = nombre_tienda
        canal.id_tienda_externo = token['user_id']
        canal.activo = True

        # Reconectar reutiliza la fila: un canal tiene una credencial vigente,
        # no una pila historica de tokens viejos.
        credencial = CredencialCanal.query.filter_by(canal_id=canal.id).first()
        if credencial is None:
            credencial = CredencialCanal(canal_id=canal.id, tipo_credencial='oauth2')
            db.session.add(credencial)

        credencial.access_token_cifrado = cripto.cifrar(token['access_token'])
        credencial.scope = (token.get('scope') or None)
        credencial.activo = True
        credencial.fecha_actualizacion = datetime.utcnow()
        # El token de Tiendanube no expira ni se refresca: no hay refresh_token
        # ni expira_en que guardar.

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        _log(f'fallo guardando la credencial de Tiendanube: {exc!r}')
        flash('Se autorizo la tienda pero no se pudo guardar la credencial. Proba de nuevo.', 'danger')
        return redirect(url_for('integraciones.listar'))

    flash(f'Tiendanube conectada: {nombre_tienda}.', 'success')
    return redirect(url_for('integraciones.listar'))
