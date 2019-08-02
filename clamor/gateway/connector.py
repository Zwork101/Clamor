# -*- coding: utf-8 -*-

import logging
import zlib
import platform
import json
from typing import Union

import anysocks
import anysocks.client
import anyio

from .encoding import ENCODERS
from clamor.utils import Emitter
from .opcodes import opcodes
from .exceptions import *

logger = logging.getLogger(__name__)


class DiscordWebsocketClient:
    """Used to connect to the gateway and handle all messages it sends

    Todo: Add sharding/resuming, handle opcodes properly especially dispatch, probably code cleanup

    Parameters
    ----------
    url : str
        URL of the gateway, usually 'wss://gateway.discord.gg'
    encoding : str
        Either 'json' or 'etf'; the encoding used in the connection. Defaults to 'json'
    zlib_compressed : bool
        Indicates if the communication should be compressed. Defaults to false

    Attributes
    ----------
    url : str
        URL of the gateway
    encoder : :class `clamor.gateway.BaseEncoder`:
        Used to en-/decode the messages from the gateway
    zlib_compressed : bool
        Indicates if the communication should be compressed
    _con : :class `anysocks.clientWebSocketConnection`
        Represents the connection to the gateway
    _running : bool
        Indicates the status of the client
    _tg : anyio.TaskGroup
        TaskGroup to be able to stop all tasks
    _interval : int
        Interval at which a heartbeat is sent
    _last_sequence : int
        Used for heartbeating
    _has_ack : bool
        Used to check if the gateway has responded with a heartbeat ACK
    _session_id : int
        Used for resuming
    _token : str
        Is normally carried using function arguments, but resuming requires it to be stored
    """

    VERSION = 6
    ZLIB_SUFFIX = b'\x00\x00\xff\xff'
    TEN_MEGABYTES = 10490000

    def __init__(self, url: str, **kwargs):
        self.url = url
        self.encoder = ENCODERS[kwargs.get('encoding', 'json')]
        self.zlib_compressed = kwargs.get('zlib_compressed', True)
        self.emitter = Emitter()

        # Compression
        if self.zlib_compressed:
            self.buffer = bytearray()
            self.inflator = zlib.decompressobj()

        # Websocket connection
        self._con = None
        self._running = False
        self._tg = None

        # Sharding
        self.shard_id = kwargs.get('shard_id')
        self.shard_count = kwargs.get('shard_count')

        # Heartbeat stuff
        self._interval = 0
        self._last_sequence = None
        self._has_ack = True

        # Resuming
        self._session_id = 0
        self._token = ""

        self.emitter.add_listener(opcodes['HELLO'], self._on_hello)
        self.emitter.add_listener(opcodes['HEARTBEAT_ACK'], self._on_heartbeat_ack)
        self.emitter.add_listener("READY", self._on_ready)

        self.format_url()

    def format_url(self):
        self.url += "/?v={}&encoding={}".format(self.VERSION, self.encoder.TYPE)
        if self.zlib_compressed:
            self.url += "&compress=zlib-stream"

    async def _receive(self):
        message = await self._con.get_message()
        logger.debug("Received message '{}'".format(message))

        if self.zlib_compressed:
            self.buffer.extend(message)
            if self.buffer.endswith(self.ZLIB_SUFFIX):
                message = self.inflator.decompress(self.buffer).decode()
                self.buffer.clear()
        elif message[0] != '{' and message[0] != 131:
            message = zlib.decompress(message, 15, self.TEN_MEGABYTES).decode('utf-8')

        try:
            message = self.encoder.decode(message)
        except Exception as e:
            raise EncodingError(str(e))

        if message.get('s'):
            self._last_sequence = message['s']

        logger.debug("Decoded message to '{}'".format(message))
        return message

    async def _send(self, opcode: Union[int, str], data):
        logger.debug("Sending payload '{}'".format(data))
        if isinstance(opcode, str):
            opcode = opcodes[opcode]
        payload = {
            'op': opcode,
            'd': data
        }
        logger.debug("Encoded payload to '{}'".format(json.dumps(payload)))
        await self._con.send(json.dumps(payload))

    async def _on_hello(self, data):
        self._interval = data["heartbeat_interval"]
        logger.debug("Found heartbeat interval: {}".format(self._interval))
        await self._tg.spawn(self._heartbeat_task)

    async def _on_heartbeat_ack(self, data):
        self._has_ack = True

    async def _on_ready(self, data):
        self._session_id = data["session_id"]

    async def _heartbeat(self):
        """|coro|

        Starts the heartbeat task

        .. warning:: This should only be called internally by the client.
        """
        await anyio.sleep(self._interval / 1000)
        if self._has_ack:
            await self._send(1, self._last_sequence)
            self._has_ack = False
        else:
            logger.error("Gateway hasn't responded with a heartbeat ACK")
            await self.resume()

    async def _receive_task(self):
        """|coro|
        Used to receive all messages from the gateway and handle them

        .. warning:: This should only be called internally by the client.
        """
        while self._running:
            message = await self._receive()
            if message['op'] == 0:
                await self.emitter.emit(message['t'], message['d'])
            else:
                await self.emitter.emit(message['op'], message['d'])

    async def _heartbeat_task(self):
        while self._running:
            await self._heartbeat()

    async def _identify(self):
        identify = {
            'token': self._token,
            'properties': {
                '$os': platform.system(),
                '$browser': 'clamor',
                '$device': 'clamor',
            },
            'compress': True,  # i guess?
            'large_threshold': 250,
        }
        if self.shard_id and self.shard_count:
            identify['shard'] = [self.shard_id, self.shard_count]
        await self._send('IDENTIFY', identify)

    async def on_open(self):
        """
        Called when a websocket connection was established

        .. warning:: This should only be called internally by the client.
        """
        async with anyio.create_task_group() as tg:
            self._tg = tg
            await tg.spawn(self._receive_task)
            await tg.spawn(self._identify)

    async def on_resume(self):
        """
        Called after the resume payload was send

        .. warning:: This should only be called internally by the client.
        """
        async with anyio.create_task_group() as tg:
            self._tg = tg
            await tg.spawn(self._heartbeat_task)
            await tg.spawn(self._receive_task)

    async def connect(self):
        """
        Opens a connection to the gateway and starts receiving

        .. warning:: This should only be called internally by the client.
        """
        async with anysocks.open_connection(self.url) as con:
            self._con = con
            self._running = True
            await self.on_open()

    async def resume(self):
        if self._running:
            await self.close()

        logger.info("Resuming")
        async with anysocks.open_connection(self.url) as con:
            self._con = con
            self._running = True
            payload = {
                'token': self._token,
                'session_id': self._session_id,
                'seq': self._last_sequence
            }
            await self._send('RESUME', payload)
            await self.on_open()

    async def start(self, token: str):
        self._token = token
        await self.connect()

    async def close(self):
        self._running = False
        await self._tg.cancel_scope.cancel()
