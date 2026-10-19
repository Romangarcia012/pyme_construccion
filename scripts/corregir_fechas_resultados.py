# -*- coding: utf-8 -*-
"""FASE-RESULTADOS-S1-AUDITORIA/CORRECCION: corrige lo que la migracion
52e5bd8bc955 tuvo que suponer. Por defecto NO escribe nada.

    python scripts/corregir_fechas_resultados.py                    # dry-run
    python scripts/corregir_fechas_resultados.py --fecha 34=2026-08-14
    python scripts/corregir_fechas_resultados.py --monto 34=229000.00
    python scripts/corregir_fechas_resultados.py --ajustar-aporte
    python scripts/corregir_fechas_resultados.py ... --aplicar      # escribe

QUE CORRIGE

1. FECHAS (`--fecha ID=AAAA-MM-DD`). La migracion recargo las compras
   borradas el 2026-09-14 (gastos 32 a 41) con fechas supuestas --
   `historial` guardo el monto pero no la fecha. La fecha buena esta en
   KORVO.xlsx y se pasa una por gasto. Sin `--fecha` el gasto se lista como
   "sin confirmar" y no se toca: el script no inventa ninguna.

2. MONTOS (`--monto ID=IMPORTE`). El reparto de "Taxes aduana prueba y
   tarj" entre tarjeteros (34) y muestras (39) fue una inferencia de la
   FASE 1; el Excel tiene los importes reales.

3. APORTE (`--ajustar-aporte`). El aporte 10 ($2.371.037,83) es la suma de
   los gastos de capital al 2026-09-06, Sorteo incluido. El Sorteo
   ($15.975,16) no fue una salida de caja y la migracion lo borro como
   gasto: el aporte baja en ese monto.

PRECONDICION

El aporte de $42.000 que cubre la aduana de las pruebas (gasto 31) tiene que
estar cargado como ingreso de "Aporte de capital (socios)". Lo carga Roman a
mano; si no esta, el script no escribe nada.

SALVAGUARDAS

- Ningun DELETE, ningun INSERT de gasto ni de ingreso, nada de esquema, ni
  categoria ni origen de ningun gasto.
- Cada gasto tiene que seguir siendo el que cargo la migracion: misma
  empresa, misma descripcion, y fecha y monto iguales al valor que dejo la
  migracion o al valor pedido. Cualquier otro valor quiere decir que alguien
  lo edito a mano, y entonces no se escribe NADA.
- Un valor que ya es el pedido se informa como "igual" y no se toca: correr
  dos veces con --aplicar no cambia nada ni duplica historial la segunda.
- Todo va en UNA transaccion, y cada cambio deja su fila en `historial` con
  el valor anterior y el nuevo (la migracion no dejo ninguna).
- El dry-run abre la transaccion de solo lectura en Postgres.
"""

import argparse
import os
import sys
from collections import OrderedDict
from datetime import date
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import (  # noqa: E402
    NATURALEZA_GASTO,
    Categoria,
    Empresa,
    Gasto,
    Historial,
    Ingreso,
    db,
)

EMPRESA = 'Korvo'
CENTAVO = Decimal('0.01')

# Lo que cargo la migracion 52e5bd8bc955: (id, descripcion, monto, fecha).
RECARGADOS = [
    (32, 'Compra tarjeteros', '481355.00', '2026-07-28'),
    (33, 'Envio Tarjeteros', '487930.00', '2026-07-28'),
    (34, "Taxes aduana tarjeteros (parte de 'Taxes aduana prueba y tarj')",
     '228969.00', '2026-07-28'),
    (35, 'Compra microfonos', '464000.00', '2026-07-28'),
    (36, 'Envio Microfonos', '294000.00', '2026-07-28'),
    (37, 'Pago taxes microfonos', '256417.17', '2026-09-14'),
    (38, 'Compra Muestras', '271740.00', '2026-07-28'),
    (39, "Taxes aduana muestras (parte de 'Taxes aduana prueba y tarj')",
     '17568.67', '2026-07-28'),
    (40, 'Piedra que se seca', '61500.00', '2026-07-28'),
    (41, 'Pago envio prod pruebas', '600000.00', '2026-09-14'),
]

APORTE_ID = 10
APORTE_CATEGORIA = 'Aporte de capital (socios)'
APORTE_MONTO = Decimal('2371037.83')
SORTEO = Decimal('15975.16')
APORTE_ADUANA_PRUEBAS = Decimal('42000.00')

ORIGEN = 'script corregir_fechas_resultados'


def _pares(crudos, nombre, convertir):
    valores = {}
    for par in crudos or []:
        try:
            crudo_id, crudo_valor = par.split('=', 1)
            valores[int(crudo_id)] = convertir(crudo_valor)
        except (ValueError, InvalidOperation):
            raise SystemExit('--%s %r: el formato es ID=VALOR' % (nombre, par))
    desconocidos = set(valores) - {fila[0] for fila in RECARGADOS}
    if desconocidos:
        raise SystemExit('--%s: %s no es un gasto recargado por la migracion'
                         % (nombre, ', '.join(map(str, sorted(desconocidos)))))
    return valores


def _monto(texto):
    valor = Decimal(texto).quantize(CENTAVO)
    if valor <= 0:
        raise ValueError(texto)
    return valor


def _plata(valor):
    return ('{:,.2f}'.format(valor)
            .replace(',', 'X').replace('.', ',').replace('X', '.'))


def _mes(fecha):
    return fecha.strftime('%Y-%m')


def _saldo_caja(empresa_id):
    """Σ ingreso - Σ gasto: la cuenta de `app.caja_general`."""
    entradas = sum((i.monto for i in Ingreso.query.filter_by(empresa_id=empresa_id)),
                   Decimal('0'))
    salidas = sum((g.monto for g in Gasto.query.filter_by(empresa_id=empresa_id)),
                  Decimal('0'))
    return entradas, salidas


def _aportes(empresa_id):
    return [i for i in Ingreso.query.filter_by(empresa_id=empresa_id)
            if i.categoria and i.categoria.nombre == APORTE_CATEGORIA]


def _resultado_por_mes(empresa_id):
    """{'AAAA-MM' | 'todo': utilidad neta} con la funcion de la pantalla."""
    from rutas_reportes import _periodos_disponibles, resultado_del_negocio
    with app.test_request_context():
        filas = OrderedDict()
        for periodo in reversed(_periodos_disponibles(empresa_id)):
            filas[periodo.clave] = resultado_del_negocio(empresa_id, periodo)['utilidad_neta']
        filas['todo'] = resultado_del_negocio(empresa_id)['utilidad_neta']
        return filas


def _conciliacion(empresa_id):
    from rutas_reportes import activos_del_negocio, resultado_del_negocio
    with app.test_request_context():
        return activos_del_negocio(empresa_id, resultado_del_negocio(empresa_id))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--fecha', action='append', metavar='ID=AAAA-MM-DD')
    parser.add_argument('--monto', action='append', metavar='ID=IMPORTE')
    parser.add_argument('--ajustar-aporte', action='store_true')
    parser.add_argument('--aplicar', action='store_true',
                        help='escribir de verdad (sin esto, dry-run)')
    args = parser.parse_args(argv)
    fechas = _pares(args.fecha, 'fecha', date.fromisoformat)
    montos = _pares(args.monto, 'monto', _monto)

    with app.app_context():
        if not args.aplicar and db.engine.dialect.name == 'postgresql':
            # El dry-run no puede escribir aunque el codigo se equivoque.
            db.session.execute(db.text('SET TRANSACTION READ ONLY'))

        empresa = Empresa.query.filter_by(nombre=EMPRESA).one()
        errores = []
        cambios = []   # (gasto, campo, antes, despues)

        print('\n%s -- %s\n' % ('APLICANDO' if args.aplicar
                                else 'DRY-RUN (no escribe nada)', EMPRESA))

        # -- precondicion ----------------------------------------------------
        aportes = _aportes(empresa.id)
        if not any(i.monto == APORTE_ADUANA_PRUEBAS and i.id != APORTE_ID
                   for i in aportes):
            errores.append('falta el aporte de %s que cubre la aduana de las '
                           'pruebas (gasto 31): cargalo desde Nuevo Ingreso'
                           % _plata(APORTE_ADUANA_PRUEBAS))

        # -- gastos ----------------------------------------------------------
        print('1. GASTOS RECARGADOS')
        print('   %-3s %-36s %-10s  %-10s  %14s  %14s  %s'
              % ('id', 'descripcion', 'fecha', 'fecha nueva', 'monto',
                 'monto nuevo', 'naturaleza'))
        filas = []
        for gasto_id, descripcion, monto, fecha in RECARGADOS:
            monto, fecha = Decimal(monto), date.fromisoformat(fecha)
            gasto = db.session.get(Gasto, gasto_id)
            pedida_fecha = fechas.get(gasto_id)
            pedido_monto = montos.get(gasto_id)
            if (gasto is None or gasto.empresa_id != empresa.id
                    or gasto.descripcion != descripcion):
                errores.append('el gasto %s ya no es "%s"' % (gasto_id, descripcion))
                continue
            if gasto.fecha not in {fecha, pedida_fecha}:
                errores.append('el gasto %s tiene fecha %s: no es la de la migracion '
                               '(%s) ni la pedida; alguien lo edito'
                               % (gasto_id, gasto.fecha, fecha))
            if gasto.monto not in {monto, pedido_monto}:
                errores.append('el gasto %s tiene monto %s: no es el de la migracion '
                               '(%s) ni el pedido; alguien lo edito'
                               % (gasto_id, gasto.monto, monto))
            nueva_fecha = pedida_fecha or gasto.fecha
            nuevo_monto = pedido_monto or gasto.monto
            if nueva_fecha != gasto.fecha:
                cambios.append((gasto, 'fecha', gasto.fecha, nueva_fecha))
            if nuevo_monto != gasto.monto:
                cambios.append((gasto, 'monto', gasto.monto, nuevo_monto))
            naturaleza = gasto.categoria.naturaleza if gasto.categoria else None
            filas.append((gasto, nueva_fecha, nuevo_monto, naturaleza))
            print('   %-3s %-36s %-10s  %-10s  %14s  %14s  %s'
                  % (gasto_id, descripcion[:36], gasto.fecha,
                     'igual' if nueva_fecha == gasto.fecha else nueva_fecha,
                     _plata(gasto.monto),
                     'igual' if nuevo_monto == gasto.monto else _plata(nuevo_monto),
                     naturaleza))
        sin_fecha = [f[0] for f in RECARGADOS if f[0] not in fechas]
        print('   sin fecha del Excel (conservan la actual): %s'
              % (', '.join(map(str, sin_fecha)) or 'ninguno'))

        # -- aporte ----------------------------------------------------------
        aporte = db.session.get(Ingreso, APORTE_ID)
        nuevo_aporte = None
        if (aporte is None or aporte.empresa_id != empresa.id
                or not aporte.categoria
                or aporte.categoria.nombre != APORTE_CATEGORIA):
            errores.append('el ingreso %s ya no es el aporte de capital' % APORTE_ID)
        elif args.ajustar_aporte:
            objetivo = APORTE_MONTO - SORTEO
            if aporte.monto not in {APORTE_MONTO, objetivo}:
                errores.append('el aporte %s vale %s: no es %s ni %s'
                               % (APORTE_ID, aporte.monto, APORTE_MONTO, objetivo))
            elif aporte.monto != objetivo:
                nuevo_aporte = objetivo
        print('\n2. APORTE %s: %s -> %s'
              % (APORTE_ID, _plata(aporte.monto) if aporte else '?',
                 _plata(nuevo_aporte) if nuevo_aporte is not None
                 else ('igual' if args.ajustar_aporte else 'sin cambio (falta --ajustar-aporte)')))

        # -- efectos ---------------------------------------------------------
        delta_gastos = sum((d - a for _, campo, a, d in cambios if campo == 'monto'),
                           Decimal('0'))
        delta_aporte = (nuevo_aporte - aporte.monto) if nuevo_aporte is not None else Decimal('0')

        entradas, salidas = _saldo_caja(empresa.id)
        print('\n3. CAJA GENERAL')
        print('   entradas  %16s -> %16s' % (_plata(entradas), _plata(entradas + delta_aporte)))
        print('   salidas   %16s -> %16s' % (_plata(salidas), _plata(salidas + delta_gastos)))
        print('   saldo     %16s -> %16s' % (_plata(entradas - salidas),
                                            _plata(entradas + delta_aporte - salidas - delta_gastos)))

        capital = sum((g.monto for g in Gasto.query.filter_by(
            empresa_id=empresa.id, origen_fondo='capital')), Decimal('0'))
        delta_capital = sum((d - a for g, campo, a, d in cambios
                             if campo == 'monto' and g.origen_fondo == 'capital'),
                            Decimal('0'))
        total_aportes = sum((i.monto for i in aportes), Decimal('0'))
        print('   gastos de capital %16s -> %16s' % (_plata(capital), _plata(capital + delta_capital)))
        print('   aportes           %16s -> %16s' % (_plata(total_aportes),
                                                    _plata(total_aportes + delta_aporte)))
        print('   aportes - capital %16s -> %16s'
              % (_plata(total_aportes - capital),
                 _plata(total_aportes + delta_aporte - capital - delta_capital)))

        # El "despues" del resultado es una proyeccion: solo los gastos de
        # naturaleza gasto entran en la utilidad, y un cambio de fecha o de
        # monto mueve su resta de un mes al otro. Despues de --aplicar el
        # script vuelve a calcular todo con la funcion de la pantalla.
        antes = _resultado_por_mes(empresa.id)
        despues = OrderedDict(antes)
        for gasto, nueva_fecha, nuevo_monto, naturaleza in filas:
            if naturaleza != NATURALEZA_GASTO:
                continue
            despues[_mes(gasto.fecha)] = despues.get(_mes(gasto.fecha), Decimal('0')) + gasto.monto
            despues[_mes(nueva_fecha)] = despues.get(_mes(nueva_fecha), Decimal('0')) - nuevo_monto
            despues['todo'] += gasto.monto - nuevo_monto
        print('\n4. UTILIDAD NETA POR MES (resultado del negocio)')
        for clave in sorted(set(antes) | set(despues), key=lambda c: (c == 'todo', c)):
            print('   %-8s %16s -> %16s' % (clave, _plata(antes.get(clave, Decimal('0'))),
                                          _plata(despues.get(clave, Decimal('0')))))

        conciliacion = _conciliacion(empresa.id)
        delta_activo = sum((d - a for g, campo, a, d in cambios if campo == 'monto'
                            and g.categoria and g.categoria.naturaleza != NATURALEZA_GASTO),
                           Decimal('0'))
        print('\n5. CONCILIACION DE ACTIVOS: diferencia %s -> %s'
              % (_plata(conciliacion['diferencia']),
                 _plata(conciliacion['diferencia'] + delta_activo)))

        if errores:
            db.session.rollback()
            print('\nNO SE ESCRIBE NADA:')
            for error in errores:
                print('   - ' + error)
            return 1

        if not args.aplicar:
            db.session.rollback()
            print('\nDry-run: %d cambio(s) en gastos y %d en el aporte. Nada escrito.'
                  % (len(cambios), 0 if nuevo_aporte is None else 1))
            return 0

        for gasto, campo, anterior, nuevo in cambios:
            db.session.add(Historial(
                empresa_id=empresa.id, usuario_id=None, accion='editar',
                tipo='gasto', id_registro=gasto.id,
                descripcion='%s corregido segun KORVO.xlsx (%s)'
                            % (campo.capitalize(), ORIGEN),
                valor_anterior=str(anterior), valor_nuevo=str(nuevo)))
            setattr(gasto, campo, nuevo)
        if nuevo_aporte is not None:
            db.session.add(Historial(
                empresa_id=empresa.id, usuario_id=None, accion='editar',
                tipo='ingreso', id_registro=aporte.id,
                descripcion='Aporte sin el Sorteo, que no fue salida de caja (%s)'
                            % ORIGEN,
                valor_anterior=str(aporte.monto), valor_nuevo=str(nuevo_aporte)))
            aporte.monto = nuevo_aporte
        db.session.commit()

        entradas, salidas = _saldo_caja(empresa.id)
        print('\nAplicado: %d cambio(s) en gastos, %d en el aporte.'
              % (len(cambios), 0 if nuevo_aporte is None else 1))
        print('\nVERIFICACION CONTRA LA BASE (recalculado, no proyectado)')
        print('   saldo Caja General %s' % _plata(entradas - salidas))
        capital = sum((g.monto for g in Gasto.query.filter_by(
            empresa_id=empresa.id, origen_fondo='capital')), Decimal('0'))
        total_aportes = sum((i.monto for i in _aportes(empresa.id)), Decimal('0'))
        print('   aportes - capital  %s' % _plata(total_aportes - capital))
        for clave, neta in _resultado_por_mes(empresa.id).items():
            print('   utilidad neta %-8s %s' % (clave, _plata(neta)))
        print('   conciliacion de activos: diferencia %s'
              % _plata(_conciliacion(empresa.id)['diferencia']))
        return 0


if __name__ == '__main__':
    sys.exit(main())
