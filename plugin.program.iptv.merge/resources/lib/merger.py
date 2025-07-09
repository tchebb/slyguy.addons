import os
import re
import shutil
import time
import gzip
import codecs
from looseversion import LooseVersion

import arrow
from kodi_six import xbmc, xbmcvfs, xbmcaddon
from six.moves.urllib.parse import unquote_plus, quote_plus

from slyguy import database, gui, userdata, monitor
from slyguy.log import log
from slyguy.util import remove_file, hash_6, FileIO, gzip_extract, xz_extract, run_plugin, safe_copy, unique, kodi_rpc
from slyguy.session import Session, gdrivedl
from slyguy.constants import ADDON_PROFILE, CHUNK_SIZE
from slyguy.exceptions import Error

from .constants import *
from .models import Source, Playlist, EPG, Channel, merge_info, parse_attribs, strip_quotes
from .language import _
from .settings import settings
from . import iptv_manager


class AddonError(Error):
    pass


def epg_file_name():
    if settings.GZ_EPG.value:
        return EPG_FILE_NAME + '.gz'
    else:
        return EPG_FILE_NAME


def copy_partial_data(file_path, _out, start_index, end_index):
    if start_index < 1 or end_index < start_index:
        return

    try:
        with FileIO(file_path, 'rb', CHUNK_SIZE) as _in:
            _seek_file(_in, start_index, truncate=False)

            while True:
                size = min(CHUNK_SIZE, end_index - _in.tell())
                chunk = _in.read(size)
                if not chunk:
                    break

                _out.write(chunk)

            return _in.tell() == end_index
    except:
        return


def _seek_file(f, index, truncate=True):
    cur_index = f.tell()
    if cur_index != index:
        log.debug('{} seeking from {} to {}'.format(f.name, cur_index, index))
        f.seek(index, os.SEEK_SET)
        if truncate:
            f.truncate()


class XMLParser(object):
    def __init__(self, out, epg_ids=None):
        self._out = out
        if epg_ids is None:
            self._epg_ids = set()
            self._check_orphans = False
        else:
            self._epg_ids = set(epg_ids)
            self._check_orphans = True

        self._tag_pattern = re.compile(br'<(channel|programme)(\s[^>]*)?(\/?)>')
        self._attr_pattern = re.compile(br'(\w+)\s*=\s*"([^"]*)"')
        self._counts = {
            'channel': {'added': 0, 'skipped': 0},
            'programme': {'added': 0, 'skipped': 0},
            'tags_ignored': 0,
        }

    def epg_count(self):
        if self._check_orphans:
            return 'Added {added} / Skipped {skipped}'.format(**self._counts['programme'])
        else:
            return 'Added {added}'.format(**self._counts['programme'])

    def _parse_attributes(self, attr_text):
        return {
            match.group(1).decode('utf-8'): match.group(2).decode('utf-8')
            for match in self._attr_pattern.finditer(attr_text)
        }

    def parse(self, _in, epg):
        epg.start_index = self._out.tell()

        buffer = b""
        while True:
            chunk = _in.read(CHUNK_SIZE)
            if not chunk:
                break
            buffer += chunk

            while True:
                match = self._tag_pattern.search(buffer)
                if not match:
                    break  # No start tag found yet

                tag = match.group(1).decode('utf-8')
                attr_text = match.group(2) or b''
                is_self_closing = bool(match.group(3))
                start_tag_pos = match.start()
                start_tag_end = match.end()

                if is_self_closing:
                    # Self-closing tag; no inner content
                    raw_element = buffer[start_tag_pos:start_tag_end]
                    buffer = buffer[start_tag_end:]
                else:
                    # Look for end tag
                    end_tag = "</{}>".format(tag).encode('utf-8')
                    end_tag_pos = buffer.find(end_tag, start_tag_end)
                    if end_tag_pos == -1:
                        break  # End tag not found yet; need more data
                    end_tag_end = end_tag_pos + len(end_tag)
                    raw_element = buffer[start_tag_pos:end_tag_end]
                    buffer = buffer[end_tag_end:]

                if tag not in ('channel', 'programme'):
                    self._counts['tags_ignored'] += 1
                    continue

                if self._check_orphans:
                    attrs = self._parse_attributes(attr_text)
                    id = attrs.get('id') if tag == 'channel' else attrs.get('channel')
                    if id and id not in self._epg_ids:
                        self._counts[tag]['skipped'] += 1
                        continue

                self._counts[tag]['added'] += 1
                self._out.write(raw_element)

        self._out.flush()
        epg.end_index = self._out.tell()


def check_merge_required():
    # dont allow auto merge more than every 5mins
    if time.time() - userdata.get('last_run', 0) < 300:
        return False

    output_dir = settings.get('output_dir', '').strip() or ADDON_PROFILE
    playlist_path = os.path.join(output_dir, PLAYLIST_FILE_NAME)
    epg_path = os.path.join(output_dir, epg_file_name())

    reload_time_hours = settings.getBool('auto_merge', True)
    if reload_time_hours:
        reload_time_hours = time.time() - userdata.get('last_run', 0) > settings.getInt('reload_time_hours', 12) * 3600

    merge_at_hour = not reload_time_hours and settings.getBool('merge_at_hour', True)
    if merge_at_hour:
        now = arrow.now()
        run_ts = now.replace(hour=int(settings.getInt('merge_hour', 3)), minute=0, second=0, microsecond=0).timestamp
        merge_at_hour = userdata.get('last_run', 0) < run_ts and now.timestamp >= run_ts

    if reload_time_hours or merge_at_hour or not xbmcvfs.exists(playlist_path) or not xbmcvfs.exists(epg_path):
        userdata.set('last_run', int(time.time()))
        return True
    else:
        return False


class Merger(object):
    def __init__(self, output_path=None, forced=False):
        self.working_path = ADDON_PROFILE
        self.output_path = output_path or xbmc.translatePath(settings.get('output_dir', '').strip() or self.working_path)
        self.temp_path = os.path.join(self.working_path, 'tmp')
        self.tmp_file = os.path.join(self.temp_path, 'iptv_merge_tmp')

        if not xbmcvfs.exists(self.working_path):
            xbmcvfs.mkdirs(self.working_path)

        if not xbmcvfs.exists(self.output_path):
            xbmcvfs.mkdirs(self.output_path)

        if not xbmcvfs.exists(self.temp_path):
            xbmcvfs.mkdirs(self.temp_path)

        self.forced = forced
        self._playlist_epgs = []
        self._extgroups = []

    def _call_addon_method(self, plugin_url, file_path):
        quoted_file_path = quote_plus(file_path)
        plugin_url = plugin_url.replace('$FILE', quoted_file_path).replace('%24FILE', quoted_file_path)
        dirs, files = run_plugin(plugin_url, wait=True)

        try:
            result, msg = int(files[0][0]), unquote_plus(files[0][1:])
        except:
            return

        if not result:
            raise AddonError(msg)

        if xbmcvfs.exists(file_path):
            return file_path
        else:
            return msg

    def _process_source(self, source, method_name, file_path):
        remove_file(file_path)

        path = source.path.strip()
        source_type = source.source_type
        archive_type = source.archive_type

        if source_type != Source.TYPE_ADDON:
            self._process_path(path, archive_type, file_path)
            return

        addon_id = path
        addon, data = merge_info(addon_id, merging=True)

        if method_name not in data:
            if method_name == 'epg':
                raise Error('EPG is now provided by the Playlist. You can remove this EPG source')
            else:
                raise Error('{} could not be found for {}'.format(method_name, addon_id))

        paths = data[method_name]

        if data['type'] == TYPE_IPTV_MANAGER:
            iptv_manager.process_path(paths, file_path)
            return

        if type(paths) is not list:
            paths = [paths]

        for path in paths:
            path = path.replace('$ID', addon_id).replace('%24ID', addon_id)
            path = path.replace('$IP', xbmc.getIPAddress()).replace('%24IP', xbmc.getIPAddress())
            self._process_path(path.strip(), archive_type, file_path)

    def _process_path(self, path, archive_type, file_path):
        if path.lower().startswith('plugin://'):
            path = self._call_addon_method(path, file_path)
            if not path:
                return

        if path.lower().startswith('http://') or path.lower().startswith('https://'):
            if 'drive.google.com' in path.lower():
                log.debug('Gdrive Downloading: {} > {}'.format(path, file_path))
                path = gdrivedl(path, file_path)
            else:
                log.debug('Downloading: {} > {}'.format(path, file_path))
                resp = Session().chunked_dl(path, file_path)
                path = resp.url

        elif not xbmcvfs.exists(path):
            raise Error(_(_.LOCAL_PATH_MISSING, path=path))
        else:
            safe_copy(path, file_path)

        if archive_type == Source.ARCHIVE_AUTO:
            try:
                with open(file_path, 'rb') as f:
                    data = f.read(6)
                    if data == b'\xfd\x37\x7a\x58\x5a\00':
                        archive_type = Source.ARCHIVE_XZ
                        log.debug('Detected XZ archive')
                    elif data[0:2] == b'\x1f\x8b':
                        archive_type = Source.ARCHIVE_GZIP
                        log.debug('Detected gz archive')
            except Exception as e:
                log.debug('Failed to detect file type')
                log.exception(e)

        if archive_type == Source.ARCHIVE_GZIP:
            gzip_extract(file_path)
        elif archive_type == Source.ARCHIVE_XZ:
            xz_extract(file_path)

    def _process_playlist(self, playlist, file_path):
        channel     = None
        to_create   = set()
        slugs       = set()
        added_count = 0

        Channel.delete().where(Channel.playlist == playlist).execute()

        if playlist.use_start_chno:
            chnos = {'tv': playlist.start_chno, 'radio': playlist.start_chno}

        default_attribs = {}
        hide_groups = [x.strip() for x in settings.get('hide_groups', '').split(';') if x.strip()]

        def is_visible(channel):
            if not playlist.default_visible:
                return False

            for group in hide_groups:
                if group in channel.groups:
                    log.debug('Setting channel: {} not visible due to hide group: {}'.format(channel.slug, group))
                    return False

            return True

        with codecs.open(file_path, 'r', encoding='utf8', errors='replace') as infile:
            for line in infile:
                line = line.strip()

                if '#EXTM3U' in line:
                    #if not playlist.ignore_playlist_epg:
                    attribs = parse_attribs(line)[0]

                    if not playlist.get_option('ignore_epgs', False):
                        xml_urls = attribs.get('x-tvg-url', '').split(',')
                        xml_urls.extend(attribs.get('url-tvg', '').split(','))
                        for url in xml_urls:
                            url = url.strip()
                            if url:
                                self._playlist_epgs.append(url)

                    if 'tvg-shift' in attribs:
                        default_attribs['tvg-shift'] = attribs['tvg-shift']
                    if 'catchup-correction' in attribs:
                        default_attribs['catchup-correction'] = attribs['catchup-correction']

                if not channel:
                    channel = Channel()
                    extgroups = []

                if line.startswith('#EXTINF'):
                    channel.load_extinf(line)
                    for key in default_attribs:
                        if key not in channel.attribs:
                            channel.attribs[key] = default_attribs[key]

                elif line.startswith('#EXTGRP'):
                    value = line.split(':',1)[1].strip()
                    if value:
                        extgroups.extend([strip_quotes(x) for x in value.split(';')])

                elif line.startswith('#KODIPROP') or line.startswith('#EXTVLCOPT'):
                    value = line.split(':',1)[1].strip()
                    if value and '=' in value:
                        key, value = value.split('=', 1)
                        channel.properties[key] = value

                elif line.startswith('#EXT-X-PLAYLIST-TYPE'):
                    value = line.split(':',1)[1].strip()
                    if value and value.upper() == 'VOD':
                        channel.is_live = False

                elif not line.startswith('#'):
                    if not line:
                        self._extgroups.extend(extgroups)
                        channel = None
                        continue

                    channel.url = line
                    channel.playlist = playlist
                    channel.groups.extend(extgroups)

                    if playlist.skip_playlist_groups:
                        channel.groups = []

                    if playlist.group_name:
                        channel.groups.extend(playlist.group_name.split(';'))

                    if playlist.skip_playlist_chno:
                        channel.chno = None

                    if playlist.use_start_chno:
                        if channel.radio:
                            if channel.chno is None:
                                channel.chno = chnos['radio']

                            chnos['radio'] = channel.chno + 1
                        else:
                            if channel.chno is None:
                                channel.chno = chnos['tv']

                            chnos['tv'] = channel.chno + 1

                    channel.groups = [x for x in channel.groups if x.strip()]
                    channel.visible = is_visible(channel)

                    channel_id = channel.attribs.get('channel-id') or channel.attribs.get('channelid') or channel.epg_id or channel.url.lower().strip()
                    channel.slug = slug = '{}.{}'.format(playlist.id, hash_6(channel_id))
                    channel.order = added_count + 1

                    count = 1
                    while channel.slug in slugs:
                        channel.slug = '{}.{}'.format(slug, count)
                        count += 1

                    slugs.add(channel.slug)
                    to_create.add(channel)

                    if Channel.bulk_create_lazy(to_create):
                        to_create.clear()

                    channel = None
                    added_count += 1

        Channel.bulk_create_lazy(to_create, force=True)
        to_create.clear()
        slugs.clear()

        return added_count

    def playlists(self, refresh=True, http_url=None):
        playlist_path = os.path.join(self.output_path, PLAYLIST_FILE_NAME)
        working_path = os.path.join(self.working_path, PLAYLIST_FILE_NAME)

        output_dir = settings.get('output_dir', '').strip() or ADDON_PROFILE
        if settings.HTTP_METHOD.value:
            epg_path = (http_url or settings.HTTP_URL.value) + epg_file_name()
        else:
            epg_path = os.path.join(output_dir, epg_file_name()) # keep as special:// path

        if not refresh and xbmcvfs.exists(playlist_path) and xbmcvfs.exists(working_path):
            return working_path

        start_time = time.time()
        database.connect()

        try:
            progress = gui.progressbg() if self.forced else None

            playlists = list(Playlist.select().where(Playlist.enabled == True).order_by(Playlist.order))
            Playlist.update({Playlist.results: []}).where(Playlist.enabled == False).execute()
            Channel.delete().where(Channel.custom == False, Channel.playlist.not_in(playlists)).execute()

            for count, playlist in enumerate(playlists):
                count += 1

                if progress: progress.update(int(count*(100/len(playlists))), 'Merging Playlist ({}/{})'.format(count, len(playlists)), _(playlist.label, _bold=True))

                process_took = 0
                playlist_took = 0

                error = None
                try:
                    log.debug('Processing: {}'.format(playlist.path))

                    if playlist.source_type != Playlist.TYPE_CUSTOM:
                        process_start = time.time()
                        self._process_source(playlist, METHOD_PLAYLIST, self.tmp_file)
                        process_took = time.time() - process_start
                        playlist_start = time.time()
                        with Channel._meta.database.atomic() as transaction:
                            try:
                                added = self._process_playlist(playlist, self.tmp_file)
                            except:
                                transaction.rollback()
                                raise
                        playlist_took = time.time() - playlist_start
                    else:
                        added = len(playlist.channels)
                except AddonError as e:
                    error = e
                except Error as e:
                    error = e
                    log.exception(e)
                except Exception as e:
                    error = e
                    log.exception(e)
                else:
                    playlist.results.insert(0, [int(time.time()), Playlist.OK, '{} Channels ({:.2f}s + {:.2f}s)'.format(added, process_took, playlist_took)])
                    error = None

                if error:
                    result = [int(time.time()), Playlist.ERROR, str(error)]
                    if playlist.results and playlist.results[0][1] == Playlist.ERROR:
                        playlist.results[0] = result
                    else:
                        playlist.results.insert(0, result)

                remove_file(self.tmp_file)

                playlist.results = playlist.results[:3]
                playlist.save()

            count = 0
            starting_ch_no = settings.getInt('start_ch_no', 1)
            groups_disabled = settings.getBool('disable_groups', False)

            with codecs.open(working_path, 'w', encoding='utf8') as outfile:
                outfile.write(u'#EXTM3U x-tvg-url="{}"\n'.format(epg_path))

                groups = []
                group_order = settings.get('group_order')
                if group_order:
                    groups.extend(group_order.split(';'))

                groups.extend(self._extgroups)
                for group in unique([x.strip() for x in groups if x.strip()]):
                    outfile.write(u'\n#EXTGRP:"{}"'.format(group))
                # empty EXTGRP to end directive
                outfile.write(u'\n#EXTGRP:')

                chno = starting_ch_no
                tv_groups = []
                for channel in Channel.playlist_list(radio=False):
                    if channel.chno is None:
                        channel.chno = chno
                    chno = channel.chno + 1

                    if groups_disabled:
                        channel.groups = []
                    else:
                        tv_groups.extend(channel.groups)

                    outfile.write(u'\n\n')
                    outfile.write(channel.get_lines())
                    count += 1

                chno = starting_ch_no
                for channel in Channel.playlist_list(radio=True):
                    if channel.chno is None:
                        channel.chno = chno
                    chno = channel.chno + 1

                    if groups_disabled:
                        channel.groups = []
                    else:
                        new_groups = []
                        for group in channel.groups:
                            count = 1
                            while group in tv_groups:
                                group = _(_.RADIO_GROUP, group=group)
                                if count > 1:
                                    group = u'{} #{}'.format(group, count)
                                count += 1
                            new_groups.append(group)
                        channel.groups = new_groups

                    outfile.write(u'\n\n')
                    outfile.write(channel.get_lines())
                    count += 1

                if count == 0:
                    outfile.write(u'\n\n#EXTINF:-1,EMPTY PLAYLIST\nhttp')

                outfile.write(u'\n')

            log.debug('Wrote {} Channels'.format(count))
            Playlist.after_merge()
            safe_copy(working_path, playlist_path)
        finally:
            database.close()
            if progress: progress.close()
            remove_file(self.tmp_file)

        log.debug('Playlist Merge Time: {0:.2f}'.format(time.time() - start_time))

        return working_path

    def epgs(self, refresh=True):
        epg_path = os.path.join(self.output_path, epg_file_name())
        working_path = os.path.join(self.working_path, epg_file_name())
        epg_path_tmp = os.path.join(self.temp_path, EPG_FILE_NAME)

        if settings.GZ_EPG.value:
            # remove old non-gz if exists
            remove_file(os.path.join(self.output_path, EPG_FILE_NAME))
            remove_file(os.path.join(self.working_path, EPG_FILE_NAME))
        else:
            # remove old gz if exists
            remove_file(os.path.join(self.output_path, EPG_FILE_NAME+'.gz'))
            remove_file(os.path.join(self.working_path, EPG_FILE_NAME+'.gz'))

        if not refresh and xbmcvfs.exists(epg_path) and xbmcvfs.exists(working_path):
            return working_path

        start_time = time.time()
        database.connect()

        try:
            progress = gui.progressbg() if self.forced else None

            epgs = list(EPG.select().where(EPG.enabled == True).order_by(EPG.id))
            EPG.update({EPG.start_index: 0, EPG.end_index: 0, EPG.results: []}).where(EPG.enabled == False).execute()

            if settings.getBool('remove_epg_orphans', True):
                epg_ids = Channel.epg_ids()
            else:
                epg_ids = None

            if self._playlist_epgs:
                epg_urls = [x.path.lower() for x in epgs]
                for url in self._playlist_epgs:
                    if url.lower() not in epg_urls:
                        epg = EPG(source_type=EPG.TYPE_URL, path=url, enabled=1)
                        epgs.append(epg)
                        epg_urls.append(url.lower())

            # gzip cant seek, so must do xml first and then gz after
            with FileIO(epg_path_tmp, "wb") as _out:
                _out.write(b'<?xml version="1.0" encoding="UTF-8"?><tv>')

                for count, epg in enumerate(epgs):
                    count += 1

                    if progress: progress.update(int(count*(100/len(epgs))), 'Merging EPG ({}/{})'.format(count, len(epgs)), _(epg.label, _bold=True))

                    start_index = _out.tell()
                    try:
                        log.debug('Processing: {}'.format(epg.path))
                        process_start = time.time()
                        self._process_source(epg, METHOD_EPG, self.tmp_file)
                        process_took = time.time() - process_start
                        parser_start = time.time()
                        with FileIO(self.tmp_file, 'rb') as _in:
                            parser = XMLParser(_out, epg_ids)
                            parser.parse(_in, epg)
                        parser_took = time.time() - parser_start
                    except Exception as e:
                        log.exception(e)
                        result = [int(time.time()), EPG.ERROR, str(e)]
                    else:
                        result = [int(time.time()), EPG.OK, '{} ({:.2f}s + {:.2f}s)'.format(parser.epg_count(), process_took, parser_took)]
                        epg.results.insert(0, result)

                    if result[1] == EPG.ERROR:
                        _seek_file(_out, start_index)

                        if epg.start_index > 0:
                            if copy_partial_data(working_path, _out, epg.start_index, epg.end_index):
                                log.debug('Last used XML data loaded successfully')
                                epg.start_index = start_index
                                epg.end_index = _out.tell()
                            else:
                                log.debug('Failed to load last XML data')
                                epg.start_index = 0
                                epg.end_index = 0
                                # rewind in case of partial failed copy
                                _seek_file(_out, start_index)

                        if epg.results and epg.results[0][1] == EPG.ERROR:
                            epg.results[0] = result
                        else:
                            epg.results.insert(0, result)

                    epg.results = epg.results[:3]
                    if epg.id:
                        epg.save()
                    remove_file(self.tmp_file)

                _out.write(b'</tv>')

            if settings.GZ_EPG.value:
                dst = epg_path_tmp + '.gz'
                with open(epg_path_tmp, "rb") as f_in:
                    with gzip.open(dst, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                remove_file(epg_path_tmp)
                epg_path_tmp = dst

            remove_file(working_path)
            shutil.move(epg_path_tmp, working_path)
            safe_copy(working_path, epg_path)
        finally:
            database.close()
            if progress: progress.close()
            remove_file(self.tmp_file)
            remove_file(epg_path_tmp)

        log.debug('EPG Merge Time: {0:.2f}'.format(time.time() - start_time))

        return working_path


def restart_pvr(forced=False):
    if not settings.getBool('restart_pvr', False):
        return False

    try:
        addon = xbmcaddon.Addon(IPTV_SIMPLE_ID)
        addon_version = LooseVersion(addon.getAddonInfo('version'))
    except Exception as e:
        return

    if forced:
        progress = gui.progressbg(heading='Reloading IPTV Simple Client')

    if not forced and addon_version >= LooseVersion('20.8.0'):
        log.info('Merge complete. IPTV Simple should reload updated playlist within 5mins')

    elif LooseVersion('4.3.0') <= addon_version < LooseVersion('20.8.0'):
        # IPTV Simple version 4.3.0 added auto reload on settings change
        log.info('Merge complete. IPTV Simple should reload immediately')
        addon.setSetting('m3uPathType', '0')
        if forced:
            progress.update(100)
            progress.close()

    elif forced or (not xbmc.getCondVisibility('Pvr.IsPlayingTv') and not xbmc.getCondVisibility('Pvr.IsPlayingRadio')):
        log.info('Merge complete. Reloading IPTV Simple using legacy enable/disable method')
        kodi_rpc('Addons.SetAddonEnabled', {'addonid': IPTV_SIMPLE_ID, 'enabled': False})

        wait_delay = 4
        for i in range(wait_delay):
            if monitor.waitForAbort(1):
                break
            if forced: progress.update((i+1)*int(100/wait_delay))

        kodi_rpc('Addons.SetAddonEnabled', {'addonid': IPTV_SIMPLE_ID, 'enabled': True})

        if forced:
            progress.update(100)
            progress.close()

    else:
        return True
