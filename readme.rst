SONOS HTTP
==========

Anforderungen
-------------
Zur Verwendung des Plugins ist eine funktionierende Installation des SONOS HTTP API von https://github.com/jishi/node-sonos-http-api notwendig.
Diese kann direkt auf einem RPI laufen oder in einem Docker-Container auf einer NAS.
Zudem ist Node notwendig, mindestens in Version 6.

Installation auf einem RPi
--------------------------

Vorbedingungen
^^^^^^^^^^^^^^

Sicherstellen, das die node und npm installiert sind.

.. code-block:: console

    node -v
    npm -v


Installation
^^^^^^^^^^^^

.. code-block:: console

    cd opt
    sudo git clone https://github.com/jishi/node-sonos-http-api.git
    cd node-sonos-http-api
    sudo npm install --production
    sudo npm start

Check
^^^^^

Nun im Browser öffnen, um die Funktionsfähig der Installation zu prüfen.
Unter der Annahme, dass eine Sonos Zone oder ein Sonos Gerät den Namen 'Küche' trägt:

.. code-block:: console

    http://IP_of_RPi:5005/Küche/state

Sollte im Browser etwas wie:

.. code-block:: json

    {"volume":13,"mute":false,"equalizer":{"bass":7,"treble":9,"loudness":true},"currentTrack":{"artist":"Cork's 96fm","title":"x-sonosapi-stream:s15078?sid=254","albumArtUri":"/getaa?s=1&u=x-sonosapi-stream%3as15078%3fsid%3d254","duration":0,"uri":"x-sonosapi-stream:s15078?sid=254","trackUri":"x-sonosapi-stream:s15078?sid=254","type":"radio","stationName":"Cork's 96fm","absoluteAlbumArtUri":"http://192.168.1.63:1400/getaa?s=1&u=x-sonosapi-stream%3as15078%3fsid%3d254"},"nextTrack":{"artist":"","title":"","album":"","albumArtUri":"","duration":0,"uri":""},"trackNo":1,"elapsedTime":25197,"elapsedTimeFormatted":"06:59:57","playbackState":"PLAYING","playMode":{"repeat":"none","shuffle":false,"crossfade":false}}

zu sehen sein.


Einrichtung als Dienst
^^^^^^^^^^^^^^^^^^^^^^
Um die Sonos-http-api als Dienst einzurichten, sind folgenden Schitte nötig:

Datei sonosapi.service anlegen mit

.. code-block:: console

  sudo nano /etc/systemd/system/sonosapi.service

und mit folgendem Inhalt füllen:

.. code-block:: yaml

        [Unit]
        Description=Sonos HTTP API Daemon
        After=syslog.target
        After=network.target

        [Service]
        Type=simple
        ExecStart=/usr/bin/node /home/smarthome/node-sonos-http-api/server.js
        Restart=always
        RestartSec=10

        [Install]
        WantedBy=default.target

Dienst starten mit:

.. code-block:: console

    sudo systemctl start sonosapi.service


Dienst für Startup konfigurieren mit:

.. code-block:: console

    sudo systemctl enable sonosapi.service


Installation als Docker Container auf einer (QNAP)-NAS
------------------------------------------------------

Das Docker Image ist hier zur finden: https://github.com/chrisns/docker-node-sonos-http-api

Erzeugen der lokalen Verzeichnisse und settings.json

.. code-block:: console

    mkdir clips
    mkdir cache
    mkdir presets
    curl https://raw.githubusercontent.com/jishi/node-sonos-http-api/master/presets/example.json > presets/example.json
    echo {} > settings.json

Start des Docker Image:

.. code-block:: console

    docker run \
      --net=host \
      --name sonos \
      --restart=always \
      -d \
      -v `pwd`/settings.json:/app/settings.json \
      -v `pwd`/clips:/app/static/clips \
      -v `pwd`/cache:/app/cache \
      -v `pwd`/presets:/app/presets \
      chrisns/docker-node-sonos-http-api

    
Konfiguration
-------------

plugin.yaml
^^^^^^^^^^^

Bitte die Dokumentation lesen, die aus den Metadaten der plugin.yaml erzeugt wurde.


items.yaml
^^^^^^^^^^

Bitte die Dokumentation lesen, die aus den Metadaten der plugin.yaml erzeugt wurde.


logic.yaml
^^^^^^^^^^

Bitte die Dokumentation lesen, die aus den Metadaten der plugin.yaml erzeugt wurde.


Funktionen
^^^^^^^^^^

Bitte die Dokumentation lesen, die aus den Metadaten der plugin.yaml erzeugt wurde.


Beispiele
---------

Hier können ausführlichere Beispiele und Anwendungsfälle beschrieben werden.


Web Interface
-------------

Ein Klick auf das auf "GUI/DOCU" in der Kopftabelle öffnet die Startseite der Sonos-HTTP-API.
Zudem besteht die Möglichkeit mit dem Button "Update Zones" die Konfiguration des Sonos Systems auszulesen.

Sonos Items
^^^^^^^^^^^

Das Webinterface zeigt die Items an, für die ein Sonos_http-Attribut konfiguriert ist.

.. image:: user_doc/assets/webif_tab1.jpg
   :class: screenshot


Sonos Maintenance
^^^^^^^^^^^^^^^^^

Das Webinterface zeigt detaillierte Informationen über die im Plugin verfügbaren Daten an.
Dies dient der Maintenance bzw. Fehlersuche.

.. image:: user_doc/assets/webif_tab2.jpg
   :class: screenshot


