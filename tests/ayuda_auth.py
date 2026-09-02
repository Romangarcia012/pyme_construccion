# -*- coding: utf-8 -*-
"""Ayuda compartida para los tests de @login_required.

EL PROBLEMA QUE RESUELVE

Todas las clases base de estos tests pushean un app_context en setUp y lo
popean en tearDown. Flask, desde 2.2, NO crea un app_context nuevo por request
si ya hay uno arriba para la misma app: lo reusa. Y flask_login cachea el
usuario resuelto en `g`, que vive en el app_context.

La suma de las dos cosas es una fuga: si dentro del mismo app_context ya corrio
un request logueado, el siguiente request -- aunque salga de un test_client
recien creado y sin una sola cookie -- se encuentra `g._login_user` ya puesto y
entra como si tuviera sesion.

Lo peligroso es como falla: un test que dice

    self.assertEqual(app.test_client().get('/lo/que/sea').status_code, 302)

pasa igual sin el @login_required en la ruta, porque el 302 lo termina
devolviendo cualquier otra cosa. El test queda verde afirmando algo que no
esta probando. Paso de verdad con TestListadoDeStock en FASE-STOCK-S1, donde
setUp dispara un GET logueado antes de que el test haga el suyo.

EL GUARD

`request_anonimo` da de baja el app_context del test mientras dura el request.
Sin app_context arriba, Flask crea uno nuevo para ese request, `g` nace vacio y
el cliente sin cookies es de verdad un anonimo.

Se usa en los tests de auth de todas las suites, incluso en los que hoy no lo
necesitan porque su setUp no dispara ningun request. Ese "hoy" es justamente el
punto: el dia que alguien le agregue un request al setUp de una de esas clases,
sin el guard el test de auth se volveria verde-mentiroso en silencio, y no hay
nada en la salida de la suite que lo delate.
"""


def request_anonimo(ctx, metodo, ruta, **kwargs):
    """Hace un request SIN sesion y devuelve la respuesta.

        resp = request_anonimo(self.ctx, 'get', '/pedidos/listar')
        resp = request_anonimo(self.ctx, 'post', '/x', data={'sku': ['A']})

    `ctx` es el app_context que pusheo el setUp (`self.ctx` en todas las clases
    base). Se popea antes del request y se vuelve a pushear despues, incluso si
    el request explota: lo que sigue del test -- las consultas que verifican
    que no se escribio nada -- necesita el contexto de vuelta.

    La app sale del propio contexto en vez de importarse, asi este modulo no
    depende del orden de imports de las suites.
    """
    ctx.pop()
    try:
        return getattr(ctx.app.test_client(), metodo)(ruta, **kwargs)
    finally:
        ctx.push()
