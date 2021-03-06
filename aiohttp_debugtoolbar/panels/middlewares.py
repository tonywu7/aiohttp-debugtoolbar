from ..utils import APP_KEY, STATIC_ROUTE_NAME
from .base import DebugPanel

__all__ = ['MiddlewaresDebugPanel']


class MiddlewaresDebugPanel(DebugPanel):
    """
    A panel to display the middlewares used by your aiohttp application.
    """
    name = 'Middlewares'
    has_content = True
    template = 'middlewares.jinja2'
    title = 'Middlewares'
    nav_title = title

    def __init__(self, request):
        super().__init__(request)
        if not request.app.middlewares:
            self.has_content = False
            self.is_active = False
        else:
            self.populate(request)

    def populate(self, request):
        middleware_names = []
        for m in request.app.middlewares:
            if hasattr(m, '__name__'):
                # name for regular functions
                middleware_names.append(m.__name__)
            else:
                middleware_names.append(m.__repr__())
        self.data = {'middlewares': middleware_names}

    def render_vars(self, request=None):
        request = request or self._request
        static_path = request.config_dict[APP_KEY]['router'][STATIC_ROUTE_NAME].url_for(filename='')
        return {'static_path': static_path}
