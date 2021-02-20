import os
import sys
from pathlib import Path
from typing import Callable

import aiohttp_jinja2
import jinja2
from aiohttp import web

from . import panels, views
from .tbtools.tbtools import get_traceback
from .toolbar import DebugToolbar
from .utils import (APP_KEY, REDIRECT_CODES, ROOT_ROUTE_NAME,
                    STATIC_ROUTE_NAME, TEMPLATE_KEY, ContextSwitcher,
                    ExceptionHistory, ToolbarStorage, addr_in, hexlify, render)

HTML_TYPES = ('text/html', 'application/xhtml+xml')

default_panel_names = [
    panels.HeaderDebugPanel,
    panels.PerformanceDebugPanel,
    panels.RequestVarsDebugPanel,
    panels.TracebackPanel,
    panels.LoggingPanel,
]

default_global_panel_names = [
    panels.RoutesDebugPanel,
    panels.SettingsDebugPanel,
    panels.MiddlewaresDebugPanel,
    panels.VersionDebugPanel,
]

default_settings = {
    'enabled': True,
    'intercept_exc': 'debug',  # display or debug or False
    'intercept_redirects': True,
    'panels': default_panel_names,
    'extra_panels': [],
    'extra_templates': [],
    'global_panels': default_global_panel_names,
    'extra_global_panels': [],
    'hosts': ['127.0.0.1', '::1'],
    'exclude_prefixes': [],
    # disable host check
    'check_host': True,
    'button_style': '',
    'max_request_history': 100,
    'max_visible_requests': 10,
    'path_prefix': '/_debugtoolbar',
}


class DebugToolbarApp(web.Application):
    def __init__(self, *args, config, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self[APP_KEY] = {}
        self[APP_KEY]['settings'] = config
        self[APP_KEY]['exc_history'] = ExceptionHistory()
        self[APP_KEY]['pdtb_token'] = hexlify(os.urandom(10))
        self[APP_KEY]['request_history'] = ToolbarStorage(config['max_request_history'])
        intercept_exc = config['intercept_exc']
        if intercept_exc:
            self[APP_KEY]['exc_history'].eval_exc = intercept_exc == 'debug'

        APP_ROOT = Path(__file__).parent
        templates_app = APP_ROOT / 'templates'
        templates_panels = APP_ROOT / 'panels/templates'
        extra_tpl_path = config.get('extra_templates', [])
        if isinstance(extra_tpl_path, str):
            extra_tpl_path = [extra_tpl_path]

        loader = jinja2.FileSystemLoader(
            [str(templates_app), str(templates_panels)] + list(extra_tpl_path))
        self.env = aiohttp_jinja2.setup(self, loader=loader, app_key=TEMPLATE_KEY)
        self[APP_KEY]['jinja'] = self.env

        static_location = APP_ROOT / 'static'
        self.add_routes([web.static('/static', static_location,
                                    name=STATIC_ROUTE_NAME)])

        exc_handlers = views.ExceptionDebugView()
        self.add_routes(exc_handlers.routes)
        self.add_routes(views.routes)
        self[APP_KEY]['router'] = self.router

    def get_middleware(self):
        @web.middleware
        async def middleware(*args, **kwargs):
            return await self.middleware(*args, **kwargs)
        return middleware

    def setup_app(self, app: web.Application):
        app[APP_KEY] = self[APP_KEY]

    async def middleware(self, request: web.Request, handler: Callable):
        settings = self[APP_KEY]['settings']
        request_history = self[APP_KEY]['request_history']
        exc_history = self[APP_KEY]['exc_history']
        intercept_exc = self[APP_KEY]['settings']['intercept_exc']

        if not settings['enabled']:
            return await handler(request)

        history = settings.get('panels', []) + settings.get('extra_panels', [])
        panel_classes = history
        global_panel_classes = settings.get('global_panels', [])
        hosts = settings.get('hosts', [])

        show_on_exc_only = settings.get('show_on_exc_only')
        intercept_redirects = settings['intercept_redirects']

        root_url = self.router[ROOT_ROUTE_NAME].url_for().raw_path
        exclude_prefixes = settings.get('exclude_prefixes')
        exclude = [root_url] + exclude_prefixes

        p = request.raw_path
        starts_with_excluded = list(filter(None, map(p.startswith, exclude)))

        peername = request.transport.get_extra_info('peername')
        remote_host, remote_port = peername[:2]

        last_proxy_addr = remote_host

        # TODO: rethink access policy by host
        if settings.get('check_host'):
            if starts_with_excluded or not addr_in(last_proxy_addr, hosts):
                return await handler(request)

        toolbar = DebugToolbar(request, panel_classes, global_panel_classes)
        _handler = handler

        context_switcher = ContextSwitcher()
        for panel in toolbar.panels:
            _handler = panel.wrap_handler(_handler, context_switcher)

        try:
            response = await context_switcher(_handler(request))
        except web.HTTPRedirection as exc:
            if not intercept_redirects:
                raise
            # Intercept http redirect codes and display an html page with a
            # link to the target.
            if not getattr(exc, 'location', None):
                raise
            context = {'redirect_to': exc.location,
                       'redirect_code': exc.status}
            response = web.Response(text=render('redirect.jinja2', self.env, context, app_key=TEMPLATE_KEY),
                                    reason=exc.reason,
                                    headers=exc.headers)
        except web.HTTPException:
            raise
        except Exception as e:
            if intercept_exc:
                tb = get_traceback(info=sys.exc_info(),
                                   skip=1,
                                   show_hidden_frames=False,
                                   ignore_system_exceptions=True,
                                   exc=e, app=self.env)
                for frame in tb.frames:
                    exc_history.frames[frame.id] = frame
                exc_history.tracebacks[tb.id] = tb
                request['pdbt_tb'] = tb

                # TODO: find out how to port following to aiohttp
                # or just remove it
                # token = request.app[APP_KEY]['pdtb_token']
                # qs = {'token': token, 'tb': str(tb.id)}
                # msg = 'Exception at %s\ntraceback url: %s'
                #
                # exc_url = request.app.router['debugtoolbar.exception']\
                #     .url(query=qs)
                # assert exc_url, msg
                # exc_msg = msg % (request.path, exc_url)
                # logger.exception(exc_msg)

                # subenviron = request.environ.copy()
                # del subenviron['PATH_INFO']
                # del subenviron['QUERY_STRING']
                # subrequest = type(request).blank(exc_url, subenviron)
                # subrequest.script_name = request.script_name
                # subrequest.path_info = \
                #     subrequest.path_info[len(request.script_name):]
                #
                # response = request.invoke_subrequest(subrequest)
                body = tb.render_full(request).encode('utf-8', 'replace')
                response = web.Response(
                    body=body, status=500,
                    content_type='text/html')

                await toolbar.process_response(request, response)

                request['id'] = str((id(request)))
                toolbar.status = response.status

                request_history.put(request['id'], toolbar)
                toolbar.inject(request, response)
                return response
            else:
                # logger.exception('Exception at %s' % request.path)
                raise e

        toolbar.status = response.status
        if intercept_redirects:
            # Intercept http redirect codes and display an html page with a
            # link to the target.
            if response.status in REDIRECT_CODES and getattr(response, 'location', None):

                context = {'redirect_to': response.location,
                           'redirect_code': response.status}

                _response = aiohttp_jinja2.render_template(
                    'redirect.jinja2', request, context,
                    app_key=TEMPLATE_KEY)
                response = _response

        await toolbar.process_response(request, response)
        request['id'] = hexlify(id(request))

        # Don't store the favicon.ico request
        # it's requested by the browser automatically
        if not '/favicon.ico' == request.path:
            request_history.put(request['id'], toolbar)

        if not show_on_exc_only and response.content_type in HTML_TYPES:
            toolbar.inject(request, response)

        return response


def setup(app: web.Application, **kw):
    config = {}
    config.update(default_settings)
    config.update(kw)

    debug_app = DebugToolbarApp(config=config)
    debug_app.setup_app(app)

    path_prefix = config['path_prefix']
    app.add_subapp(path_prefix, debug_app)
    middleware = debug_app.get_middleware()
    if middleware not in app.middlewares:
        app.middlewares.append(middleware)

    return debug_app
