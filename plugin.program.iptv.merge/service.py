from resources.lib.settings import settings

if settings.HTTP_METHOD.value:
    from resources.lib.http import serve_forever
    serve_forever()
else:
    settings.HTTP_URL.clear()
    from resources.lib.service import run_forever
    run_forever()
