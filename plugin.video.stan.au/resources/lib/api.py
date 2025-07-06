import time

from six.moves.urllib_parse import urlencode

from slyguy import userdata, keep_alive, log
from slyguy.session import Session
from slyguy.exceptions import Error
from slyguy.util import cenc_init

from .constants import *
from .settings import settings
from .language import _


class APIError(Error):
    pass


class API(object):
    def new_session(self):
        self.logged_in = False
        self._session  = Session(HEADERS, base_url=API_URL)
        self._set_authentication()
        keep_alive.register(self._refresh_token, hours=12, enable=self.logged_in)

    def _set_authentication(self):
        self.logged_in = userdata.get('token') != None

    def nav_items(self, key):
        data = self.page('sitemap')

        for row in data['navs']['browse']:
            if row['path'] == '/'+key:
                return row['items']

        return []

    def page(self, key, page=1):
        return self.url('/pages/v6/{}.json'.format(key), page=page)

    def url(self, url, page=1):
        self._refresh_token()

        params = {
            'feedTypes': 'posters,landscapes,hero',
            'jwToken': userdata.get('token'),
        }
        if page > 1:
            params['offset'] = 30 * (page-1)

        return self._session.get(url, params=params).json()

    def search(self, query, page=1, limit=50):
        self._refresh_token()

        params = {
            'q': query,
            'limit': limit,
            'offset': (page-1)*limit,
            'jwToken': userdata.get('token'),
        }

        if userdata.get('profile_kids', False):
            url = '/search/v12/kids/search'
        else:
            url = '/search/v12/search'

        return self._session.get(url, params=params).json()

    def _refresh_token(self, force=False):
        if not self.logged_in or (not force and userdata.get('expires', 0) > time.time()):
            return

        log.debug('Refreshing token')

        payload = {
            'jwToken': userdata.get('token'),
        }
        self._oauth(payload)

    def set_profile(self, profile_id):
        self._refresh_token()

        payload = {
            'jwToken': userdata.get('token'),
            'profileId': profile_id,
        }
        self._oauth(payload)

    # def login(self, username, password):
    #     self.logout()

    #     payload = {
    #         'email': username,
    #         'password': password,
    #         'rnd': '',
    #         'sign': '',
    #     }
    #     self._oauth(payload)

    def device_code(self):
        self.logout()
        params = {'generate': 'true'}
        data = self._session.post('/login/v1/activation-codes/', params=params).json()
        return data['code'], data['url']

    def device_login(self, url):
        resp = self._session.get(url)
        if resp.status_code != 200:
            return False
        self._oauth(resp.json())
        return True

    def _device_data(self):
        enable_h265 = settings.H265.value

        return {
            'type': 'console', #console, tv
          #  'clientId': '',
          #  'deviceID': '',
            'screenSize': '3840x2160',
            'stanName': STAN_NAME,
            'stanVersion': '4.32.1',
            'manufacturer': 'NVIDIA', #NVIDIA, Sony
            'model': 'SHIELD Android TV' if enable_h265 else '', #SHIELD Android TV, BRAVIA 4K 2020
            'os': 'Android-9',
            'videoCodecs': 'h264,decode,dovi,h263,h265,hevc,mjpeg,mpeg2v,mp4,mpeg4,vc1,vp8,vp9',
            'audioCodecs': 'aac',
            'drm': 'widevine', #playready
            'hdcpVersion': '2.2', #0, 1, 2, 2.2
            'colorSpace': 'hdr10',
            #'tz': '',
        }

    def _oauth(self, payload):
        headers = {
            'Accept-Encoding': 'gzip',
            'Accept': None,
            'Connection': None,
        }
        payload.update(self._device_data())
        data = self._session.post('/login/v1/sessions/app', data=payload, headers=headers).json()

        if 'errors' in data:
            try:
                msg = data['errors'][0]['code']
                if msg == 'Streamco.Login.VPNDetected':
                    msg = _.GEO_ERROR
            except:
                msg = ''

            raise APIError(_(_.LOGIN_ERROR, msg=msg))

        userdata.set('token', data['jwToken'])
        userdata.set('expires', int(time.time() + (data['renew'] - data['now']) - 30))
        userdata.set('user_id', data['userId'])
        userdata.set('profile_id', data['profile']['id'])
        userdata.set('profile_name', data['profile']['name'])
        userdata.set('profile_icon', data['profile']['iconImage']['url'])
        userdata.set('profile_kids', int(data['profile'].get('isKidsProfile', False)))
        self._set_authentication()

    def watchlist(self):
        self._refresh_token()

        params = {
            'jwToken': userdata.get('token'),
        }

        url = '/watchlist/v1/users/{user_id}/profiles/{profile_id}/watchlistitems'.format(user_id=userdata.get('user_id'), profile_id=userdata.get('profile_id'))
        return self._session.get(url, params=params).json()

    def history(self, program_ids=None):
        self._refresh_token()

        params = {
            'jwToken': userdata.get('token'),
            'limit': 100,
        }

        if program_ids:
            params['programIds'] = program_ids

        url = '/history/v1/users/{user_id}/profiles/{profile_id}/history'.format(user_id=userdata.get('user_id'), profile_id=userdata.get('profile_id'))
        return self._session.get(url, params=params).json()

    def profiles(self):
        self._refresh_token()

        params = {
            'jwToken': userdata.get('token'),
        }

        return self._session.get('/accounts/v1/users/{user_id}/profiles'.format(user_id=userdata.get('user_id')), params=params).json()

    def add_profile(self, name, icon_set, icon_index, kids=False):
        self._refresh_token()

        payload = {
            'jwToken': userdata.get('token'),
            'name': name,
            'isKidsProfile': kids,
            'iconSet': icon_set,
            'iconIndex': icon_index,
        }

        return self._session.post('/accounts/v1/users/{user_id}/profiles'.format(user_id=userdata.get('user_id')), data=payload).json()

    def delete_profile(self, profile_id):
        self._refresh_token()

        params = {
            'jwToken': userdata.get('token'),
            'profileId': profile_id,
        }

        return self._session.delete('/accounts/v1/users/{user_id}/profiles'.format(user_id=userdata.get('user_id')), params=params).ok

    def profile_icons(self):
        self._refresh_token()

        params = {
            'jwToken': userdata.get('token'),
        }

        return self._session.get('/accounts/v1/accounts/icons', params=params).json()

    def program(self, program_id):
        self._refresh_token()

        params = {
            'jwToken': userdata.get('token'),
        }

        return self._session.get('/programs/v1/programs/{program_id}'.format(program_id=program_id), params=params).json()

    def play(self, program_id):
        self._refresh_token()

        program_data = self.program(program_id)
        if 'errors' in program_data:
            try:
                msg = program_data['errors'][0]['code']
                if msg == 'Streamco.Concurrency.OutOfRegion':
                    msg = _.GEO_ERROR
                elif msg == 'Streamco.Catalogue.NOT_SAFE_FOR_KIDS':
                    msg = _.KIDS_PLAY_DENIED
            except:
                msg = ''

            raise APIError(_(_.PLAYBACK_ERROR, msg=msg))

        jw_token = userdata.get('token')

        params = {
            'jwToken': jw_token,
            'programId': program_id,
            'stanName': STAN_NAME,
            'quality': 'ultra', #auto, ultra, high, low
            'format': 'dash',
        }

        data = self._session.post('/concurrency/v1/streams', params=params).json()

        if 'errors' in data:
            try:
                msg = data['errors'][0]['code']
                if msg == 'Streamco.Concurrency.OutOfRegion':
                    msg = _.GEO_ERROR
            except:
                msg = ''

            raise APIError(_(_.PLAYBACK_ERROR, msg=msg))

        play_data = data['media']
        play_data['drm']['init_data'] = self._init_data(play_data['drm']['keyId'])

        params = {
            'url': play_data['videoUrl'],
            'audioType': 'all',
            # 'audioCodecs': 'aac',
            # 'screenSize': '1080x1920',
            # 'hdcpVersion': '0',
            # 'colorSpace': '',
            # 'minHeight': '520',
            # 'drm': 'widevine',
            # 'type': 'console',
            # 'model': 'SHIELD Android TV',
            # 'manufacturer': 'NVIDIA',
            # 'osVersion': '28',
        }

        play_data['videoUrl'] = API_URL.format('/manifest/v1/dash/androidtv.mpd?{}'.format(urlencode(params)))

        params = {
            'form': 'json',
            'schema': '1.0',
            'jwToken': jw_token,
            '_id': data['concurrency']['lockID'],
            '_sequenceToken': data['concurrency']['lockSequenceToken'],
            '_encryptedLock': 'STAN',
        }

        self._session.get('/concurrency/v1/unlock', params=params).json()

        return program_data, play_data

    def _init_data(self, key):
        key = key.replace('-', '')
        key_len = '{:x}'.format(len(bytearray.fromhex(key)))
        data = '12{}{}'.format(key_len, key)
        data = bytearray.fromhex(data)
        # kids needed to support IA in Kodi 21 that does not parse kid from data
        return cenc_init(data, kids=[bytearray.fromhex(key)])

    def logout(self):
        userdata.delete('token')
        userdata.delete('expires')
        userdata.delete('user_id')

        userdata.delete('profile_id')
        userdata.delete('profile_icon')
        userdata.delete('profile_name')
        userdata.delete('profile_kids')

        self.new_session()

    # def resume_series(self, series_id):
    #     params = {
    #         'jwToken': userdata.get('token'),
    #     }

    #     url = '/resume/v1/users/{user_id}/profiles/{profile_id}/resumeSeries/{series_id}'.format(user_id=userdata.get('user_id'), profile_id=userdata.get('profile_id'), series_id=series_id)
    #     return self._session.get(url, params=params).json()

    # def resume_program(self, program_id):
    #     params = {
    #         'jwToken': userdata.get('token'),
    #     }

    #     url = '/resume/v1/users/{user_id}/profiles/{profile_id}/resume/{program_id}'.format(user_id=userdata.get('user_id'), profile_id=userdata.get('profile_id'), program_id=program_id)
    #     return self._session.get(url, params=params).json()
