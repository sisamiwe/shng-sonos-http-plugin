#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
#  Copyright 2022-             Michael Wenzel       wenzel_michael@web.de
#########################################################################
#  This file is part of SmartHomeNG.
#  https://www.smarthomeNG.de
#  https://knx-user-forum.de/forum/supportforen/smarthome-py
#
#  Plugin to connect to Sonos-HTTP-API to run with SmartHomeNG version 1.8 
#  and upwards.
#
#  SmartHomeNG is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHomeNG is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHomeNG. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################


from lib.model.smartplugin import SmartPlugin
from lib.utils import Utils
from .webif import WebInterface

import threading
import queue
from http.server import BaseHTTPRequestHandler
import socketserver
import urllib.parse as urlparse
import logging
import requests
import json
import socket
import datetime


class SonosHttp(SmartPlugin):

    PLUGIN_VERSION = '1.0.0'

    def __init__(self, sh):
        """
        Initalizes the plugin.

        If you need the sh object at all, use the method self.get_sh() to get it. There should be almost no need for
        a reference to the sh object any more.

        Plugins have to use the new way of getting parameter values:
        use the SmartPlugin method get_parameter_value(parameter_name). Anywhere within the Plugin you can get
        the configured (and checked) value for a parameter by calling self.get_parameter_value(parameter_name). It
        returns the value in the datatype that is defined in the metadata.
        """

        # Call init code of parent class (SmartPlugin)
        super().__init__()

        # get the parameters for the plugin (as defined in metadata plugin.yaml):
        self._sonos_http_api_host = self.get_parameter_value('Sonos_HTTP_API_Host') if self.get_parameter_value('Sonos_HTTP_API_Host') != '127.0.0.1' else Utils.get_local_ipv4_address()
        self._sonos_http_api_port = self.get_parameter_value('Sonos_HTTP_API_Port') if is_valid_port(self.get_parameter_value('Sonos_HTTP_API_Port')) else None
        self._tts_language = self.get_parameter_value('Sprache')

        if not self._sonos_http_api_host or not self._sonos_http_api_port:
            self.logger.error(f"Settings for Sonos-HTTP-API-Host incorrect. Plugin will not be started.")
            self._init_complete = False
                    
        self._http_server_ip = self.get_parameter_value('Server_IP') if self.get_parameter_value('Server_IP') != '127.0.0.1' else Utils.get_local_ipv4_address()
        _http_server_port = self.get_parameter_value('Server_Port')
        if is_valid_port(_http_server_port):
            if is_open_port(_http_server_port):
                self._http_server_port = int(_http_server_port)
            else:
                self.logger.error(f"Your chosen webservice port {_http_server_port} is already in use.")

        # define properties
        self._item_dict = {}                        # dict to hold all items {item1: ('sonos_zone', 'sonos_cmd'), item2: ('sonos_zone', 'sonos_cmd')...}
        self.sonos = {}                             # dict to hold state information per zone
        self.sonos_zone_uuid = set()                # set of tuples for [(zone1, uuid1), (zone2, uuid2), ...]
        self.sonos_topology = {}                    # dict for topology {uuid1 {'coordinator': 'RINCON_******', 'members': {'RINCON_******'}}, uuid2....
        self.sonos_favorites = []                   # list holding sonos favourites
        self.sonos_playlists = []                   # list holding sonos playlists
        self.webhook_thread = None
        self.alive = None
        
        # init HttpServer
        if self._http_server_ip and self._http_server_port:
            self.client = HttpServer(self._http_server_ip, self._http_server_port, self)
            self.client.startup()
        else:
            self.logger.error(f"Settings for Webhook server incorrect. Server will not be started.")
            self._init_complete = False

        # init webinterface
        if not self.init_webinterface(WebInterface):
            self.logger.error("Unable to start Webinterface")
            self._init_complete = False
        else:
            self.logger.debug(f"Init of Plugin {self.get_shortname()} complete")
            
    def run(self):
        """
        Run method for the plugin
        """
        # self.logger.debug(f"{self.get_shortname()}: Run method called")
        self.alive = True

        # initialize sonos system
        response = self.zones
        # self.logger.debug(f"run: zones={response}")
        self._decode_zones(response)

        # finally run 'get_webhook_data' in an endless loop as long as plugin is alive in an own thread
        self.webhook_startup()

    def stop(self):
        """
        Stop method for the plugin
        """
        self.logger.debug(f"{self.get_shortname()}: Stop method called")
        self.alive = False
        self.client.stop_server()
        self.client.shutdown()
        self.webhook_shutdown()
    
    def parse_item(self, item):
        """
        Default plugin parse_item method. Is called when the plugin is initialized.
        The plugin can, corresponding to its attribute keywords, decide what to do with
        the item in future, like adding it to an internal array for future reference
        :param item:    The item to process.
        :return:        If the plugin needs to be informed of an items change you should return a call back function
                        like the function update_item down below. An example when this is needed is the knx plugin
                        where parse_item returns the update_item function when the attribute knx_send is found.
                        This means that when the items value is about to be updated, the call back function is called
                        with the item, caller, source and dest as arguments and in case of the knx plugin the value
                        can be sent to the knx with a knx write function within the knx plugin.
        """
        _sonos_global_cmd = None
        _sonos_zone_cmd = None
        _sonos_zone_info = None
        _sonos_zone = None

        if self.has_iattr(item.conf, 'sonos_zone_cmd'):
            # self.logger.debug(f"parse item: {item.id()}")
            _sonos_zone_cmd = str(self.get_iattr_value(item.conf, 'sonos_zone_cmd'))
        elif self.has_iattr(item.conf, 'sonos_zone_info'):
            # self.logger.debug(f"parse item: {item.id()}")
            _sonos_zone_info = str(self.get_iattr_value(item.conf, 'sonos_zone_info'))
        elif self.has_iattr(item.conf, 'sonos_global_cmd'):
            # self.logger.debug(f"parse item: {item.id()}")
            _sonos_global_cmd = str(self.get_iattr_value(item.conf, 'sonos_global_cmd'))

        if _sonos_global_cmd:
            self._item_dict[item] = (None, _sonos_global_cmd)
            return self.update_item

        elif _sonos_zone_cmd or _sonos_zone_info:
            _lookup_item = item
            for i in range(3):
                if self.has_iattr(_lookup_item.conf, 'sonos_zone'):
                    _sonos_zone = str(self.get_iattr_value(_lookup_item.conf, 'sonos_zone'))
                    break
                else:
                    _lookup_item = _lookup_item.return_parent()

            if _sonos_zone_cmd and _sonos_zone is not None:
                self._item_dict[item] = (_sonos_zone, _sonos_zone_cmd)
                # self.logger.debug(f"Item with attribut 'sonos_zone_cmd'={_sonos_zone_cmd} for zone='{_sonos_zone}' added")
                return self.update_item        
                
            if _sonos_zone_info and _sonos_zone is not None:
                # self.logger.debug(f"Item with attribut 'sonos_zone_info'={_sonos_zone_info} for zone='{_sonos_zone}' added")
                self._item_dict[item] = (_sonos_zone, _sonos_zone_info)

    def update_item(self, item, caller=None, source=None, dest=None):
        """
        Item has been updated

        This method is called, if the value of an item has been updated by SmartHomeNG.
        It should write the changed value out to the device (hardware/interface) that
        is managed by this plugin.

        :param item: item to be updated towards the plugin
        :param caller: if given it represents the callers name
        :param source: if given it represents the source
        :param dest: if given it represents the dest
        """
        if self.alive and caller != self.get_shortname():
            # code to execute if the plugin is not stopped and only, if the item has not been changed by this this plugin:
            self.logger.info(f"Update item: {item.property.path}, item has been changed outside this plugin")

            if item in self._item_dict:
                self.logger.debug(f"update_item was called with item {item.property.path} with value {item()} from caller {caller}, source {source} and dest {dest}")

                _sonos_zone = self._item_dict[item][0]
                _sonos_cmd = self._item_dict[item][1]
                # if global command, _sonos_zone is None
                _sonos_zone = _sonos_zone if _sonos_zone else ''

                if _sonos_cmd.endswith('_up'):
                    request = f"{_sonos_zone}/{_sonos_cmd.split('_up')[0]}/+1"
                elif _sonos_cmd.endswith('_down'):
                    request = f"{_sonos_zone}/{_sonos_cmd.split('_down')[0]}/-1"
                elif _sonos_cmd in ['play', 'pause', 'playpause', 'mute', 'unmute', 'groupMute', 'groupUnmute', 'togglemute', 'next', 'previous', 'state']:
                    request = f"{_sonos_zone}/{_sonos_cmd}"
                elif 'say' in _sonos_cmd:
                    request = f"{_sonos_zone}/{_sonos_cmd}/{urlparse.quote(item())}/{self._tts_language}"
                elif _sonos_cmd == 'favorite_nr':
                    request = f"{_sonos_zone}/favorite/{self.sonos_favorites[item()-1]}"
                else:
                    request = f"{_sonos_zone}/{_sonos_cmd}/{item()}"

                response = self.get_request(request)
                if response.get('status', None) == 'success':
                    self.logger.debug(f"update_item: Command {_sonos_cmd } successfully sent.")
                else:
                    self.logger.debug(f"update_item: Command {_sonos_cmd} failed.")

    def get_request(self, request):
        """Create payload, send get request and return response

            in case of no failure it returns the response
            in case of failure it returns None

        """

        # self.logger.debug(f"get_request: request={request}")
        protocol = 'http'
        url_base = f"{protocol}://{self._sonos_http_api_host}:{self._sonos_http_api_port}"
        request = f"{url_base}/{request}"

        try:
            r = requests.get(request, verify=False)
        except Exception as e:
            self.logger.error(f"get_request: request={request} failed with Error {e}")
        else:
            if r.status_code == requests.codes.ok:
                # self.logger.error(f"get_request: request={request} sucessful")
                response = json.loads(r.text)
                # self.logger.error(f"json={response}")
                # self.logger.debug(f"json={json.dumps(d, indent=4, sort_keys=True)}")
                return response
            else:
                self.logger.error(f"get_request: request={request} failed")
                return

    def get_webhook_data(self):
        """get data from queue of server listening on webhook and hand over to decode"""
        
        while self.alive:
            try:
                queue_data = self.client.get_queue().get(True, 10)
                # self.logger.debug(f"get_webhook_data: queue_data={queue_data}")
            except queue.Empty:
                # self.logger.debug("get_webhook_data: there was nothing in the queue so continue")
                # there was nothing in the queue so continue
                pass
            else:
                try:
                    response = json.loads(queue_data)
                except Exception as e:
                    self.logger.debug(f"get_webhook_data: was not in json format Error={e}, response skipped")
                    pass
                else:
                    self.logger.debug(f"get_webhook_data: response={response}")
                    response_type = response['type']

                    if response_type == "transport-state":
                        # response={'type': 'transport-state', 'data': {'uuid': 'RINCON_7828CAEB625E01400', 'coordinator': 'RINCON_7828CAEB625E01400', 'roomName': 'Esszimmer', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 8, 'loudness': True}, 'currentTrack': {'artist': 'Antenne Bayern', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8', 'duration': 0, 'uri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'trackUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'type': 'radio', 'stationName': 'Antenne Bayern', 'absoluteAlbumArtUri': 'http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON68871_</desc></item></DIDL-Lite>'}}
                        self._decode_state(response['data'])
                    elif response_type == "topology-change":
                        # response={'type': 'topology-change', 'data': [{'coordinator': {'uuid': 'RINCON_7828CA59548701400', 'coordinator': 'RINCON_7828CA59548701400', 'roomName': 'TV', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 6, 'loudness': True, 'speechEnhancement': True, 'nightMode': False}, 'currentTrack': {'title': 'google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}, 'sub': {'gain': 7, 'crossover': 0, 'polarity': 0, 'enabled': True}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'avTransportUriMetadata': ''}, 'members': [{'uuid': 'RINCON_7828CA59548701400', 'coordinator': 'RINCON_7828CA59548701400', 'roomName': 'TV', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 6, 'loudness': True, 'speechEnhancement': True, 'nightMode': False}, 'currentTrack': {'title': 'google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}, 'sub': {'gain': 7, 'crossover': 0, 'polarity': 0, 'enabled': True}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'avTransportUriMetadata': ''}], 'uuid': 'RINCON_7828CA59548701400', 'id': 'RINCON_7828CAEB625E01400:1640192871'}, {'coordinator': {'uuid': 'RINCON_7828CAEAC58601400', 'coordinator': 'RINCON_7828CAEAC58601400', 'roomName': 'Büronos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 3, 'loudness': True}, 'currentTrack': {'artist': 'BR Schlager', 'title': 'BR Schlager', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9', 'duration': 0, 'uri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'trackUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'type': 'radio', 'stationName': 'BR Schlager', 'absoluteAlbumArtUri': 'http://192.168.2.123:1400/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>BR Schlager</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON77575_X_#Svc77575-644c3615-Token</desc></item></DIDL-Lite>'}, 'members': [{'uuid': 'RINCON_7828CAEAC58601400', 'coordinator': 'RINCON_7828CAEAC58601400', 'roomName': 'Büronos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 3, 'loudness': True}, 'currentTrack': {'artist': 'BR Schlager', 'title': 'BR Schlager', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9', 'duration': 0, 'uri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'trackUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'type': 'radio', 'stationName': 'BR Schlager', 'absoluteAlbumArtUri': 'http://192.168.2.123:1400/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>BR Schlager</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON77575_X_#Svc77575-644c3615-Token</desc></item></DIDL-Lite>'}], 'uuid': 'RINCON_7828CAEAC58601400', 'id': 'RINCON_7828CAEAC58601400:3457120174'}, {'coordinator': {'uuid': 'RINCON_7828CA060F5401400', 'coordinator': 'RINCON_7828CA060F5401400', 'roomName': 'Carlisonos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 4, 'treble': 4, 'loudness': True}, 'currentTrack': {'title': 'google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'avTransportUriMetadata': ''}, 'members': [{'uuid': 'RINCON_7828CA060F5401400', 'coordinator': 'RINCON_7828CA060F5401400', 'roomName': 'Carlisonos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 4, 'treble': 4, 'loudness': True}, 'currentTrack': {'title': 'google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'avTransportUriMetadata': ''}], 'uuid': 'RINCON_7828CA060F5401400', 'id': 'RINCON_7828CA060F5401400:2557459617'}, {'coordinator': {'uuid': 'RINCON_7828CAEB625E01400', 'coordinator': 'RINCON_7828CAEB625E01400', 'roomName': 'Esszimmer', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 8, 'loudness': True}, 'currentTrack': {'artist': 'Antenne Bayern', 'title': 'ZPSTR_BUFFERING', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8', 'duration': 0, 'uri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'trackUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'type': 'radio', 'stationName': 'Antenne Bayern', 'absoluteAlbumArtUri': 'http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'TRANSITIONING', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON68871_</desc></item></DIDL-Lite>'}, 'members': [{'uuid': 'RINCON_7828CAEB625E01400', 'coordinator': 'RINCON_7828CAEB625E01400', 'roomName': 'Esszimmer', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 8, 'loudness': True}, 'currentTrack': {'artist': 'Antenne Bayern', 'title': 'ZPSTR_BUFFERING', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8', 'duration': 0, 'uri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'trackUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'type': 'radio', 'stationName': 'Antenne Bayern', 'absoluteAlbumArtUri': 'http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'TRANSITIONING', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON68871_</desc></item></DIDL-Lite>'}], 'uuid': 'RINCON_7828CAEB625E01400', 'id': 'RINCON_7828CAEB625E01400:1640192896'}]}
                        self._decode_zones(response['data'])
                    elif response_type == "volume-change":
                        # response={'type': 'volume-change', 'data': {'uuid': 'RINCON_7828CAEB625E01400', 'previousVolume': 8, 'newVolume': 8, 'roomName': 'Esszimmer'}}
                        self._decode_volume(response['data'])
                    elif response_type == "mute-change":
                        # response={'type': 'mute-change', 'data': {'uuid': 'RINCON_7828CAEB625E01400', 'previousMute': True, 'newMute': True, 'roomName': 'Esszimmer'}}
                        self._decode_mute(response['data'])

    def webhook_startup(self):
        """Start a thread that to get Webhook data in endless loop"""

        try:
            self.webhook_thread = threading.Thread(target=self.get_webhook_data)
            self.webhook_thread.setDaemon(False)
            _name = 'plugins.' + self.get_fullname() + '.SonosHttpWebhook'
            self.webhook_thread.setName(_name)
            self.webhook_thread.start()
        except threading.ThreadError:
            self.logger.error("Unable to launch SonosHttpWebhook thread")
            self.webhook_thread = None

    def webhook_shutdown(self):
        """Shut a thread that to get Webhook data in endless loop"""

        if self.webhook_thread:
            # terminate the thread
            self.webhook_thread.join()
            # log the outcome
            if self.webhook_thread.is_alive():
                self.logger.error("Unable to shut down SonosHttpWebhook thread")
            else:
                self.logger.info("SonosHttpWebhook thread has been terminated")
        self.webhook_thread = None

    def _update_item_value_change(self, zone, cmd, value):
        """Updates item values based on sonos zone, sonos_cmd and value"""
    
        for item in self._item_dict:
            _sonos_zone = self._item_dict[item][0]
            _sonos_cmd = self._item_dict[item][1]

            if _sonos_zone == zone and _sonos_cmd == cmd:
                item(value, self.get_shortname(), _sonos_cmd)

    def _update_item_value_state(self, zone):
        """Updates item values based on state dict of sonos zone"""

        # self.logger.debug(f"_update_item_value_state called for zone {zone}")

        for item in self._item_dict:
            _sonos_zone = self._item_dict[item][0]
            _sonos_cmd = self._item_dict[item][1]
            _value = None

            if _sonos_zone == zone:
                # self.logger.debug(f"item {item.id()} to be updated since part of zone='{zone} with _sonos_cmd='{_sonos_cmd}'")
                sonos_zone_data = self.sonos.get(_sonos_zone, None)
                if sonos_zone_data is not None:
                    sonos_zone_data_state = sonos_zone_data.get('state', None)
                    if sonos_zone_data_state is not None:
                        if _sonos_cmd.startswith('current_'):
                            current_track = sonos_zone_data_state.get('currentTrack', None)
                            if current_track is not None:
                                cmd = _sonos_cmd.split('_')[1]
                                try:
                                    _value = current_track[cmd]
                                except:
                                    pass
                                else:
                                    if cmd == 'duration' and _sonos_cmd.endswith('_str'):
                                        _value = convert_sec_to_str(_value)
                        elif _sonos_cmd.startswith('next_'):
                            next_track = sonos_zone_data_state.get('nextTrack', None)
                            if next_track is not None:
                                cmd = _sonos_cmd.split('_')[1]
                                try:
                                    _value = next_track[cmd]
                                except:
                                    pass
                                else:
                                    if cmd == 'duration' and _sonos_cmd.endswith('_str'):
                                        _value = convert_sec_to_str(_value)
                        elif _sonos_cmd in ['play', 'playpause']:
                            playback_state = sonos_zone_data_state.get('playbackState', None)
                            if playback_state == 'STOPPED':
                                _value = False
                            else:
                                _value = True
                        else:
                            if _sonos_cmd in ['togglemute']:
                                _sonos_cmd = 'mute'

                            _value = self._recursive_lookup(_sonos_cmd, sonos_zone_data)

                        if _value is not None:
                            item(_value, self.get_shortname(), _sonos_cmd)
                    else:
                        self.logger.warning(f"no 'sonos_zone_data_state' data")
                else:
                    self.logger.warning(f"no 'sonos_zone_data' data")

    def _recursive_lookup(self, k, d):
        """searched for key k in nested dict d and returns value of k if found and None of key k is not on nested dict"""
        
        if k in d:
            return d[k]
        for v in d.values():
            if isinstance(v, dict):
                a = self._recursive_lookup(k, v)
                if a is not None:
                    return a
        return None

    def _get_uuid_from_zone(self, zone):
        """get uuid from a zone; uses list of tuples for [(zone1, uuid1), (zone2, uuid2), ...]"""

        return dict(self.sonos_zone_uuid).get(zone, None)

    def _get_zone_from_uuid(self, uuid):
        """get zone from a uuid; uses list of tuples for [(zone1, uuid1), (zone2, uuid2), ...]"""

        return {value: key for key, value in dict(self.sonos_zone_uuid).items()}.get(uuid, None)

    def _decode_zones(self, zones):
        """decode webhook response_type "topology-change" and hand over to _decode_state"""

        if zones is not None:
            # get all zones and uuids
            for entry in zones:
                members = entry['members']
                for member in members:
                    self.sonos_zone_uuid.update([(member['roomName'], member['uuid'])])

            # get topology
            for entry in zones:
                uuid = entry['uuid']
                if entry['uuid'] not in self.sonos_topology:
                    self.sonos_topology[uuid] = {}
                self.sonos_topology[uuid]['coordinator'] = entry['coordinator']['uuid']
                if self.sonos_topology[uuid].get('members', None) is None:
                    self.sonos_topology[uuid]['members'] = set()
                for member in entry['members']:
                    self.sonos_topology[uuid]['members'].update([(member['uuid'])])
                # decode state
                self._decode_state(entry['coordinator'])

        else:
            self.logger.warning(f"_decode_zones: Input was {zones}; get request might have been not succesfull")

    def _decode_state(self, data):
        """decode webhook response_type "transport-state" and hand over to _update_item_value_state"""

        zone_name = data.get('roomName', None)

        if zone_name not in self.sonos:
            self.sonos[zone_name] = {}

        self.sonos[zone_name]['uuid'] = data.get('uuid', None)
        self.sonos[zone_name]['coordinator'] = data.get('coordinator', None)
        self.sonos[zone_name]['state'] = data.get('state', None)
        self.sonos[zone_name]['groupstate'] = data.get('groupState', None)

        self._update_item_value_state(zone_name)

    def _decode_mute(self, data):
        """decode webhook response_type "mute-change" and hand over to _update_item_value_change"""
    
        roomname = data.get('roomName', None)
        mute = bool(data.get('newMute', None))
        self._update_item_value_change(roomname, 'mute', mute)
        self._update_item_value_change(roomname, 'tooglemute', not mute)

    def _decode_volume(self, data):
        """decode webhook response_type "volume-change" and hand over to _update_item_value_change"""
        
        roomname = data.get('roomName', None)
        volume = int(data.get('newVolume', None))
        self._update_item_value_change(roomname, 'volume', volume)

    @property
    def topology(self):
        if self.sonos_topology:
            topology_zone = {}
            for uuid in self.sonos_topology:
                zone = self._get_zone_from_uuid(uuid)
                if zone not in topology_zone:
                    topology_zone[zone] = {}
                for entry in self.sonos_topology[uuid]:
                    if entry == 'coordinator':
                        topology_zone[zone][entry] = self._get_zone_from_uuid(self.sonos_topology[uuid][entry])
                    if entry == 'members':
                        topology_zone[zone][entry] = set()
                        for member in self.sonos_topology[uuid][entry]:
                            topology_zone[zone][entry].update([(self._get_zone_from_uuid(member))])
            return topology_zone

    @property
    def favourites(self):
        return {v+1: k for v, k in enumerate(self.get_request('favorites'))}

    @property
    def playlists(self):
        return {v+1: k for v, k in enumerate(self.get_request('playlists'))}

    @property
    def zones(self):
        return self.get_request('zones')


class Consumer(object):
    """The Consumer contains two primary parts - a Server and a Parser."""

    queue = queue.Queue()

    def __init__(self, plugin_instance):

        # get instance
        self._plugin_instance = plugin_instance

    def startup(self):
        pass

    def shutdown(self):
        pass

    def get_queue(self):
        return Consumer.queue


class HttpServer(Consumer):
    """Create HTTP Server to get Webhook Data"""

    def __init__(self, tcp_server_address, tcp_server_port, plugin_instance):

        # now initialize my superclasses
        super(HttpServer, self).__init__(plugin_instance)

        # init instance
        self._plugin_instance = plugin_instance

        self._server_thread = None

        # log the relevant settings/parameters we are using
        self._plugin_instance.logger.debug("Starting SonosHttpServer")

        # init tcp server
        self._server = HttpServer.TCPServer(tcp_server_address, tcp_server_port, HttpServer.Handler, plugin_instance)

    def run_server(self):
        self._server.run()

    def stop_server(self):
        self._server.stop()
        self._server = None

    def startup(self):
        """Start a thread for SonosHttpServer."""

        try:
            self._server_thread = threading.Thread(target=self.run_server)
            self._server_thread.setDaemon(True)
            _name = 'plugins.' + self._plugin_instance.get_fullname() + '.SonosHttpServer'
            self._server_thread.setName(_name)
            self._server_thread.start()
        except threading.ThreadError:
            self._plugin_instance.logger.error("Unable to launch SonosHttpServer thread")
            self._server_thread = None

    def shutdown(self):
        """Shut down the thread for SonosHttpServer"""

        if self._server_thread:
            # terminate the thread
            self._server_thread.join(10.0)
            # log the outcome
            if self._server_thread.is_alive():
                self._plugin_instance.logger.error("Unable to shut down SonosHttpServer thread")
            else:
                self._plugin_instance.logger.info("SonosHttpServer thread has been terminated")
        self._server_thread = None
        
    class Server(object):

        def run(self):
            pass

        def stop(self):
            pass

    class TCPServer(Server, socketserver.TCPServer):
    
        daemon_threads = True
        allow_reuse_address = True

        def __init__(self, address, port, handler, plugin_instance):

            # init instance
            self._plugin_instance = plugin_instance
            
            # init TCP Server
            self._plugin_instance.logger.info(f"start SonosHttpServer at {address}:{port}")
            socketserver.TCPServer.__init__(self, (address, int(port)), handler)

        def run(self):
            # self._plugin_instance.logger.debug("start SonosHttp server")
            self.serve_forever()

        def stop(self):
            self._plugin_instance.logger.debug("stop SonosHttpServer")
            self.shutdown()
            self.server_close()

    class Handler(BaseHTTPRequestHandler):

        def reply(self):
            # standard reply is HTTP code of 200 and the response string
            ok_answer = "OK\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(ok_answer)))
            self.end_headers()
            self.wfile.write(ok_answer.encode())

        def do_POST(self):
            # get the payload from an HTTP POST
            # logger = logging.getLogger(__name__)
            # logger.debug(f"POST: client_address={client_ip}")
            length = int(self.headers["Content-Length"])
            data = self.rfile.read(length)
            # logger.debug(f"POST: data={str(data)}")
            self.reply()
            Consumer.queue.put(data)

        def do_PUT(self):
            pass

        def do_GET(self):
            logger = logging.getLogger(__name__)
            # get the query string from an HTTP GET
            data = urlparse.urlparse(self.path).query
            logger.debug(f"GET: {str(data)}")
            self.reply()
            Consumer.queue.put(data)

####################################################################################
#                                Utility Fuctions
####################################################################################


def is_open_port(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except socket.error:
        return False
    return True


def is_valid_port(port):
    if 1 <= port <= 65535:
        return True
    else:
        return False


def convert_sec_to_str(seconds):
    return str(datetime.timedelta(seconds=seconds))


"""
# response={'type': 'topology-change', 'data': [{'coordinator': {'uuid': 'RINCON_7828CA59548701400', 'coordinator': 'RINCON_7828CA59548701400', 'roomName': 'TV', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 6, 'loudness': True, 'speechEnhancement': True, 'nightMode': False}, 'currentTrack': {'title': 'google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}, 'sub': {'gain': 7, 'crossover': 0, 'polarity': 0, 'enabled': True}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'avTransportUriMetadata': ''}, 'members': [{'uuid': 'RINCON_7828CA59548701400', 'coordinator': 'RINCON_7828CA59548701400', 'roomName': 'TV', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 6, 'loudness': True, 'speechEnhancement': True, 'nightMode': False}, 'currentTrack': {'title': 'google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}, 'sub': {'gain': 7, 'crossover': 0, 'polarity': 0, 'enabled': True}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'avTransportUriMetadata': ''}], 'uuid': 'RINCON_7828CA59548701400', 'id': 'RINCON_7828CAEB625E01400:1640192871'}, {'coordinator': {'uuid': 'RINCON_7828CAEAC58601400', 'coordinator': 'RINCON_7828CAEAC58601400', 'roomName': 'Büronos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 3, 'loudness': True}, 'currentTrack': {'artist': 'BR Schlager', 'title': 'BR Schlager', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9', 'duration': 0, 'uri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'trackUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'type': 'radio', 'stationName': 'BR Schlager', 'absoluteAlbumArtUri': 'http://192.168.2.123:1400/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>BR Schlager</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON77575_X_#Svc77575-644c3615-Token</desc></item></DIDL-Lite>'}, 'members': [{'uuid': 'RINCON_7828CAEAC58601400', 'coordinator': 'RINCON_7828CAEAC58601400', 'roomName': 'Büronos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 3, 'loudness': True}, 'currentTrack': {'artist': 'BR Schlager', 'title': 'BR Schlager', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9', 'duration': 0, 'uri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'trackUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'type': 'radio', 'stationName': 'BR Schlager', 'absoluteAlbumArtUri': 'http://192.168.2.123:1400/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>BR Schlager</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON77575_X_#Svc77575-644c3615-Token</desc></item></DIDL-Lite>'}], 'uuid': 'RINCON_7828CAEAC58601400', 'id': 'RINCON_7828CAEAC58601400:3457120174'}, {'coordinator': {'uuid': 'RINCON_7828CA060F5401400', 'coordinator': 'RINCON_7828CA060F5401400', 'roomName': 'Carlisonos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 4, 'treble': 4, 'loudness': True}, 'currentTrack': {'title': 'google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'avTransportUriMetadata': ''}, 'members': [{'uuid': 'RINCON_7828CA060F5401400', 'coordinator': 'RINCON_7828CA060F5401400', 'roomName': 'Carlisonos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 4, 'treble': 4, 'loudness': True}, 'currentTrack': {'title': 'google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'avTransportUriMetadata': ''}], 'uuid': 'RINCON_7828CA060F5401400', 'id': 'RINCON_7828CA060F5401400:2557459617'}, {'coordinator': {'uuid': 'RINCON_7828CAEB625E01400', 'coordinator': 'RINCON_7828CAEB625E01400', 'roomName': 'Esszimmer', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 8, 'loudness': True}, 'currentTrack': {'artist': 'Antenne Bayern', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8', 'duration': 0, 'uri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'trackUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'type': 'radio', 'stationName': 'Antenne Bayern', 'absoluteAlbumArtUri': 'http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON68871_</desc></item></DIDL-Lite>'}, 'members': [{'uuid': 'RINCON_7828CAEB625E01400', 'coordinator': 'RINCON_7828CAEB625E01400', 'roomName': 'Esszimmer', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 8, 'loudness': True}, 'currentTrack': {'artist': 'Antenne Bayern', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8', 'duration': 0, 'uri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'trackUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'type': 'radio', 'stationName': 'Antenne Bayern', 'absoluteAlbumArtUri': 'http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON68871_</desc></item></DIDL-Lite>'}], 'uuid': 'RINCON_7828CAEB625E01400', 'id': 'RINCON_7828CAEB625E01400:1640192896'}]}
data =
        {
          "type": "topology-change",
          "data": [
            {
              "coordinator": {
                "uuid": "RINCON_7828CA59548701400",
                "coordinator": "RINCON_7828CA59548701400",
                "roomName": "TV",
                "state": {
                  "volume": 10,
                  "mute": false,
                  "equalizer": {
                    "bass": 7,
                    "treble": 6,
                    "loudness": true,
                    "speechEnhancement": true,
                    "nightMode": false
                  },
                  "currentTrack": {
                    "title": "google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3",
                    "duration": 2,
                    "uri": "http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3",
                    "trackUri": "http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3",
                    "type": "track",
                    "stationName": "",
                    "absoluteAlbumArtUri": "http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3"
                  },
                  "nextTrack": {
                    "artist": "",
                    "title": "",
                    "album": "",
                    "albumArtUri": "",
                    "duration": 0,
                    "uri": ""
                  },
                  "trackNo": 1,
                  "elapsedTime": 0,
                  "elapsedTimeFormatted": "00:00:00",
                  "playbackState": "STOPPED",
                  "playMode": {
                    "repeat": "none",
                    "shuffle": false,
                    "crossfade": false
                  },
                  "sub": {
                    "gain": 7,
                    "crossover": 0,
                    "polarity": 0,
                    "enabled": true
                  }
                },
                "groupState": {
                  "volume": 10,
                  "mute": false
                },
                "avTransportUri": "http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3",
                "avTransportUriMetadata": ""
              },
              "members": [
                {
                  "uuid": "RINCON_7828CA59548701400",
                  "coordinator": "RINCON_7828CA59548701400",
                  "roomName": "TV",
                  "state": {
                    "volume": 10,
                    "mute": false,
                    "equalizer": {
                      "bass": 7,
                      "treble": 6,
                      "loudness": true,
                      "speechEnhancement": true,
                      "nightMode": false
                    },
                    "currentTrack": {
                      "title": "google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3",
                      "duration": 2,
                      "uri": "http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3",
                      "trackUri": "http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3",
                      "type": "track",
                      "stationName": "",
                      "absoluteAlbumArtUri": "http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3"
                    },
                    "nextTrack": {
                      "artist": "",
                      "title": "",
                      "album": "",
                      "albumArtUri": "",
                      "duration": 0,
                      "uri": ""
                    },
                    "trackNo": 1,
                    "elapsedTime": 0,
                    "elapsedTimeFormatted": "00:00:00",
                    "playbackState": "STOPPED",
                    "playMode": {
                      "repeat": "none",
                      "shuffle": false,
                      "crossfade": false
                    },
                    "sub": {
                      "gain": 7,
                      "crossover": 0,
                      "polarity": 0,
                      "enabled": true
                    }
                  },
                  "groupState": {
                    "volume": 10,
                    "mute": false
                  },
                  "avTransportUri": "http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3",
                  "avTransportUriMetadata": ""
                }
              ],
              "uuid": "RINCON_7828CA59548701400",
              "id": "RINCON_7828CAEB625E01400:1640192871"
            },
            {
              "coordinator": {
                "uuid": "RINCON_7828CAEAC58601400",
                "coordinator": "RINCON_7828CAEAC58601400",
                "roomName": "B\u00fcronos",
                "state": {
                  "volume": 10,
                  "mute": false,
                  "equalizer": {
                    "bass": 7,
                    "treble": 3,
                    "loudness": true
                  },
                  "currentTrack": {
                    "artist": "BR Schlager",
                    "title": "BR Schlager",
                    "albumArtUri": "/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9",
                    "duration": 0,
                    "uri": "x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9",
                    "trackUri": "x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9",
                    "type": "radio",
                    "stationName": "BR Schlager",
                    "absoluteAlbumArtUri": "http://192.168.2.123:1400/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9"
                  },
                  "nextTrack": {
                    "artist": "",
                    "title": "",
                    "album": "",
                    "albumArtUri": "",
                    "duration": 0,
                    "uri": ""
                  },
                  "trackNo": 1,
                  "elapsedTime": 0,
                  "elapsedTimeFormatted": "00:00:00",
                  "playbackState": "STOPPED",
                  "playMode": {
                    "repeat": "none",
                    "shuffle": false,
                    "crossfade": false
                  }
                },
                "groupState": {
                  "volume": 10,
                  "mute": false
                },
                "avTransportUri": "x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9",
                "avTransportUriMetadata": "<DIDL-Lite xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\" xmlns:r=\"urn:schemas-rinconnetworks-com:metadata-1-0/\" xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\"><item id=\"-1\" parentID=\"-1\" restricted=\"true\"><dc:title>BR Schlager</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id=\"cdudn\" nameSpace=\"urn:schemas-rinconnetworks-com:metadata-1-0/\">SA_RINCON77575_X_#Svc77575-644c3615-Token</desc></item></DIDL-Lite>"
              },
              "members": [
                {
                  "uuid": "RINCON_7828CAEAC58601400",
                  "coordinator": "RINCON_7828CAEAC58601400",
                  "roomName": "B\u00fcronos",
                  "state": {
                    "volume": 10,
                    "mute": false,
                    "equalizer": {
                      "bass": 7,
                      "treble": 3,
                      "loudness": true
                    },
                    "currentTrack": {
                      "artist": "BR Schlager",
                      "title": "BR Schlager",
                      "albumArtUri": "/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9",
                      "duration": 0,
                      "uri": "x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9",
                      "trackUri": "x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9",
                      "type": "radio",
                      "stationName": "BR Schlager",
                      "absoluteAlbumArtUri": "http://192.168.2.123:1400/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9"
                    },
                    "nextTrack": {
                      "artist": "",
                      "title": "",
                      "album": "",
                      "albumArtUri": "",
                      "duration": 0,
                      "uri": ""
                    },
                    "trackNo": 1,
                    "elapsedTime": 0,
                    "elapsedTimeFormatted": "00:00:00",
                    "playbackState": "STOPPED",
                    "playMode": {
                      "repeat": "none",
                      "shuffle": false,
                      "crossfade": false
                    }
                  },
                  "groupState": {
                    "volume": 10,
                    "mute": false
                  },
                  "avTransportUri": "x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9",
                  "avTransportUriMetadata": "<DIDL-Lite xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\" xmlns:r=\"urn:schemas-rinconnetworks-com:metadata-1-0/\" xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\"><item id=\"-1\" parentID=\"-1\" restricted=\"true\"><dc:title>BR Schlager</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id=\"cdudn\" nameSpace=\"urn:schemas-rinconnetworks-com:metadata-1-0/\">SA_RINCON77575_X_#Svc77575-644c3615-Token</desc></item></DIDL-Lite>"
                }
              ],
              "uuid": "RINCON_7828CAEAC58601400",
              "id": "RINCON_7828CAEAC58601400:3457120174"
            },
            {
              "coordinator": {
                "uuid": "RINCON_7828CA060F5401400",
                "coordinator": "RINCON_7828CA060F5401400",
                "roomName": "Carlisonos",
                "state": {
                  "volume": 10,
                  "mute": false,
                  "equalizer": {
                    "bass": 4,
                    "treble": 4,
                    "loudness": true
                  },
                  "currentTrack": {
                    "title": "google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3",
                    "duration": 2,
                    "uri": "http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3",
                    "trackUri": "http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3",
                    "type": "track",
                    "stationName": "",
                    "absoluteAlbumArtUri": "http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3"
                  },
                  "nextTrack": {
                    "artist": "",
                    "title": "",
                    "album": "",
                    "albumArtUri": "",
                    "duration": 0,
                    "uri": ""
                  },
                  "trackNo": 1,
                  "elapsedTime": 0,
                  "elapsedTimeFormatted": "00:00:00",
                  "playbackState": "STOPPED",
                  "playMode": {
                    "repeat": "none",
                    "shuffle": false,
                    "crossfade": false
                  }
                },
                "groupState": {
                  "volume": 10,
                  "mute": false
                },
                "avTransportUri": "http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3",
                "avTransportUriMetadata": ""
              },
              "members": [
                {
                  "uuid": "RINCON_7828CA060F5401400",
                  "coordinator": "RINCON_7828CA060F5401400",
                  "roomName": "Carlisonos",
                  "state": {
                    "volume": 10,
                    "mute": false,
                    "equalizer": {
                      "bass": 4,
                      "treble": 4,
                      "loudness": true
                    },
                    "currentTrack": {
                      "title": "google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3",
                      "duration": 2,
                      "uri": "http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3",
                      "trackUri": "http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3",
                      "type": "track",
                      "stationName": "",
                      "absoluteAlbumArtUri": "http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3"
                    },
                    "nextTrack": {
                      "artist": "",
                      "title": "",
                      "album": "",
                      "albumArtUri": "",
                      "duration": 0,
                      "uri": ""
                    },
                    "trackNo": 1,
                    "elapsedTime": 0,
                    "elapsedTimeFormatted": "00:00:00",
                    "playbackState": "STOPPED",
                    "playMode": {
                      "repeat": "none",
                      "shuffle": false,
                      "crossfade": false
                    }
                  },
                  "groupState": {
                    "volume": 10,
                    "mute": false
                  },
                  "avTransportUri": "http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3",
                  "avTransportUriMetadata": ""
                }
              ],
              "uuid": "RINCON_7828CA060F5401400",
              "id": "RINCON_7828CA060F5401400:2557459617"
            },
            {
              "coordinator": {
                "uuid": "RINCON_7828CAEB625E01400",
                "coordinator": "RINCON_7828CAEB625E01400",
                "roomName": "Esszimmer",
                "state": {
                  "volume": 10,
                  "mute": false,
                  "equalizer": {
                    "bass": 7,
                    "treble": 8,
                    "loudness": true
                  },
                  "currentTrack": {
                    "artist": "Antenne Bayern",
                    "albumArtUri": "/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8",
                    "duration": 0,
                    "uri": "x-sonosapi-stream:top40?sid=269&flags=32&sn=8",
                    "trackUri": "x-sonosapi-stream:top40?sid=269&flags=32&sn=8",
                    "type": "radio",
                    "stationName": "Antenne Bayern",
                    "absoluteAlbumArtUri": "http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8"
                  },
                  "nextTrack": {
                    "artist": "",
                    "title": "",
                    "album": "",
                    "albumArtUri": "",
                    "duration": 0,
                    "uri": ""
                  },
                  "trackNo": 1,
                  "elapsedTime": 0,
                  "elapsedTimeFormatted": "00:00:00",
                  "playbackState": "STOPPED",
                  "playMode": {
                    "repeat": "none",
                    "shuffle": false,
                    "crossfade": false
                  }
                },
                "groupState": {
                  "volume": 10,
                  "mute": false
                },
                "avTransportUri": "x-sonosapi-stream:top40?sid=269&flags=32&sn=8",
                "avTransportUriMetadata": "<DIDL-Lite xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\" xmlns:r=\"urn:schemas-rinconnetworks-com:metadata-1-0/\" xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\"><item id=\"-1\" parentID=\"-1\" restricted=\"true\"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id=\"cdudn\" nameSpace=\"urn:schemas-rinconnetworks-com:metadata-1-0/\">SA_RINCON68871_</desc></item></DIDL-Lite>"
              },
              "members": [
                {
                  "uuid": "RINCON_7828CAEB625E01400",
                  "coordinator": "RINCON_7828CAEB625E01400",
                  "roomName": "Esszimmer",
                  "state": {
                    "volume": 10,
                    "mute": false,
                    "equalizer": {
                      "bass": 7,
                      "treble": 8,
                      "loudness": true
                    },
                    "currentTrack": {
                      "artist": "Antenne Bayern",
                      "albumArtUri": "/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8",
                      "duration": 0,
                      "uri": "x-sonosapi-stream:top40?sid=269&flags=32&sn=8",
                      "trackUri": "x-sonosapi-stream:top40?sid=269&flags=32&sn=8",
                      "type": "radio",
                      "stationName": "Antenne Bayern",
                      "absoluteAlbumArtUri": "http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8"
                    },
                    "nextTrack": {
                      "artist": "",
                      "title": "",
                      "album": "",
                      "albumArtUri": "",
                      "duration": 0,
                      "uri": ""
                    },
                    "trackNo": 1,
                    "elapsedTime": 0,
                    "elapsedTimeFormatted": "00:00:00",
                    "playbackState": "STOPPED",
                    "playMode": {
                      "repeat": "none",
                      "shuffle": false,
                      "crossfade": false
                    }
                  },
                  "groupState": {
                    "volume": 10,
                    "mute": false
                  },
                  "avTransportUri": "x-sonosapi-stream:top40?sid=269&flags=32&sn=8",
                  "avTransportUriMetadata": "<DIDL-Lite xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\" xmlns:r=\"urn:schemas-rinconnetworks-com:metadata-1-0/\" xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\"><item id=\"-1\" parentID=\"-1\" restricted=\"true\"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id=\"cdudn\" nameSpace=\"urn:schemas-rinconnetworks-com:metadata-1-0/\">SA_RINCON68871_</desc></item></DIDL-Lite>"
                }
              ],
              "uuid": "RINCON_7828CAEB625E01400",
              "id": "RINCON_7828CAEB625E01400:1640192896"
            }
          ]
        }


# response={'type': 'transport-state', 'data': {'uuid': 'RINCON_7828CAEB625E01400', 'coordinator': 'RINCON_7828CAEB625E01400', 'roomName': 'Esszimmer', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 8, 'loudness': True}, 'currentTrack': {'artist': 'Antenne Bayern', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8', 'duration': 0, 'uri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'trackUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'type': 'radio', 'stationName': 'Antenne Bayern', 'absoluteAlbumArtUri': 'http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON68871_</desc></item></DIDL-Lite>'}}
data =  {
          "type": "transport-state",
          "data": {
            "uuid": "RINCON_7828CAEB625E01400",
            "coordinator": "RINCON_7828CAEB625E01400",
            "roomName": "Esszimmer",
            "state": {
              "volume": 10,
              "mute": false,
              "equalizer": {
                "bass": 7,
                "treble": 8,
                "loudness": true
              },
              "currentTrack": {
                "artist": "Antenne Bayern",
                "albumArtUri": "/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8",
                "duration": 0,
                "uri": "x-sonosapi-stream:top40?sid=269&flags=32&sn=8",
                "trackUri": "x-sonosapi-stream:top40?sid=269&flags=32&sn=8",
                "type": "radio",
                "stationName": "Antenne Bayern",
                "absoluteAlbumArtUri": "http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8"
              },
              "nextTrack": {
                "artist": "",
                "title": "",
                "album": "",
                "albumArtUri": "",
                "duration": 0,
                "uri": ""
              },
              "trackNo": 1,
              "elapsedTime": 0,
              "elapsedTimeFormatted": "00:00:00",
              "playbackState": "STOPPED",
              "playMode": {
                "repeat": "none",
                "shuffle": false,
                "crossfade": false
              }
            },
            "groupState": {
              "volume": 10,
              "mute": false
            },
            "avTransportUri": "x-sonosapi-stream:top40?sid=269&flags=32&sn=8",
            "avTransportUriMetadata": "<DIDL-Lite xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\" xmlns:r=\"urn:schemas-rinconnetworks-com:metadata-1-0/\" xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\"><item id=\"-1\" parentID=\"-1\" restricted=\"true\"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id=\"cdudn\" nameSpace=\"urn:schemas-rinconnetworks-com:metadata-1-0/\">SA_RINCON68871_</desc></item></DIDL-Lite>"
          }
        }   

"""