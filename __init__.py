#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
#  Copyright 2020-      <AUTHOR>                                  <EMAIL>
#########################################################################
#  This file is part of SmartHomeNG.
#  https://www.smarthomeNG.de
#  https://knx-user-forum.de/forum/supportforen/smarthome-py
#
#  Sample plugin for new plugins to run with SmartHomeNG version 1.8 and
#  upwards.
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
from lib.network import Tcp_server
from .webif import WebInterface

import requests
import json

ZONE_CMDS = [
    'play',
    'pause',
    'playpause',
    'volume',
    'groupVolume',
    'mute',
    'unmute',
    'groupMute',
    'groupUnmute',
    'togglemute',
    'trackseek',
    'timeseek',
    'next',
    'previous',
    'state',
    'favorite',
    'favorites',
    'playlist',
    'repeat',
    'shuffle',
    'crossfade',
    'say',
    'sayall',
    'saypreset',
    'queue',
    'clearqueue',
    'sleep',
    'linein',
    'clip',
    'clipall',
    'clippreset',
    'join',
    'leave',
    'sub',
    'nightmode',
    'speechenhancement',
    'bass',
    'treble']

SYSTEM_CMDS_RO = ['zones',]
SYSTEM_CMDS_RW = ['pauseall', 'resumeall', 'lockvolumes', 'unlockvolumes', 'preset', 'reindex']
SYSTEM_CMDS = SYSTEM_CMDS_RO + SYSTEM_CMDS_RW


class SonosHttp(SmartPlugin):

    PLUGIN_VERSION = '1.0.1'

    def __init__(self, sh):
        """
        Initializes the plugin.

        If you need the sh object at all, use the method self.get_sh() to get it. There should be almost no need for
        a reference to the sh object anymore.

        Plugins have to use the new way of getting parameter values:
        use the SmartPlugin method get_parameter_value(parameter_name). Anywhere within the Plugin you can get
        the configured (and checked) value for a parameter by calling self.get_parameter_value(parameter_name). It
        returns the value in the datatype that is defined in the metadata.
        """

        # Call init code of parent class (SmartPlugin)
        super().__init__()

        # get the parameters for the plugin
        ip = self.get_local_ipv4_address()
        self._http_api_server_ip = self.get_parameter_value('API_Server_IP') if self.get_parameter_value('API_Server_IP') != '0.0.0.0' else self.ip
        self._http_api_server_port = self.get_parameter_value('API_Server_Port')
        port = self.get_parameter_value('WebHook_Server_Port')
        self._pause_item_path = self.get_parameter_value('pause_item')

        # define properties
        self.sonos = {}                             # dict to hold state information per zone
        self.sonos_room_uuid = set()                # set of tuples for [(room1, uuid1), (room2, uuid2), ...]
        self.sonos_topology = {}                    # dict for topology {uuid1 {'coordinator': 'RINCON_', 'members': {'RINCON_7828CAEAC58601400'}}, uuid2....
        self.alive = None

        # Initialize the TCP server
        try:
            self.server = Tcp_server(port=port, host=ip, name='sonos_http', mode=3, terminator=b"\r\n\r\n")
            self.server.set_callbacks(data_received=self.handle_received_data, incoming_connection=self.handle_connection)
        except Exception as e:
            self.logger.warning(f"Server for receiving webhook data could not be set up. Exception {e} occurred.")
            self.server = None
            pass

        # check webinterface
        if not self.init_webinterface(WebInterface):
            self.logger.error("Unable to start Webinterface")
            self._init_complete = False
        else:
            self.logger.debug(f"Init of Plugin {self.get_fullname()} complete")

    def run(self):
        """
        Run method for the plugin
        """
        self.logger.debug(f"{self.get_shortname()}: Run method called")

        # let the plugin change the state of pause_item
        if self._pause_item:
            self._pause_item(False, self.get_fullname())

        if self.server:
            self.server.start()

        # set plugin to alive
        self.alive = True

        # read sonos config
        self.logger.info(f"Initially read Sonos topology and status.")
        self.get_zones()

    def stop(self):
        """
        Stop method for the plugin
        """
        self.logger.debug(f"{self.get_shortname()}: Stop method called")

        # let the plugin change the state of pause_item
        if self._pause_item:
            self._pause_item(True, self.get_fullname())

        if self.server:
            self.server.close()

        self.alive = False
    
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

        def get_sonos_zone(item_level_up: int = 3, attribute: str = 'sonos_zone'):
            _sonos_zone = None
            lookup_item = item
            for i in range(item_level_up):
                if self.has_iattr(lookup_item.conf, attribute):
                    return self.get_iattr_value(lookup_item.conf, attribute)
                else:
                    lookup_item = lookup_item.return_parent()

        # check for pause item
        if item.property.path == self._pause_item_path:
            self.logger.debug(f'pause item {item.property.path} registered')
            self._pause_item = item
            self.add_item(item, updating=True)
            return self.update_item

        if self.has_iattr(item.conf, 'sonos_cmd'):
            sonos_cmd = self.get_iattr_value(item.conf, 'sonos_cmd')
            sonos_zone = get_sonos_zone()

            if sonos_zone:
                self.logger.debug(f'{self.get_fullname()} {item.property.path} registered')
                self.add_item(item, config_data_dict={'sonos_zone': sonos_zone, 'sonos_cmd': sonos_cmd})
                return self.update_item
            elif sonos_cmd in SYSTEM_CMDS:
                self.logger.debug(f'{self.get_fullname()} {item.property.path} registered')
                self.add_item(item, config_data_dict={'sonos_zone': 'system', 'sonos_cmd': sonos_cmd})
                return self.update_item
            else:
                self.logger.warning(f"'{sonos_cmd=} found in {item.property.path} but 'sonos_zone' not defined. Item will be ignored.")

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

        # check for pause item
        if item is self._pause_item and caller != self.get_shortname():
            self.logger.debug(f'pause item changed to {item()}')
            if item() and self.alive:
                self.stop()
            elif not item() and not self.alive:
                self.run()
            return


        if self.alive and caller != self.get_shortname():
            self.logger.info(f"Update item: {item.property.path}, item has been changed outside this plugin")
            self.logger.debug(f"update_item was called with item {item.property.path} with value {item()} from caller {caller}, source {source} and dest {dest}")

            item_config = self.get_item_config(item)
            _sonos_zone = item_config['sonos_zone']
            _sonos_cmd = item_config['sonos_cmd']

            from urllib.parse import quote

            dispatcher = {
                'volume_up': lambda _: f"{_sonos_zone}/volume/+1",
                'volume_down': lambda _: f"{_sonos_zone}/volume/-1",
                'playpause': lambda _: f"{_sonos_zone}/playpause",
                'togglemute': lambda _: f"{_sonos_zone}/togglemute",
                'next': lambda _: f"{_sonos_zone}/next",
                'previous': lambda _: f"{_sonos_zone}/previous",
                'state': lambda _: f"{_sonos_zone}/state",
                'sleep': lambda timeout: f"{_sonos_zone}/sleep/{int(timeout)}" if timeout else f"{_sonos_zone}/sleep",
                'say': lambda _: f"{_sonos_zone}/say/{quote(item())}/de",
            }

            play_pause_dispatcher = {
                'play': ('pause', 'play'),
                'pause': ('play', 'pause'),
                'mute': ('unmute', 'mute'),
                'unmute': ('mute', 'unmute'),
                'groupMute': ('groupUnmute', 'groupMute'),
                'groupUnmute': ('groupMute', 'groupUnmute'),
            }

            system_dispatcher = {
                'pauseall': lambda timeout: f"pauseall/{int(timeout)}" if timeout else "pauseall",
                'resumeall': lambda timeout: f"resumeall/{int(timeout)}" if timeout else "resumeall",
                'preset': lambda item_value: f"preset/{item_value}",
            }

            # Hauptlogik
            if _sonos_cmd in dispatcher:
                request = dispatcher[_sonos_cmd](item() if _sonos_cmd == 'sleep' else None)

            elif _sonos_cmd in play_pause_dispatcher:
                _new_sonos_cmd = play_pause_dispatcher[_sonos_cmd][int(item())]
                request = f"{_sonos_zone}/{_new_sonos_cmd}"

            elif _sonos_cmd in SYSTEM_CMDS_RW:
                if _sonos_cmd in system_dispatcher:
                    request = system_dispatcher[_sonos_cmd](item())
                else:
                    request = _sonos_cmd
            else:
                request = f"{_sonos_zone}/{_sonos_cmd}/{item()}"

            """
            if _sonos_cmd in ['volume_up']:
                request = f"{_sonos_zone}/volume/+1"

            elif _sonos_cmd in ['volume_down']:
                request = f"{_sonos_zone}/volume/-1"

            elif _sonos_cmd in ['play', 'pause', 'mute', 'unmute', 'groupMute', 'groupUnmute']:
                dispatcher = {'play':   ('pause', 'play'),
                              'pause':  ('play', 'pause'),
                              'mute':   ('unmute', 'mute'),
                              'unmute': ('mute', 'unmute')
                              }
                _new_sonos_cmd = dispatcher[_sonos_cmd][int(item())]
                request = f"{_sonos_zone}/{_new_sonos_cmd}"

            elif _sonos_cmd in ['playpause', 'togglemute', 'next', 'previous', 'state']:
                request = f"{_sonos_zone}/{_sonos_cmd}"

            elif _sonos_cmd in ['sleep']:
                timeout = item()
                request = f"{_sonos_zone}/{_sonos_cmd}/{timeout}" if timeout else f"{_sonos_zone}/{_sonos_cmd}"

            elif 'say' in _sonos_cmd:
                request = f"{_sonos_zone}/{_sonos_cmd}/{urlparse.quote(item())}/de"

            elif _sonos_cmd in SYSTEM_CMDS:
                if _sonos_cmd in ['pauseall', 'resumeall']:
                    timeout = item()
                    request = f"{_sonos_cmd}/{timeout}" if timeout else f"{_sonos_cmd}"
                elif _sonos_cmd in ['preset']:
                    request = f"{_sonos_cmd}/{item()}"
                else:
                    request = f"{_sonos_cmd}"

            else:
                request = f"{_sonos_zone}/{_sonos_cmd}/{item()}"
                
            """

            response = self.get_request(request)
            self.logger.debug(f"update_item: {response=}")

    def get_request(self, request):

        self.logger.debug(f"get_request: {request=}")
        url_base = f"http://{self._http_api_server_ip}:{self._http_api_server_port}"
        request = f"{url_base}/{request}"

        try:
            r = requests.get(request, verify=False)
        except Exception as e:
            self.logger.error(f"get_request: {request=} failed with Error {e}")
        else:
            if r.status_code == requests.codes.ok:
                # self.logger.error(f"get_request: request={request} successful")
                response = json.loads(r.text)
                # self.logger.error(f"json={response}")
                # self.logger.debug(f"json={json.dumps(d, indent=4, sort_keys=True)}")
                return response
            else:
                self.logger.error(f"get_request: {request=} failed")
                return

    def handle_connection(self, server, client):
        """
        Handle incoming connection. Just used for debugging

        :param server: Tcp_server object serving the connection
        :type server: lib.network.Tcp_server
        :param client: Client object for connection
        :type client: lib.network.Client
        """
        self.logger.debug(f'Incoming HTTP connection from {client.name}')

    def handle_received_data(self, server, client, data):
        """
        handle received data, strip it and forward message body to parser

        :param server: Tcp_server object serving the connection
        :type server: lib.network.Tcp_server
        :param client: Client object for connection
        :type client: lib.network.Client
        :param data: received data
        :type data: string
        """
        self.logger.debug(f'Received packet from {client.ip}:{client.port} via HTTP with content {data=}')

        # Split the request into headers and body
        headers, body = data.split('\r\n\r\n', 1)

        # Split the headers into individual lines
        headers_lines = headers.split('\r\n')

        # Parse the headers into a dictionary
        headers_dict = {}
        for line in headers_lines[1:]:  # Skip the first line ("POST / HTTP/1.1")
            key, value = line.split(': ', 1)
            headers_dict[key] = value

        # Parse the JSON body
        json_body = json.loads(body)

        self.logger.debug(f"From {headers_dict.get('Host')} received message for content: {json_body}")

        self.parse_webhook_data(json_body)

    def parse_webhook_data(self, json_body):
        """Parse received data and extract content"""

        if json_body['type'] == "transport-state":
            # response={'type': 'transport-state', 'data': {'uuid': 'RINCON_7828CAEB625E01400', 'coordinator': 'RINCON_7828CAEB625E01400', 'roomName': 'Esszimmer', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 8, 'loudness': True}, 'currentTrack': {'artist': 'Antenne Bayern', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8', 'duration': 0, 'uri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'trackUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'type': 'radio', 'stationName': 'Antenne Bayern', 'absoluteAlbumArtUri': 'http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON68871_</desc></item></DIDL-Lite>'}}
            self._decode_zone_state(json_body['data'])

        elif json_body['type'] == "topology-change":
            # response={'type': 'topology-change', 'data': [{'coordinator': {'uuid': 'RINCON_7828CA59548701400', 'coordinator': 'RINCON_7828CA59548701400', 'roomName': 'TV', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 6, 'loudness': True, 'speechEnhancement': True, 'nightMode': False}, 'currentTrack': {'title': 'google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}, 'sub': {'gain': 7, 'crossover': 0, 'polarity': 0, 'enabled': True}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'avTransportUriMetadata': ''}, 'members': [{'uuid': 'RINCON_7828CA59548701400', 'coordinator': 'RINCON_7828CA59548701400', 'roomName': 'TV', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 6, 'loudness': True, 'speechEnhancement': True, 'nightMode': False}, 'currentTrack': {'title': 'google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}, 'sub': {'gain': 7, 'crossover': 0, 'polarity': 0, 'enabled': True}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-808092f232a9736dfa6447c6e12bfa4f27a74993-de.mp3', 'avTransportUriMetadata': ''}], 'uuid': 'RINCON_7828CA59548701400', 'id': 'RINCON_7828CAEB625E01400:1640192871'}, {'coordinator': {'uuid': 'RINCON_7828CAEAC58601400', 'coordinator': 'RINCON_7828CAEAC58601400', 'roomName': 'Büronos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 3, 'loudness': True}, 'currentTrack': {'artist': 'BR Schlager', 'title': 'BR Schlager', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9', 'duration': 0, 'uri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'trackUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'type': 'radio', 'stationName': 'BR Schlager', 'absoluteAlbumArtUri': 'http://192.168.2.123:1400/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>BR Schlager</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON77575_X_#Svc77575-644c3615-Token</desc></item></DIDL-Lite>'}, 'members': [{'uuid': 'RINCON_7828CAEAC58601400', 'coordinator': 'RINCON_7828CAEAC58601400', 'roomName': 'Büronos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 3, 'loudness': True}, 'currentTrack': {'artist': 'BR Schlager', 'title': 'BR Schlager', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9', 'duration': 0, 'uri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'trackUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'type': 'radio', 'stationName': 'BR Schlager', 'absoluteAlbumArtUri': 'http://192.168.2.123:1400/getaa?s=1&u=x-sonosapi-stream%3atunein%253a15544%3fsid%3d303%26flags%3d8224%26sn%3d9'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:tunein%3a15544?sid=303&flags=8224&sn=9', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>BR Schlager</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON77575_X_#Svc77575-644c3615-Token</desc></item></DIDL-Lite>'}], 'uuid': 'RINCON_7828CAEAC58601400', 'id': 'RINCON_7828CAEAC58601400:3457120174'}, {'coordinator': {'uuid': 'RINCON_7828CA060F5401400', 'coordinator': 'RINCON_7828CA060F5401400', 'roomName': 'Carlisonos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 4, 'treble': 4, 'loudness': True}, 'currentTrack': {'title': 'google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'avTransportUriMetadata': ''}, 'members': [{'uuid': 'RINCON_7828CA060F5401400', 'coordinator': 'RINCON_7828CA060F5401400', 'roomName': 'Carlisonos', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 4, 'treble': 4, 'loudness': True}, 'currentTrack': {'title': 'google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'duration': 2, 'uri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'trackUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'type': 'track', 'stationName': '', 'absoluteAlbumArtUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'STOPPED', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'http://192.168.2.12:5005/tts/google-d49ec1435dbe5f9d4e1fc04a3cab8e61749d85be-de.mp3', 'avTransportUriMetadata': ''}], 'uuid': 'RINCON_7828CA060F5401400', 'id': 'RINCON_7828CA060F5401400:2557459617'}, {'coordinator': {'uuid': 'RINCON_7828CAEB625E01400', 'coordinator': 'RINCON_7828CAEB625E01400', 'roomName': 'Esszimmer', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 8, 'loudness': True}, 'currentTrack': {'artist': 'Antenne Bayern', 'title': 'ZPSTR_BUFFERING', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8', 'duration': 0, 'uri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'trackUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'type': 'radio', 'stationName': 'Antenne Bayern', 'absoluteAlbumArtUri': 'http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'TRANSITIONING', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON68871_</desc></item></DIDL-Lite>'}, 'members': [{'uuid': 'RINCON_7828CAEB625E01400', 'coordinator': 'RINCON_7828CAEB625E01400', 'roomName': 'Esszimmer', 'state': {'volume': 10, 'mute': False, 'equalizer': {'bass': 7, 'treble': 8, 'loudness': True}, 'currentTrack': {'artist': 'Antenne Bayern', 'title': 'ZPSTR_BUFFERING', 'albumArtUri': '/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8', 'duration': 0, 'uri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'trackUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'type': 'radio', 'stationName': 'Antenne Bayern', 'absoluteAlbumArtUri': 'http://192.168.2.130:1400/getaa?s=1&u=x-sonosapi-stream%3atop40%3fsid%3d269%26flags%3d32%26sn%3d8'}, 'nextTrack': {'artist': '', 'title': '', 'album': '', 'albumArtUri': '', 'duration': 0, 'uri': ''}, 'trackNo': 1, 'elapsedTime': 0, 'elapsedTimeFormatted': '00:00:00', 'playbackState': 'TRANSITIONING', 'playMode': {'repeat': 'none', 'shuffle': False, 'crossfade': False}}, 'groupState': {'volume': 10, 'mute': False}, 'avTransportUri': 'x-sonosapi-stream:top40?sid=269&flags=32&sn=8', 'avTransportUriMetadata': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="-1" parentID="-1" restricted="true"><dc:title>Antenne Bayern</dc:title><upnp:class>object.item.audioItem.audioBroadcast</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON68871_</desc></item></DIDL-Lite>'}], 'uuid': 'RINCON_7828CAEB625E01400', 'id': 'RINCON_7828CAEB625E01400:1640192896'}]}
            self._decode_zones(json_body['data'])

        elif json_body['type'] == "volume-change":
            # response={'type': 'volume-change', 'data': {'uuid': 'RINCON_7828CAEB625E01400', 'previousVolume': 8, 'newVolume': 8, 'roomName': 'Esszimmer'}}
            zone = json_body['data']['roomName']
            volume = int(json_body['data']['newVolume'])
            self.logger.debug(f"Change Volume of {zone} to {volume}")
            self.update_item_value(zone, 'volume', volume)

        elif json_body['type'] == "mute-change":
            # response={'type': 'mute-change', 'data': {'uuid': 'RINCON_7828CAEB625E01400', 'previousMute': True, 'newMute': True, 'roomName': 'Esszimmer'}}
            zone = json_body['data']['roomName']
            mute = bool(json_body['data']['newMute'])
            self.logger.debug(f"Change Mute of {zone} to {mute}")
            self.update_item_value(zone, 'mute', mute)
            self.update_item_value(zone, 'mutetoggle', mute)
            self.update_item_value(zone, 'unmute', not mute)

    def update_item_value(self, zone, cmd, value):
        """Update item value if zone, cmd and value is given"""
        self.logger.debug(f"{zone=}, {cmd=}, {value=}")
        for item in self._get_item_list_for_zone_and_cmd(zone, cmd):
            self.logger.debug(f"{item.path()}")
            item(value, self.get_fullname(), 'update_item_value')

    def update_items_value(self, zone):
        """updates all item values for given zone"""

        for item in self._get_item_list_for_zone(zone):
            _sonos_cmd = self.get_item_config(item)['sonos_cmd']
            _value = None

            if not zone in self.sonos:
                return

            if not 'state' in self.sonos[zone]:
                return

            sonos_zone_state = self.sonos[zone]['state']

            if _sonos_cmd.startswith('current_'):
                current_track = sonos_zone_state.get('currentTrack')
                if current_track:
                    cmd = _sonos_cmd.split('_')[1]
                    try:
                        _value = current_track[cmd]
                    except:
                        pass
            elif _sonos_cmd.startswith('next_'):
                next_track = sonos_zone_state.get('nextTrack')
                if next_track:
                    cmd = _sonos_cmd.split('_')[1]
                    try:
                        _value = next_track[cmd]
                    except:
                        pass
            elif _sonos_cmd in ['play', 'playpause']:
                _value = True if sonos_zone_state.get('playbackState') == 'PLAYING' else False
            elif _sonos_cmd in ['pause']:
                _value = True if sonos_zone_state.get('playbackState') == 'STOPPED' else False
            elif _sonos_cmd in ['mute', 'togglemute']:
                _value = sonos_zone_state.get('mute', False)
            elif _sonos_cmd in ['unmute']:
                _value = not sonos_zone_state.get('mute', False)
            else:
                _value = self._recursive_lookup(_sonos_cmd, sonos_zone_state)

            if _value is not None:
                item(_value, self.get_fullname(), 'update_items_value')

        for item in self._get_item_list_for_zone('system'):
            _sonos_cmd = self.get_item_config(item)['sonos_cmd']
            _value = None

            if _sonos_cmd == 'zones':
                _value = list(self.sonos.keys())

            if _value is not None:
                item(_value, self.get_fullname(), 'update_items_value')

    def get_zones(self):
        return self._decode_zones(self.get_request('zones'))

    def get_zone(self, zone):
        return self._decode_zone_state(self.get_request(f"{zone}/state"))

    def is_zone_playing(self, zone):
        zone_state = self.get_zone(zone)
        return True if zone_state.get('playbackState') == 'PLAYING' else False

    def _recursive_lookup(self, k, d):
        """ """
        if k in d: return d[k]
        for v in d.values():
            if isinstance(v, dict):
                a = self._recursive_lookup(k, v)
                if a is not None: return a
        return None

    def _decode_zones(self, zones: list[dict]):

        if not zones:
            return

        self.logger.debug(f"{zones=}")

        # get all rooms and uuids
        for zone in zones:

            uuid = zone['uuid']
            if uuid not in self.sonos_topology:
                self.sonos_topology[uuid] = {}

            self.sonos_topology[uuid]['coordinator'] = zone['coordinator']['uuid']
            if 'members' not in self.sonos_topology[uuid]:
                self.sonos_topology[uuid]['members'] = set()

            members = zone['members']
            for member in members:
                self.sonos_room_uuid.update([(member['roomName'], member['uuid'])])
                self.sonos_topology[uuid]['members'].update([(member['uuid'])])

            # decode state
            self._decode_zone_state(zone['coordinator'])

        return self.sonos

    def _decode_zone_state(self, data):

        self.logger.debug(f"{data}")

        zone = data.get('roomName', None)

        if zone not in self.sonos:
            self.sonos[zone] = {}

        self.sonos[zone]['uuid'] = data.get('uuid')
        self.sonos[zone]['coordinator'] = data.get('coordinator')
        self.sonos[zone]['state'] = data.get('state')
        self.sonos[zone]['groupstate'] = data.get('groupState')

        self.update_items_value(zone)

        return self.sonos[zone]

    def _get_item_list_for_zone_and_cmd(self, zone: str, cmd: str):
        return list(set(self._get_item_list_for_zone(zone)) & set(self._get_item_list_for_cmd(cmd)))

    def _get_item_list_for_zone(self, zone):
        return self.get_item_list('sonos_zone', zone)

    def _get_item_list_for_cmd(self, cmd):
        return self.get_item_list('sonos_cmd', cmd)
