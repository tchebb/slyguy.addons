import os
from difflib import SequenceMatcher

from kodi_six import xbmcvfs
from six.moves.urllib_parse import urlparse

from slyguy import plugin, gui
from slyguy.constants import ROUTE_CONTEXT, ROUTE_SETTINGS, KODI_VERSION, ADDON_ID
from slyguy.log import log
from slyguy.util import get_addon, kodi_rpc, remove_kodi_formatting

from .settings import settings
from .youtube import play_youtube, get_youtube_id
from .mdblist import API
from .imdb import play_imdb
from .language import _
from .constants import SEARCH_MATCH_RATIO

mdblist_api = API()


@plugin.route('/')
def home(**kwargs):
    return plugin.url_for(ROUTE_SETTINGS)


def _get_trailer_path(path):
    if not path:
        return ''

    video_id = get_youtube_id(path)
    if video_id:
        return plugin.url_for(play_yt, video_id=video_id)
    else:
        return path


def _li_to_item(li):
    vid_tag = li.getVideoInfoTag()

    clean_title = remove_kodi_formatting(vid_tag.getTitle() or li.getLabel())
    # (Trailer) stops trakt scrobbling (workaround)
    title = u"{} ({})".format(clean_title, _.TRAILER)

    item = plugin.Item()
    item.label = title
    item.info = {
        'title': title,
        'plot': vid_tag.getPlot(),
        'tagline': vid_tag.getTagLine(),
        'trailer': vid_tag.getTrailer(),
        'year': vid_tag.getYear(),
        'mediatype': vid_tag.getMediaType(),
        'dir': None,
        'filename': None,
        'clean_title': clean_title,
        'unique_id': {},
    }

    if item.info['mediatype'] == 'movie':
        path = vid_tag.getFilenameAndPath()
        item.info['dir'] = os.path.dirname(path)
        item.info['filename'] = os.path.basename(path)
    elif item.info['mediatype'] == 'tvshow':
        item.info['dir'] = os.path.dirname(vid_tag.getPath())

    for key in ['thumb','poster','banner','fanart','clearart','clearlogo','landscape','icon']:
        item.art[key] = li.getArt(key)

    if KODI_VERSION >= 20:
        item.info['genre'] = vid_tag.getGenres()
        for id_type in ('imdb', 'tvdb', 'tmdb'):
            unique_id = vid_tag.getUniqueID(id_type)
            if unique_id:
                item.info['unique_id'] = {'type': id_type, 'id': unique_id}
                break
    else:
        item.info['genre'] = vid_tag.getGenre()
        id = vid_tag.getIMDBNumber() or ''
        id_type = 'imdb' if id.lower().startswith('tt') else None
        item.info['unique_id'] = {'type': id_type, 'id': id}

    return item


def _rpc_to_item(data):
    clean_title = remove_kodi_formatting(data.get('title') or data.get('label'))
    title = u"{} ({})".format(clean_title, _.TRAILER)

    item = plugin.Item()
    item.label = title
    item.info = {
        'title': title,
        'trailer': data['trailer'],
        'year': data['year'],
        'mediatype': 'movie' if 'movieid' in data else 'tvshow',
        'dir': None,
        'filename': None,
        'clean_title': clean_title,
        'unique_id': {},
    }

    if item.info['mediatype'] == 'movie':
        path = data['file']
        item.info['dir'] = os.path.dirname(path)
        item.info['filename'] = os.path.basename(path)
    elif item.info['mediatype'] == 'tvshow':
        item.info['dir'] = os.path.dirname(data['file'])

    if KODI_VERSION >= 20:
        for id_type in ('imdb', 'tvdb', 'tmdb'):
            unique_id = data.get('uniqueid', {}).get(id_type)
            if unique_id:
                item.info['unique_id'] = {'type': id_type, 'id': unique_id}
                break
    else:
        id = data.get('imdbnumber') or ''
        id_type = 'imdb' if id.lower().startswith('tt') else None
        item.info['unique_id'] = {'type': id_type, 'id': id}

    return item


@plugin.route('/redirect')
def redirect(url, **kwargs):
    parsed = urlparse(url)
    if parsed.path.lower() in ('/search', '/kodion/search/query'):
        log.warning("SlyGuy Trailers does not support Youtube search ({}). Returning empty result".format(url))
        return plugin.Folder(no_items_label=None, show_news=False)

    matches = _find_content_from_trailer(url)
    # TODO: how to handle multiple items with same trailer url?
    if len(matches) != 1:
        video_id = get_youtube_id(url)
        return plugin.url_for(play_yt, video_id=video_id)

    return _process_item(matches[0])


@plugin.route(ROUTE_CONTEXT)
def context_trailer(listitem, **kwargs):
    item = _li_to_item(listitem)
    return _process_item(item)


def _require_addon(url):
    parsed = urlparse(url)
    if parsed.scheme.lower() == 'plugin' and parsed.netloc != ADDON_ID:
        get_addon(parsed.netloc, install=True, required=True)


def _process_item(item):
    # check local trailer first
    if settings.TRAILER_LOCAL.value:
        item.path = _get_local_trailer(
            mediatype = item.info['mediatype'],
            dir = item.info['dir'],
            filename = item.info['filename'],
        )
        if item.path:
            _require_addon(item.path)
            return item

    # scraped trailer
    if not settings.IGNORE_SCRAPED.value:
        item.path = _get_trailer_path(path=item.info['trailer'])
        if item.path:
            _require_addon(item.path)
            return item

    # if no unique id, try mdblist search by title/year
    if not item.info['unique_id'].get('id') and settings.MDBLIST_SEARCH.value:
        item.info['unique_id'] = _search_mdblist_for_id(
            mediatype = item.info['mediatype'],
            title = item.info['clean_title'],
            year = item.info['year'],
        ) or {}

    # IMDB trailer
    if settings.TRAILER_IMDB.value:
        item.path = _get_imdb_trailer(
            mediatype = item.info['mediatype'],
            id = item.info['unique_id'].get('id'),
            id_type = item.info['unique_id'].get('type'),
        )
        if item.path:
            return item

    # mdblist YouTube trailer
    if settings.MDBLIST.value:
        item.path = _get_mdblist_trailer(
            mediatype = item.info['mediatype'],
            id = item.info['unique_id'].get('id'),
            id_type = item.info['unique_id'].get('type'),
        )
        if item.path:
            return item

    gui.notification(_.TRAILER_NOT_FOUND)


def _find_content_from_trailer(trailer):
    trailer = trailer.lower()
    if not trailer:
        return []

    results = []
    rows = kodi_rpc('VideoLibrary.GetMovies', {'filter': {'field': 'hastrailer', 'operator': 'true', 'value': '1'}, 'properties': ['trailer']})['movies']
    for row in rows:
        if trailer in row["trailer"].lower():
            results.append(kodi_rpc('VideoLibrary.GetMovieDetails', {'movieid': row['movieid'], 'properties': ['title', 'year', 'imdbnumber', 'uniqueid', 'file', 'trailer']})['moviedetails'])

    if not results and KODI_VERSION >= 22:
        # Kodi 22 supports show trailer filter: https://github.com/xbmc/xbmc/pull/26719
        rows = kodi_rpc('VideoLibrary.GetTvShows', {'filter': {'field': 'hastrailer', 'operator': 'true', 'value': '1'}, 'properties': ['trailer']})['tvshows']
        for row in rows:
            if trailer in row["trailer"].lower():
                results.append(kodi_rpc('VideoLibrary.GetTvShowDetails', {'tvshowid': row['tvshowid'], 'properties': ['title', 'year', 'imdbnumber', 'uniqueid', 'file', 'trailer']})['tvshowdetails'])

    return [_rpc_to_item(result) for result in results]


def _get_local_trailer(mediatype, dir=None, filename=None):
    if mediatype == 'movie' and filename:
        filename = os.path.splitext(filename)[0].lower()
        files = xbmcvfs.listdir(dir)[1]
        for file in files:
            name, ext = os.path.splitext(file.lower())
            if name in ('movie-trailer', "{}-trailer".format(filename)):
                path = os.path.join(dir, file)
                if ext == '.txt':
                    with xbmcvfs.File(path) as f:
                        path = _get_trailer_path(f.read().strip())
                return path

    elif mediatype == 'tvshow' and dir:
        folder_name = os.path.basename(dir).lower()
        files = xbmcvfs.listdir(dir)[1]
        for file in files:
            name, ext = os.path.splitext(file.lower())
            if name in ('tvshow-trailer', "{}-trailer".format(folder_name)):
                path = os.path.join(dir, file)
                if ext == '.txt':
                    with xbmcvfs.File(path) as f:
                        path = _get_trailer_path(f.read().strip())
                return path


def _get_imdb_trailer(mediatype, id, id_type=None):
    if not mediatype or not id or (mediatype == 'tvshow' and not settings.TRAILER_IMDB_TV.value):
        return

    if id_type != 'imdb':
        try:
            imdb_id = mdblist_api.get_media(mediatype, id, id_type)['ids']['imdb']
        except KeyError:
            return
    else:
        imdb_id = id

    return plugin.url_for(imdb, video_id=imdb_id)


def _search_mdblist_for_id(mediatype, title, year):
    if not mediatype or not title or not year:
        return

    log.debug("mdblist search for: {} '{}' ({})".format(mediatype, title, year))
    results = mdblist_api.search_media(mediatype, title, year, limit=10)
    title = "{} {}".format(title.lower().strip().replace(' ', ''), year)
    for result in results:
        result['ratio'] = SequenceMatcher(None, title, "{} {}".format(result['title'].lower().strip().replace(' ', ''), result['year'])).ratio()
    results = sorted(results, key=lambda x: x['ratio'], reverse=True)
    log.debug("mdblist search results: {}".format(results))

    results = [x for x in results if x['ratio'] >= SEARCH_MATCH_RATIO]
    if not results:
        return

    log.info("mdblist search result: {}".format(results[0]))
    if not results[0].get('ids'):
        return

    for id_type in ('imdb', 'tvdb', 'tmdb'):
        id = results[0]['ids'].get(id_type) or results[0]['ids'].get(id_type+'id')
        if id:
            return {'type': id_type, 'id': id}


def _get_mdblist_trailer(mediatype, id, id_type=None):
    if not mediatype or not id:
        return

    data = mdblist_api.get_media(mediatype, id, id_type=id_type)
    trailer = _get_trailer_path(data.get('trailer'))
    if ADDON_ID in trailer:
        log.info("mdblist trailer: {}".format(trailer))
        return trailer


@plugin.route('/by_unique_id')
def by_unique_id(mediatype, id, id_type=None, **kwargs):
    item = plugin.Item()
    item.label = ''
    item.info = {
        'title': '',
        'trailer': '',
        'year': '',
        'mediatype': mediatype,
        'dir': None,
        'filename': None,
        'clean_title': '',
        'unique_id': {'type': id_type, 'id': id},
    }
    return _process_item(item)


@plugin.route('/by_title_year')
def by_title_year(mediatype, title, year, **kwargs):
    item = plugin.Item()
    item.label = title
    item.info = {
        'title': title,
        'trailer': '',
        'year': year,
        'mediatype': mediatype,
        'dir': None,
        'filename': None,
        'clean_title': title,
        'unique_id': {},
    }
    return _process_item(item)


@plugin.route('/play')
def play_yt(video_id, **kwargs):
    with gui.busy():
        return play_youtube(video_id)


@plugin.route('/imdb')
def imdb(video_id, **kwargs):
    with gui.busy():
        return play_imdb(video_id)


@plugin.route('/test_streams')
def test_streams(**kwargs):
    STREAMS = [
        ['YouTube 4K', plugin.url_for(play_yt, video_id='Q82tQJyJwgk')],
        ['YouTube 4K HDR', plugin.url_for(play_yt, video_id='tO01J-M3g0U')],
        ['IMDB', plugin.url_for(imdb, video_id='tt10548174')],
        ['Movie imdb id -> mdblist', plugin.url_for(by_unique_id, mediatype='movie', id='tt0133093', id_type='imdb')],
        ['Movie Title / Year -> mdblist', plugin.url_for(by_title_year, mediatype='movie', title='The Matrix', year='1999')],
        ['Show tvdb id -> mdblist', plugin.url_for(by_unique_id, mediatype='tvshow', id='392256', id_type='tvdb')],
        ['Show Title / Year -> mdblist', plugin.url_for(by_title_year, mediatype='tvshow', title='The Last of Us', year='2023')],
    ]

    folder = plugin.Folder(_.TEST_STREAMS, content=None)
    for stream in STREAMS:
        folder.add_item(label=stream[0], is_folder=False, path=stream[1])
    return folder
