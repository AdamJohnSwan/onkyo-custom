"""Support for Onkyo Receivers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal
import voluptuous as vol

import socket
import netifaces

import time
import struct
import re

import argparse

from dataclasses import dataclass

from collections import namedtuple

from homeassistant.components.media_player import (
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    PLATFORM_SCHEMA as MEDIA_PLAYER_PLATFORM_SCHEMA,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_NAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util.hass_dict import HassKey

class ISCPMessage(object):
    """Deals with formatting and parsing data wrapped in an ISCP
    containers. The docs say:
        ISCP (Integra Serial Control Protocol) consists of three
        command characters and parameter character(s) of variable
        length.
    It seems this was the original protocol used for communicating
    via a serial cable.
    """

    def __init__(self, data):
        self.data = data

    def __str__(self):
        # ! = start character
        # 1 = destination unit type, 1 means receiver
        # End character may be CR, LF or CR+LF, according to doc
        return "!1{}\r".format(self.data)

    @classmethod
    def parse(cls, data):
        EOF = "\x1a"
        TERMINATORS = ["\n", "\r"]
        assert data[:2] == "!1"
        eof_offset = -1
        # EOF can be followed by CR/LF/CR+LF
        if data[eof_offset] in TERMINATORS:
            eof_offset -= 1
            if data[eof_offset] in TERMINATORS:
                eof_offset -= 1
        assert data[eof_offset] == EOF
        return data[2:eof_offset]


class eISCPPacket(object):
    """For communicating over Ethernet, traditional ISCP messages are
    wrapped inside an eISCP package.
    """

    header = namedtuple("header", ("magic, header_size, data_size, version, reserved"))

    def __init__(self, iscp_message):
        iscp_message = str(iscp_message)
        # We attach data separately, because Python's struct module does
        # not support variable length strings,
        header = struct.pack(
            "! 4s I I b 3s",
            b"ISCP",  # magic
            16,  # header size (16 bytes)
            len(iscp_message),  # data size
            0x01,  # version
            b"\x00\x00\x00",  # reserved
        )

        self._bytes = header + iscp_message.encode("utf-8")
        # __new__, string subclass?

    def __str__(self):
        return self._bytes.decode("utf-8")

    def get_raw(self):
        return self._bytes

    @classmethod
    def parse(cls, bytes):
        """Parse the eISCP package given by ``bytes``.
        """
        h = cls.parse_header(bytes[:16])
        data = bytes[h.header_size : h.header_size + h.data_size].decode(errors='replace')
        assert len(data) == h.data_size
        return data

    @classmethod
    def parse_header(cls, bytes):
        """Parse the header of an eISCP package.
        This is useful when reading data in a streaming fashion,
        because you can subsequently know the number of bytes to
        expect in the packet.
        """
        # A header is always 16 bytes in length
        assert len(bytes) == 16

        # Parse the header
        magic, header_size, data_size, version, reserved = struct.unpack(
            "! 4s I I b 3s", bytes
        )

        magic = magic.decode()
        reserved = reserved.decode()

        # Strangly, the header contains a header_size field.
        assert magic == "ISCP"
        assert header_size == 16

        return eISCPPacket.header(magic, header_size, data_size, version, reserved)

    @classmethod
    def parse_info(cls, bytes):
        response = cls.parse(bytes)
        # Return string looks something like this:
        # !1ECNTX-NR609/60128/DX
        info = re.match(r'''
            !
            (?P<device_category>\d)
            ECN
            (?P<model_name>[^/]*)/
            (?P<iscp_port>\d{5})/
            (?P<area_code>\w{2})/
            (?P<identifier>.{0,12})
        ''', response.strip(), re.VERBOSE)

        if info:
            return info.groupdict()


def command_to_packet(command):
    """Convert an ascii command like (PVR00) to the binary data we
    need to send to the receiver.
    """
    return eISCPPacket(ISCPMessage(command)).get_raw()


def normalize_command(command):
    """Ensures that various ways to refer to a command can be used."""
    command = command.lower()
    command = command.replace("_", " ")
    command = command.replace("-", " ")
    return command


def command_to_iscp(command, arguments=None, zone=None):
    """Transform the given given high-level command to a
    low-level ISCP message.
    Raises :class:`ValueError` if `command` is not valid.
    This exposes a system of human-readable, "pretty"
    commands, which is organized into three parts: the zone, the
    command, and arguments. For example::
        command('power', 'on')
        command('power', 'on', zone='main')
        command('volume', 66, zone='zone2')
    As you can see, if no zone is given, the main zone is assumed.
    Instead of passing three different parameters, you may put the
    whole thing in a single string, which is helpful when taking
    input from users::
        command('power on')
        command('zone2 volume 66')
    To further simplify things, for example when taking user input
    from a command line, where whitespace needs escaping, the
    following is also supported:
        command('power=on')
        command('zone2.volume=66')
    """
    default_zone = "main"
    command_sep = r"[. ]"
    norm = lambda s: s.strip().lower()

    # If parts are not explicitly given, parse the command
    if arguments is None and zone is None:
        # Separating command and args with colon allows multiple args
        if ":" in command or "=" in command:
            base, arguments = re.split(r"[:=]", command, 1)
            parts = [norm(c) for c in re.split(command_sep, base)]
            if len(parts) == 2:
                zone, command = parts
            else:
                zone = default_zone
                command = parts[0]
            # Split arguments by comma or space
            arguments = [norm(a) for a in re.split(r"[ ,]", arguments)]
        else:
            # Split command part by space or dot
            parts = [norm(c) for c in re.split(command_sep, command)]
            if len(parts) >= 3:
                zone, command = parts[:2]
                arguments = parts[3:]
            elif len(parts) == 2:
                zone = default_zone
                command = parts[0]
                arguments = parts[1:]
            else:
                raise ValueError("Need at least command and argument")

    # Find the command in our database, resolve to internal eISCP command
    group = ZONE_MAPPINGS.get(zone, zone)
    if not zone in COMMANDS:
        raise ValueError('"{}" is not a valid zone'.format(zone))

    prefix = COMMAND_MAPPINGS[group].get(command, command)
    if not prefix in COMMANDS[group]:
        raise ValueError(
            '"{}" is not a valid command in zone "{}"'.format(command, zone)
        )

    # Resolve the argument to the command. This is a bit more involved,
    # because some commands support ranges (volume) or patterns
    # (setting tuning frequency). In some cases, we might imagine
    # providing the user an API with multiple arguments (TODO: not
    # currently supported).
    argument = arguments[0]

    # 1. Consider if there is a alias, e.g. level-up for UP.
    try:
        value = VALUE_MAPPINGS[group][prefix][argument]
    except KeyError:
        # 2. See if we can match a range or pattern
        for possible_arg in VALUE_MAPPINGS[group][prefix]:
            if argument.isdigit():
                if isinstance(possible_arg, ValueRange):
                    if int(argument) in possible_arg:
                        # We need to send the format "FF", hex() gives us 0xff
                        value = hex(int(argument))[2:].zfill(2).upper()
                        break

            # TODO: patterns not yet supported
        else:
            raise ValueError(
                '"{}" is not a valid argument for command '
                '"{}" in zone "{}"'.format(argument, command, zone)
            )

    return "{}{}".format(prefix, value)


def iscp_to_command(iscp_message):
    for zone, zone_cmds in COMMANDS.items():
        # For now, ISCP commands are always three characters, which
        # makes this easy.
        command, args = iscp_message[:3], iscp_message[3:]
        if command in zone_cmds:
            if args in zone_cmds[command]["values"]:
                if "," in zone_cmds[command]["values"][args]["name"]:
                    value = tuple(zone_cmds[command]["values"][args]["name"].split(","))
                else:
                    value = zone_cmds[command]["values"][args]["name"]

                return (
                    zone,
                    zone_cmds[command]["name"],
                    value
                )
            else:
                match = re.match("[+-]?[0-9a-f]+$", args, re.IGNORECASE)
                if match:
                    return zone, zone_cmds[command]["name"], int(args, 16)
                else:
                    if "," in args:
                        value = tuple(args.split(","))
                    else:
                        value = args

                    return zone, zone_cmds[command]["name"], value

    else:
        raise ValueError(
            "Cannot convert ISCP message to command: {}".format(iscp_message)
        )


# pylint: disable=too-many-instance-attributes, too-many-public-methods
class AVR(asyncio.Protocol):
    """The Anthem AVR IP control protocol handler."""

    def __init__(self,
        update_callback=None,
        connect_callback=None,
        loop=None,
        connection_lost_callback=None,
    ):
        """Protocol handler that handles all status and changes on AVR.

        This class is expected to be wrapped inside a Connection class object
        which will maintain the socket and handle auto-reconnects.

            :param update_callback:
                called if any state information changes in device (optional)
            :param connection_lost_callback:
                called when connection is lost to device (optional)
            :param loop:
                asyncio event loop (optional)

            :type update_callback:
                callable
            :type: connection_lost_callback:
                callable
            :type loop:
                asyncio.loop
        """
        self._loop = loop
        self.log = logging.getLogger(__name__)
        self._connection_lost_callback = connection_lost_callback
        self._update_callback = update_callback
        self._connect_callback = connect_callback
        self.buffer = b""
        self._input_names = {}
        self._input_numbers = {}
        self.transport = None

    def command(self, command, arguments=None, zone=None):
        """Issue a formatted command to the device.

        This function sends a message to the device without waiting for a response.

            :param command: Any command as documented in the readme
            :param arguments: The value to send with the command
            :param zone: One of dock, main, zone1, zone2, zone3, zone4
            :type command: str
            :type arguments: str
            :type zone: str

        :Example:

        >>> command(volume, 55, main)
        or
        >>> command(main.volume=55)
        """
        try:
            iscp_message = command_to_iscp(command, arguments, zone)
        except ValueError as error:
            self.log.error(f"Invalid message. {error}")
            return

        self.log.debug("> %s", command)
        try:
            self.transport.write(command_to_packet(iscp_message))
        except:
            self.log.warning("No transport found, unable to send command")

    #
    # asyncio network functions
    #

    def connection_made(self, transport):
        """Called when asyncio.Protocol establishes the network connection."""
        self.transport = transport

        if self._connect_callback:
            self._loop.call_soon(self._connect_callback)

        # self.transport.set_write_buffer_limits(0)
        limit_low, limit_high = self.transport.get_write_buffer_limits()
        self.log.debug("Write buffer limits %d to %d", limit_low, limit_high)

    def data_received(self, data):
        """Called when asyncio.Protocol detects received data from network."""
        self.buffer += data
        self.log.debug("Received %d bytes from AVR: %s", len(self.buffer), self.buffer)
        self._assemble_buffer()

    def connection_lost(self, exc):
        """Called when asyncio.Protocol loses the network connection."""
        if exc is not None:
            self.log.warning("Lost connection to receiver: %s", exc)

        self.transport = None

        if self._connection_lost_callback:
            self._loop.call_soon(self._connection_lost_callback)

    def _assemble_buffer(self):
        """Data for a command may not arrive all in one go.
        First read the header to determin the total command size, then wait
        until we have that much data before decoding it.
        """
        self.transport.pause_reading()

        if len(self.buffer) >= 16:
            size = eISCPPacket.parse_header(self.buffer[:16]).data_size
            if len(self.buffer) - 16 >= size:
                data = self.buffer[16 : 16 + size]
                try:
                    message = iscp_to_command(ISCPMessage.parse(data.decode()))
                    if self._update_callback:
                        self._loop.call_soon(self._update_callback, message)
                except:
                    self.log.debug("Unable to parse recieved message: %s", data.decode('utf-8', 'backslashreplace').rstrip())

                self.buffer = self.buffer[16 + size :]  # shift data to start
                # If there is still data in the buffer,
                # don't wait for more, process it now!
                if len(self.buffer):
                    self._assemble_buffer()

        self.transport.resume_reading()
        return


@dataclass
class Receiver:
    """Onkyo receiver."""

    conn: Connection
    model_name: str
    identifier: str
    name: str
    discovered: bool


@dataclass
class ReceiverInfo:
    """Onkyo receiver information."""

    host: str
    port: int
    model_name: str
    identifier: str


class DiscoveryProtocol(asyncio.DatagramProtocol):

    def __init__(self,
        target,
        discovered_callback=None,
        loop=None,
    ):
        """Protocol handler that handles AVR discovery by broadcasting a discovery packet.

            :param target:
                the target (host, port) to broadcast the discovery packet over
            :param discovered_callback:
                called when a device has been discovered (optional)
            :param loop:
                asyncio event loop (optional)

            :type target:
                tuple
            :type: discovered_callback:
                coroutine
            :type loop:
                asyncio.loop
        """
        self.log = logging.getLogger(__name__)
        self._target = target
        self._discovered_callback = discovered_callback
        self._loop = loop

        self.discovered = []
        self.transport = None

    def connection_made(self, transport):
        """Discovery connection created, broadcast discovery packet."""
        self.transport = transport
        self.broadcast_discovery_packet()

    def datagram_received(self, data, addr):
        """Received response from device."""
        info = eISCPPacket.parse_info(data)
        if info and info['identifier'] not in self.discovered:
            self.log.info(f"{info['model_name']} discovered at {addr}")
            self.discovered.append(info['identifier'])
            if self._discovered_callback:
                ensure_future(self._discovered_callback(addr[0], int(info['iscp_port']), info['model_name'], info['identifier']))

    def broadcast_discovery_packet(self):
        """Broadcast discovery packets over the target."""
        self.log.debug(f"Broadcast discovery packet to {self._target}")
        self.transport.sendto(eISCPPacket('!xECNQSTN').get_raw(), self._target)
        self.transport.sendto(eISCPPacket('!pECNQSTN').get_raw(), self._target)

    def close(self):
        """Close the discovery connection."""
        self.log.debug("Closing broadcast discovery connection")
        if self.transport:
            self.transport.close()

    async def async_close_delayed(self, delay):
        """Close the discovery connection after a certain delay."""
        await asyncio.sleep(delay)
        self.close()


class Connection:
    """Connection handler to maintain network connection for AVR Protocol."""

    def __init__(self):
        """Instantiate the Connection object."""
        self.log = logging.getLogger(__name__)

    @classmethod
    async def create(
        cls,
        host="localhost",
        port=60128,
        auto_reconnect=True,
        max_retry_interval=300,
        loop=None,
        protocol_class=AVR,
        update_callback=None,
        connect_callback=None,
        disconnect_callback=None,
        auto_connect=True
    ):
        """Initiate a connection to a specific device.

        Here is where we supply the host and port and callback callables we
        expect for this AVR class object.

        :param host:
            Hostname or IP address of the device
        :param port:
            TCP port number of the device
        :param auto_reconnect:
            Should the Connection try to automatically reconnect if needed?
        :param max_retry_interval:
            Maximum time between reconnects when auto reconnect is enabled
        :param loop:
            asyncio.loop for async operation
        :param update_callback
            This function is called whenever AVR state data changes
        :param connect_callback
            This function is called when the connection with the AVR is established
        :param disconnect_callback
            This function is called when the connection with the AVR is lost
        :param auto_connect
            Should the Connection try to automatically connect?

        :type host:
            str
        :type port:
            int
        :type auto_reconnect:
            boolean
        :param max_retry_interval:
            int
        :type loop:
            asyncio.loop
        :type update_callback:
            callable
        :type connect_callback:
            callable
        :param disconnect_callback
            callable
        :type auto_connect:
            boolean
        """
        assert port >= 0, "Invalid port value: %r" % (port)
        conn = cls()

        conn.host = host
        conn.port = port
        conn._loop = loop or asyncio.get_event_loop()
        conn._retry_interval = 1
        conn._closed = False
        conn._closing = False
        conn._halted = False
        conn._auto_reconnect = auto_reconnect
        conn._max_retry_interval = max_retry_interval
        conn._disconnect_callback = disconnect_callback
        conn._unexpected_disconnect = False

        def _disconnected_callback():
            """Function callback for Protocol class when connection is lost."""
            if conn._auto_reconnect and not conn._closing:
                # Don't call the disconnect callback, but try to reconnect first.
                conn._unexpected_disconnect = True
                ensure_future(conn._reconnect(), loop=conn._loop)

            elif disconnect_callback:
                # No auto reconnect, so call the disconnect callback directly.
                conn._loop.call_soon(disconnect_callback, conn.host)

        def _update_callback(message):
            """Function callback for Protocol class when the AVR sends updates."""
            if update_callback:
                conn._loop.call_soon(update_callback, message, conn.host)

        def _connect_callback():
            """Function callback for Protocoal class when connection is established."""
            conn._unexpected_disconnect = False
            if connect_callback:
                conn._loop.call_soon(connect_callback, conn.host)

        conn.protocol = protocol_class(
            loop=conn._loop,
            update_callback=_update_callback,
            connect_callback=_connect_callback,
            connection_lost_callback=_disconnected_callback,
        )

        if auto_connect:
            await conn._reconnect()

        return conn

    @classmethod
    async def discover(
        cls,
        host=None,
        port=60128,
        auto_reconnect=True,
        max_retry_interval=300,
        loop=None,
        protocol_class=AVR,
        update_callback=None,
        connect_callback=None,
        discovery_callback=None,
        disconnect_callback=None,
        timeout = 5
    ):
        """Discover Onkyo or Pioneer Network Receivers on the network.

        Here we discover devices on the available networks and for every
        discovered device, a Connection object is returned through the
        discovery callback coroutine. The connection is not yet established,
        this should be done manually by calling connect on the Connection

        :param host:
            If specified, a direct connection is made to discover the AVR.
            Else, the available broadcast addresses are used to disvoer AVRs.
        :param port:
            TCP port number of the device
        :param auto_reconnect:
            Should the Connection try to automatically reconnect if needed?
        :param max_retry_interval:
            Maximum time between reconnects when auto reconnect is enabled
        :param loop:
            asyncio.loop for async operation
        :param update_callback
            This function is called whenever discovered devices state data change
        :param connect_callback
            This function is called when the connection with discovered devices is established
        :param discovery_callback
            This function is called when a device has been discovered on the network
        :param disconnect_callback
            This function is called when the connection with the AVR is lost
        :param timeout
            Number of seconds to detect devices

        :type host:
            str
        :type port:
            int
        :type auto_reconnect:
            boolean
        :type max_retry_interval:
            int
        :type loop:
            asyncio.loop
        :type update_callback:
            callable
        :type connect_callback:
            callable
        :type discovery_callback:
            coroutine
        :param disconnect_callback
            coroutine
        :type timeout
            int
        """
        assert port >= 0, "Invalid port value: %r" % (port)

        _loop = loop or asyncio.get_event_loop()

        async def discovered_callback(discovered_host, port, name, identifier):
            """Async function callback for Discovery Protocol when an AVR is discovered"""
            # Create a Connection, but do not auto connect
            conn = await cls.create(
                host=discovered_host,
                port=port,
                auto_reconnect=auto_reconnect,
                max_retry_interval=max_retry_interval,
                loop=_loop,
                protocol_class=protocol_class,
                update_callback=update_callback,
                connect_callback=connect_callback,
                disconnect_callback=disconnect_callback,
                auto_connect=False
            )

            # Pass the created Connection to the discovery callback
            conn.name = name
            conn.identifier = identifier
            if discovery_callback:
                ensure_future(discovery_callback(conn))

        # Iterate over all network interfaces to find broadcast addresses
        ifaddrs = [
            ifaddr
            for interface in netifaces.interfaces()
            for ifaddr in netifaces.ifaddresses(interface).get(netifaces.AF_INET, [])
        ]

        for ifaddr in ifaddrs:
            if "addr" in ifaddr:
                if host:
                    # Set target to specified host
                    target = (host, port)
                elif "broadcast" in ifaddr:
                    # Use the broadcast address to send the discovery packets
                    target = (ifaddr["broadcast"], port)
                else:
                    # No host provided and no broadcast address available, so skip
                    continue

                try:
                    protocol = DiscoveryProtocol(
                        target=target,
                        discovered_callback=discovered_callback,
                        loop=_loop,
                    )

                    await _loop.create_datagram_endpoint(
                        lambda: protocol,
                        local_addr=(ifaddr["addr"], 0),
                        allow_broadcast=True,
                    )
                    # Close the DiscoveryProtocol connections after timeout seconds
                    ensure_future(protocol.async_close_delayed(timeout))
                except PermissionError:
                    continue

    def update_property(self, zone, propname, value):
        """Format an update message and send to the receiver."""
        self.send(f"{zone}.{propname}={value}")

    def query_property(self, zone, propname):
        """Format a query message and send to the receiver."""
        self.send(f"{zone}.{propname}=query")

    def send(self, msg):
        """Fire and forget data to the reciever."""
        self.protocol.command(msg)

    def _get_retry_interval(self):
        return self._retry_interval

    def _reset_retry_interval(self):
        self._retry_interval = 1

    def _increase_retry_interval(self):
        self._retry_interval = min(self._max_retry_interval, 1.5 * self._retry_interval)

    async def _reconnect(self):
        while True:
            try:
                if self._halted:
                    await asyncio.sleep(2, loop=self._loop)
                else:
                    self.log.debug(
                        "Connecting to Network Receiver at %s:%d", self.host, self.port
                    )
                    await self._loop.create_connection(
                        lambda: self.protocol, self.host, self.port
                    )
                    self._reset_retry_interval()
                    return

            except OSError:
                if self._unexpected_disconnect:
                    # Reconnect started by a disconnect and connecting failed again,
                    # so call the disconnect callback if there is one.
                    # Also clear the unexpected disconnect flag to make sure we only
                    # call the disconnect callback once for this disconnect.
                    self._unexpected_disconnect = False
                    if self._disconnect_callback:
                        self._loop.call_soon(self._disconnect_callback, self.host)

                self._increase_retry_interval()
                interval = self._get_retry_interval()
                self.log.debug("Connecting failed, retrying in %i seconds", interval)
                await asyncio.sleep(interval, loop=self._loop)

    async def connect(self):
        """Establish the AVR device connection"""
        if not self.protocol.transport:
            await self._reconnect()

    def close(self):
        """Close the AVR device connection and don't try to reconnect."""
        self.log.info("Closing connection to Network Receiver")
        self._closing = True
        if self.protocol.transport:
            self.protocol.transport.close()

    def halt(self):
        """Close the AVR device connection and wait for a resume() request."""
        self.log.info("Halting connection to Network Receiver")
        self._halted = True
        if self.protocol.transport:
            self.protocol.transport.close()

    def resume(self):
        """Resume the AVR device connection if we have been halted."""
        self.log.info("Resuming connection to Network Receiver")
        self._halted = False

    @property
    def dump_conndata(self):
        """Developer tool for debugging forensics."""
        attrs = vars(self)
        return ", ".join("%s: %s" % item for item in attrs.items())


COMMANDS = OrderedDict([('main', OrderedDict([('PWR', {'values': OrderedDict([('00', {'name': ('standby',
      'off'),
     'description': 'sets System Standby'}),
    ('01', {'name': 'on', 'description': 'sets System On'}),
    ('ALL', {'name': 'standby-all',
     'description': 'All Zone(including Main Zone) Standby'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the System Power Status'})]),
   'name': 'system-power',
   'description': 'System Power Command'}),
  ('AMT', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Audio Muting Off'}),
    ('01', {'name': 'on', 'description': 'sets Audio Muting On'}),
    ('TG', {'name': 'toggle', 'description': 'sets Audio Muting Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Audio Muting State'})]),
   'name': 'audio-muting',
   'description': 'Audio Muting Command'}),
  ('CMT', {'values': OrderedDict([('aabbccddeeffgghhiijjkkllmm', {'name': 'aabbccddeeffgghhiijjkkllmm',
     'description': 'sets Audio Muting by Channel\nxx=00 Muting Off\nxx=01 Muting On\nxx=TG Muting Wrap-Around\nfor not exist channel is always 00.\n\naa:Front Left\nbb:Front Right\ncc:Center\ndd:Surround Left\nee:Surround Right\nff:Surround Back Left\ngg:Surround Back Right\nhh:Subwoofer 1\nii:Height 1 Left\njj:Height 1 Right\nkk:Height 2 Left\nll:Height2 Right\nmm:Subwoofer 2'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Audio Muting State'})]),
   'name': 'audio-muting-by-channel',
   'description': 'Audio Muting by Channel Command'}),
  ('SPA', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Speaker Off'}),
    ('01', {'name': 'on', 'description': 'sets Speaker On'}),
    ('UP', {'name': 'up', 'description': 'sets Speaker Switch Wrap-Around'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Speaker State'})]),
   'name': 'speaker-a',
   'description': 'Speaker A Command'}),
  ('SPB', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Speaker Off'}),
    ('01', {'name': 'on', 'description': 'sets Speaker On'}),
    ('UP', {'name': 'up', 'description': 'sets Speaker Switch Wrap-Around'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Speaker State'})]),
   'name': 'speaker-b',
   'description': 'Speaker B Command'}),
  ('SPL', {'values': OrderedDict([('SB', {'name': 'surrback',
     'description': 'sets SurrBack Speaker'}),
    ('FH', {'name': ('front-high', 'surrback-front-high-speakers'),
     'description': 'sets Front High Speaker / SurrBack+Front High Speakers'}),
    ('FW', {'name': ('front-wide', 'surrback-front-wide-speakers'),
     'description': 'sets Front Wide Speaker / SurrBack+Front Wide Speakers'}),
    ('HW', {'name': ('front-high-front-wide-speakers'),
     'description': 'sets, Front High+Front Wide Speakers'}),
    ('H1', {'name': 'height1-speakers',
     'description': 'sets Height1 Speakers'}),
    ('H2', {'name': 'height2-speakers',
     'description': 'sets Height2 Speakers'}),
    ('BH', {'name': 'back-height1-speakers',
     'description': 'sets Back+Height1 Speakers'}),
    ('BW', {'name': 'back-wide-speakers',
     'description': 'sets Back+Wide Speakers'}),
    ('HH', {'name': 'height1-height2-speakers',
     'description': 'sets Height1+Height2 Speakers'}),
    ('A', {'name': 'speakers-a', 'description': 'sets Speakers A'}),
    ('B', {'name': 'speakers-b', 'description': 'sets Speakers B'}),
    ('AB', {'name': 'speakers-a-b', 'description': 'sets Speakers A+B'}),
    ('UP', {'name': 'up', 'description': 'sets Speaker Switch Wrap-Around'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Spea  ker State'})]),
   'name': 'speaker-layout',
   'description': 'Speaker Layout Command'}),
  ('MVL', {'values': OrderedDict([((0, 200), {'name': None,
     'description': u'Volume Level 0.0 \u2013 100.0 ( 0.5 Step In hexadecimal representation)'}),
    ((0, 100), {'name': 'vol-0-100,',
     'description': u'Volume Level 0 \u2013 100 ( In hexadecimal representation)'}),
    ((0, 80), {'name': None,
     'description': u'Volume Level 0 \u2013 80 ( In hexadecimal representation)'}),
    ((0, 50), {'name': 'vol-0-50,',
     'description': u'Volume Level 0 \u2013 50 ( In hexadecimal representation)'}),
    ('UP', {'name': 'level-up', 'description': 'sets Volume Level Up'}),
    ('DOWN', {'name': 'level-down', 'description': 'sets Volume Level Down'}),
    ('UP1', {'name': 'level-up-1db-step',
     'description': 'sets Volume Level Up 1dB Step'}),
    ('DOWN1', {'name': 'level-down-1db-step',
     'description': 'sets Volume Level Down 1dB Step'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Volume Level'})]),
   'name': 'master-volume',
   'description': 'Master Volume Command'}),
  ('TFR', {'values': OrderedDict([('B{xx}', {'name': 'b-xx',
     'description': 'Front Bass (xx is "-A"..."00"..."+A"[-10...0...+10 1 step]'}),
    ('T{xx}', {'name': 't-xx',
     'description': 'Front Treble (xx is "-A"..."00"..."+A"[-10...0...+10 1 step]'}),
    ('BUP', {'name': 'bass-up', 'description': 'sets Front Bass up(1 step)'}),
    ('BDOWN', {'name': 'bass-down',
     'description': 'sets Front Bass down(1 step)'}),
    ('TUP', {'name': 'treble-up',
     'description': 'sets Front Treble up(1 step)'}),
    ('TDOWN', {'name': 'treble-down',
     'description': 'sets Front Treble down(1 step)'}),
    ('QSTN', {'name': 'query', 'description': 'gets Front Tone ("BxxTxx")'})]),
   'name': 'tone-front',
   'description': 'Tone(Front) Command'}),
  ('TFW', {'values': OrderedDict([('B{xx}', {'name': 'b-xx',
     'description': 'Front Wide Bass (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('T{xx}', {'name': 't-xx',
     'description': 'Front Wide Treble (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('BUP', {'name': 'bass-up',
     'description': 'sets Front Wide Bass up(2 step)'}),
    ('BDOWN', {'name': 'bass-down',
     'description': 'sets Front Wide Bass down(2 step)'}),
    ('TUP', {'name': 'treble-up',
     'description': 'sets Front Wide Treble up(2 step)'}),
    ('TDOWN', {'name': 'treble-down',
     'description': 'sets Front Wide Treble down(2 step)'}),
    ('QSTN', {'name': 'query',
     'description': 'gets Front Wide Tone ("BxxTxx")'})]),
   'name': 'tone-front-wide',
   'description': 'Tone(Front Wide) Command'}),
  ('TFH', {'values': OrderedDict([('B{xx}', {'name': 'b-xx',
     'description': 'Front High Bass (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('T{xx}', {'name': 't-xx',
     'description': 'Front High Treble (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('BUP', {'name': 'bass-up',
     'description': 'sets Front High Bass up(2 step)'}),
    ('BDOWN', {'name': 'bass-down',
     'description': 'sets Front High Bass down(2 step)'}),
    ('TUP', {'name': 'treble-up',
     'description': 'sets Front High Treble up(2 step)'}),
    ('TDOWN', {'name': 'treble-down',
     'description': 'sets Front High Treble down(2 step)'}),
    ('QSTN', {'name': 'query',
     'description': 'gets Front High Tone ("BxxTxx")'})]),
   'name': 'tone-front-high',
   'description': 'Tone(Front High) Command'}),
  ('TCT', {'values': OrderedDict([('B{xx}', {'name': 'b-xx',
     'description': 'Center Bass (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('T{xx}', {'name': 't-xx',
     'description': 'Center Treble (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('BUP', {'name': 'bass-up', 'description': 'sets Center Bass up(2 step)'}),
    ('BDOWN', {'name': 'bass-down',
     'description': 'sets Center Bass down(2 step)'}),
    ('TUP', {'name': 'treble-up',
     'description': 'sets Center Treble up(2 step)'}),
    ('TDOWN', {'name': 'treble-down',
     'description': 'sets Center Treble down(2 step)'}),
    ('QSTN', {'name': 'query',
     'description': 'gets Cetner Tone ("BxxTxx")'})]),
   'name': 'tone-center',
   'description': 'Tone(Center) Command'}),
  ('TSR', {'values': OrderedDict([('B{xx}', {'name': 'b-xx',
     'description': 'Surround Bass (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('T{xx}', {'name': 't-xx',
     'description': 'Surround Treble (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('BUP', {'name': 'bass-up',
     'description': 'sets Surround Bass up(2 step)'}),
    ('BDOWN', {'name': 'bass-down',
     'description': 'sets Surround Bass down(2 step)'}),
    ('TUP', {'name': 'treble-up',
     'description': 'sets Surround Treble up(2 step)'}),
    ('TDOWN', {'name': 'treble-down',
     'description': 'sets Surround Treble down(2 step)'}),
    ('QSTN', {'name': 'query',
     'description': 'gets Surround Tone ("BxxTxx")'})]),
   'name': 'tone-surround',
   'description': 'Tone(Surround) Command'}),
  ('TSB', {'values': OrderedDict([('B{xx}', {'name': 'b-xx',
     'description': 'Surround Back Bass (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('T{xx}', {'name': 't-xx',
     'description': 'Surround Back Treble (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('BUP', {'name': 'bass-up',
     'description': 'sets Surround Back Bass up(2 step)'}),
    ('BDOWN', {'name': 'bass-down',
     'description': 'sets Surround Back Bass down(2 step)'}),
    ('TUP', {'name': 'treble-up',
     'description': 'sets Surround Back Treble up(2 step)'}),
    ('TDOWN', {'name': 'treble-down',
     'description': 'sets Surround Back Treble down(2 step)'}),
    ('QSTN', {'name': 'query',
     'description': 'gets Surround Back Tone ("BxxTxx")'})]),
   'name': 'tone-surround-back',
   'description': 'Tone(Surround Back) Command'}),
  ('TSW', {'values': OrderedDict([('B{xx}', {'name': 'b-xx',
     'description': 'Subwoofer Bass (xx is "-A"..."00"..."+A"[-10...0...+10 2 step]'}),
    ('BUP', {'name': 'bass-up',
     'description': 'sets Subwoofer Bass up(2 step)'}),
    ('BDOWN', {'name': 'bass-down',
     'description': 'sets Subwoofer Bass down(2 step)'}),
    ('QSTN', {'name': 'query',
     'description': 'gets Subwoofer Tone ("BxxTxx")'})]),
   'name': 'tone-subwoofer',
   'description': 'Tone(Subwoofer) Command'}),
  ('PMB', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Off'}),
    ('01', {'name': 'on', 'description': 'sets On'}),
    ('TG', {'name': 'toggle',
     'description': 'sets Phase Matching Bass Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets Phase Matching Bass'})]),
   'name': 'phase-matching-bass',
   'description': 'Phase Matching Bass Command'}),
  ('SLP', {'values': OrderedDict([(u'\u201c01\u201d-\u201c5A\u201d', {'name': 'time-1-90min',
     'description': 'sets Sleep Time 1 - 90min ( In hexadecimal representation)'}),
    (u'\u201cOFF\u201d', {'name': 'time-off',
     'description': 'sets Sleep Time Off'}),
    (u'\u201cUP\u201d', {'name': 'up',
     'description': 'sets Sleep Time Wrap-Around UP'}),
    (u'\u201cQSTN\u201d', {'name': 'qstn',
     'description': 'gets The Sleep Time'})]),
   'name': 'sleep-set',
   'description': 'Sleep Set Command'}),
  ('SLC', {'values': OrderedDict([(u'\u201cTEST\u201d', {'name': 'test',
     'description': 'TEST Key'}),
    ('OFF', {'name': 'test-tone-off', 'description': 'sets TEST TONE OFF'}),
    (u'\u201cCHSEL\u201d', {'name': 'chsel', 'description': 'CH SEL Key'}),
    (u'\u201cUP\u201d', {'name': 'up', 'description': 'LEVEL + Key'}),
    (u'\u201cDOWN\u201d', {'name': 'down',
     'description': u'LEVEL \u2013 KEY'})]),
   'name': 'speaker-level-calibration',
   'description': 'Speaker Level Calibration Command'}),
  ('SWL', {'values': OrderedDict([((-30, 24), {'name': '15-0db-0-0db-12-0db',
     'description': 'sets Subwoofer Level -15.0dB - 0.0dB - +12.0dB(0.5dB Step)'}),
    ((-15, 12), {'name': '15db-0db-12db',
     'description': 'sets Subwoofer Level -15dB - 0dB - +12dB'}),
    (u'\u201cUP\u201d', {'name': 'up', 'description': 'LEVEL + Key'}),
    (u'\u201cDOWN\u201d', {'name': 'down',
     'description': u'LEVEL \u2013 KEY'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Subwoofer Level'})]),
   'name': 'subwoofer-temporary-level',
   'description': 'Subwoofer (temporary) Level Command'}),
  ('SW2', {'values': OrderedDict([((-30, 24), {'name': '15-0db-0-0db-12-0db',
     'description': 'sets Subwoofer 2 Level -15.0dB - 0.0dB - +12.0dB(0.5dB Step)'}),
    ((-15, 12), {'name': '15db-0db-12db',
     'description': 'sets Subwoofer 2 Level -15dB - 0dB - +12dB'}),
    (u'\u201cUP\u201d', {'name': 'up', 'description': 'LEVEL + Key'}),
    (u'\u201cDOWN\u201d', {'name': 'down',
     'description': u'LEVEL \u2013 KEY'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Subwoofer Level'})]),
   'name': 'subwoofer-2-temporary-level',
   'description': 'Subwoofer 2 (temporary) Level Command'}),
  ('CTL', {'values': OrderedDict([((-24, 24), {'name': '12-0db-0-0db-12-0db',
     'description': 'sets Center Level -12.0dB - 0.0dB - +12.0dB(0.5dB Step)'}),
    ((-12, 12), {'name': '12db-0db-12db',
     'description': 'sets Center Level -12dB - 0dB - +12dB'}),
    (u'\u201cUP\u201d', {'name': 'up', 'description': 'LEVEL + Key'}),
    (u'\u201cDOWN\u201d', {'name': 'down',
     'description': u'LEVEL \u2013 KEY'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Subwoofer Level'})]),
   'name': 'center-temporary-level',
   'description': 'Center (temporary) Level Command'}),
  ('TCL', {'values': OrderedDict([('aaabbbcccdddeeefffggghhhiiijjjkkklllmmm', {'name': 'levels',
     'description': 'sets Temporary Channel Level\nSubwoofer1/2 xxx=-1E(-15.0dB)~000(0.0dB)~+18(+12.0dB)\nOther Ch xxx=-18(-12.0dB)~000(0.0dB)~+18(+12.0dB)\nfor not exist channel is always 000.\n\naaa:Front Left\nbbb:Front Right\nccc:Center\nddd:Surround Left\neee:Surround Right\nfff:Surround Back Left\nggg:Surround Back Right\nhhh:Subwoofer 1\niii:Height 1 Left\njjj:Height 1 Right\nkkk:Height 2 Left\nlll:Height2 Right\nmmm:Subwoofer 2'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Subwoofer Level'})]),
   'name': 'temporary-channel-level',
   'description': 'Temporary Channel Level Command'}),
  ('DIF', {'values': OrderedDict([('00', {'name': ('selector-volume-1line',
      'default-2line'),
     'description': 'sets Selector + Volume Display Mode@1line, Default@2line'}),
    ('01', {'name': 'selector-listening-1line',
     'description': 'sets Selector + Listening Mode Display Mode@1line'}),
    ('02', {'name': '02',
     'description': 'Display Digital Format(temporary display)'}),
    ('03', {'name': '03',
     'description': 'Display Video Format(temporary display)'}),
    ('TG', {'name': 'toggle',
     'description': 'sets Display Mode Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Display Mode'})]),
   'name': 'display-mode',
   'description': 'Display Mode Command'}),
  ('DIM', {'values': OrderedDict([('00', {'name': 'bright',
     'description': 'sets Dimmer Level "Bright"'}),
    ('01', {'name': 'dim', 'description': 'sets Dimmer Level "Dim"'}),
    ('02', {'name': 'dark', 'description': 'sets Dimmer Level "Dark"'}),
    ('03', {'name': 'shut-off',
     'description': 'sets Dimmer Level "Shut-Off"'}),
    ('08', {'name': 'bright-led-off',
     'description': 'sets Dimmer Level "Bright & LED OFF"'}),
    ('DIM', {'name': 'dim',
     'description': 'sets Dimmer Level Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Dimmer Level'})]),
   'name': 'dimmer-level',
   'description': 'Dimmer Level Command'}),
  ('OSD', {'values': OrderedDict([('MENU', {'name': 'menu',
     'description': 'Menu Key              Setup Key'}),
    ('UP', {'name': 'up', 'description': 'Up Key'}),
    ('DOWN', {'name': 'down', 'description': 'Down Key'}),
    ('RIGHT', {'name': 'right', 'description': 'Right Key'}),
    ('LEFT', {'name': 'left', 'description': 'Left Key'}),
    ('ENTER', {'name': 'enter', 'description': 'Enter Key'}),
    ('EXIT', {'name': 'exit', 'description': 'Exit Key'}),
    ('AUDIO', {'name': 'audio', 'description': 'Audio Adjust Key'}),
    ('VIDEO', {'name': 'video', 'description': 'Video Adjust Key'}),
    ('HOME', {'name': 'home', 'description': 'Home Key'}),
    ('QUICK', {'name': 'quick',
     'description': 'Quick Setup Key     Quick Menu Key/AV Adjust Key'}),
    ('IPV', {'name': 'ipv', 'description': 'Instaprevue Key'})]),
   'name': 'setup',
   'description': 'Setup Operation Command'}),
  ('MEM', {'values': OrderedDict([('STR', {'name': 'str',
     'description': 'stores memory'}),
    ('RCL', {'name': 'rcl', 'description': 'recalls memory'}),
    ('LOCK', {'name': 'lock', 'description': 'locks memory'}),
    ('UNLK', {'name': 'unlk', 'description': 'unlocks memory'})]),
   'name': 'memory-setup',
   'description': 'Memory Setup Command'}),
  ('RST', {'values': OrderedDict([('ALL', {'name': 'all',
     'description': 'Reset All'})]),
   'name': 'reset',
   'description': 'Reset Command'}),
  ('IFA', {'values': OrderedDict([(u'a..a,b..b,c\u2026c,d..d,e\u2026e,f\u2026f,', {'name': 'a-a-b-b-c-c-d-d-e-e-f-f',
     'description': u"Infomation of Audio(Same Immediate Display ',' is separator of infomations)\na...a: Audio Input Port\nb\u2026b: Input Signal Format\nc\u2026c: Sampling Frequency\nd\u2026d: Input Signal Channel\ne\u2026e: Listening Mode\nf\u2026f: Output Signal Channel"}),
    (u'a..a,b..b,c\u2026c,d..d,e\u2026e,f\u2026f,g\u2026g,h\u2026h,i\u2026I,j\u2026j,k\u2026k', {'name': 'a-a-b-b-c-c-d-d-e-e-f-f-g-g-h-h-i-i-j-j',
     'description': u"Information of Audio(Same Immediate Display ',' is separator of infomartions)\na...a: Audio Input Port\nb\u2026b: Input Signal Format\nc\u2026c: Sampling Frequency\nd\u2026d: Input Signal Channel\ne\u2026e: Listening Mode\nf\u2026f: Output Signal Channel\ng\u2026g: Output Sampling Frequency\nh...h: PQLS (Off/2ch/Multich/Bitstream)\ni...i: Auto Phase Control Current Delay (0ms - 16ms / ---)\nj...j: Auto Phase Control Phase (Normal/Reverse)"}),
    ('QSTN', {'name': 'query', 'description': 'gets Infomation of Audio'})]),
   'name': 'audio-information',
   'description': 'Audio Information Command'}),
  ('IFV', {'values': OrderedDict([(u'a..a,b..b,c\u2026c,d..d,e\u2026e,f\u2026f,g\u2026g,h\u2026h,i\u2026i,', {'name': 'a-a-b-b-c-c-d-d-e-e-f-f-g-g-h-h-i-i',
     'description': u"information of Video(Same Immediate Display ',' is separator of informations)\na\u2026a: Video Input Port\nb\u2026b: Input Resolution, Frame Rate\nc\u2026c: RGB/YCbCr\nd\u2026d: Color Depth \ne\u2026e: Video Output Port\nf\u2026f: Output Resolution, Frame Rate\ng\u2026g: RGB/YCbCr\nh\u2026h: Color Depth\ni...i: Picture Mode"}),
    ('QSTN', {'name': 'query', 'description': 'gets Information of Video'})]),
   'name': 'video-information',
   'description': 'Video Information Command'}),
  ('FLD', {'values': OrderedDict([('{xx}{xx}{xx}{xx}{xx}x', {'name': 'xx-xx-xx-xx-xx-x',
     'description': 'FL Display Information\nCharacter Code for FL Display (UTF-8 encoded)'}),
    ('QSTN', {'name': 'query',
     'description': 'gets FL Display Information'})]),
   'name': 'fl-display-information',
   'description': 'FL Display Information Command'}),
  ('SLI', {'values': OrderedDict([('00', {'name': ('video1',
      'vcr',
      'dvr',
      'stb',
      'dvr'),
     'description': 'sets VIDEO1, VCR/DVR, STB/DVR'}),
    ('01', {'name': ('video2', 'cbl', 'sat'),
     'description': 'sets VIDEO2, CBL/SAT'}),
    ('02', {'name': ('video3', 'game/tv', 'game', 'game1'),
     'description': 'sets VIDEO3, GAME/TV, GAME, GAME1'}),
    ('03', {'name': ('video4', 'aux1'),
     'description': 'sets VIDEO4, AUX1(AUX)'}),
    ('04', {'name': ('video5', 'aux2', 'game2'),
     'description': 'sets VIDEO5, AUX2, GAME2'}),
    ('05', {'name': ('video6', 'pc'), 'description': 'sets VIDEO6, PC'}),
    ('06', {'name': 'video7', 'description': 'sets VIDEO7'}),
    ('07', {'name': '07', 'description': 'Hidden1     EXTRA1'}),
    ('08', {'name': '08', 'description': 'Hidden2     EXTRA2'}),
    ('09', {'name': '09', 'description': 'Hidden3     EXTRA3'}),
    ('10', {'name': ('dvd', 'bd', 'dvd'), 'description': 'sets DVD, BD/DVD'}),
    ('11', {'name': 'strm-box', 'description': 'sets STRM BOX'}),
    ('12', {'name': 'tv', 'description': 'sets TV'}),
    ('20', {'name': ('tape-1', 'tv/tape'),
     'description': 'sets TAPE(1), TV/TAPE'}),
    ('21', {'name': 'tape2', 'description': 'sets TAPE2'}),
    ('22', {'name': 'phono', 'description': 'sets PHONO'}),
    ('23', {'name': ('cd', 'tv/cd'), 'description': 'sets CD, TV/CD'}),
    ('24', {'name': 'fm', 'description': 'sets FM'}),
    ('25', {'name': 'am', 'description': 'sets AM'}),
    (u'\u201c26\u201d', {'name': 'tuner', 'description': 'sets TUNER'}),
    ('27', {'name': ('music-server', 'p4s', 'dlna'),
     'description': 'sets MUSIC SERVER, P4S, DLNA'}),
    ('28', {'name': ('internet-radio', 'iradio-favorite'),
     'description': 'sets INTERNET RADIO, iRadio Favorite'}),
    ('29', {'name': ('usb', 'usb'), 'description': 'sets USB/USB(Front)'}),
    ('2A', {'name': 'usb', 'description': 'sets USB(Rear)'}),
    ('2B', {'name': ('network', 'net'), 'description': 'sets NETWORK, NET'}),
    ('2C', {'name': 'usb', 'description': 'sets USB(toggle)'}),
    ('2D', {'name': 'aiplay', 'description': 'sets Aiplay'}),
    ('2E', {'name': 'bluetooth', 'description': 'sets Bluetooth'}),
    ('2F', {'name': 'usb-dac-in', 'description': 'sets USB DAC In'}),
    ('41', {'name': 'line', 'description': 'sets LINE'}),
    ('42', {'name': 'line2', 'description': 'sets LINE2'}),
    ('44', {'name': 'optical', 'description': 'sets OPTICAL'}),
    ('45', {'name': 'coaxial', 'description': 'sets COAXIAL'}),
    ('40', {'name': 'universal-port', 'description': 'sets Universal PORT'}),
    ('30', {'name': 'multi-ch', 'description': 'sets MULTI CH'}),
    ('31', {'name': 'xm', 'description': 'sets XM'}),
    ('32', {'name': 'sirius', 'description': 'sets SIRIUS'}),
    ('33', {'name': 'dab', 'description': 'sets DAB '}),
    ('55', {'name': 'hdmi-5', 'description': 'sets HDMI 5'}),
    ('56', {'name': 'hdmi-6', 'description': 'sets HDMI 6'}),
    ('57', {'name': 'hdmi-7', 'description': 'sets HDMI 7'}),
    ('UP', {'name': 'up',
     'description': 'sets Selector Position Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Selector Position Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Selector Position'})]),
   'name': 'input-selector',
   'description': 'Input Selector Command'}),
  ('SLR', {'values': OrderedDict([('00', {'name': 'video1',
     'description': 'sets VIDEO1'}),
    ('01', {'name': 'video2', 'description': 'sets VIDEO2'}),
    ('02', {'name': 'video3', 'description': 'sets VIDEO3'}),
    ('03', {'name': 'video4', 'description': 'sets VIDEO4'}),
    ('04', {'name': 'video5', 'description': 'sets VIDEO5'}),
    ('05', {'name': 'video6', 'description': 'sets VIDEO6'}),
    ('06', {'name': 'video7', 'description': 'sets VIDEO7'}),
    ('10', {'name': 'dvd', 'description': 'sets DVD'}),
    ('20', {'name': 'tape', 'description': 'sets TAPE(1)'}),
    ('21', {'name': 'tape2', 'description': 'sets TAPE2'}),
    ('22', {'name': 'phono', 'description': 'sets PHONO'}),
    ('23', {'name': 'cd', 'description': 'sets CD'}),
    ('24', {'name': 'fm', 'description': 'sets FM'}),
    ('25', {'name': 'am', 'description': 'sets AM'}),
    ('26', {'name': 'tuner', 'description': 'sets TUNER'}),
    ('27', {'name': 'music-server', 'description': 'sets MUSIC SERVER'}),
    ('28', {'name': 'internet-radio', 'description': 'sets INTERNET RADIO'}),
    ('30', {'name': 'multi-ch', 'description': 'sets MULTI CH'}),
    ('31', {'name': 'xm', 'description': 'sets XM'}),
    ('7F', {'name': 'off', 'description': 'sets OFF'}),
    ('80', {'name': 'source', 'description': 'sets SOURCE'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Selector Position'})]),
   'name': 'recout-selector',
   'description': 'RECOUT Selector Command'}),
  ('SLA', {'values': OrderedDict([('00', {'name': 'auto',
     'description': 'sets AUTO'}),
    ('01', {'name': 'multi-channel', 'description': 'sets MULTI-CHANNEL'}),
    ('02', {'name': 'analog', 'description': 'sets ANALOG'}),
    ('03', {'name': 'ilink', 'description': 'sets iLINK'}),
    ('04', {'name': 'hdmi', 'description': 'sets HDMI'}),
    ('05', {'name': ('coax', 'opt'), 'description': 'sets COAX/OPT'}),
    ('06', {'name': 'balance', 'description': 'sets BALANCE'}),
    ('07', {'name': 'arc', 'description': 'sets ARC'}),
    ('0F', {'name': 'none', 'description': 'sets None'}),
    ('UP', {'name': 'up',
     'description': 'sets Audio Selector Wrap-Around Up'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Audio Selector Status'})]),
   'name': 'audio-selector',
   'description': 'Audio Selector Command'}),
  ('TGA', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets 12V Trigger A Off'}),
    ('01', {'name': 'on', 'description': 'sets 12V Trigger A On'}),
    ('QSTN', {'name': 'query', 'description': 'gets 12V Trigger A Status'})]),
   'name': '12v-trigger-a',
   'description': '12V Trigger A Command'}),
  ('TGB', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets 12V Trigger B Off'}),
    ('01', {'name': 'on', 'description': 'sets 12V Trigger B On'}),
    ('QSTN', {'name': 'query', 'description': 'gets 12V Trigger B Status'})]),
   'name': '12v-trigger-b',
   'description': '12V Trigger B Command'}),
  ('TGC', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets 12V Trigger C Off'}),
    ('01', {'name': 'on', 'description': 'sets 12V Trigger C On'}),
    ('QSTN', {'name': 'query', 'description': 'gets 12V Trigger C Status'})]),
   'name': '12v-trigger-c',
   'description': '12V Trigger C Command'}),
  ('VOS', {'values': OrderedDict([('00', {'name': 'd4',
     'description': 'sets D4'}),
    ('01', {'name': 'component', 'description': 'sets Component'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Selector Position'})]),
   'name': 'video-output-selector',
   'description': 'Video Output Selector (Japanese Model Only)'}),
  ('HDO', {'values': OrderedDict([('00', {'name': ('no', 'analog'),
     'description': 'sets No, Analog'}),
    ('01', {'name': ('yes', 'out'),
     'description': 'sets Yes/Out Main, HDMI Main, HDMI'}),
    ('02', {'name': ('out-sub', 'sub', 'hdbaset'),
     'description': 'sets Out Sub, HDMI Sub, HDBaseT'}),
    ('03', {'name': ('both', 'sub'), 'description': 'sets, Both, Main+Sub'}),
    ('04', {'name': ('both'), 'description': 'sets, Both(Main)'}),
    ('05', {'name': ('both'), 'description': 'sets, Both(Sub)'}),
    ('UP', {'name': 'up',
     'description': 'sets HDMI Out Selector Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The HDMI Out Selector'})]),
   'name': 'hdmi-output-selector',
   'description': 'HDMI Output Selector'}),
  ('HAO', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Off'}),
    ('01', {'name': 'on', 'description': 'sets On'}),
    ('02', {'name': 'auto', 'description': 'sets Auto'}),
    ('UP', {'name': 'up',
     'description': 'sets HDMI Audio Out Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets HDMI Audio Out'})]),
   'name': 'hdmi-audio-out',
   'description': 'HDMI Audio Out (Main)'}),
  ('HAS', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Off'}),
    ('01', {'name': 'on', 'description': 'sets On'}),
    ('UP', {'name': 'up',
     'description': 'sets HDMI Audio Out Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets HDMI Audio Out'})]),
   'name': 'hdmi-audio-out',
   'description': 'HDMI Audio Out (Sub)'}),
  ('CEC', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Off'}),
    ('01', {'name': 'on', 'description': 'sets On'}),
    ('UP', {'name': 'up', 'description': 'sets HDMI CEC Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets HDMI CEC'})]),
   'name': 'hdmi-cec',
   'description': 'HDMI CEC'}),
  ('CCM', {'values': OrderedDict([('01', {'name': 'main',
     'description': 'sets Main'}),
    ('02', {'name': 'zone2', 'description': 'sets Zone2'}),
    ('10', {'name': 'sub', 'description': 'sets Sub'}),
    ('UP', {'name': 'up',
     'description': 'sets Control Monitor Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets Control Monitor'})]),
   'name': 'hdmi-cec-control-monitor',
   'description': 'HDMI CEC Control Monitor'}),
  ('RES', {'values': OrderedDict([('00', {'name': 'through',
     'description': 'sets Through'}),
    ('01', {'name': 'auto', 'description': 'sets Auto(HDMI Output Only)'}),
    ('02', {'name': '480p', 'description': 'sets 480p'}),
    ('03', {'name': '720p', 'description': 'sets 720p'}),
    ('13', {'name': '1680x720p', 'description': 'sets 1680x720p'}),
    ('04', {'name': '1080i', 'description': 'sets 1080i'}),
    ('05', {'name': '1080p', 'description': 'sets 1080p(HDMI Output Only)'}),
    ('07', {'name': ('1080p', '24fs'),
     'description': 'sets 1080p/24fs(HDMI Output Only)'}),
    ('15', {'name': '2560x1080p', 'description': 'sets 2560x1080p'}),
    ('08', {'name': '4k-upcaling',
     'description': 'sets 4K Upcaling(HDMI Output Only) 4K(HDMI Output Only)'}),
    ('06', {'name': 'source', 'description': 'sets Source'}),
    ('UP', {'name': 'up',
     'description': 'sets Monitor Out Resolution Wrap-Around Up'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Monitor Out Resolution'})]),
   'name': 'monitor-out-resolution',
   'description': 'Monitor Out Resolution'}),
  ('SPR', {'values': OrderedDict([((0, 3), {'name': 'no-0-3',
     'description': 'sets Super Resolution'}),
    ('UP', {'name': 'up',
     'description': 'sets Super Resolution Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Super Resolution Wrap-Around DOWN'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Super Resolution State'})]),
   'name': 'super-resolution',
   'description': 'Super Resolution'}),
  ('HOI', {'values': OrderedDict([('ab', {'name': ('a-1-for-zone-b-sub-0-none',
      '1-for-zone',
      '2-for-zone-2'),
     'description': 'sets HDMI Information\na:HDMI Out MAIN 1:for Main Zone\nb:HDMI Out SUB 0:None,1:for Main Zone,2:for Zone 2'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The HDMI Out Information State'})]),
   'name': 'hdmi-out-information',
   'description': 'HDMI Out Information'}),
  ('ISF', {'values': OrderedDict([('00', {'name': 'custom',
     'description': 'sets ISF Mode Custom'}),
    ('01', {'name': 'day', 'description': 'sets ISF Mode Day'}),
    ('02', {'name': 'night', 'description': 'sets ISF Mode Night'}),
    ('UP', {'name': 'up',
     'description': 'sets ISF Mode State Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The ISF Mode State'})]),
   'name': 'isf-mode',
   'description': 'ISF Mode'}),
  ('VWM', {'values': OrderedDict([('00', {'name': 'auto',
     'description': 'sets Auto'}),
    ('01', {'name': '4-3', 'description': 'sets 4:3'}),
    ('02', {'name': 'full', 'description': 'sets Full'}),
    ('03', {'name': 'zoom', 'description': 'sets Zoom'}),
    ('04', {'name': 'zoom', 'description': 'sets Wide Zoom'}),
    ('05', {'name': 'smart-zoom', 'description': 'sets Smart Zoom'}),
    ('UP', {'name': 'up',
     'description': 'sets Video Zoom Mode Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets Video Zoom Mode'})]),
   'name': 'video-wide-mode',
   'description': 'Video Wide Mode'}),
  ('VPM', {'values': OrderedDict([('00', {'name': ('through', 'standard'),
     'description': 'sets Through, Standard'}),
    ('01', {'name': 'custom', 'description': 'sets Custom'}),
    ('02', {'name': 'cinema', 'description': 'sets Cinema'}),
    ('03', {'name': 'game', 'description': 'sets Game'}),
    ('05', {'name': 'isf-day', 'description': 'sets ISF Day'}),
    ('06', {'name': 'isf-night', 'description': 'sets ISF Night'}),
    ('07', {'name': 'streaming', 'description': 'sets Streaming'}),
    ('08', {'name': ('direct', 'bypass'),
     'description': 'sets Direct, Bypass'}),
    ('UP', {'name': 'up',
     'description': 'sets Video Zoom Mode Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets Video Zoom Mode'})]),
   'name': 'video-picture-mode',
   'description': 'Video Picture Mode'}),
  ('LMD', {'values': OrderedDict([('00', {'name': 'stereo',
     'description': 'sets STEREO'}),
    ('01', {'name': 'direct', 'description': 'sets DIRECT'}),
    ('02', {'name': 'surround', 'description': 'sets SURROUND'}),
    ('03', {'name': ('film', 'game-rpg'),
     'description': 'sets FILM, Game-RPG'}),
    ('04', {'name': 'thx', 'description': 'sets THX'}),
    ('05', {'name': ('action', 'game-action'),
     'description': 'sets ACTION, Game-Action'}),
    ('06', {'name': ('musical', 'game-rock'),
     'description': 'sets MUSICAL, Game-Rock'}),
    ('07', {'name': 'mono-movie', 'description': 'sets MONO MOVIE'}),
    ('08', {'name': 'orchestra', 'description': 'sets ORCHESTRA'}),
    ('09', {'name': 'unplugged', 'description': 'sets UNPLUGGED'}),
    ('0A', {'name': 'studio-mix', 'description': 'sets STUDIO-MIX'}),
    ('0B', {'name': 'tv-logic', 'description': 'sets TV LOGIC'}),
    ('0C', {'name': 'all-ch-stereo', 'description': 'sets ALL CH STEREO'}),
    ('0D', {'name': 'theater-dimensional',
     'description': 'sets THEATER-DIMENSIONAL'}),
    ('0E', {'name': ('enhanced-7', 'enhance', 'game-sports'),
     'description': 'sets ENHANCED 7/ENHANCE, Game-Sports'}),
    ('0F', {'name': 'mono', 'description': 'sets MONO'}),
    ('11', {'name': 'pure-audio', 'description': 'sets PURE AUDIO'}),
    ('12', {'name': 'multiplex', 'description': 'sets MULTIPLEX'}),
    ('13', {'name': 'full-mono', 'description': 'sets FULL MONO'}),
    ('14', {'name': ('dolby-virtual', 'surround-enhancer'),
     'description': 'sets DOLBY VIRTUAL / Surround Enhancer'}),
    ('15', {'name': 'dts-surround-sensation',
     'description': 'sets DTS Surround Sensation'}),
    ('16', {'name': 'audyssey-dsx', 'description': 'sets Audyssey DSX'}),
    ('1F', {'name': 'whole-house', 'description': 'sets Whole House Mode'}),
    ('23', {'name': 'stage',
     'description': 'sets Stage (when Genre Control is Enable in Japan Model)'}),
    ('25', {'name': 'action',
     'description': 'sets Action (when Genre Control is Enable in Japan Model)'}),
    ('26', {'name': 'music',
     'description': 'sets Music (when Genre Contorl is Enable in Japan Model)'}),
    ('2E', {'name': 'sports',
     'description': 'sets Sports (when Genre Control is Enable in Japan Model)'}),
    ('40', {'name': 'straight-decode', 'description': 'sets Straight Decode'}),
    ('41', {'name': 'dolby-ex', 'description': 'sets Dolby EX'}),
    ('42', {'name': 'thx-cinema', 'description': 'sets THX Cinema'}),
    ('43', {'name': 'thx-surround-ex', 'description': 'sets THX Surround EX'}),
    ('44', {'name': 'thx-music', 'description': 'sets THX Music'}),
    ('45', {'name': 'thx-games', 'description': 'sets THX Games'}),
    ('50', {'name': ('thx-u2', 's2', 'i', 's-cinema', 'cinema2'),
     'description': 'sets THX U2/S2/I/S Cinema/Cinema2'}),
    ('51', {'name': ('thx-musicmode', 'thx-u2', 's2', 'i', 's-music'),
     'description': 'sets THX MusicMode,THX U2/S2/I/S Music'}),
    ('52', {'name': ('thx-games', 'thx-u2', 's2', 'i', 's-games'),
     'description': 'sets THX Games Mode,THX U2/S2/I/S Games'}),
    ('80', {'name': ('plii', 'pliix-movie', 'dolby-atmos', 'dolby-surround'),
     'description': 'sets PLII/PLIIx Movie, Dolby Atmos/Dolby Surround'}),
    ('81', {'name': ('plii', 'pliix-music'),
     'description': 'sets PLII/PLIIx Music'}),
    ('82', {'name': ('neo-6-cinema', 'neo-x-cinema', 'dts-x', 'neural-x'),
     'description': 'sets Neo:6 Cinema/Neo:X Cinema, DTS:X/Neural:X'}),
    ('83', {'name': ('neo-6-music', 'neo-x-music'),
     'description': 'sets Neo:6 Music/Neo:X Music'}),
    ('84', {'name': ('plii', 'pliix-thx-cinema', 'dolby-surround-thx-cinema'),
     'description': 'sets PLII/PLIIx THX Cinema, Dolby Surround THX Cinema'}),
    ('85', {'name': ('neo-6', 'neo-x-thx-cinema', 'dts-neural-x-thx-cinema'),
     'description': 'sets Neo:6/Neo:X THX Cinema, DTS Neural:X THX Cinema'}),
    ('86', {'name': ('plii', 'pliix-game'),
     'description': 'sets PLII/PLIIx Game'}),
    ('87', {'name': 'neural-surr', 'description': 'sets Neural Surr'}),
    ('88', {'name': ('neural-thx', 'neural-surround'),
     'description': 'sets Neural THX/Neural Surround'}),
    ('89', {'name': ('plii', 'pliix-thx-games', 'dolby-surround-thx-games'),
     'description': 'sets PLII/PLIIx THX Games, Dolby Surround THX Games'}),
    ('8A', {'name': ('neo-6', 'neo-x-thx-games', 'dts-neural-x-thx-games'),
     'description': 'sets Neo:6/Neo:X THX Games, DTS Neural:X THX Games'}),
    ('8B', {'name': ('plii', 'pliix-thx-music', 'dolby-surround-thx-music'),
     'description': 'sets PLII/PLIIx THX Music, Dolby Surround THX Music'}),
    ('8C', {'name': ('neo-6', 'neo-x-thx-music', 'dts-neural-x-thx-music'),
     'description': 'sets Neo:6/Neo:X THX Music, DTS Neural:X THX Music'}),
    ('8D', {'name': 'neural-thx-cinema',
     'description': 'sets Neural THX Cinema'}),
    ('8E', {'name': 'neural-thx-music',
     'description': 'sets Neural THX Music'}),
    ('8F', {'name': 'neural-thx-games',
     'description': 'sets Neural THX Games'}),
    ('90', {'name': 'pliiz-height', 'description': 'sets PLIIz Height'}),
    ('91', {'name': 'neo-6-cinema-dts-surround-sensation',
     'description': 'sets Neo:6 Cinema DTS Surround Sensation'}),
    ('92', {'name': 'neo-6-music-dts-surround-sensation',
     'description': 'sets Neo:6 Music DTS Surround Sensation'}),
    ('93', {'name': 'neural-digital-music',
     'description': 'sets Neural Digital Music'}),
    ('94', {'name': 'pliiz-height-thx-cinema',
     'description': 'sets PLIIz Height + THX Cinema'}),
    ('95', {'name': 'pliiz-height-thx-music',
     'description': 'sets PLIIz Height + THX Music'}),
    ('96', {'name': 'pliiz-height-thx-games',
     'description': 'sets PLIIz Height + THX Games'}),
    ('97', {'name': ('pliiz-height-thx-u2', 's2-cinema'),
     'description': 'sets PLIIz Height + THX U2/S2 Cinema'}),
    ('98', {'name': ('pliiz-height-thx-u2', 's2-music'),
     'description': 'sets PLIIz Height + THX U2/S2 Music'}),
    ('99', {'name': ('pliiz-height-thx-u2', 's2-games'),
     'description': 'sets PLIIz Height + THX U2/S2 Games'}),
    ('9A', {'name': 'neo-x-game', 'description': 'sets Neo:X Game'}),
    ('A0', {'name': ('pliix', 'plii-movie-audyssey-dsx'),
     'description': 'sets PLIIx/PLII Movie + Audyssey DSX'}),
    ('A1', {'name': ('pliix', 'plii-music-audyssey-dsx'),
     'description': 'sets PLIIx/PLII Music + Audyssey DSX'}),
    ('A2', {'name': ('pliix', 'plii-game-audyssey-dsx'),
     'description': 'sets PLIIx/PLII Game + Audyssey DSX'}),
    ('A3', {'name': 'neo-6-cinema-audyssey-dsx',
     'description': 'sets Neo:6 Cinema + Audyssey DSX'}),
    ('A4', {'name': 'neo-6-music-audyssey-dsx',
     'description': 'sets Neo:6 Music + Audyssey DSX'}),
    ('A5', {'name': 'neural-surround-audyssey-dsx',
     'description': 'sets Neural Surround + Audyssey DSX'}),
    ('A6', {'name': 'neural-digital-music-audyssey-dsx',
     'description': 'sets Neural Digital Music + Audyssey DSX'}),
    ('A7', {'name': 'dolby-ex-audyssey-dsx',
     'description': 'sets Dolby EX + Audyssey DSX'}),
    ('FF', {'name': 'auto-surround', 'description': 'sets Auto Surround'}),
    ('UP', {'name': 'up',
     'description': 'sets Listening Mode Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Listening Mode Wrap-Around Down'}),
    ('MOVIE', {'name': 'movie',
     'description': 'sets Listening Mode Wrap-Around Up'}),
    ('MUSIC', {'name': 'music',
     'description': 'sets Listening Mode Wrap-Around Up'}),
    ('GAME', {'name': 'game',
     'description': 'sets Listening Mode Wrap-Around Up'}),
    ('THX', {'name': 'thx',
     'description': 'sets Listening Mode Wrap-Around Up'}),
    ('AUTO', {'name': 'auto',
     'description': 'sets Listening Mode Wrap-Around Up'}),
    ('SURR', {'name': 'surr',
     'description': 'sets Listening Mode Wrap-Around Up'}),
    ('STEREO', {'name': 'stereo',
     'description': 'sets Listening Mode Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Listening Mode'})]),
   'name': 'listening-mode',
   'description': 'Listening Mode Command'}),
  ('DIR', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Off'}),
    ('01', {'name': 'on', 'description': 'sets On'}),
    ('TG', {'name': 'toggle', 'description': 'sets Direct Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets Direct Status'})]),
   'name': 'direct',
   'description': 'Direct Command'}),
  ('LTN', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Late Night Off'}),
    ('01', {'name': ('low-dolbydigital', 'on-dolby-truehd'),
     'description': 'sets Late Night Low@DolbyDigital,On@Dolby TrueHD'}),
    ('02', {'name': ('high-dolbydigital'),
     'description': 'sets Late Night High@DolbyDigital,(On@Dolby TrueHD)'}),
    ('03', {'name': 'auto-dolby-truehd',
     'description': 'sets Late Night Auto@Dolby TrueHD'}),
    ('UP', {'name': 'up',
     'description': 'sets Late Night State Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Late Night Level'})]),
   'name': 'late-night',
   'description': 'Late Night Command'}),
  ('RAS', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Cinema Filter Off'}),
    ('01', {'name': 'on', 'description': 'sets Cinema Filter On'}),
    ('UP', {'name': 'up',
     'description': 'sets Cinema Filter State Wrap-Around Up'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Cinema Filter State'})]),
   'name': 'cinema-filter',
   'description': 'Cinema Filter Command'}),
  ('ADY', {'values': OrderedDict([('00', {'name': ('off'),
     'description': 'sets Audyssey 2EQ/MultEQ/MultEQ XT Off'}),
    ('01', {'name': ('on', 'movie'),
     'description': 'sets Audyssey 2EQ/MultEQ/MultEQ XT On/Movie'}),
    ('02', {'name': ('music'),
     'description': 'sets Audyssey 2EQ/MultEQ/MultEQ XT Music'}),
    ('UP', {'name': 'up',
     'description': 'sets Audyssey 2EQ/MultEQ/MultEQ XT State Wrap-Around Up'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Audyssey 2EQ/MultEQ/MultEQ XT State'})]),
   'name': 'audyssey-2eq-multeq-multeq-xt',
   'description': 'Audyssey 2EQ/MultEQ/MultEQ XT'}),
  ('ADQ', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Audyssey Dynamic EQ Off'}),
    ('01', {'name': 'on', 'description': 'sets Audyssey Dynamic EQ On'}),
    ('UP', {'name': 'up',
     'description': 'sets Audyssey Dynamic EQ State Wrap-Around Up'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Audyssey Dynamic EQ State'})]),
   'name': 'audyssey-dynamic-eq',
   'description': 'Audyssey Dynamic EQ'}),
  ('ADV', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Audyssey Dynamic Volume Off'}),
    ('01', {'name': 'light',
     'description': 'sets Audyssey Dynamic Volume Light'}),
    ('02', {'name': 'medium',
     'description': 'sets Audyssey Dynamic Volume Medium'}),
    ('03', {'name': 'heavy',
     'description': 'sets Audyssey Dynamic Volume Heavy'}),
    ('UP', {'name': 'up',
     'description': 'sets Audyssey Dynamic Volume State Wrap-Around Up'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Audyssey Dynamic Volume State'})]),
   'name': 'audyssey-dynamic-volume',
   'description': 'Audyssey Dynamic Volume'}),
  ('DVL', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Dolby Volume Off'}),
    ('01', {'name': ('low', 'on'), 'description': 'sets Dolby Volume Low/On'}),
    ('02', {'name': 'mid', 'description': 'sets Dolby Volume Mid'}),
    ('03', {'name': 'high', 'description': 'sets Dolby Volume High'}),
    ('UP', {'name': 'up',
     'description': 'sets Dolby Volume State Wrap-Around Up'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Dolby Volume State'})]),
   'name': 'dolby-volume',
   'description': 'Dolby Volume'}),
  ('AEQ', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets AccuEQ Off'}),
    ('01', {'name': ('on', 'on'),
     'description': 'sets AccuEQ On, On(All Ch)'}),
    ('02', {'name': ('on'), 'description': 'sets AccuEQ, On(ex. Front L/R)'}),
    ('UP', {'name': 'up', 'description': 'sets AccuEQ State Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The AccuEQ State'})]),
   'name': 'accueq',
   'description': 'AccuEQ'}),
  ('MCM', {'values': OrderedDict([('01', {'name': 'memory-1',
     'description': 'sets MCACC MEMORY 1'}),
    ('02', {'name': 'memory-2', 'description': 'sets MCACC MEMORY 2'}),
    ('03', {'name': 'memory-3', 'description': 'sets MCACC MEMORY 3'}),
    ('04', {'name': 'memory-4', 'description': 'sets MCACC MEMORY 4'}),
    ('05', {'name': 'memory-5', 'description': 'sets MCACC MEMORY 5'}),
    ('06', {'name': 'memory-6', 'description': 'sets MCACC MEMORY 6'}),
    ('UP', {'name': 'up', 'description': 'sets MCACC MEMORY Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets MCACC MEMORY Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The MCACC MEMORY'})]),
   'name': 'mcacc-eq',
   'description': 'MCACC EQ'}),
  ('EQS', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Equalizer Off'}),
    ('01', {'name': 'preset-1', 'description': 'sets Equalizer Preset 1'}),
    ('02', {'name': 'preset-2', 'description': 'sets Equalizer Preset 2'}),
    ('03', {'name': 'preset-3', 'description': 'sets Equalizer Preset 3'}),
    ('UP', {'name': 'up',
     'description': 'sets Equalizer Preset Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Equalizer Preset Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Equalizer Preset'})]),
   'name': 'equalizer-select',
   'description': 'Equalizer Select(O/I:Equalizer, P:Manual EQ Select)'}),
  ('STW', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Standing Wave Off'}),
    ('01', {'name': 'on', 'description': 'sets Standing Wave On'}),
    ('UP', {'name': 'up', 'description': 'sets Standing Wave Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Standing Wave'})]),
   'name': 'eq-for-standing-wave-standing-wave',
   'description': 'EQ for Standing Wave / Standing Wave'}),
  ('PCT', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Phase Control Off'}),
    ('01', {'name': 'on', 'description': 'sets Phase Control On'}),
    ('02', {'name': 'full-band-on',
     'description': 'sets Full Band Phase Control On'}),
    ('UP', {'name': 'up', 'description': 'sets Phase Control Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Phase Control'})]),
   'name': 'phase-control',
   'description': 'Phase Control'}),
  ('PCP', {'values': OrderedDict([((0, 16), {'name': '0msec-16msec',
     'description': 'sets Phase Control Plus 0msec - 16msec'}),
    ('AT', {'name': 'auto', 'description': 'sets Auto Phase Control Plus'}),
    ('UP', {'name': 'up', 'description': 'sets Phase Control Plus Up'}),
    ('DOWN', {'name': 'down', 'description': 'sets Phase Control Plus Down'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Phase Control Plus'})]),
   'name': 'phase-control-plus',
   'description': 'Phase Control Plus'}),
  ('LFE', {'values': OrderedDict([('xx', {'name': '00-0db-01-1db-02-2db-03-3db-04-4db-05-5db-0a-10db-0f-15db-14-20db-ff-oodb',
     'description': 'sets LFE Mute Level\n00:0dB\n01:-1dB\n02:-2dB\n03:-3dB\n04:-4dB\n05:-5dB\n0A:-10dB\n0F:-15dB\n14:-20dB\nFF:-oodB'}),
    ('UP', {'name': 'up', 'description': 'sets LFE Mute Level Up'}),
    ('DOWN', {'name': 'down', 'description': 'sets LFE Mute Level Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The LFE Mute Level'})]),
   'name': 'lfe-level-lfe-mute-level',
   'description': 'LFE Level / LFE Mute Level'}),
  ('ACE', {'values': OrderedDict([('aaabbbcccdddeeefffggghhhiii', {'name': 'eq',
     'description': 'sets All Channel EQ for Temporary Value\nxxx=-18(-12.0dB)~000(0.0dB)~+18(+12.0dB)\n\naaa:63Hz\nbbb:125Hz\nccc:250Hz\nddd:500Hz\neee:1kHz\nfff:2kHz\nggg:4kHz\nhhh:8kHz\niii:16kHz'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Phase Control'})]),
   'name': 'all-channel-eq-for-temporary-value',
   'description': 'All Channel EQ for Temporary Value'}),
  ('MCC', {'values': OrderedDict([('00', {'name': '00',
     'description': 'not complete MCACC calibration'}),
    ('01', {'name': '01', 'description': 'complete MCACC calibration'}),
    ('QSTN', {'name': 'query', 'description': 'gets The MCACC calibration'})]),
   'name': 'mcacc-calibration',
   'description': 'MCACC Calibration'}),
  ('MFB', {'values': OrderedDict([('00', {'name': '00',
     'description': 'not complete Fullband MCACC calibration or\nnot have Fullband MCACC function'}),
    ('01', {'name': '01',
     'description': 'complete Fullband MCACC calibration'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Fullband MCACC calibration'})]),
   'name': 'fullband-mcacc-calibration',
   'description': 'Fullband MCACC Calibration'}),
  ('MOT', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Music Optimizer Off'}),
    ('01', {'name': 'on', 'description': 'sets Music Optimizer On'}),
    ('UP', {'name': 'up',
     'description': 'sets Music Optimizer State Wrap-Around Up'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Music Optimizer State'})]),
   'name': 'music-optimizer-sound-retriever',
   'description': 'Music Optimizer / Sound Retriever'}),
  ('AVS', {'values': OrderedDict([('snnn', {'name': 'offset',
     'description': 'sets A/V Sync\n s: sign ( "+" or "-" or "0")\n If set minus value, s="-". (only HDMI Lipsync is available)\n If set plus value, s="+"\n If set zero value, s="0"\nnnn : value; If set 100msec, nnn="100"'}),
    ('UP', {'name': ('is-increased'),
     'description': 'sets A/V Sync is increased (step is depend on model)'}),
    ('DOWN', {'name': ('is-decreased'),
     'description': 'sets A/V Sync is decreased (step is depend on model)'}),
    ('QSTN', {'name': 'query', 'description': 'gets A/V Sync Value'})]),
   'name': 'a-v-sync',
   'description': 'A/V Sync'}),
  ('ASC', {'values': OrderedDict([('00', {'name': 'auto',
     'description': 'sets Audio Scalar Auto'}),
    ('01', {'name': 'manual', 'description': 'sets Audio Scalar Manual'}),
    ('UP', {'name': 'up', 'description': 'sets Audio Scalar Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Audio Scalar State'})]),
   'name': 'audio-scalar',
   'description': 'Audio Scalar'}),
  ('UPS', {'values': OrderedDict([('00', {'name': 'x1',
     'description': 'sets Upsampling x1'}),
    ('01', {'name': 'x2', 'description': 'sets Upsampling x2'}),
    ('02', {'name': 'x4', 'description': 'sets Upsampling x4'}),
    ('03', {'name': 'x8', 'description': 'sets Upsampling x8'}),
    ('UP', {'name': 'up', 'description': 'sets Upsampling Wrap-Around'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Upscaling State'})]),
   'name': 'upsampling',
   'description': 'Upsampling'}),
  ('HBT', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Hi-Bit Off'}),
    ('01', {'name': 'on', 'description': 'sets Hi-Bit On'}),
    ('UP', {'name': 'up', 'description': 'sets Hi-Bit Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Hi-Bit State'})]),
   'name': 'hi-bit',
   'description': 'Hi-Bit'}),
  ('DGF', {'values': OrderedDict([('00', {'name': 'slow',
     'description': 'sets Digital Filter Slow'}),
    ('01', {'name': 'sharp', 'description': 'sets Digital Filter Sharp'}),
    ('02', {'name': 'short', 'description': 'sets Digital Filter Short'}),
    ('UP', {'name': 'up', 'description': 'sets Digital Filter Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Digital Filter State'})]),
   'name': 'digital-filter',
   'description': 'Digital Filter'}),
  ('LRA', {'values': OrderedDict([((1, 7), {'name': 'no-1-7',
     'description': 'sets Lock Range Adjust'}),
    ('UP', {'name': 'up', 'description': 'sets Lock Range Adjust Up'}),
    ('Down', {'name': 'down', 'description': 'sets Lock Range Adjust Down'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Lock Range Adjust State'})]),
   'name': 'lock-range-adjust',
   'description': 'Lock Range Adjust'}),
  ('PBS', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets P.BASS Off'}),
    ('01', {'name': 'on', 'description': 'sets P.BASS On'}),
    ('UP', {'name': 'toggle', 'description': 'sets P.BASS Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The P.BASS State'})]),
   'name': 'p-bass',
   'description': 'P.BASS'}),
  ('SBS', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets S.BASS Off'}),
    ('01', {'name': 'on', 'description': 'sets S.BASS On'}),
    ('UP', {'name': 'toggle', 'description': 'sets S.BASS Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The S.BASS State'})]),
   'name': 's-bass',
   'description': 'S.BASS'}),
  ('SCD', {'values': OrderedDict([('00', {'name': 'enhancement-off',
     'description': 'sets Dialog Enhancement Off'}),
    ('01', {'name': 'enhancement-on',
     'description': 'sets Dialog Enhancement On'}),
    ((2, 5), {'name': 'up1-up4',
     'description': 'sets Dialog Enahncement UP1-UP4'}),
    ('UP', {'name': 'up',
     'description': 'sets Dialog Enhancement Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Dialog Enhancement State'})]),
   'name': 'screen-centered-dialog-dialog-enahncement',
   'description': 'Screen Centered Dialog / Dialog Enahncement'}),
  ('CTS', {'values': OrderedDict([('00', {'name': 'center-off',
     'description': 'sets Center Spread Off'}),
    ('01', {'name': 'center-on', 'description': 'sets Center Spread On'}),
    ('TG', {'name': 'toggle',
     'description': 'sets Center Spread Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Center Spread State'})]),
   'name': 'cener-spread-for-dolby-surround',
   'description': 'Cener Spread for Dolby Surround'}),
  ('PNR', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Panorama Off'}),
    ('01', {'name': 'on', 'description': 'sets Panorama On'}),
    ('TG', {'name': 'toggle', 'description': 'sets Panorama Wrap-Around'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Panorama State'})]),
   'name': 'panorama-for-plii-music',
   'description': 'Panorama for PLII Music'}),
  ('DMS', {'values': OrderedDict([((-3, 3), {'name': 'no--3-3',
     'description': 'sets Dimension'}),
    ('UP', {'name': 'up', 'description': 'sets Dimension Up'}),
    ('DOWN', {'name': 'down', 'description': 'sets Dimension Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Dimension State'})]),
   'name': 'dimension-for-plii-music',
   'description': 'Dimension for PLII Music'}),
  ('CTW', {'values': OrderedDict([((0, 7), {'name': 'no-0-7',
     'description': 'sets Center Width'}),
    ('UP', {'name': 'up', 'description': 'sets Center Width Up'}),
    ('DOWN', {'name': 'down', 'description': 'sets Center Width Down'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Center Width State'})]),
   'name': 'center-width-for-plii-music',
   'description': 'Center Width for PLII Music'}),
  ('CTI', {'values': OrderedDict([((0, 10), {'name': None,
     'description': 'sets Center Image'}),
    ('UP', {'name': 'up', 'description': 'sets Center Image Up'}),
    ('DOWN', {'name': 'down', 'description': 'sets Center Image Down'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Center Image State'})]),
   'name': 'center-image-for-neo-6-music',
   'description': 'Center Image for Neo:6 Music'}),
  ('DLC', {'values': OrderedDict([((0, 6), {'name': 'no-0-6',
     'description': 'sets Dialog Control'}),
    ('UP', {'name': 'up', 'description': 'sets Dialog Control Up'}),
    ('DOWN', {'name': 'down', 'description': 'sets Dialog Control Down'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Dialog Control State'})]),
   'name': 'dialog-control',
   'description': 'Dialog Control'}),
  ('DCE', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'Dialog Control is disabled'}),
    ('01', {'name': 'on', 'description': 'Dialog Control is enabled'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Dialog Control Enabled State'})]),
   'name': 'dialog-control-enabled',
   'description': 'Dialog Control Enabled'}),
  ('SPI', {'values': OrderedDict([('abcdefghhhijk', {'name': ('a-subwoofer-0-no',
      '1-yes',
      '1ch',
      '2-2ch-b-front-1-small',
      '2-large-c-center-0-none',
      '1-small',
      '2-lage-d-surround-0-none',
      '1-small',
      '2-lage-e-surround-back-0-none',
      '1-small',
      '2-lage-f-height-1-0-none',
      '1-small',
      '2-lage-g-height-2-0-none',
      '1-small',
      '2-lage-hhh-crossover-50',
      '80',
      '100',
      '150',
      '200-i-height-1-position-0-no',
      '1-fh',
      '2-tf',
      '3-tm',
      '4-tr',
      '5-rh',
      '6-dd-sp-f',
      '7-dd-sp-s',
      '8-dd-sp-b-j-height-2-position-0-no',
      '1-fh',
      '2-tf',
      '3-tm',
      '4-tr',
      '5-rh',
      '6-dd-sp-f',
      '7-dd-sp-s',
      '8-dd-sp-b-k-bi-amp-0-no',
      '1-f',
      '3-f-c',
      '5-f-s',
      '6-c-s',
      '7-f-c-s'),
     'description': 'sets Speaker Information\n\na:Subwoofer 0:No,1:Yes/1ch,2:2ch\nb:Front 1:Small,2:Large\nc:Center 0:None,1:Small,2:Lage\nd:Surround 0:None,1:Small,2:Lage\ne:Surround Back 0:None,1:Small,2:Lage\nf:Height 1 0:None,1:Small,2:Lage\ng:Height 2 0:None,1:Small,2:Lage\nhhh:Crossover 50,80,100,150,200\ni:Height 1 Position 0:No,1:FH,2:TF,3:TM,4:TR,5:RH,6:DD SP(F),7:DD SP(S),8:DD SP(B)\nj:Height 2 Position 0:No,1:FH,2:TF,3:TM,4:TR,5:RH,6:DD SP(F),7:DD SP(S),8:DD SP(B)\nk:Bi-Amp 0:No,1:F,3:F+C,5:F+S,6:C+S,7:F+C+S'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Speaker Information'})]),
   'name': 'speaker-information',
   'description': 'Speaker Information'}),
  ('SPD', {'values': OrderedDict([('Muaaabbbcccdddeeefffggghhhiiijjjkkklllmmm', {'name': None,
     'description': 'sets Speaker Distance\nxxx=001-384 (0.01m - 9.00m unit is meters)\nxxx=001-12C (0.1ft - 30.0ft unit is feet)\nxxx=001-2D0 (0\'0-1/2" - 30\'0" unit is feet/inch)\nfor not exist channel is always 000.\n\nM:MCACC Memory 1-6\nu:Unit 0:feet,1:meters,2:feet/inch\naaa:Front Left\nbbb:Front Right\nccc:Center\nddd:Surround Left\neee:Surround Right\nfff:Surround Back Left\nggg:Surround Back Right\nhhh:Subwoofer 1\niii:Height 1 Left\njjj:Height 1 Right\nkkk:Height 2 Left\nlll:Height2 Right\nmmm:Subwoofer 2'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Speaker Distance'})]),
   'name': 'speaker-distance',
   'description': 'Speaker Distance Command'}),
  ('DMN', {'values': OrderedDict([('00', {'name': 'main',
     'description': 'sets DUAL MONO MAIN'}),
    ('01', {'name': 'sub', 'description': 'sets DUAL MONO SUB'}),
    ('02', {'name': 'main-sub', 'description': 'sets DUAL MONO MAIN+SUB'}),
    ('UP', {'name': 'up', 'description': 'sets Panorama Wrap-Around'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Panorama State'})]),
   'name': 'input-channel-multiplex-dual-mono',
   'description': 'Input Channel (Multiplex) / Dual Mono'}),
  ('LDM', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Loudness Management Off'}),
    ('01', {'name': 'on', 'description': 'sets Loudness management On'}),
    ('UP', {'name': 'up', 'description': 'sets Panorama Wrap-Around'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Panorama State'})]),
   'name': 'loudness-management',
   'description': 'Loudness Management'}),
  ('ITV', {'values': OrderedDict([((-24, 24), {'name': '12-0db-0db-12-0db',
     'description': 'sets IntelliVolume -12.0dB~0dB~+12.0dB(0.5dB Step)'}),
    ('UP', {'name': 'up', 'description': 'sets IntelliVolume Up'}),
    ('DOWN', {'name': 'down', 'description': 'sets IntelliVolume Down'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The IntelliVolume State'})]),
   'name': 'intellivolume-input-volume-absorber',
   'description': 'IntelliVolume / Input Volume Absorber'}),
  ('IRN', {'values': OrderedDict([('iixxxxxxxxxx', {'name': 'name-10-characters-ii-number-the-same-as-for-sli-command-xxxxxxxxxx-name',
     'description': 'sets Input Selector Name (10 characters)\nii: Selector Number (the same as for SLI command)\nxxxxxxxxxx: Name(Max 10 characters)'})]),
   'name': 'input-selector-rename-input-function-rename',
   'description': 'Input Selector Rename / Input Function Rename'}),
  ('FXP', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets PCM Fixed Mode Off'}),
    ('01', {'name': 'on', 'description': 'sets PCM Fixed Mode On'}),
    ('UP', {'name': 'up', 'description': 'sets PCM Fixed Mode Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The PCM Fixed Mode State'})]),
   'name': 'pcm-fixed-mode-fixed-pcm-mode',
   'description': 'PCM Fixed Mode / Fixed PCM Mode'}),
  ('HST', {'values': OrderedDict([('xx', {'name': 'xx-sli-number',
     'description': 'sets HDMI Standby Through xx=SLI Number'}),
    ('OFF', {'name': 'off', 'description': 'sets HDMI Standby Through Off'}),
    ('LAST', {'name': 'last',
     'description': 'sets HDMI Standby Through Last'}),
    ('AT', {'name': 'throguh-auto',
     'description': 'sets HDMI Standby Throguh Auto'}),
    ('ATE', {'name': 'auto',
     'description': 'sets HDMI Standby Through Auto(Eco)'}),
    ('UP', {'name': 'up',
     'description': 'sets HDMI Standby Through Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The HDMI Standby Through State'})]),
   'name': 'hdmi-standby-through',
   'description': 'HDMI Standby Through'}),
  ('PQL', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets PQLS Off'}),
    ('01', {'name': 'on', 'description': 'sets PQLS On'}),
    ('UP', {'name': 'up', 'description': 'sets PQLS Wrap-Around'}),
    ('QSTN', {'name': 'query', 'description': 'gets The PQLS State'})]),
   'name': 'pqls',
   'description': 'PQLS'}),
  ('ARC', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Audio Return Channel Off'}),
    ('01', {'name': 'auto', 'description': 'sets Audio Return Channel Auto'}),
    ('UP', {'name': 'up',
     'description': 'sets Audio Return Channel Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Audio Return Channel State'})]),
   'name': 'audio-return-channel',
   'description': 'Audio Return Channel'}),
  ('LPS', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Lip Sync Off'}),
    ('01', {'name': 'on', 'description': 'sets Lip Sync On'}),
    ('UP', {'name': 'up', 'description': 'sets Lip Sync Wrap-Around'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Lip Sync State'})]),
   'name': 'lip-sync-auto-delay',
   'description': 'Lip Sync / Auto Delay'}),
  ('APD', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Auto Power Down Off'}),
    ('01', {'name': 'on', 'description': 'sets Auto Power Down On'}),
    ('UP', {'name': 'up', 'description': 'sets Auto Power Down Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Auto Power Down State'})]),
   'name': 'auto-power-down',
   'description': 'Auto Power Down'}),
  ('PAM', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Pre Amp Mode Off'}),
    ('01', {'name': 'front', 'description': 'sets Pre Amp Mode Front'}),
    ('03', {'name': 'front-center',
     'description': 'sets Pre Amp Mode Front+Center'}),
    ('07', {'name': 'all', 'description': 'sets Pre Amp Mode All'}),
    ('UP', {'name': 'up', 'description': 'sets Auto Power Down Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Auto Power Down State'})]),
   'name': 'pre-amp-mode-amp-mode',
   'description': 'Pre Amp Mode / AMP Mode'}),
  ('ECO', {'values': OrderedDict([('01', {'name': 'volume-1db-down-and-dimmer-level-dark',
     'description': 'sets Volume 1dB down and Dimmer Level "Dark"'}),
    ('03', {'name': 'volume-3db-down-and-dimmer-level-dark',
     'description': 'sets Volume 3dB down and Dimmer Level "Dark"'}),
    ('06', {'name': 'volume-6db-down-and-dimmer-level-dark',
     'description': 'sets Volume 6dB down and Dimmer Level "Dark"'})]),
   'name': 'for-smart-grid',
   'description': 'for Smart Grid Command'}),
  ('FWV', {'values': OrderedDict([('abce-fhik-lmno-qrtu', {'name': 'version',
     'description': 'sets Firmware Version'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Firmware Version State'})]),
   'name': 'firmware-version',
   'description': 'Firmware Version'}),
  ('UPD', {'values': OrderedDict([('NET', {'name': 'net',
     'description': 'start Device Update via Network'}),
    ('USB', {'name': 'usb', 'description': 'start Device Update via USB'}),
    ('D**-nn', {'name': 'd-nn',
     'description': 'nn Progress (%)\n**=DL Downloading\n**=NT Arm writng\n**=D1 DSP1\n**=D2 DSP2\n**=D3 DSP3\n**=VM VMPU\n**=OS OSD\n**=MP MMPU'}),
    ('CMP', {'name': 'cmp', 'description': 'Device Update is completed'}),
    ('E{xx}-yy', {'name': 'e-xx-yy',
     'description': 'xx=ErrorCode1\nyy=ErrorCode2'}),
    ('00', {'name': '00', 'description': 'not exist new firmware'}),
    ('01', {'name': '01', 'description': 'exist new firmware'}),
    ('02', {'name': 'force', 'description': 'exist new firmware(force)'}),
    ('QSTN', {'name': 'query', 'description': 'gets exist new firmware'})]),
   'name': 'update',
   'description': 'Update'}),
  ('POP', {'values': OrderedDict([('t----<.....>', {'name': 't',
     'description': "t -> message type 'X' : XML\n---- -> reserved\n<.....> : XML data ( [CR] and [LF] are removed )"}),
    ('Ullt<.....>', {'name': 'ullt',
     'description': 'U : UI Type\n 0 : List, 1 : Menu, 2 : Playback, 3 : Popup, 4 : Keyboard, 5 : Menu List\nll -> number of layer (00-FF)\nt : Update Type\n 0 : All, 1 : Button, 2 : Textbox, 3 : Listbox\n<.....> : XML data ( [CR] and [LF] are removed )'})]),
   'name': 'popup-message',
   'description': 'Popup Message'}),
  ('TPD', {'values': OrderedDict([('-99-999', {'name': 'temp',
     'description': u'The temperature Data(Fahrenheit and Celsius) 0 \u2013 999\n"F-99C-73": -99 degree Fahrenheit / -73 degree Celsius\n"F 32C  0": 32 degree Fahrenheit / 0 degree Celsius\n"F 41C  5": 41 degree Fahrenheit / 5 degree Celsius\n"F 50C 10": 50 degree Fahrenheit / 10 degree Celsius\n"F122C 50": 122 degree Fahrenheit / 50 degree Celsius\n"F212C100": 212 degree Fahrenheit / 100 degree Celsius\n"F302C150": 302 degree Fahrenheit / 150 degree Celsius\n\nReference Information:\n[TX-NR474/NR575E/8270/NR575/NR676/NR676E/RZ620/NR777/RZ720/RZ820, DTM-7, DRX-2.1/3.1/4.1/5.1 VSX-832/932/LX102/LX302]\n Yellow Zone: "F150C 66" or more & "F210C 99" or less\n Red Zone:" F212C100" or more\n[TX-RZ920, DRX-7.1/R1.1, DRC-R1.1]\n Yellow Zone: "F176C 80" or more & "F246C119" or less\n Red Zone: "F248C120" or more'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Temperature Data'})]),
   'name': 'temperature-data',
   'description': 'Temperature Data'}),
  ('TUN', {'values': OrderedDict([('nnnnn', {'name': 'freq-nnnnn',
     'description': 'sets Directly Tuning Frequency (FM nnn.nn MHz / AM nnnnn kHz / SR nnnnn ch)\nput 0 in the first two digits of nnnnn at SR'}),
    ('BAND', {'name': 'band', 'description': 'Change BAND'}),
    ('DIRECT', {'name': 'direct',
     'description': 'starts/restarts Direct Tuning Mode'}),
    ('0', {'name': '0-in-direct-mode',
     'description': 'sets 0 in Direct Tuning Mode'}),
    ('1', {'name': '1-in-direct-mode',
     'description': 'sets 1 in Direct Tuning Mode'}),
    ('2', {'name': '2-in-direct-mode',
     'description': 'sets 2 in Direct Tuning Mode'}),
    ('3', {'name': '3-in-direct-mode',
     'description': 'sets 3 in Direct Tuning Mode'}),
    ('4', {'name': '4-in-direct-mode',
     'description': 'sets 4 in Direct Tuning Mode'}),
    ('5', {'name': '5-in-direct-mode',
     'description': 'sets 5 in Direct Tuning Mode'}),
    ('6', {'name': '6-in-direct-mode',
     'description': 'sets 6 in Direct Tuning Mode'}),
    ('7', {'name': '7-in-direct-mode',
     'description': 'sets 7 in Direct Tuning Mode'}),
    ('8', {'name': '8-in-direct-mode',
     'description': 'sets 8 in Direct Tuning Mode'}),
    ('9', {'name': '9-in-direct-mode',
     'description': 'sets 9 in Direct Tuning Mode'}),
    ('UP', {'name': 'up',
     'description': 'sets Tuning Frequency Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Tuning Frequency Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Tuning Frequency'})]),
   'name': 'tuning',
   'description': 'Tuning Command (Include Tuner Pack Model Only)'}),
  ('PRS', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'}),
    ((1, 30), {'name': 'no-1-30',
     'description': 'sets Preset No. 1 - 30 ( In hexadecimal representation)'}),
    ('UP', {'name': 'up', 'description': 'sets Preset No. Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Preset No. Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Preset No.'})]),
   'name': 'preset',
   'description': 'Preset Command (Include Tuner Pack Model Only)'}),
  ('PRM', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'}),
    ((1, 30), {'name': 'no-1-30',
     'description': 'sets Preset No. 1 - 30 ( In hexadecimal representation)'})]),
   'name': 'preset-memory',
   'description': 'Preset Memory Command (Include Tuner Pack Model Only)'}),
  ('RDS', {'values': OrderedDict([(u'\u201c00\u201d', {'name': '00',
     'description': 'Display RT Information'}),
    (u'\u201c01\u201d', {'name': '01',
     'description': 'Display PTY Information'}),
    (u'\u201c02\u201d', {'name': '02',
     'description': 'Display TP Information'}),
    (u'\u201cUP\u201d', {'name': 'up',
     'description': 'Display RDS Information Wrap-Around Change'})]),
   'name': 'rds-information',
   'description': 'RDS Information Command (RDS Model Only)'}),
  ('PTS', {'values': OrderedDict([(u'\u201c01\u201d-\u201c1D\u201d', {'name': 'no-1-29',
     'description': u'sets PTY No \u201c1 - 29\u201d ( In hexadecimal representation)'}),
    (u'\u201cENTER\u201d', {'name': 'enter',
     'description': 'Finish PTY Scan'})]),
   'name': 'pty-scan',
   'description': 'PTY Scan Command (RDS Model Only)'}),
  ('TPS', {'values': OrderedDict([(u'\u201c\u201d', {'name': 'start',
     'description': u'Start TP Scan (When Don\u2019t Have Parameter)'}),
    (u'\u201cENTER\u201d', {'name': 'finish',
     'description': 'Finish TP Scan'})]),
   'name': 'tp-scan',
   'description': 'TP Scan Command (RDS Model Only)'}),
  ('XCN', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'channel-name',
     'description': 'XM Channel Name'}),
    ('QSTN', {'name': 'query', 'description': 'gets XM Channel Name'})]),
   'name': 'xm-channel-name-info',
   'description': 'XM Channel Name Info (XM Model Only)'}),
  ('XAT', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'artist-name',
     'description': 'XM Artist Name'}),
    ('QSTN', {'name': 'query', 'description': 'gets XM Artist Name'})]),
   'name': 'xm-artist-name-info',
   'description': 'XM Artist Name Info (XM Model Only)'}),
  ('XTI', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'title',
     'description': 'XM Title'}),
    ('QSTN', {'name': 'query', 'description': 'gets XM Title'})]),
   'name': 'xm-title-info',
   'description': 'XM Title Info (XM Model Only)'}),
  ('XCH', {'values': OrderedDict([((0, 597), {'name': 'channel-no-0-597',
     'description': u'XM Channel Number  \u201c000 - 255\u201d'}),
    ('UP', {'name': 'up', 'description': 'sets XM Channel Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets XM Channel Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets XM Channel Number'})]),
   'name': 'xm-channel-number',
   'description': 'XM Channel Number Command (XM Model Only)'}),
  ('XCT', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'category-info',
     'description': 'XM Category Info'}),
    ('UP', {'name': 'up', 'description': 'sets XM Category Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets XM Category Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets XM Category'})]),
   'name': 'xm-category',
   'description': 'XM Category Command (XM Model Only)'}),
  ('SCN', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'channel-name',
     'description': 'SIRIUS Channel Name'}),
    ('QSTN', {'name': 'query', 'description': 'gets SIRIUS Channel Name'})]),
   'name': 'sirius-channel-name-info',
   'description': 'SIRIUS Channel Name Info (SIRIUS Model Only)'}),
  ('SAT', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'artist-name',
     'description': 'SIRIUS Artist Name'}),
    ('QSTN', {'name': 'query', 'description': 'gets SIRIUS Artist Name'})]),
   'name': 'sirius-artist-name-info',
   'description': 'SIRIUS Artist Name Info (SIRIUS Model Only)'}),
  ('STI', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'title',
     'description': 'SIRIUS Title'}),
    ('QSTN', {'name': 'query', 'description': 'gets SIRIUS Title'})]),
   'name': 'sirius-title-info',
   'description': 'SIRIUS Title Info (SIRIUS Model Only)'}),
  ('SCH', {'values': OrderedDict([((0, 597), {'name': 'channel-no-0-597',
     'description': u'SIRIUS Channel Number  \u201c000 - 255\u201d'}),
    ('UP', {'name': 'up',
     'description': 'sets SIRIUS Channel Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets SIRIUS Channel Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets SIRIUS Channel Number'})]),
   'name': 'sirius-channel-number',
   'description': 'SIRIUS Channel Number Command (SIRIUS Model Only)'}),
  ('SCT', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'category-info',
     'description': 'SIRIUS Category Info'}),
    ('UP', {'name': 'up',
     'description': 'sets SIRIUS Category Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets SIRIUS Category Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets SIRIUS Category'})]),
   'name': 'sirius-category',
   'description': 'SIRIUS Category Command (SIRIUS Model Only)'}),
  ('SLK', {'values': OrderedDict([('nnnn', {'name': 'password',
     'description': 'Lock Password (4Digits)'}),
    ('INPUT', {'name': 'input',
     'description': 'displays "Please input the Lock password"'}),
    ('WRONG', {'name': 'wrong',
     'description': 'displays "The Lock password is wrong"'})]),
   'name': 'sirius-parental-lock',
   'description': 'SIRIUS Parental Lock Command (SIRIUS Model Only)'}),
  ('HAT', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'artist-name',
     'description': 'HD Radio Artist Name (variable-length, 64 digits max)'}),
    ('QSTN', {'name': 'query', 'description': 'gets HD Radio Artist Name'})]),
   'name': 'hd-radio-artist-name-info',
   'description': 'HD Radio Artist Name Info (HD Radio Model Only)'}),
  ('HCN', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'channel-name',
     'description': 'HD Radio Channel Name (Station Name) (7 digits)'}),
    ('QSTN', {'name': 'query', 'description': 'gets HD Radio Channel Name'})]),
   'name': 'hd-radio-channel-name-info',
   'description': 'HD Radio Channel Name Info (HD Radio Model Only)'}),
  ('HTI', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'title',
     'description': 'HD Radio Title (variable-length, 64 digits max)'}),
    ('QSTN', {'name': 'query', 'description': 'gets HD Radio Title'})]),
   'name': 'hd-radio-title-info',
   'description': 'HD Radio Title Info (HD Radio Model Only)'}),
  ('HDS', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'info',
     'description': 'HD Radio Title'}),
    ('QSTN', {'name': 'query', 'description': 'gets HD Radio Title'})]),
   'name': 'hd-radio-detail-info',
   'description': 'HD Radio Detail Info (HD Radio Model Only)'}),
  ('HPR', {'values': OrderedDict([((1, 8), {'name': 'directly',
     'description': 'sets directly HD Radio Channel Program'}),
    ('QSTN', {'name': 'query',
     'description': 'gets HD Radio Channel Program'})]),
   'name': 'hd-radio-channel-program',
   'description': 'HD Radio Channel Program Command (HD Radio Model Only)'}),
  ('HBL', {'values': OrderedDict([('00', {'name': 'auto',
     'description': 'sets HD Radio Blend Mode "Auto"'}),
    ('01', {'name': 'analog',
     'description': 'sets HD Radio Blend Mode "Analog"'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the HD Radio Blend Mode Status'})]),
   'name': 'hd-radio-blend-mode',
   'description': 'HD Radio Blend Mode Command (HD Radio Model Only)'}),
  ('HTS', {'values': OrderedDict([('mmnnoo', {'name': 'mmnnoo',
     'description': 'HD Radio Tuner Status (3 bytes)\nmm -> "00" not HD, "01" HD\nnn -> current Program "01"-"08"\noo -> receivable Program (8 bits are represented in hexadecimal notation. Each bit shows receivable or not.)'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the HD Radio Tuner Status'})]),
   'name': 'hd-radio-tuner-status',
   'description': 'HD Radio Tuner Status (HD Radio Model Only)'}),
  ('BCS', {'values': OrderedDict([('00', {'name': 'charging',
     'description': 'charging'}),
    ('01', {'name': 'completed', 'description': 'charge completed'}),
    ('10', {'name': 'low', 'description': 'battery level Low'}),
    ('11', {'name': 'middle', 'description': 'battery level Middle'}),
    ('12', {'name': 'high', 'description': 'battery level High'}),
    ('QSTN', {'name': 'query', 'description': 'gets battery charge status'})]),
   'name': 'battery-charge-status',
   'description': 'Battery Charge Status Command (Battery Model Only)'}),
  ('CCD', {'values': OrderedDict([('PLAY', {'name': 'play',
     'description': 'PLAY'}),
    ('STOP', {'name': 'stop', 'description': 'STOP'}),
    ('PAUSE', {'name': 'pause', 'description': 'PAUSE'}),
    ('SKIP.F', {'name': 'next', 'description': '>>I'}),
    ('SKIP.R', {'name': 'previous', 'description': 'I<<'}),
    ('REPEAT', {'name': 'repeat', 'description': 'REPEAT'}),
    ('RANDOM', {'name': 'random', 'description': 'RANDOM'})]),
   'name': 'cd-player',
   'description': 'CD Player Operation Command  (Include CD Function Model Only)'}),
  ('CST', {'values': OrderedDict([('prs', {'name': 'status',
     'description': 'CD Play Status (3 letters)\np -> Play Status: "S": STOP, "P": Play, "p": Pause, "F": FF, "R": FR\nr -> Repeat Status: "-": Off, "R": All,  "1": Repeat 1\ns -> Shuffle(Random) Status: "-": Off, "S": All'}),
    ('QSTN', {'name': 'query', 'description': 'gets CD Play Status'})]),
   'name': 'cd-play-status',
   'description': 'CD Play Status'}),
  ('DST', {'values': OrderedDict([('00', {'name': 'none',
     'description': 'No disc'}),
    ('04', {'name': 'cd', 'description': 'Audio CD'}),
    ('07', {'name': 'mp3-cd', 'description': 'MP3 CD'}),
    ('FF', {'name': 'unknown', 'description': 'Unknown'}),
    ('QSTN', {'name': 'query', 'description': 'gets Disc Status'})]),
   'name': 'current-disc-status-notice',
   'description': 'Current disc status notice'}),
  ('CFS', {'values': OrderedDict([((1, 153), {'name': 'folder-no-1-153',
     'description': 'Folder Number'}),
    ('QSTN', {'name': 'query', 'description': 'gets Folder Number Info'})]),
   'name': 'current-folder-status-no',
   'description': u'Current Folder Status\uff08No.\uff09'}),
  ('CTM', {'values': OrderedDict([('mm:ss/mm:ss', {'name': 'time-mm-ss-mm-ss',
     'description': 'CD Time Info (Elapsed time/Track Time Max 99:59)'}),
    ('QSTN', {'name': 'query', 'description': 'gets CDTime Info'})]),
   'name': 'cd-time-info',
   'description': 'CD Time Info'}),
  ('SCE', {'values': OrderedDict([('mm:ss', {'name': 'time-mm-ss',
     'description': u'Specified\u3000Elapsed CD Time'})]),
   'name': 'set-cd-elapsed-time',
   'description': u'Set\u3000CD Elapsed\u3000Time'}),
  ('DSN', {'values': OrderedDict([(u'xx\u2026xx', {'name': 'station-name',
     'description': u'xx\u2026xx   : DAB Station Name (UTF-8)\n\u4e0b\u8a18\u6587\u5b57\u306f\u7279\u6b8a\u30b3\u30fc\u30c9\u3067\u9001\u4fe1\u3059\u308b\n0x02  \uff1a \u2190\n0x03  \uff1a \u2191\n0x04  \uff1a \u2192\n0x05  \uff1a \u2193\n0x06  \uff1a \u2551\n\u4e0a\u8a18\u4ee5\u5916\u306e0x00~0x1F\u30010x80~0xA0\u306f\u30b9\u30da\u30fc\u30b9\u8868\u793a\u3068\u3059\u308b'}),
    ('QSTN', {'name': 'query', 'description': 'gets Station Name'})]),
   'name': 'dab-station-name',
   'description': 'DAB Station Name'})])),
 ('zone2', OrderedDict([('ZPW', {'values': OrderedDict([('00', {'name': 'standby',
     'description': 'sets Zone2 Standby'}),
    ('01', {'name': 'on', 'description': 'sets Zone2 On'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Zone2 Power Status'})]),
   'name': 'power',
   'description': 'Zone2 Power Command'}),
  ('ZPA', {'values': OrderedDict([('00', {'name': ('off'),
     'description': 'sets Zone 2 A Off'}),
    ('01', {'name': ('on'), 'description': 'sets Zone 2 A On'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Speaker State'})]),
   'name': 'zone-2-a',
   'description': 'Zone 2 A Command'}),
  ('ZPB', {'values': OrderedDict([('00', {'name': ('off'),
     'description': 'sets Zone 2 B Off'}),
    ('01', {'name': ('on'), 'description': 'sets Zone 2 B On'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Speaker State'})]),
   'name': 'zone-2-b',
   'description': 'Zone 2 B Command'}),
  ('ZMT', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Zone2 Muting Off'}),
    ('01', {'name': 'on', 'description': 'sets Zone2 Muting On'}),
    ('TG', {'name': 'toggle', 'description': 'sets Zone2 Muting Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Zone2 Muting Status'})]),
   'name': 'muting',
   'description': 'Zone2 Muting Command'}),
  ('ZVL', {'values': OrderedDict([((0, 200), {'name': None,
     'description': u'Volume Level 0.0 \u2013 100.0 ( In hexadecimal representation)'}),
    ((0, 100), {'name': 'vol-0-100',
     'description': u'Volume Level 0 \u2013 100 ( In hexadecimal representation)'}),
    ((0, 80), {'name': None,
     'description': u'Volume Level 0 \u2013 80 ( In hexadecimal representation)'}),
    ('UP', {'name': 'level-up', 'description': 'sets Volume Level Up'}),
    ('DOWN', {'name': 'level-down', 'description': 'sets Volume Level Down'}),
    ('UP1', {'name': 'level-up-1db-step',
     'description': 'sets Volume Level Up 1dB Step'}),
    ('DOWN1', {'name': 'level-down-1db-step',
     'description': 'sets Volume Level Down 1dB Step'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Volume Level'})]),
   'name': 'volume',
   'description': 'Zone2 Volume Command'}),
  ('ZTN', {'values': OrderedDict([('B{xx}', {'name': 'bass-xx-is-a-00-a-10-0-10-1-step',
     'description': 'sets Zone2 Bass (xx is "-A"..."00"..."+A"[-10...0...+10 1 step]'}),
    ('T{xx}', {'name': 'treble-xx-is-a-00-a-10-0-10-1-step',
     'description': 'sets Zone2 Treble (xx is "-A"..."00"..."+A"[-10...0...+10 1 step]'}),
    ('BUP', {'name': 'bass-up', 'description': 'sets Bass Up (1 Step)'}),
    ('BDOWN', {'name': 'bass-down', 'description': 'sets Bass Down (1 Step)'}),
    ('TUP', {'name': 'treble-up', 'description': 'sets Treble Up (1 Step)'}),
    ('TDOWN', {'name': 'treble-down',
     'description': 'sets Treble Down (1 Step)'}),
    ('QSTN', {'name': 'query', 'description': 'gets Zone2 Tone ("BxxTxx")'})]),
   'name': 'tone',
   'description': 'Zone2 Tone Command'}),
  ('ZBL', {'values': OrderedDict([('{xx}', {'name': 'xx-is-a-00-a-l-10-0-r-10-1-step',
     'description': 'sets Zone2 Balance (xx is "-A"..."00"..."+A"[L+10...0...R+10 1 step]'}),
    ('UP', {'name': 'up', 'description': 'sets Balance Up (to R 1 Step)'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Balance Down (to L 1 Step)'}),
    ('QSTN', {'name': 'query', 'description': 'gets Zone2 Balance'})]),
   'name': 'balance',
   'description': 'Zone2 Balance Command'}),
  ('SLZ', {'values': OrderedDict([('00', {'name': ('video1',
      'vcr',
      'dvr',
      'stb',
      'dvr'),
     'description': 'sets VIDEO1, VCR/DVR, STB/DVR'}),
    ('01', {'name': ('video2', 'cbl', 'sat'),
     'description': 'sets VIDEO2, CBL/SAT'}),
    ('02', {'name': ('video3', 'game/tv', 'game', 'game1'),
     'description': 'sets VIDEO3, GAME/TV, GAME, GAME1'}),
    ('03', {'name': ('video4', 'aux1'),
     'description': 'sets VIDEO4, AUX1(AUX)'}),
    ('04', {'name': ('video5', 'aux2', 'game2'),
     'description': 'sets VIDEO5, AUX2, GAME2'}),
    ('05', {'name': ('video6', 'pc'), 'description': 'sets VIDEO6, PC'}),
    ('06', {'name': 'video7', 'description': 'sets VIDEO7'}),
    ('07', {'name': ('hidden1', 'extra1'),
     'description': 'sets Hidden1, EXTRA1'}),
    ('08', {'name': ('hidden2', 'extra2'),
     'description': 'sets Hidden2, EXTRA2'}),
    ('09', {'name': ('hidden3', 'extra3'),
     'description': 'sets Hidden3, EXTRA3'}),
    ('10', {'name': ('dvd', 'bd', 'dvd'), 'description': 'sets DVD, BD/DVD'}),
    ('11', {'name': 'strm-box', 'description': 'sets STRM BOX'}),
    ('12', {'name': 'tv', 'description': 'sets TV'}),
    ('20', {'name': 'tape', 'description': 'sets TAPE(1)'}),
    ('21', {'name': 'tape2', 'description': 'sets TAPE2'}),
    ('22', {'name': 'phono', 'description': 'sets PHONO'}),
    ('23', {'name': ('cd', 'tv/cd'), 'description': 'sets CD, TV/CD'}),
    ('24', {'name': 'fm', 'description': 'sets FM'}),
    ('25', {'name': 'am', 'description': 'sets AM'}),
    ('26', {'name': 'tuner', 'description': 'sets TUNER'}),
    ('27', {'name': ('music-server', 'p4s', 'dlna'),
     'description': 'sets MUSIC SERVER, P4S, DLNA'}),
    ('28', {'name': ('internet-radio', 'iradio-favorite'),
     'description': 'sets INTERNET RADIO, iRadio Favorite'}),
    ('29', {'name': ('usb', 'usb'), 'description': 'sets USB/USB(Front)'}),
    ('2A', {'name': 'usb', 'description': 'sets USB(Rear)'}),
    ('2B', {'name': ('network', 'net'), 'description': 'sets NETWORK, NET'}),
    ('2C', {'name': 'usb', 'description': 'sets USB(toggle)'}),
    ('2D', {'name': 'airplay', 'description': 'sets Airplay'}),
    ('2E', {'name': 'bluetooth', 'description': 'sets Bluetooth'}),
    ('40', {'name': 'universal-port', 'description': 'sets Universal PORT'}),
    ('30', {'name': 'multi-ch', 'description': 'sets MULTI CH'}),
    ('31', {'name': 'xm', 'description': 'sets XM'}),
    ('32', {'name': 'sirius', 'description': 'sets SIRIUS'}),
    ('33', {'name': 'dab', 'description': 'sets DAB '}),
    ('55', {'name': 'hdmi-5', 'description': 'sets HDMI 5'}),
    ('56', {'name': 'hdmi-6', 'description': 'sets HDMI 6'}),
    ('57', {'name': 'hdmi-7', 'description': 'sets HDMI 7'}),
    ('7F', {'name': 'off', 'description': 'sets OFF'}),
    ('80', {'name': 'source', 'description': 'sets SOURCE'}),
    ('UP', {'name': 'up',
     'description': 'sets Selector Position Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Selector Position Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Selector Position'})]),
   'name': 'selector',
   'description': 'ZONE2 Selector Command'}),
  ('TUN', {'values': OrderedDict([('nnnnn', {'name': 'freq-nnnnn',
     'description': 'sets Directly Tuning Frequency (FM nnn.nn MHz / AM nnnnn kHz / XM nnnnn ch)'}),
    ('UP', {'name': 'up',
     'description': 'sets Tuning Frequency Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Tuning Frequency Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Tuning Frequency'})]),
   'name': 'tuning',
   'description': 'Tuning Command'}),
  ('TUZ', {'values': OrderedDict([('nnnnn', {'name': 'freq-nnnnn',
     'description': 'sets Directly Tuning Frequency (FM nnn.nn MHz / AM nnnnn kHz / SR nnnnn ch)'}),
    ('DIRECT', {'name': 'direct',
     'description': 'starts/restarts Direct Tuning Mode'}),
    ('BAND', {'name': 'band', 'description': 'Change BAND'}),
    ('0', {'name': '0-in-direct-mode',
     'description': 'sets 0 in Direct Tuning Mode'}),
    ('1', {'name': '1-in-direct-mode',
     'description': 'sets 1 in Direct Tuning Mode'}),
    ('2', {'name': '2-in-direct-mode',
     'description': 'sets 2 in Direct Tuning Mode'}),
    ('3', {'name': '3-in-direct-mode',
     'description': 'sets 3 in Direct Tuning Mode'}),
    ('4', {'name': '4-in-direct-mode',
     'description': 'sets 4 in Direct Tuning Mode'}),
    ('5', {'name': '5-in-direct-mode',
     'description': 'sets 5 in Direct Tuning Mode'}),
    ('6', {'name': '6-in-direct-mode',
     'description': 'sets 6 in Direct Tuning Mode'}),
    ('7', {'name': '7-in-direct-mode',
     'description': 'sets 7 in Direct Tuning Mode'}),
    ('8', {'name': '8-in-direct-mode',
     'description': 'sets 8 in Direct Tuning Mode'}),
    ('9', {'name': '9-in-direct-mode',
     'description': 'sets 9 in Direct Tuning Mode'}),
    ('UP', {'name': 'up',
     'description': 'sets Tuning Frequency Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Tuning Frequency Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Tuning Frequency'})]),
   'name': 'tuning',
   'description': 'Tuning Command'}),
  ('PRS', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'}),
    ((1, 30), {'name': 'no-1-30',
     'description': 'sets Preset No. 1 - 30 ( In hexadecimal representation)'}),
    ('UP', {'name': 'up', 'description': 'sets Preset No. Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Preset No. Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Preset No.'})]),
   'name': 'preset',
   'description': 'Preset Command'}),
  ('PRZ', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'}),
    ((1, 30), {'name': 'no-1-30',
     'description': 'sets Preset No. 1 - 30 ( In hexadecimal representation)'}),
    ('UP', {'name': 'up', 'description': 'sets Preset No. Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Preset No. Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Preset No.'})]),
   'name': 'preset',
   'description': 'Preset Command'}),
  ('NTC', {'values': OrderedDict([('PLAYz', {'name': 'playz',
     'description': 'PLAY KEY'}),
    ('STOPz', {'name': 'stopz', 'description': 'STOP KEY'}),
    ('PAUSEz', {'name': 'pausez', 'description': 'PAUSE KEY'}),
    ('TRUPz', {'name': 'trupz', 'description': 'TRACK UP KEY'}),
    ('TRDNz', {'name': 'trdnz', 'description': 'TRACK DOWN KEY'})]),
   'name': 'net-tune-network',
   'description': 'Net-Tune/Network Operation Command(Net-Tune Model Only)'}),
  ('NTZ', {'values': OrderedDict([('PLAY', {'name': 'play',
     'description': 'PLAY KEY'}),
    ('STOP', {'name': 'stop', 'description': 'STOP KEY'}),
    ('PAUSE', {'name': 'pause', 'description': 'PAUSE KEY'}),
    ('P/P', {'name': 'p-p', 'description': 'PLAY / PAUSE KEY'}),
    ('TRUP', {'name': 'trup', 'description': 'TRACK UP KEY'}),
    ('TRDN', {'name': 'trdn', 'description': 'TRACK DOWN KEY'}),
    ('CHUP', {'name': 'chup', 'description': 'CH UP(for iRadio)'}),
    ('CHDN', {'name': 'chdn', 'description': 'CH DOWN(for iRadio)'}),
    ('FF', {'name': 'ff',
     'description': 'FF KEY (CONTINUOUS*) (for iPod 1wire)'}),
    ('REW', {'name': 'rew',
     'description': 'REW KEY (CONTINUOUS*) (for iPod 1wire)'}),
    ('REPEAT', {'name': 'repeat',
     'description': 'REPEAT KEY(for iPod 1wire)'}),
    ('RANDOM', {'name': 'random',
     'description': 'RANDOM KEY(for iPod 1wire)'}),
    ('REP/SHF', {'name': 'rep-shf', 'description': 'REPEAT / SHUFFLE KEY'}),
    ('DISPLAY', {'name': 'display',
     'description': 'DISPLAY KEY(for iPod 1wire)'}),
    ('MEMORY', {'name': 'memory', 'description': 'MEMORY KEY'}),
    ('MODE', {'name': 'mode', 'description': 'MODE KEY'}),
    ('RIGHT', {'name': 'right', 'description': 'RIGHT KEY(for iPod 1wire)'}),
    ('LEFT', {'name': 'left', 'description': 'LEFT KEY(for iPod 1wire)'}),
    ('UP', {'name': 'up', 'description': 'UP KEY(for iPod 1wire)'}),
    ('DOWN', {'name': 'down', 'description': 'DOWN KEY(for iPod 1wire)'}),
    ('SELECT', {'name': 'select',
     'description': 'SELECT KEY(for iPod 1wire)'}),
    ('RETURN', {'name': 'return',
     'description': 'RETURN KEY(for iPod 1wire)'})]),
   'name': 'net-tune-network',
   'description': 'Net-Tune/Network Operation Command(Network Model Only)'}),
  ('NPZ', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'})]),
   'name': 'internet-radio-preset',
   'description': 'Internet Radio Preset Command (Network Model Only)'}),
  ('LMZ', {'values': OrderedDict([('00', {'name': 'stereo',
     'description': 'sets STEREO'}),
    ('01', {'name': 'direct', 'description': 'sets DIRECT'}),
    ('0F', {'name': 'mono', 'description': 'sets MONO'}),
    ('12', {'name': 'multiplex', 'description': 'sets MULTIPLEX'}),
    ('87', {'name': 'dvs', 'description': 'sets DVS(Pl2)'}),
    ('88', {'name': 'dvs', 'description': 'sets DVS(NEO6)'})]),
   'name': 'listening-mode',
   'description': 'Listening Mode Command'}),
  ('LTZ', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Late Night Off'}),
    ('01', {'name': 'low', 'description': 'sets Late Night Low'}),
    ('02', {'name': 'high', 'description': 'sets Late Night High'}),
    ('UP', {'name': 'up',
     'description': 'sets Late Night State Wrap-Around Up'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Late Night Level'})]),
   'name': 'late-night',
   'description': 'Late Night Command'}),
  ('RAZ', {'values': OrderedDict([('00', {'name': 'both-off',
     'description': 'sets Both Off'}),
    ('01', {'name': 'on', 'description': 'sets Re-EQ On'}),
    ('02', {'name': 'on', 'description': 'sets Academy On'}),
    ('UP', {'name': 'up',
     'description': 'sets Re-EQ/Academy State Wrap-Around Up'}),
    ('QSTN', {'name': 'query',
     'description': 'gets The Re-EQ/Academy State'})]),
   'name': 're-eq-academy-filter',
   'description': 'Re-EQ/Academy Filter Command'})])),
 ('zone3', OrderedDict([('PW3', {'values': OrderedDict([('00', {'name': 'standby',
     'description': 'sets Zone3 Standby'}),
    ('01', {'name': 'on', 'description': 'sets Zone3 On'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Zone3 Power Status'})]),
   'name': 'power',
   'description': 'Zone3 Power Command'}),
  ('MT3', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Zone3 Muting Off'}),
    ('01', {'name': 'on', 'description': 'sets Zone3 Muting On'}),
    ('TG', {'name': 'toggle', 'description': 'sets Zone3 Muting Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Zone3 Muting Status'})]),
   'name': 'muting',
   'description': 'Zone3 Muting Command'}),
  ('VL3', {'values': OrderedDict([((0, 200), {'name': None,
     'description': u'Volume Level 0.0 \u2013 100.0 ( In hexadecimal representation)'}),
    ((0, 100), {'name': 'vol-0-100',
     'description': u'Volume Level 0 \u2013 100 ( In hexadecimal representation)'}),
    ((0, 80), {'name': None,
     'description': u'Volume Level 0 \u2013 80 ( In hexadecimal representation)'}),
    ('UP', {'name': 'level-up', 'description': 'sets Volume Level Up'}),
    ('DOWN', {'name': 'level-down', 'description': 'sets Volume Level Down'}),
    ('UP1', {'name': 'level-up-1db-step',
     'description': 'sets Volume Level Up 1dB Step'}),
    ('DOWN1', {'name': 'level-down-1db-step',
     'description': 'sets Volume Level Down 1dB Step'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Volume Level'})]),
   'name': 'volume',
   'description': 'Zone3 Volume Command'}),
  ('TN3', {'values': OrderedDict([('B{xx}', {'name': 'b-xx',
     'description': 'Zone3 Bass (xx is "-A"..."00"..."+A"[-10...0...+10 1 step])'}),
    ('T{xx}', {'name': 't-xx',
     'description': 'Zone3 Treble (xx is "-A"..."00"..."+A"[-10...0...+10 1 step])'}),
    ('BUP', {'name': 'bass-up', 'description': 'sets Bass Up (1 Step)'}),
    ('BDOWN', {'name': 'bass-down', 'description': 'sets Bass Down (1 Step)'}),
    ('TUP', {'name': 'treble-up', 'description': 'sets Treble Up (1 Step)'}),
    ('TDOWN', {'name': 'treble-down',
     'description': 'sets Treble Down (1 Step)'}),
    ('QSTN', {'name': 'query', 'description': 'gets Zone3 Tone ("BxxTxx")'})]),
   'name': 'tone',
   'description': 'Zone3 Tone Command'}),
  ('BL3', {'values': OrderedDict([('{xx}', {'name': 'xx',
     'description': 'Zone3 Balance (xx is "-A"..."00"..."+A"[L+10...0...R+10 1 step])'}),
    ('UP', {'name': 'up', 'description': 'sets Balance Up (to R 1 Step)'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Balance Down (to L 1 Step)'}),
    ('QSTN', {'name': 'query', 'description': 'gets Zone3 Balance'})]),
   'name': 'balance',
   'description': 'Zone3 Balance Command'}),
  ('SL3', {'values': OrderedDict([('00', {'name': ('video1',
      'vcr',
      'dvr',
      'stb',
      'dvr'),
     'description': 'sets VIDEO1, VCR/DVR, STB/DVR'}),
    ('01', {'name': ('video2', 'cbl', 'sat'),
     'description': 'sets VIDEO2, CBL/SAT'}),
    ('02', {'name': ('video3', 'game/tv', 'game', 'game1'),
     'description': 'sets VIDEO3, GAME/TV, GAME, GAME1'}),
    ('03', {'name': ('video4', 'aux1'),
     'description': 'sets VIDEO4, AUX1(AUX)'}),
    ('04', {'name': ('video5', 'aux2', 'game2'),
     'description': 'sets VIDEO5, AUX2, GAME2'}),
    ('05', {'name': ('video6', 'pc'), 'description': 'sets VIDEO6, PC'}),
    ('06', {'name': 'video7', 'description': 'sets VIDEO7'}),
    ('07', {'name': ('hidden1', 'extra1'),
     'description': 'sets Hidden1, EXTRA1'}),
    ('08', {'name': ('hidden2', 'extra2'),
     'description': 'sets Hidden2, EXTRA2'}),
    ('09', {'name': ('hidden3', 'extra3'),
     'description': 'sets Hidden3, EXTRA3'}),
    ('10', {'name': 'dvd', 'description': 'sets DVD'}),
    ('11', {'name': 'strm-box', 'description': 'sets STRM BOX'}),
    ('12', {'name': 'tv', 'description': 'sets TV'}),
    ('20', {'name': 'tape', 'description': 'sets TAPE(1)'}),
    ('21', {'name': 'tape2', 'description': 'sets TAPE2'}),
    ('22', {'name': 'phono', 'description': 'sets PHONO'}),
    ('23', {'name': ('cd', 'tv/cd'), 'description': 'sets CD, TV/CD'}),
    ('24', {'name': 'fm', 'description': 'sets FM'}),
    ('25', {'name': 'am', 'description': 'sets AM'}),
    ('26', {'name': 'tuner', 'description': 'sets TUNER'}),
    ('27', {'name': ('music-server', 'p4s', 'dlna'),
     'description': 'sets MUSIC SERVER, P4S, DLNA'}),
    ('28', {'name': ('internet-radio', 'iradio-favorite'),
     'description': 'sets INTERNET RADIO, iRadio Favorite'}),
    ('29', {'name': ('usb', 'usb'), 'description': 'sets USB/USB(Front)'}),
    ('2A', {'name': 'usb', 'description': 'sets USB(Rear)'}),
    ('2B', {'name': ('network', 'net'), 'description': 'sets NETWORK, NET'}),
    ('2C', {'name': 'usb', 'description': 'sets USB(toggle)'}),
    ('2D', {'name': 'airplay', 'description': 'sets Airplay'}),
    ('2E', {'name': 'bluetooth', 'description': 'sets Bluetooth'}),
    ('40', {'name': 'universal-port', 'description': 'sets Universal PORT'}),
    ('30', {'name': 'multi-ch', 'description': 'sets MULTI CH'}),
    ('31', {'name': 'xm', 'description': 'sets XM'}),
    ('32', {'name': 'sirius', 'description': 'sets SIRIUS'}),
    ('33', {'name': 'dab', 'description': 'sets DAB '}),
    ('80', {'name': 'source', 'description': 'sets SOURCE'}),
    ('UP', {'name': 'up',
     'description': 'sets Selector Position Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Selector Position Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Selector Position'})]),
   'name': 'selector',
   'description': 'ZONE3 Selector Command'}),
  ('TUN', {'values': OrderedDict([('nnnnn', {'name': 'freq-nnnnn',
     'description': 'sets Directly Tuning Frequency (FM nnn.nn MHz / AM nnnnn kHz)'}),
    ('UP', {'name': 'up',
     'description': 'sets Tuning Frequency Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Tuning Frequency Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Tuning Frequency'})]),
   'name': 'tuning',
   'description': 'Tuning Command'}),
  ('TU3', {'values': OrderedDict([('nnnnn', {'name': 'freq-nnnnn',
     'description': 'sets Directly Tuning Frequency (FM nnn.nn MHz / AM nnnnn kHz / SR nnnnn ch)'}),
    ('BAND', {'name': 'band', 'description': 'Change BAND'}),
    ('DIRECT', {'name': 'direct',
     'description': 'starts/restarts Direct Tuning Mode'}),
    ('0', {'name': '0-in-direct-mode',
     'description': 'sets 0 in Direct Tuning Mode'}),
    ('1', {'name': '1-in-direct-mode',
     'description': 'sets 1 in Direct Tuning Mode'}),
    ('2', {'name': '2-in-direct-mode',
     'description': 'sets 2 in Direct Tuning Mode'}),
    ('3', {'name': '3-in-direct-mode',
     'description': 'sets 3 in Direct Tuning Mode'}),
    ('4', {'name': '4-in-direct-mode',
     'description': 'sets 4 in Direct Tuning Mode'}),
    ('5', {'name': '5-in-direct-mode',
     'description': 'sets 5 in Direct Tuning Mode'}),
    ('6', {'name': '6-in-direct-mode',
     'description': 'sets 6 in Direct Tuning Mode'}),
    ('7', {'name': '7-in-direct-mode',
     'description': 'sets 7 in Direct Tuning Mode'}),
    ('8', {'name': '8-in-direct-mode',
     'description': 'sets 8 in Direct Tuning Mode'}),
    ('9', {'name': '9-in-direct-mode',
     'description': 'sets 9 in Direct Tuning Mode'}),
    ('UP', {'name': 'up',
     'description': 'sets Tuning Frequency Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Tuning Frequency Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Tuning Frequency'})]),
   'name': 'tuning',
   'description': 'Tuning Command'}),
  ('PRS', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'}),
    ((1, 30), {'name': 'no-1-30',
     'description': 'sets Preset No. 1 - 30 ( In hexadecimal representation)'}),
    ('UP', {'name': 'up', 'description': 'sets Preset No. Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Preset No. Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Preset No.'})]),
   'name': 'preset',
   'description': 'Preset Command'}),
  ('PR3', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'}),
    ((1, 30), {'name': 'no-1-30',
     'description': 'sets Preset No. 1 - 30 ( In hexadecimal representation)'}),
    ('UP', {'name': 'up', 'description': 'sets Preset No. Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Preset No. Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Preset No.'})]),
   'name': 'preset',
   'description': 'Preset Command'}),
  ('NTC', {'values': OrderedDict([('PLAYz', {'name': 'playz',
     'description': 'PLAY KEY'}),
    ('STOPz', {'name': 'stopz', 'description': 'STOP KEY'}),
    ('PAUSEz', {'name': 'pausez', 'description': 'PAUSE KEY'}),
    ('TRUPz', {'name': 'trupz', 'description': 'TRACK UP KEY'}),
    ('TRDNz', {'name': 'trdnz', 'description': 'TRACK DOWN KEY'})]),
   'name': 'net-tune-network',
   'description': 'Net-Tune/Network Operation Command(Net-Tune Model Only)'}),
  ('NT3', {'values': OrderedDict([('PLAY', {'name': 'play',
     'description': 'PLAY KEY'}),
    ('STOP', {'name': 'stop', 'description': 'STOP KEY'}),
    ('PAUSE', {'name': 'pause', 'description': 'PAUSE KEY'}),
    ('P/P', {'name': 'p-p', 'description': 'PLAY / PAUSE KEY'}),
    ('TRUP', {'name': 'trup', 'description': 'TRACK UP KEY'}),
    ('TRDN', {'name': 'trdn', 'description': 'TRACK DOWN KEY'}),
    ('CHUP', {'name': 'chup', 'description': 'CH UP(for iRadio)'}),
    ('CHDN', {'name': 'chdn', 'description': 'CH DOWNP(for iRadio)'}),
    ('FF', {'name': 'ff',
     'description': 'FF KEY (CONTINUOUS*) (for iPod 1wire)'}),
    ('REW', {'name': 'rew',
     'description': 'REW KEY (CONTINUOUS*) (for iPod 1wire)'}),
    ('REPEAT', {'name': 'repeat',
     'description': 'REPEAT KEY(for iPod 1wire)'}),
    ('RANDOM', {'name': 'random',
     'description': 'RANDOM KEY(for iPod 1wire)'}),
    ('REP/SHF', {'name': 'rep-shf', 'description': 'REPEAT / SHUFFLE KEY'}),
    ('DISPLAY', {'name': 'display',
     'description': 'DISPLAY KEY(for iPod 1wire)'}),
    ('MEMORY', {'name': 'memory', 'description': 'MEMORY KEY'}),
    ('RIGHT', {'name': 'right', 'description': 'RIGHT KEY(for iPod 1wire)'}),
    ('LEFT', {'name': 'left', 'description': 'LEFT KEY(for iPod 1wire)'}),
    ('UP', {'name': 'up', 'description': 'UP KEY(for iPod 1wire)'}),
    ('DOWN', {'name': 'down', 'description': 'DOWN KEY(for iPod 1wire)'}),
    ('SELECT', {'name': 'select',
     'description': 'SELECT KEY(for iPod 1wire)'}),
    ('RETURN', {'name': 'return',
     'description': 'RETURN KEY(for iPod 1wire)'})]),
   'name': 'net-tune-network',
   'description': 'Net-Tune/Network Operation Command(Network Model Only)'}),
  ('NP3', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'})]),
   'name': 'internet-radio-preset',
   'description': 'Internet Radio Preset Command (Network Model Only)'})])),
 ('zone4', OrderedDict([('PW4', {'values': OrderedDict([('00', {'name': 'standby',
     'description': 'sets Zone4 Standby'}),
    ('01', {'name': 'on', 'description': 'sets Zone4 On'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Zone4 Power Status'})]),
   'name': 'power',
   'description': 'Zone4 Power Command'}),
  ('MT4', {'values': OrderedDict([('00', {'name': 'off',
     'description': 'sets Zone4 Muting Off'}),
    ('01', {'name': 'on', 'description': 'sets Zone4 Muting On'}),
    ('TG', {'name': 'toggle', 'description': 'sets Zone4 Muting Wrap-Around'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Zone4 Muting Status'})]),
   'name': 'muting',
   'description': 'Zone4 Muting Command'}),
  ('VL4', {'values': OrderedDict([((0, 100), {'name': 'vol-0-100',
     'description': u'Volume Level 0 \u2013 100 ( In hexadecimal representation)'}),
    ((0, 80), {'name': None,
     'description': u'Volume Level 0 \u2013 80 ( In hexadecimal representation)'}),
    ('UP', {'name': 'level-up', 'description': 'sets Volume Level Up'}),
    ('DOWN', {'name': 'level-down', 'description': 'sets Volume Level Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Volume Level'})]),
   'name': 'volume',
   'description': 'Zone4 Volume Command'}),
  ('SL4', {'values': OrderedDict([('00', {'name': ('video1',
      'vcr',
      'dvr',
      'stb',
      'dvr'),
     'description': 'sets VIDEO1, VCR/DVR, STB/DVR'}),
    ('01', {'name': ('video2', 'cbl', 'sat'),
     'description': 'sets VIDEO2, CBL/SAT'}),
    ('02', {'name': ('video3', 'game/tv', 'game', 'game1'),
     'description': 'sets VIDEO3, GAME/TV, GAME, GAME1'}),
    ('03', {'name': ('video4', 'aux1'),
     'description': 'sets VIDEO4, AUX1(AUX)'}),
    ('04', {'name': ('video5', 'aux2', 'game2'),
     'description': 'sets VIDEO5, AUX2, GAME2'}),
    ('05', {'name': ('video6', 'pc'), 'description': 'sets VIDEO6, PC'}),
    ('06', {'name': 'video7', 'description': 'sets VIDEO7'}),
    ('07', {'name': ('hidden1', 'extra1'),
     'description': 'sets Hidden1, EXTRA1'}),
    ('08', {'name': ('hidden2', 'extra2'),
     'description': 'sets Hidden2, EXTRA2'}),
    ('09', {'name': ('hidden3', 'extra3'),
     'description': 'sets Hidden3, EXTRA3'}),
    ('10', {'name': ('dvd', 'bd', 'dvd'), 'description': 'sets DVD, BD/DVD'}),
    ('20', {'name': ('tape-1', 'tv/tape'),
     'description': 'sets TAPE(1), TV/TAPE'}),
    ('21', {'name': 'tape2', 'description': 'sets TAPE2'}),
    ('22', {'name': 'phono', 'description': 'sets PHONO'}),
    ('23', {'name': ('cd', 'tv/cd'), 'description': 'sets CD, TV/CD'}),
    ('24', {'name': 'fm', 'description': 'sets FM'}),
    ('25', {'name': 'am', 'description': 'sets AM'}),
    ('26', {'name': 'tuner', 'description': 'sets TUNER'}),
    ('27', {'name': ('music-server', 'p4s', 'dlna'),
     'description': 'sets MUSIC SERVER, P4S, DLNA'}),
    ('28', {'name': ('internet-radio', 'iradio-favorite'),
     'description': 'sets INTERNET RADIO, iRadio Favorite'}),
    ('29', {'name': ('usb', 'usb'), 'description': 'sets USB/USB(Front)'}),
    ('2A', {'name': 'usb', 'description': 'sets USB(Rear)'}),
    ('2B', {'name': ('network', 'net'), 'description': 'sets NETWORK, NET'}),
    ('2C', {'name': 'usb', 'description': 'sets USB(toggle)'}),
    ('2D', {'name': 'airplay', 'description': 'sets Airplay'}),
    ('2E', {'name': 'bluetooth', 'description': 'sets Bluetooth'}),
    ('40', {'name': 'universal-port', 'description': 'sets Universal PORT'}),
    ('30', {'name': 'multi-ch', 'description': 'sets MULTI CH'}),
    ('31', {'name': 'xm', 'description': 'sets XM'}),
    ('32', {'name': 'sirius', 'description': 'sets SIRIUS'}),
    ('33', {'name': 'dab', 'description': 'sets DAB '}),
    ('80', {'name': 'source', 'description': 'sets SOURCE'}),
    ('UP', {'name': 'up',
     'description': 'sets Selector Position Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Selector Position Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Selector Position'})]),
   'name': 'selector',
   'description': 'ZONE4 Selector Command'}),
  ('TUN', {'values': OrderedDict([('nnnnn', {'name': 'freq-nnnnn,',
     'description': 'sets Directly Tuning Frequency (FM nnn.nn MHz / AM nnnnn kHz)'}),
    ('UP', {'name': 'up',
     'description': 'sets Tuning Frequency Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Tuning Frequency Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Tuning Frequency'})]),
   'name': 'tuning',
   'description': 'Tuning Command'}),
  ('TU4', {'values': OrderedDict([('nnnnn', {'name': 'freq-nnnnn,',
     'description': 'sets Directly Tuning Frequency (FM nnn.nn MHz / AM nnnnn kHz)'}),
    ('DIRECT', {'name': 'direct',
     'description': 'starts/restarts Direct Tuning Mode'}),
    ('0', {'name': '0-in-direct-mode',
     'description': 'sets 0 in Direct Tuning Mode'}),
    ('1', {'name': '1-in-direct-mode',
     'description': 'sets 1 in Direct Tuning Mode'}),
    ('2', {'name': '2-in-direct-mode',
     'description': 'sets 2 in Direct Tuning Mode'}),
    ('3', {'name': '3-in-direct-mode',
     'description': 'sets 3 in Direct Tuning Mode'}),
    ('4', {'name': '4-in-direct-mode',
     'description': 'sets 4 in Direct Tuning Mode'}),
    ('5', {'name': '5-in-direct-mode',
     'description': 'sets 5 in Direct Tuning Mode'}),
    ('6', {'name': '6-in-direct-mode',
     'description': 'sets 6 in Direct Tuning Mode'}),
    ('7', {'name': '7-in-direct-mode',
     'description': 'sets 7 in Direct Tuning Mode'}),
    ('8', {'name': '8-in-direct-mode',
     'description': 'sets 8 in Direct Tuning Mode'}),
    ('9', {'name': '9-in-direct-mode',
     'description': 'sets 9 in Direct Tuning Mode'}),
    ('UP', {'name': 'up',
     'description': 'sets Tuning Frequency Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Tuning Frequency Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Tuning Frequency'})]),
   'name': 'tuning',
   'description': 'Tuning Command'}),
  ('PRS', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'}),
    ((1, 30), {'name': 'no-1-30',
     'description': 'sets Preset No. 1 - 30 ( In hexadecimal representation)'}),
    ('UP', {'name': 'up', 'description': 'sets Preset No. Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Preset No. Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Preset No.'})]),
   'name': 'preset',
   'description': 'Preset Command'}),
  ('PR4', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'}),
    ((1, 30), {'name': 'no-1-30',
     'description': 'sets Preset No. 1 - 30 ( In hexadecimal representation)'}),
    ('UP', {'name': 'up', 'description': 'sets Preset No. Wrap-Around Up'}),
    ('DOWN', {'name': 'down',
     'description': 'sets Preset No. Wrap-Around Down'}),
    ('QSTN', {'name': 'query', 'description': 'gets The Preset No.'})]),
   'name': 'preset',
   'description': 'Preset Command'}),
  ('NTC', {'values': OrderedDict([('PLAYz', {'name': 'playz',
     'description': 'PLAY KEY'}),
    ('STOPz', {'name': 'stopz', 'description': 'STOP KEY'}),
    ('PAUSEz', {'name': 'pausez', 'description': 'PAUSE KEY'}),
    ('TRUPz', {'name': 'trupz', 'description': 'TRACK UP KEY'}),
    ('TRDNz', {'name': 'trdnz', 'description': 'TRACK DOWN KEY'})]),
   'name': 'net-tune-network',
   'description': 'Net-Tune/Network Operation Command(Net-Tune Model Only)'}),
  ('NT4', {'values': OrderedDict([('PLAY', {'name': 'play',
     'description': 'PLAY KEY'}),
    ('STOP', {'name': 'stop', 'description': 'STOP KEY'}),
    ('PAUSE', {'name': 'pause', 'description': 'PAUSE KEY'}),
    ('TRUP', {'name': 'trup', 'description': 'TRACK UP KEY'}),
    ('TRDN', {'name': 'trdn', 'description': 'TRACK DOWN KEY'}),
    ('FF', {'name': 'ff',
     'description': 'FF KEY (CONTINUOUS*) (for iPod 1wire)'}),
    ('REW', {'name': 'rew',
     'description': 'REW KEY (CONTINUOUS*) (for iPod 1wire)'}),
    ('REPEAT', {'name': 'repeat',
     'description': 'REPEAT KEY(for iPod 1wire)'}),
    ('RANDOM', {'name': 'random',
     'description': 'RANDOM KEY(for iPod 1wire)'}),
    ('DISPLAY', {'name': 'display',
     'description': 'DISPLAY KEY(for iPod 1wire)'}),
    ('RIGHT', {'name': 'right', 'description': 'RIGHT KEY(for iPod 1wire)'}),
    ('LEFT', {'name': 'left', 'description': 'LEFT KEY(for iPod 1wire)'}),
    ('UP', {'name': 'up', 'description': 'UP KEY(for iPod 1wire)'}),
    ('DOWN', {'name': 'down', 'description': 'DOWN KEY(for iPod 1wire)'}),
    ('SELECT', {'name': 'select',
     'description': 'SELECT KEY(for iPod 1wire)'}),
    ('RETURN', {'name': 'return',
     'description': 'RETURN KEY(for iPod 1wire)'})]),
   'name': 'net-tune-network',
   'description': 'Net-Tune/Network Operation Command(Network Model Only)'}),
  ('NP4', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'})]),
   'name': 'internet-radio-preset',
   'description': 'Internet Radio Preset Command (Network Model Only)'})])),
 ('dock', OrderedDict([('NTC', {'values': OrderedDict([('PLAY', {'name': 'play',
     'description': 'PLAY KEY'}),
    ('STOP', {'name': 'stop', 'description': 'STOP KEY'}),
    ('PAUSE', {'name': 'pause', 'description': 'PAUSE KEY'}),
    ('P/P', {'name': 'p-p', 'description': 'PLAY/PAUSE KEY'}),
    ('TRUP', {'name': 'trup', 'description': 'TRACK UP KEY'}),
    ('TRDN', {'name': 'trdn', 'description': 'TRACK DOWN KEY'}),
    ('FF', {'name': 'ff', 'description': 'FF KEY (CONTINUOUS*)'}),
    ('REW', {'name': 'rew', 'description': 'REW KEY (CONTINUOUS*)'}),
    ('REPEAT', {'name': 'repeat', 'description': 'REPEAT KEY'}),
    ('RANDOM', {'name': 'random', 'description': 'RANDOM KEY'}),
    ('REP/SHF', {'name': 'rep-shf', 'description': 'REPEAT/SHUFFLE KEY'}),
    ('DISPLAY', {'name': 'display', 'description': 'DISPLAY KEY'}),
    ('ALBUM', {'name': 'album', 'description': 'ALBUM KEY'}),
    ('ARTIST', {'name': 'artist', 'description': 'ARTIST KEY'}),
    ('GENRE', {'name': 'genre', 'description': 'GENRE KEY'}),
    ('PLAYLIST', {'name': 'playlist', 'description': 'PLAYLIST KEY'}),
    ('RIGHT', {'name': 'right', 'description': 'RIGHT KEY'}),
    ('LEFT', {'name': 'left', 'description': 'LEFT KEY'}),
    ('UP', {'name': 'up', 'description': 'UP KEY'}),
    ('DOWN', {'name': 'down', 'description': 'DOWN KEY'}),
    ('SELECT', {'name': 'select', 'description': 'SELECT KEY'}),
    ('0', {'name': '0', 'description': '0 KEY'}),
    ('1', {'name': '1', 'description': '1 KEY'}),
    ('2', {'name': '2', 'description': '2 KEY'}),
    ('3', {'name': '3', 'description': '3 KEY'}),
    ('4', {'name': '4', 'description': '4 KEY'}),
    ('5', {'name': '5', 'description': '5 KEY'}),
    ('6', {'name': '6', 'description': '6 KEY'}),
    ('7', {'name': '7', 'description': '7 KEY'}),
    ('8', {'name': '8', 'description': '8 KEY'}),
    ('9', {'name': '9', 'description': '9 KEY'}),
    ('DELETE', {'name': 'delete', 'description': 'DELETE KEY'}),
    ('CAPS', {'name': 'caps', 'description': 'CAPS KEY'}),
    ('LOCATION', {'name': 'location', 'description': 'LOCATION KEY'}),
    ('LANGUAGE', {'name': 'language', 'description': 'LANGUAGE KEY'}),
    ('SETUP', {'name': 'setup', 'description': 'SETUP KEY'}),
    ('RETURN', {'name': 'return', 'description': 'RETURN KEY'}),
    ('CHUP', {'name': 'chup', 'description': 'CH UP(for iRadio)'}),
    ('CHDN', {'name': 'chdn', 'description': 'CH DOWN(for iRadio)'}),
    ('MENU', {'name': 'menu', 'description': 'MENU'}),
    ('TOP', {'name': 'top', 'description': 'TOP MENU'}),
    ('MODE', {'name': 'mode', 'description': 'MODE(for iPod) STD<->EXT'}),
    ('LIST', {'name': 'list', 'description': 'LIST <-> PLAYBACK'}),
    ('MEMORY', {'name': 'memory', 'description': 'MEMORY (add Favorite)'}),
    ('F1', {'name': 'f1', 'description': 'Positive Feed or Mark/Unmark '}),
    ('F2', {'name': 'f2', 'description': 'Negative Feed '})]),
   'name': 'network-usb',
   'description': 'Network/USB Operation Command (Network Model Only after TX-NR905)'}),
  ('NBS', {'values': OrderedDict([('OFF', {'name': 'off',
     'description': 'sets Bluetooth Off'}),
    ('ON', {'name': 'on', 'description': 'sets Bluetooth On'}),
    ('QSTN', {'name': 'query', 'description': 'gets Bluetooth Setting'})]),
   'name': 'bluetooth-setting',
   'description': 'Bluetooth Setting Operation Command'}),
  ('NBT', {'values': OrderedDict([('PAIRING', {'name': 'pairing',
     'description': 'PAIRING'}),
    ('CLEAR', {'name': 'clear', 'description': 'CLEAR PAIRING INFORMATION'})]),
   'name': 'bluetooth-internal',
   'description': 'Bluetooth(Internal) Operation Command'}),
  ('NAT', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'artist-name',
     'description': 'NET/USB Artist Name (variable-length, 64 Unicode letters [UTF-8 encoded] max , for Network Control only)'}),
    ('QSTN', {'name': 'query', 'description': 'gets NET/USB Artist Name'})]),
   'name': 'net-usb-artist-name-info',
   'description': 'NET/USB Artist Name Info'}),
  ('NAL', {'values': OrderedDict([('nnnnnnn', {'name': 'album-name',
     'description': 'NET/USB Album Name (variable-length, 64 Unicode letters [UTF-8 encoded] max , for Network Control only)'}),
    ('QSTN', {'name': 'query', 'description': 'gets NET/USB Album Name'})]),
   'name': 'net-usb-album-name-info',
   'description': 'NET/USB Album Name Info'}),
  ('NTI', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'title',
     'description': 'NET/USB Title Name (variable-length, 64 Unicode letters [UTF-8 encoded] max , for Network Control only)'}),
    ('QSTN', {'name': 'query', 'description': 'gets NET/USB Title Name'})]),
   'name': 'net-usb-title-name',
   'description': 'NET/USB Title Name'}),
  ('NTM', {'values': OrderedDict([('mm:ss/mm:ss', {'name': 'mm-ss-mm-ss',
     'description': 'NET/USB Time Info (Elapsed time/Track Time Max 99:59. If time is unknown, this response is --:--)'}),
    ('hh:mm:ss/hh:mm:ss', {'name': 'hh-mm-ss-hh-mm-ss',
     'description': 'NET/USB Time Info (Elapsed time/Track Time Max 99:59:59. If time is unknown, this response is --:--)'}),
    ('QSTN', {'name': 'query', 'description': 'gets NET/USB Time Info'})]),
   'name': 'net-usb-time-info',
   'description': 'NET/USB Time Info'}),
  ('NTR', {'values': OrderedDict([('cccc/tttt', {'name': 'cccc-tttt',
     'description': 'NET/USB Track Info (Current Track/Toral Track Max 9999. If Track is unknown, this response is ----)'}),
    ('QSTN', {'name': 'query', 'description': 'gets NET/USB Track Info'})]),
   'name': 'net-usb-track-info',
   'description': 'NET/USB Track Info'}),
  ('NST', {'values': OrderedDict([('prs', {'name': 'prs',
     'description': 'NET/USB Play Status (3 letters)\np -> Play Status: "S": STOP, "P": Play, "p": Pause, "F": FF, "R": FR, "E": EOF\nr -> Repeat Status: "-": Off, "R": All, "F": Folder, "1": Repeat 1, "x": disable\ns -> Shuffle Status: "-": Off, "S": All , "A": Album, "F": Folder, "x": disable'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Net/USB Play Status'})]),
   'name': 'net-usb-play-status',
   'description': 'NET/USB Play Status'}),
  ('NMS', {'values': OrderedDict([('maabbstii', {'name': 'maabbstii',
     'description': u'NET/USB Menu Status (9 letters)\nm -> Track Menu: "M": Menu is enable, "x": Menu is disable\naa -> F1 button icon (Positive Feed or Mark/Unmark)\nbb -> F2 button icon (Negative Feed)\n aa or bb : "xx":disable, "01":Like, "02":don\'t like, "03":Love, "04":Ban,\n                  "05":episode, "06":ratings, "07":Ban(black), "08":Ban(white),\n                  "09":Favorite(black), "0A":Favorite(white), "0B":Favorite(yellow)\ns -> Time Seek "S": Time Seek is enable "x": Time Seek is disable\nt -> Time Display "1": Elapsed Time/Total Time, "2": Elapsed Time, "x": disable\nii-> Service icon\n ii : "00":Music Server (DLNA), "01":My Favorite, "02":vTuner, \n      "03":SiriusXM, "04":Pandora,\n      "05":Rhapsody, "06":Last.fm, "07":Napster, "08":Slacker, "09":Mediafly,\n      "0A":Spotify, "0B":AUPEO!,\n      "0C":radiko, "0D":e-onkyo, "0E":TuneIn, "0F":MP3tunes, "10":Simfy,\n      "11":Home Media, "12":Deezer, "13":iHeartRadio, "18":Airplay,\n      \u201c1A\u201d: onkyo Music, \u201c1B\u201d:TIDAL, \u201c41\u201d:FireConnect,\n      "F0": USB/USB(Front), "F1: USB(Rear), "F2":Internet Radio\n      "F3":NET, "F4":Bluetooth'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Net/USB Menu Status'})]),
   'name': 'net-usb-menu-status',
   'description': 'NET/USB Menu Status'}),
  ('NTS', {'values': OrderedDict([('mm:ss', {'name': 'mm-ss',
     'description': 'mm: munites (00-99)\nss: seconds (00-59)\nThis command is only available when Time Seek is enable.'}),
    ('hh:mm:ss', {'name': 'hh-mm-ss',
     'description': 'hh: hours(00-99)\nmm: munites (00-59)\nss: seconds (00-59)\nThis command is only available when Time Seek is enable.'})]),
   'name': 'net-usb-time-seek',
   'description': 'NET/USB Time Seek'}),
  ('NPR', {'values': OrderedDict([((1, 40), {'name': 'no-1-40',
     'description': 'sets Preset No. 1 - 40 ( In hexadecimal representation)'}),
    ('SET', {'name': 'set', 'description': 'preset memory current station'})]),
   'name': 'internet-radio-preset',
   'description': 'Internet Radio Preset Command'}),
  ('NDS', {'values': OrderedDict([('nfr', {'name': 'nfr',
     'description': 'NET Connection/USB Device Status (3 letters)\nn -> NET Connection status: "-": no connection, "E": Ether, "W": Wireless\nf -> Front USB(USB1) Device Status: "-": no device, "i": iPod/iPhone, \n      "M": Memory/NAS, "W": Wireless Adaptor, "B": Bluetooth Adaptor,\n      "x": disable\nr -> Rear USB(USB2) Device Status: "-": no device, "i": iPod/iPhone, \n      "M": Memory/NAS, "W": Wireless Adaptor, "B": Bluetooth Adaptor, \n      "x": disable'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Net/USB Status'})]),
   'name': 'net-connection-usb-device-status',
   'description': 'NET Connection/USB Device Status'}),
  ('NLS', {'values': OrderedDict([('tlpnnnnnnnnnn', {'name': 'info',
     'description': u'NET/USB List Info\nt ->Information Type (A : ASCII letter, C : Cursor Info, U : Unicode letter)\nwhen t = A,\n  l ->Line Info (0-9 : 1st to 10th Line)\n  nnnnnnnnn:Listed data (variable-length, 64 ASCII letters max)\n    when AVR is not displayed NET/USB List(Keyboard,Menu,Popup\u2026), "nnnnnnnnn" is "See TV".\n  p ->Property\n         - : no\n         0 : Playing, A : Artist, B : Album, F : Folder, M : Music, P : Playlist, S : Search\n         a : Account, b : Playlist-C, c : Starred, d : Unstarred, e : What\'s New\nwhen t = C,\n  l ->Cursor Position (0-9 : 1st to 10th Line, - : No Cursor)\n  p ->Update Type (P : Page Information Update ( Page Clear or Disable List Info) , C : Cursor Position Update)\nwhen t = U, (for Network Control Only)\n  l ->Line Info (0-9 : 1st to 10th Line)\n  nnnnnnnnn:Listed data (variable-length, 64 Unicode letters [UTF-8 encoded] max)\n    when AVR is not displayed NET/USB List(Keyboard,Menu,Popup\u2026), "nnnnnnnnn" is "See TV".\n  p ->Property\n         - : no\n         0 : Playing, A : Artist, B : Album, F : Folder, M : Music, P : Playlist, S : Search\n         a : Account, b : Playlist-C, c : Starred, d : Unstarred, e : What\'s New'}),
    ('ti', {'name': 'ti',
     'description': 'select the listed item\n t -> Index Type (L : Line, I : Index)\nwhen t = L,\n  i -> Line number (0-9 : 1st to 10th Line [1 digit] )\nwhen t = I,\n  iiiii -> Index number (00001-99999 : 1st to 99999th Item [5 digits] )'})]),
   'name': 'net-usb-list-info',
   'description': 'NET/USB List Info'}),
  ('NLA', {'values': OrderedDict([('tzzzzsurr<.....>', {'name': 'tzzzzsurr',
     'description': u't -> responce type \'X\' : XML\nzzzz -> sequence number (0000-FFFF)\ns -> status \'S\' : success, \'E\' : error\nu -> UI type \'0\' : List, \'1\' : Menu, \'2\' : Playback, \'3\' : Popup, \'4\' : Keyboard, "5" : Menu List\nrr -> reserved\n<.....> : XML data ( [CR] and [LF] are removed )\n If s=\'S\',\n <?xml version="1.0" encoding="UFT-8"?>\n <response status="ok">\n   <items offset="xxxx" totalitems="yyyy" >\n     <item iconid="aa" title="bbb\u2026bbb" url="ccc...ccc"/>\n     \u2026\n     <item iconid="aa" title="bbb\u2026bbb" url="ccc...ccc"/>\n   </Items>\n </response>\n If s=\'E\',\n <?xml version="1.0" encoding="UFT-8"?>\n <response status="fail">\n   <error code="[error code]" message="[error message]" />\n </response>\nxxxx : index of 1st item (0000-FFFF : 1st to 65536th Item [4 HEX digits] )\nyyyy : number of items (0000-FFFF : 1 to 65536 Items [4 HEX digits] )\naa : Icon ID\n \'29\' : Folder, \'2A\' : Folder X, \'2B\' : Server, \'2C\' : Server X, \'2D\' : Title, \'2E\' : Title X,\n \'2F\' : Program, \'31\' : USB, \'36\' : Play, \'37\' : MultiAccount,\n for Spotify\n \'38\' : Account, \'39\' : Album, \'3A\' : Playlist, \'3B\' : Playlist-C, \'3C\' : starred,\n \'3D\' : What\'sNew, \'3E\' : Artist, \'3F\' : Track, \'40\' : unstarred, \'41\' : Play, \'43\' : Search, \'44\' : Folder\n for AUPEO!\n \'42\' : Program\nbbb...bbb : Title'}),
    ('Lzzzzll{xx}{xx}yyyy', {'name': 'lzzzzll-xx-xx-yyyy',
     'description': 'specifiy to get the listed data (from Network Control Only)\nzzzz -> sequence number (0000-FFFF)\nll -> number of layer (00-FF)\nxxxx -> index of start item (0000-FFFF : 1st to 65536th Item [4 HEX digits] )\nyyyy -> number of items (0000-FFFF : 1 to 65536 Items [4 HEX digits] )'}),
    ('Izzzzll{xx}{xx}----', {'name': 'izzzzll-xx-xx',
     'description': 'select the listed item (from Network Control Only)\nzzzz -> sequence number (0000-FFFF)\nll -> number of layer (00-FF)\nxxxx -> index number (0000-FFFF : 1st to 65536th Item [4 HEX digits] )\n---- -> not used'})]),
   'name': 'net-usb-list-info',
   'description': 'NET/USB List Info(All item, need processing XML data, for Network Control Only)'}),
  ('NJA', {'values': OrderedDict([('tp{xx}{xx}{xx}{xx}{xx}{xx}', {'name': 'tp-xx-xx-xx-xx-xx-xx',
     'description': 'NET/USB Jacket Art/Album Art Data\nt-> Image type 0:BMP, 1:JPEG, 2:URL, n:No Image\np-> Packet flag 0:Start, 1:Next, 2:End, -:not used\nxxxxxxxxxxxxxx -> Jacket/Album Art Data (valiable length, 1024 ASCII HEX letters max)'}),
    ('DIS', {'name': 'disable', 'description': 'sets Jacket Art disable'}),
    ('ENA', {'name': 'enable', 'description': 'sets Jacket Art enable'}),
    ('BMP', {'name': 'enable-and-image-type-bmp',
     'description': 'sets Jacket Art enable and Image type BMP'}),
    ('LINK', {'name': 'enable-and-image-type-link',
     'description': 'sets Jacket Art enable and Image type LINK'}),
    ('UP', {'name': 'up', 'description': 'sets Jacket Art Wrap-Around Up'}),
    ('REQ', {'name': 'req', 'description': 'gets Jacket Art data'}),
    ('QSTN', {'name': 'query',
     'description': 'gets Jacket Art enable/disable'})]),
   'name': 'net-usb-jacket-art',
   'description': 'NET/USB Jacket Art (When Jacket Art is available and Output for Network Control Only)'}),
  ('NSV', {'values': OrderedDict([(u'ssiaaaa\u2026aaaabbbb\u2026bbbb', {'name': 'service-id',
     'description': 'select Network Service directly\nss -> Network Serveice\n 00:Music Server (DLNA), 01:Favorite, 02:vTuner, 03:SiriusXM, 04:Pandora, 05:Rhapsody, 06:Last.fm,\n 07:Napster, 08:Slacker, 09:Mediafly, 0A:Spotify, 0B:AUPEO!, 0C:Radiko, 0D:e-onkyo,\n 0E:TuneIn Radio, 0F:mp3tunes, 10:Simfy, 11:Home Media, 12:Deezer, 13:iHeartRadio, 18:Airplay, 19:TIDAL, 1A:onkyo music, F0;USB/USB(Front), F1:USB(Rear)\ni-> Acount Info\n 0: No\n 1: Yes\n"aaaa...aaaa": User Name ( 128 Unicode letters [UTF-8 encoded] max )\n"bbbb...bbbb": Password ( 128 Unicode letters [UTF-8 encoded] max )'})]),
   'name': 'net-service',
   'description': 'NET Service(for Network Control Only)'}),
  ('NKY', {'values': OrderedDict([('ll', {'name': 'll',
     'description': 'waiting Keyboard Input\nll -> category\n 00: Off ( Exit Keyboard Input )\n 01: User Name\n 02: Password\n 03: Artist Name\n 04: Album Name\n 05: Song Name\n 06: Station Name\n 07: Tag Name\n 08: Artist or Song\n 09: Episode Name\n 0A: Pin Code (some digit Number [0-9])\n 0B: User Name (available ISO 8859-1 character set)\n 0C: Password (available ISO 8859-1 character set)\n 0D: URL'}),
    ('nnnnnnnnn', {'name': 'input',
     'description': 'set Keyboard Input letter\n"nnnnnnnn" is variable-length, 128 Unicode letters [UTF-8 encoded] max'})]),
   'name': 'net-keyboard',
   'description': 'NET Keyboard(for Network Control Only)'}),
  ('NPU', {'values': OrderedDict([(u'xaaa\u2026aaaybbb\u2026bbb', {'name': 'popup',
     'description': "x -> Popup Display Type\n 'T': Popup text is top\n 'B': Popup text is bottom\n 'L': Popup text is list format\n\naaa...aaa -> Popup Title, Massage\n when x = 'T' or 'B'\n    Top Title [0x00] Popup Title [0x00] Popup Message [0x00]\n    (valiable-length Unicode letter [UTF-8 encoded] )\n\n when x = 'L'\n    Top Title [0x00] Item Title 1 [0x00] Item Parameter 1 [0x00] ... [0x00] Item Title 6 [0x00] Item Parameter 6 [0x00]\n    (valiable-length Unicode letter [UTF-8 encoded] )\n\ny -> Cursor Position on button\n '0' : Button is not Displayed\n '1' : Cursor is on the button 1\n '2' : Cursor is on the button 2\n\nbbb...bbb -> Text of Button\n    Text of Button 1 [0x00] Text of Button 2 [0x00]\n    (valiable-length Unicode letter [UTF-8 encoded] )"})]),
   'name': 'net-popup-message',
   'description': 'NET Popup Message(for Network Control Only)'}),
  ('NLT', {'values': OrderedDict([('{xx}uycccciiiillrraabbssnnn...nnn', {'name': 'title-info',
     'description': 'NET/USB List Title Info\nxx : Service Type\n 00 : DLNA, 01 : Favorite, 02 : vTuner, 03 : SiriusXM, 04 : Pandora, 05 : Rhapsody, 06 : Last.fm,\n 07 : Napster, 08 : Slacker, 09 : Mediafly, 0A : Spotify, 0B : AUPEO!, 0C : radiko, 0D : e-onkyo,\n 0E : TuneIn Radio, 0F : MP3tunes, 10 : Simfy, 11:Home Media, 12:Deezer, 13:iHeartRadio,\n F0 : USB Front, F1 : USB Rear, F2 : Internet Radio, F3 : NET, FF : None\nu : UI Type\n 0 : List, 1 : Menu, 2 : Playback, 3 : Popup, 4 : Keyboard, "5" : Menu List\ny : Layer Info\n 0 : NET TOP, 1 : Service Top,DLNA/USB/iPod Top, 2 : under 2nd Layer\ncccc : Current Cursor Position (HEX 4 letters)\niiii : Number of List Items (HEX 4 letters)\nll : Number of Layer(HEX 2 letters)\nrr : Reserved (2 leters)\naa : Icon on Left of Title Bar\n 00 : Internet Radio, 01 : Server, 02 : USB, 03 : iPod, 04 : DLNA, 05 : WiFi, 06 : Favorite\n 10 : Account(Spotify), 11 : Album(Spotify), 12 : Playlist(Spotify), 13 : Playlist-C(Spotify)\n 14 : Starred(Spotify), 15 : What\'s New(Spotify), 16 : Track(Spotify), 17 : Artist(Spotify)\n 18 : Play(Spotify), 19 : Search(Spotify), 1A : Folder(Spotify)\n FF : None\nbb : Icon on Right of Title Bar\n 00 : DLNA, 01 : Favorite, 02 : vTuner, 03 : SiriusXM, 04 : Pandora, 05 : Rhapsody, 06 : Last.fm,\n 07 : Napster, 08 : Slacker, 09 : Mediafly, 0A : Spotify, 0B : AUPEO!, 0C : radiko, 0D : e-onkyo,\n 0E : TuneIn Radio, 0F : MP3tunes, 10 : Simfy, 11:Home Media, 12:Deezer, 13:iHeartRadio,\n FF : None\nss : Status Info\n 00 : None, 01 : Connecting, 02 : Acquiring License, 03 : Buffering\n 04 : Cannot Play, 05 : Searching, 06 : Profile update, 07 : Operation disabled\n 08 : Server Start-up, 09 : Song rated as Favorite, 0A : Song banned from station,\n 0B : Authentication Failed, 0C : Spotify Paused(max 1 device), 0D : Track Not Available, 0E : Cannot Skip\nnnn...nnn : Character of Title Bar (variable-length, 64 Unicode letters [UTF-8 encoded] max)'}),
    ('{xx}uycccciiiillsraabbssnnn...nnn', {'name': None,
     'description': 'NET/USB List Title Info\nxx : Service Type\n 00 : Music Server (DLNA), 01 : Favorite, 02 : vTuner, 03 : SiriusXM, 04 : Pandora, 05 : Rhapsody, 06 : Last.fm,\n 07 : Napster, 08 : Slacker, 09 : Mediafly, 0A : Spotify, 0B : AUPEO!, 0C : radiko, 0D : e-onkyo,\n 0E : TuneIn Radio, 0F : MP3tunes, 10 : Simfy, 11:Home Media, 12:Deezer, 13:iHeartRadio, 18:Airplay, 19:TIDAL, 1A:onkyo music,\n F0 : USB/USB(Front) F1 : USB(Rear), F2 : Internet Radio, F3 : NET, FF : None\nu : UI Type\n 0 : List, 1 : Menu, 2 : Playback, 3 : Popup, 4 : Keyboard, "5" : Menu List\ny : Layer Info\n 0 : NET TOP, 1 : Service Top,DLNA/USB/iPod Top, 2 : under 2nd Layer\ncccc : Current Cursor Position (HEX 4 letters)\niiii : Number of List Items (HEX 4 letters)\nll : Number of Layer(HEX 2 letters)\ns : Start Flag\n 0 : Not First, 1 : First\nr : Reserved (1 leters, don\'t care)\naa : Icon on Left of Title Bar\n 00 : Internet Radio, 01 : Server, 02 : USB, 03 : iPod, 04 : DLNA, 05 : WiFi, 06 : Favorite\n 10 : Account(Spotify), 11 : Album(Spotify), 12 : Playlist(Spotify), 13 : Playlist-C(Spotify)\n 14 : Starred(Spotify), 15 : What\'s New(Spotify), 16 : Track(Spotify), 17 : Artist(Spotify)\n 18 : Play(Spotify), 19 : Search(Spotify), 1A : Folder(Spotify)\n FF : None\nbb : Icon on Right of Title Bar\n 00 : Muisc Server (DLNA), 01 : Favorite, 02 : vTuner, 03 : SiriusXM, 04 : Pandora, 05 : Rhapsody, 06 : Last.fm,\n 07 : Napster, 08 : Slacker, 09 : Mediafly, 0A : Spotify, 0B : AUPEO!, 0C : radiko, 0D : e-onkyo,\n 0E : TuneIn Radio, 0F : MP3tunes, 10 : Simfy, 11:Home Media, 12:Deezer, 13:iHeartRadio, 18:Airplay, 19:TIDAL, 1A:onkyo music,\nF0:USB/USB(Front), F1:USB(Rear),\n FF : None\nss : Status Info\n 00 : None, 01 : Connecting, 02 : Acquiring License, 03 : Buffering\n 04 : Cannot Play, 05 : Searching, 06 : Profile update, 07 : Operation disabled\n 08 : Server Start-up, 09 : Song rated as Favorite, 0A : Song banned from station,\n 0B : Authentication Failed, 0C : Spotify Paused(max 1 device), 0D : Track Not Available, 0E : Cannot Skip\nnnn...nnn : Character of Title Bar (variable-length, 64 Unicode letters [UTF-8 encoded] max)'}),
    ('QSTN', {'name': 'query', 'description': 'gets List Title Info'})]),
   'name': 'net-usb-list-title-info',
   'description': 'NET/USB List Title Info(for Network Control Only)'}),
  ('NMD', {'values': OrderedDict([('STD', {'name': 'std',
     'description': 'Standerd Mode'}),
    ('EXT', {'name': 'ext', 'description': 'Extend Mode(If available)'}),
    ('VDC', {'name': 'vdc', 'description': 'Video Contents in Extended Mode'}),
    ('QSTN', {'name': 'query', 'description': 'gets iPod Mode Status'})]),
   'name': 'ipod-mode-change',
   'description': 'iPod Mode Change (with USB Connection Only)'}),
  ('NSB', {'values': OrderedDict([('OFF', {'name': 'is-off',
     'description': 'sets Network Standby is Off'}),
    ('ON', {'name': 'is-on', 'description': 'sets Network Standby is On'}),
    ('QSTN', {'name': 'query',
     'description': 'gets Network Standby Setting'})]),
   'name': 'network-standby-settings',
   'description': 'Network Standby Settings (for Network Control Only and Available in AVR is PowerOn)'}),
  ('NRI', {'values': OrderedDict([(u'<\u2026>', {'name': 'xml',
     'description': u'<\u2026>: XML Data <?xml\u2026>'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Receiver Information Status'}),
    ('t----<.....>', {'name': 't',
     'description': "t -> message type 'X' : XML\n---- -> reserved\n<.....> : XML data ( [CR] and [LF] are removed )"}),
    ('Ullt<.....>', {'name': 'ullt',
     'description': 'U : UI Type\n 0 : List, 1 : Menu, 2 : Playback, 3 : Popup, 4 : Keyboard, 5 : Menu List\nll -> number of layer (00-FF)\nt : Update Type\n 0 : All, 1 : Button, 2 : Textbox, 3 : Listbox\n<.....> : XML data ( [CR] and [LF] are removed )'})]),
   'name': 'receiver-information',
   'description': 'Receiver Information (for Network Control Only)'}),
  ('NLU', {'values': OrderedDict([('{xx}{xx}yyyy', {'name': 'xx-xx-yyyy',
     'description': 'xxxx -> index of update item (0000-FFFF : 1st to 65536th Item [4 HEX digits] )\nyyyy : number of items (0000-FFFF : 1 to 65536 Items [4 HEX digits] )'})]),
   'name': 'net-usb-list-info',
   'description': 'NET/USB List Info (Update item, need processing XML data, for Network Control Only)'}),
  ('NPB', {'values': OrderedDict([('pudtsrrr', {'name': 'pudtsrrr',
     'description': 'NET/USB Playback view Status (5 letters)\np -> Play/Pause button: "1": button is enable, "0": button is disable\nu ->  Skip up button : "1": button is enable, "0": button is disable\nd -> Skip down button : "1": button is enable, "0": button is disable\nt -> Timer button : "1": button is enable, "0": button is disable\ns -> Preset button : "1": button is enable, "0": button is disable\n rrr-> reserved'}),
    ('QSTN', {'name': 'query',
     'description': 'gets the Net/USB Playback view Button'})]),
   'name': 'net-usb-playback-view-button',
   'description': 'NET/USB Playback view Button'}),
  ('NAF', {'values': OrderedDict([('{xx}{xx}', {'name': 'xx-xx',
     'description': 'Add Favorite Lsit in List View (from Network Control Only)\nxxxx -> index number (0000-FFFF : 1st to 65536th Item [4 HEX digits] )'})]),
   'name': 'net-usb-add-favorite-list-in-list-view',
   'description': 'NET/USB Add Favorite List in List View'}),
  ('NRF', {'values': OrderedDict([((1, 40), {'name': 'fav-no-1-40',
     'description': 'Remove Item from Favorite List ( In hexadecimal representation)'})]),
   'name': 'net-usb-remove-favorite-list',
   'description': 'NET/USB Remove Favorite List'}),
  ('NSD', {'values': OrderedDict([('{xx}{xx}{xx}{xx}{xx}x', {'name': 'xx-xx-xx-xx-xx-x',
     'description': 'Search Word (Max 128 Character)'})]),
   'name': 'net-usb-music-server-dlna-search-list',
   'description': 'NET/USB Music Server(DLNA) Search List'}),
  ('AAT', {'values': OrderedDict([('nnnnnnnnnn', {'name': None,
     'description': 'NET/USB Artist Name (variable-length, 64 Unicode letters [UTF-8 encoded] max , for Network Control only)'}),
    ('QSTN', {'name': 'query', 'description': 'gets iPod Artist Name'})]),
   'name': 'airplay-artist-name-info',
   'description': 'Airplay Artist Name Info (Airplay Model Only)'}),
  ('AAL', {'values': OrderedDict([('nnnnnnn', {'name': 'album-name',
     'description': 'NET/USB Album Name (variable-length, 64 Unicode letters [UTF-8 encoded] max , for Network Control only)'}),
    ('QSTN', {'name': 'query', 'description': 'gets iPod Album Name'})]),
   'name': 'airplay-album-name-info',
   'description': 'Airplay Album Name Info (Airplay Model Only)'}),
  ('ATI', {'values': OrderedDict([('nnnnnnnnnn', {'name': 'title',
     'description': 'NET/USB Title Name (variable-length, 64 Unicode letters [UTF-8 encoded] max , for Network Control only)'}),
    ('QSTN', {'name': 'query', 'description': 'gets HD Radio Title'})]),
   'name': 'airplay-title-name',
   'description': 'Airplay Title Name (Airplay Model Only)'}),
  ('ATM', {'values': OrderedDict([('mm:ss/mm:ss', {'name': 'mm-ss-mm-ss',
     'description': 'NET/USB Time Info (Elapsed time/Track Time Max 99:59)'}),
    ('QSTN', {'name': 'query', 'description': 'gets iPod Time Info'})]),
   'name': 'airplay-time-info',
   'description': 'Airplay Time Info (Airplay Model Only)'}),
  ('AST', {'values': OrderedDict([('prs', {'name': 'prs',
     'description': 'NET/USB Play Status (3 letters)\np -> Play Status: "S": STOP, "P": Play, "p": Pause\nr -> Repeat Status: "-": Off\ns -> Shuffle Status: "-": Off'}),
    ('QSTN', {'name': 'query', 'description': 'gets the Net/USB Status'})]),
   'name': 'airplay-play-status',
   'description': 'Airplay Play Status (Airplay Model Only)'})]))])

ZONE_MAPPINGS = {'': 'main', None: 'main'}

COMMAND_MAPPINGS = {'zone3': {'tone': 'TN3',
  'tuning': 'TU3',
  'power': 'PW3',
  'muting': 'MT3',
  'net-tune-network': 'NT3',
  'internet-radio-preset': 'NP3',
  'selector': 'SL3',
  'volume': 'VL3',
  'preset': 'PR3',
  'balance': 'BL3'},
 'zone2': {'late-night': 'LTZ',
  'tone': 'ZTN',
  'tuning': 'TUZ',
  'power': 'ZPW',
  'muting': 'ZMT',
  'net-tune-network': 'NTZ',
  'internet-radio-preset': 'NPZ',
  'selector': 'SLZ',
  'volume': 'ZVL',
  'preset': 'PRZ',
  're-eq-academy-filter': 'RAZ',
  'zone-2-a': 'ZPA',
  'zone-2-b': 'ZPB',
  'balance': 'ZBL',
  'listening-mode': 'LMZ'},
 'main': {'audio-scalar': 'ASC',
  'audio-muting-by-channel': 'CMT',
  'screen-centered-dialog-dialog-enahncement': 'SCD',
  'lock-range-adjust': 'LRA',
  'sirius-category': 'SCT',
  'cinema-filter': 'RAS',
  'tone-center': 'TCT',
  'hdmi-out-information': 'HOI',
  'audio-selector': 'SLA',
  'hd-radio-channel-name-info': 'HCN',
  'input-selector-rename-input-function-rename': 'IRN',
  'cd-play-status': 'CST',
  'sirius-channel-number': 'SCH',
  'speaker-information': 'SPI',
  'sirius-parental-lock': 'SLK',
  'source': 'SLI',
  'battery-charge-status': 'BCS',
  'system-power': 'PWR',
  'dialog-control-enabled': 'DCE',
  'current-folder-status-no': 'CFS',
  'xm-category': 'XCT',
  'audyssey-dynamic-eq': 'ADQ',
  'fullband-mcacc-calibration': 'MFB',
  'phase-matching-bass': 'PMB',
  'for-smart-grid': 'ECO',
  'pcm-fixed-mode-fixed-pcm-mode': 'FXP',
  'hdmi-audio-out': 'HAS',
  'display-mode': 'DIF',
  'center-width-for-plii-music': 'CTW',
  'hi-bit': 'HBT',
  'all-channel-eq-for-temporary-value': 'ACE',
  'intellivolume-input-volume-absorber': 'ITV',
  'xm-channel-number': 'XCH',
  'loudness-management': 'LDM',
  'direct': 'DIR',
  'isf-mode': 'ISF',
  'hd-radio-title-info': 'HTI',
  'tone-front': 'TFR',
  'video-output-selector': 'VOS',
  'audyssey-2eq-multeq-multeq-xt': 'ADY',
  'tone-front-wide': 'TFW',
  'firmware-version': 'FWV',
  'hd-radio-channel-program': 'HPR',
  'accueq': 'AEQ',
  'power': 'PWR',
  'center-temporary-level': 'CTL',
  'tone-subwoofer': 'TSW',
  'speaker-a': 'SPA',
  'auto-power-down': 'APD',
  'tone-surround': 'TSR',
  'speaker-b': 'SPB',
  'current-disc-status-notice': 'DST',
  'digital-filter': 'DGF',
  'dialog-control': 'DLC',
  'dimension-for-plii-music': 'DMS',
  'hd-radio-blend-mode': 'HBL',
  'equalizer-select': 'EQS',
  'tp-scan': 'TPS',
  'speaker-distance': 'SPD',
  'video-picture-mode': 'VPM',
  'hdmi-cec-control-monitor': 'CCM',
  'video-information': 'IFV',
  'xm-channel-name-info': 'XCN',
  'tone-front-high': 'TFH',
  'listening-mode': 'LMD',
  'audio-muting': 'AMT',
  'hd-radio-artist-name-info': 'HAT',
  'mcacc-eq': 'MCM',
  'tone-surround-back': 'TSB',
  'popup-message': 'POP',
  'p-bass': 'PBS',
  'dab-station-name': 'DSN',
  'xm-title-info': 'XTI',
  'video-wide-mode': 'VWM',
  'center-image-for-neo-6-music': 'CTI',
  'tuning': 'TUN',
  'hdmi-output-selector': 'HDO',
  'sleep-set': 'SLP',
  'speaker-layout': 'SPL',
  'lfe-level-lfe-mute-level': 'LFE',
  'panorama-for-plii-music': 'PNR',
  'fl-display-information': 'FLD',
  'sirius-title-info': 'STI',
  'audyssey-dynamic-volume': 'ADV',
  'volume': 'MVL',
  'temporary-channel-level': 'TCL',
  'recout-selector': 'SLR',
  'cd-player': 'CCD',
  'subwoofer-2-temporary-level': 'SW2',
  'pqls': 'PQL',
  'a-v-sync': 'AVS',
  'reset': 'RST',
  'memory-setup': 'MEM',
  'cd-time-info': 'CTM',
  'preset-memory': 'PRM',
  'rds-information': 'RDS',
  'setup': 'OSD',
  'hd-radio-detail-info': 'HDS',
  'phase-control-plus': 'PCP',
  'upsampling': 'UPS',
  'master-volume': 'MVL',
  'monitor-out-resolution': 'RES',
  'hd-radio-tuner-status': 'HTS',
  'dimmer-level': 'DIM',
  'music-optimizer-sound-retriever': 'MOT',
  'pty-scan': 'PTS',
  'lip-sync-auto-delay': 'LPS',
  'hdmi-standby-through': 'HST',
  'set-cd-elapsed-time': 'SCE',
  'eq-for-standing-wave-standing-wave': 'STW',
  'mcacc-calibration': 'MCC',
  'input-selector': 'SLI',
  'sirius-artist-name-info': 'SAT',
  'subwoofer-temporary-level': 'SWL',
  'xm-artist-name-info': 'XAT',
  'cener-spread-for-dolby-surround': 'CTS',
  'update': 'UPD',
  'audio-return-channel': 'ARC',
  'preset': 'PRS',
  '12v-trigger-a': 'TGA',
  '12v-trigger-b': 'TGB',
  '12v-trigger-c': 'TGC',
  'pre-amp-mode-amp-mode': 'PAM',
  'dolby-volume': 'DVL',
  'phase-control': 'PCT',
  'hdmi-cec': 'CEC',
  'late-night': 'LTN',
  'super-resolution': 'SPR',
  'sirius-channel-name-info': 'SCN',
  's-bass': 'SBS',
  'input-channel-multiplex-dual-mono': 'DMN',
  'audio-information': 'IFA',
  'temperature-data': 'TPD',
  'speaker-level-calibration': 'SLC'},
 'dock': {'bluetooth-setting': 'NBS',
  'airplay-artist-name-info': 'AAT',
  'network-usb': 'NTC',
  'net-usb-album-name-info': 'NAL',
  'net-popup-message': 'NPU',
  'internet-radio-preset': 'NPR',
  'airplay-time-info': 'ATM',
  'net-usb-time-info': 'NTM',
  'net-usb-music-server-dlna-search-list': 'NSD',
  'airplay-album-name-info': 'AAL',
  'net-usb-remove-favorite-list': 'NRF',
  'network-standby-settings': 'NSB',
  'net-service': 'NSV',
  'net-usb-jacket-art': 'NJA',
  'net-usb-add-favorite-list-in-list-view': 'NAF',
  'net-keyboard': 'NKY',
  'net-usb-play-status': 'NST',
  'receiver-information': 'NRI',
  'net-usb-playback-view-button': 'NPB',
  'net-usb-list-info': 'NLU',
  'net-usb-time-seek': 'NTS',
  'ipod-mode-change': 'NMD',
  'net-usb-artist-name-info': 'NAT',
  'net-usb-track-info': 'NTR',
  'net-usb-list-title-info': 'NLT',
  'airplay-play-status': 'AST',
  'bluetooth-internal': 'NBT',
  'airplay-title-name': 'ATI',
  'net-usb-menu-status': 'NMS',
  'net-usb-title-name': 'NTI',
  'net-connection-usb-device-status': 'NDS'},
 'zone4': {'tuning': 'TU4',
  'power': 'PW4',
  'muting': 'MT4',
  'net-tune-network': 'NT4',
  'internet-radio-preset': 'NP4',
  'selector': 'SL4',
  'volume': 'VL4',
  'preset': 'PR4'}}

VALUE_MAPPINGS = {'zone3': {'PW3': {'standby': '00', 'on': '01', 'query': 'QSTN'},
  'TN3': {'treble-up': 'TUP',
   'bass-up': 'BUP',
   'bass-down': 'BDOWN',
   'treble-down': 'TDOWN',
   'b-xx': 'B{xx}',
   'query': 'QSTN',
   't-xx': 'T{xx}'},
  'PRS': {'down': 'DOWN',
   ValueRange(1, 30): (1, 30),
   'query': 'QSTN',
   ValueRange(1, 40): (1, 40),
   'up': 'UP'},
  'MT3': {'on': '01', 'toggle': 'TG', 'off': '00', 'query': 'QSTN'},
  'NT3': {'trdn': 'TRDN',
   'down': 'DOWN',
   'play': 'PLAY',
   'pause': 'PAUSE',
   'p-p': 'P/P',
   'ff': 'FF',
   'trup': 'TRUP',
   'random': 'RANDOM',
   'stop': 'STOP',
   'rew': 'REW',
   'up': 'UP',
   'rep-shf': 'REP/SHF',
   'repeat': 'REPEAT',
   'right': 'RIGHT',
   'chup': 'CHUP',
   'memory': 'MEMORY',
   'return': 'RETURN',
   'left': 'LEFT',
   'display': 'DISPLAY',
   'select': 'SELECT',
   'chdn': 'CHDN'},
  'TU3': {'6-in-direct-mode': '6',
   'down': 'DOWN',
   '3-in-direct-mode': '3',
   '8-in-direct-mode': '8',
   'band': 'BAND',
   '5-in-direct-mode': '5',
   'direct': 'DIRECT',
   '0-in-direct-mode': '0',
   '9-in-direct-mode': '9',
   'up': 'UP',
   'freq-nnnnn': 'nnnnn',
   '4-in-direct-mode': '4',
   '1-in-direct-mode': '1',
   '7-in-direct-mode': '7',
   'query': 'QSTN',
   '2-in-direct-mode': '2'},
  'PR3': {'down': 'DOWN',
   ValueRange(1, 40): (1, 40),
   'query': 'QSTN',
   ValueRange(1, 30): (1, 30),
   'up': 'UP'},
  'BL3': {'down': 'DOWN', 'query': 'QSTN', 'xx': '{xx}', 'up': 'UP'},
  'SL3': {'hidden3': '09',
   'hidden2': '08',
   'hidden1': '07',
   'xm': '31',
   'am': '25',
   'airplay': '2D',
   'cbl': '01',
   'cd': '23',
   'down': 'DOWN',
   'tv/cd': '23',
   'aux2': '04',
   'query': 'QSTN',
   'aux1': '03',
   'bluetooth': '2E',
   'sat': '01',
   'usb': '2C',
   'pc': '05',
   'game/tv': '02',
   'extra2': '08',
   'internet-radio': '28',
   'extra1': '07',
   'vcr': '00',
   'extra3': '09',
   'tape': '20',
   'game2': '04',
   'game1': '02',
   'net': '2B',
   'sirius': '32',
   'video5': '04',
   'video4': '03',
   'video7': '06',
   'video6': '05',
   'video1': '00',
   'video3': '02',
   'video2': '01',
   'game': '02',
   'phono': '22',
   'p4s': '27',
   'fm': '24',
   'network': '2B',
   'multi-ch': '30',
   'universal-port': '40',
   'stb': '00',
   'dab': '33',
   'dvd': '10',
   'tape2': '21',
   'iradio-favorite': '28',
   'up': 'UP',
   'dlna': '27',
   'tv': '12',
   'strm-box': '11',
   'music-server': '27',
   'source': '80',
   'tuner': '26',
   'dvr': '00'},
  'TUN': {'down': 'DOWN', 'freq-nnnnn': 'nnnnn', 'up': 'UP', 'query': 'QSTN'},
  'VL3': {'level-down': 'DOWN',
   'level-up-1db-step': 'UP1',
   'level-up': 'UP',
   ValueRange(0, 80): (0, 80),
   'level-down-1db-step': 'DOWN1',
   ValueRange(0, 200): (0, 200),
   ValueRange(0, 100): (0, 100),
   'query': 'QSTN'},
  'NTC': {'trupz': 'TRUPz',
   'trdnz': 'TRDNz',
   'playz': 'PLAYz',
   'pausez': 'PAUSEz',
   'stopz': 'STOPz'},
  'NP3': {ValueRange(1, 40): (1, 40)}},
 'zone2': {'TUZ': {'6-in-direct-mode': '6',
   'down': 'DOWN',
   '3-in-direct-mode': '3',
   '8-in-direct-mode': '8',
   'band': 'BAND',
   '5-in-direct-mode': '5',
   'direct': 'DIRECT',
   '0-in-direct-mode': '0',
   '9-in-direct-mode': '9',
   'up': 'UP',
   'freq-nnnnn': 'nnnnn',
   '4-in-direct-mode': '4',
   '1-in-direct-mode': '1',
   '7-in-direct-mode': '7',
   'query': 'QSTN',
   '2-in-direct-mode': '2'},
  'ZTN': {'treble-up': 'TUP',
   'bass-up': 'BUP',
   'bass-down': 'BDOWN',
   'treble-down': 'TDOWN',
   'query': 'QSTN',
   'bass-xx-is-a-00-a-10-0-10-1-step': 'B{xx}',
   'treble-xx-is-a-00-a-10-0-10-1-step': 'T{xx}'},
  'PRS': {'down': 'DOWN',
   ValueRange(1, 40): (1, 40),
   'query': 'QSTN',
   ValueRange(1, 30): (1, 30),
   'up': 'UP'},
  'SLZ': {'hidden3': '09',
   'hidden2': '08',
   'hidden1': '07',
   'xm': '31',
   'am': '25',
   'airplay': '2D',
   'cbl': '01',
   'cd': '23',
   'down': 'DOWN',
   'tv/cd': '23',
   'aux2': '04',
   'query': 'QSTN',
   'aux1': '03',
   'bluetooth': '2E',
   'sat': '01',
   'hdmi-6': '56',
   'usb': '2C',
   'pc': '05',
   'game/tv': '02',
   'extra2': '08',
   'internet-radio': '28',
   'hdmi-7': '57',
   'extra1': '07',
   'vcr': '00',
   'extra3': '09',
   'tape': '20',
   'game2': '04',
   'game1': '02',
   'net': '2B',
   'bd': '10',
   'sirius': '32',
   'video5': '04',
   'video4': '03',
   'video7': '06',
   'video6': '05',
   'video1': '00',
   'video3': '02',
   'video2': '01',
   'hdmi-5': '55',
   'game': '02',
   'phono': '22',
   'p4s': '27',
   'fm': '24',
   'network': '2B',
   'multi-ch': '30',
   'universal-port': '40',
   'off': '7F',
   'stb': '00',
   'dab': '33',
   'dvd': '10',
   'tape2': '21',
   'iradio-favorite': '28',
   'up': 'UP',
   'dlna': '27',
   'tv': '12',
   'strm-box': '11',
   'music-server': '27',
   'source': '80',
   'tuner': '26',
   'dvr': '00'},
  'ZVL': {'level-down': 'DOWN',
   'level-up-1db-step': 'UP1',
   'level-up': 'UP',
   'level-down-1db-step': 'DOWN1',
   ValueRange(0, 80): (0, 80),
   ValueRange(0, 200): (0, 200),
   'query': 'QSTN',
   ValueRange(0, 100): (0, 100)},
  'LTZ': {'high': '02', 'query': 'QSTN', 'off': '00', 'low': '01', 'up': 'UP'},
  'ZBL': {'down': 'DOWN',
   'query': 'QSTN',
   'xx-is-a-00-a-l-10-0-r-10-1-step': '{xx}',
   'up': 'UP'},
  'LMZ': {'mono': '0F',
   'stereo': '00',
   'direct': '01',
   'dvs': '88',
   'multiplex': '12'},
  'ZPW': {'standby': '00', 'on': '01', 'query': 'QSTN'},
  'NTZ': {'right': 'RIGHT',
   'chup': 'CHUP',
   'random': 'RANDOM',
   'down': 'DOWN',
   'return': 'RETURN',
   'select': 'SELECT',
   'trdn': 'TRDN',
   'pause': 'PAUSE',
   'rew': 'REW',
   'memory': 'MEMORY',
   'play': 'PLAY',
   'repeat': 'REPEAT',
   'p-p': 'P/P',
   'trup': 'TRUP',
   'stop': 'STOP',
   'ff': 'FF',
   'chdn': 'CHDN',
   'rep-shf': 'REP/SHF',
   'up': 'UP',
   'mode': 'MODE',
   'display': 'DISPLAY',
   'left': 'LEFT'},
  'TUN': {'down': 'DOWN', 'freq-nnnnn': 'nnnnn', 'up': 'UP', 'query': 'QSTN'},
  'NPZ': {ValueRange(1, 40): (1, 40)},
  'ZPA': {'on': '01', 'off': '00', 'query': 'QSTN'},
  'NTC': {'trupz': 'TRUPz',
   'trdnz': 'TRDNz',
   'playz': 'PLAYz',
   'pausez': 'PAUSEz',
   'stopz': 'STOPz'},
  'ZPB': {'on': '01', 'off': '00', 'query': 'QSTN'},
  'RAZ': {'both-off': '00', 'query': 'QSTN', 'up': 'UP', 'on': '02'},
  'ZMT': {'on': '01', 'toggle': 'TG', 'off': '00', 'query': 'QSTN'},
  'PRZ': {'down': 'DOWN',
   ValueRange(1, 40): (1, 40),
   'query': 'QSTN',
   'up': 'UP',
   ValueRange(1, 30): (1, 30)}},
 'main': {'HBL': {'auto': '00', 'analog': '01', 'query': 'QSTN'},
  'CCM': {'zone2': '02',
   'main': '01',
   'query': 'QSTN',
   'sub': '10',
   'up': 'UP'},
  'HBT': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'FWV': {'query': 'QSTN', 'version': 'abce-fhik-lmno-qrtu'},
  'PAM': {'all': '07',
   'off': '00',
   'up': 'UP',
   'front': '01',
   'query': 'QSTN',
   'front-center': '03'},
  'PRM': {ValueRange(1, 30): (1, 30), ValueRange(1, 40): (1, 40)},
  'SPR': {'down': 'DOWN',
   'query': 'QSTN',
   ValueRange(0, 3): (0, 3),
   'up': 'UP'},
  'DCE': {'on': '01', 'off': '00', 'query': 'QSTN'},
  'SPI': {'5-rh': 'abcdefghhhijk',
   '150': 'abcdefghhhijk',
   '1-small': 'abcdefghhhijk',
   '5-f-s': 'abcdefghhhijk',
   '2-lage-e-surround-back-0-none': 'abcdefghhhijk',
   '3-tm': 'abcdefghhhijk',
   '2-2ch-b-front-1-small': 'abcdefghhhijk',
   'query': 'QSTN',
   '80': 'abcdefghhhijk',
   '1-f': 'abcdefghhhijk',
   '200-i-height-1-position-0-no': 'abcdefghhhijk',
   '8-dd-sp-b-j-height-2-position-0-no': 'abcdefghhhijk',
   '2-tf': 'abcdefghhhijk',
   '3-f-c': 'abcdefghhhijk',
   '2-lage-d-surround-0-none': 'abcdefghhhijk',
   '1-fh': 'abcdefghhhijk',
   '6-dd-sp-f': 'abcdefghhhijk',
   '4-tr': 'abcdefghhhijk',
   '2-large-c-center-0-none': 'abcdefghhhijk',
   '7-dd-sp-s': 'abcdefghhhijk',
   '6-c-s': 'abcdefghhhijk',
   '2-lage-f-height-1-0-none': 'abcdefghhhijk',
   '100': 'abcdefghhhijk',
   '2-lage-hhh-crossover-50': 'abcdefghhhijk',
   '8-dd-sp-b-k-bi-amp-0-no': 'abcdefghhhijk',
   '7-f-c-s': 'abcdefghhhijk',
   '2-lage-g-height-2-0-none': 'abcdefghhhijk',
   '1-yes': 'abcdefghhhijk',
   'a-subwoofer-0-no': 'abcdefghhhijk',
   '1ch': 'abcdefghhhijk'},
  'SPL': {'front-high': 'FH',
   'back-wide-speakers': 'BW',
   'height1-height2-speakers': 'HH',
   'speakers-b': 'B',
   'front-wide': 'FW',
   'up': 'UP',
   'surrback-front-wide-speakers': 'FW',
   'surrback': 'SB',
   'speakers-a': 'A',
   'front-high-front-wide-speakers': 'HW',
   'surrback-front-high-speakers': 'FH',
   'height2-speakers': 'H2',
   'query': 'QSTN',
   'back-height1-speakers': 'BH',
   'height1-speakers': 'H1',
   'speakers-a-b': 'AB'},
  'RAS': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'HST': {'throguh-auto': 'AT',
   'last': 'LAST',
   'auto': 'ATE',
   'up': 'UP',
   'query': 'QSTN',
   'xx-sli-number': 'xx',
   'off': 'OFF'},
  'SPA': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'SPB': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'SPD': {'query': 'QSTN'},
  'SAT': {'artist-name': 'nnnnnnnnnn', 'query': 'QSTN'},
  'TPD': {'query': 'QSTN', 'temp': '-99-999'},
  'HAO': {'on': '01', 'query': 'QSTN', 'off': '00', 'up': 'UP', 'auto': '02'},
  'EQS': {'off': '00',
   'preset-2': '02',
   'up': 'UP',
   'preset-3': '03',
   'down': 'DOWN',
   'query': 'QSTN',
   'preset-1': '01'},
  'LPS': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'TPS': {'start': u'\u201c\u201d', 'finish': u'\u201cENTER\u201d'},
  'HAS': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'HAT': {'artist-name': 'nnnnnnnnnn', 'query': 'QSTN'},
  'PQL': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'OSD': {'right': 'RIGHT',
   'ipv': 'IPV',
   'home': 'HOME',
   'menu': 'MENU',
   'up': 'UP',
   'down': 'DOWN',
   'exit': 'EXIT',
   'enter': 'ENTER',
   'quick': 'QUICK',
   'video': 'VIDEO',
   'audio': 'AUDIO',
   'left': 'LEFT'},
  'APD': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'AEQ': {'on': '02', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'IFV': {'a-a-b-b-c-c-d-d-e-e-f-f-g-g-h-h-i-i': u'a..a,b..b,c\u2026c,d..d,e\u2026e,f\u2026f,g\u2026g,h\u2026h,i\u2026i,',
   'query': 'QSTN'},
  'HDS': {'info': 'nnnnnnnnnn', 'query': 'QSTN'},
  'PCP': {'down': 'DOWN',
   'auto': 'AT',
   'query': 'QSTN',
   ValueRange(0, 16): (0, 16),
   'up': 'UP'},
  'TSB': {'treble-up': 'TUP',
   'bass-up': 'BUP',
   'bass-down': 'BDOWN',
   'treble-down': 'TDOWN',
   'b-xx': 'B{xx}',
   'query': 'QSTN',
   't-xx': 'T{xx}'},
  'PCT': {'on': '01',
   'query': 'QSTN',
   'off': '00',
   'up': 'UP',
   'full-band-on': '02'},
  'DMS': {'down': 'DOWN',
   ValueRange(-3, 3): (-3, 3),
   'up': 'UP',
   'query': 'QSTN'},
  'IFA': {'a-a-b-b-c-c-d-d-e-e-f-f': u'a..a,b..b,c\u2026c,d..d,e\u2026e,f\u2026f,',
   'a-a-b-b-c-c-d-d-e-e-f-f-g-g-h-h-i-i-j-j': u'a..a,b..b,c\u2026c,d..d,e\u2026e,f\u2026f,g\u2026g,h\u2026h,i\u2026I,j\u2026j,k\u2026k',
   'query': 'QSTN'},
  'CCD': {'play': 'PLAY',
   'pause': 'PAUSE',
   'random': 'RANDOM',
   'stop': 'STOP',
   'next': 'SKIP.F',
   'repeat': 'REPEAT',
   'previous': 'SKIP.R'},
  'HDO': {'both': '05',
   'sub': '03',
   'no': '00',
   'hdbaset': '02',
   'up': 'UP',
   'out-sub': '02',
   'query': 'QSTN',
   'yes': '01',
   'analog': '00',
   'out': '01'},
  'TSW': {'query': 'QSTN',
   'bass-up': 'BUP',
   'bass-down': 'BDOWN',
   'b-xx': 'B{xx}'},
  'CMT': {'aabbccddeeffgghhiijjkkllmm': 'aabbccddeeffgghhiijjkkllmm',
   'query': 'QSTN'},
  'TSR': {'treble-up': 'TUP',
   'bass-up': 'BUP',
   'bass-down': 'BDOWN',
   'treble-down': 'TDOWN',
   'b-xx': 'B{xx}',
   'query': 'QSTN',
   't-xx': 'T{xx}'},
  'FLD': {'query': 'QSTN', 'xx-xx-xx-xx-xx-x': '{xx}{xx}{xx}{xx}{xx}x'},
  'MFB': {'query': 'QSTN', '00': '00', '01': '01'},
  'SCN': {'query': 'QSTN', 'channel-name': 'nnnnnnnnnn'},
  'CEC': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'SCH': {'down': 'DOWN',
   'query': 'QSTN',
   ValueRange(0, 597): (0, 597),
   'up': 'UP'},
  'SCE': {'time-mm-ss': 'mm:ss'},
  'SCD': {'enhancement-off': '00',
   'query': 'QSTN',
   'up': 'UP',
   'enhancement-on': '01',
   ValueRange(2, 5): (2, 5)},
  'TCT': {'treble-up': 'TUP',
   'bass-up': 'BUP',
   'bass-down': 'BDOWN',
   'treble-down': 'TDOWN',
   'b-xx': 'B{xx}',
   'query': 'QSTN',
   't-xx': 'T{xx}'},
  'LDM': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'CTI': {'down': 'DOWN',
   ValueRange(0, 10): (0, 10),
   'up': 'UP',
   'query': 'QSTN'},
  'TCL': {'query': 'QSTN',
   'levels': 'aaabbbcccdddeeefffggghhhiiijjjkkklllmmm'},
  'HPR': {'query': 'QSTN', ValueRange(1, 8): (1, 8)},
  'SCT': {'down': 'DOWN',
   'query': 'QSTN',
   'category-info': 'nnnnnnnnnn',
   'up': 'UP'},
  'PTS': {'no-1-29': u'\u201c01\u201d-\u201c1D\u201d',
   'enter': u'\u201cENTER\u201d'},
  'AMT': {'on': '01', 'toggle': 'TG', 'off': '00', 'query': 'QSTN'},
  'DLC': {'down': 'DOWN',
   'query': 'QSTN',
   ValueRange(0, 6): (0, 6),
   'up': 'UP'},
  'PBS': {'on': '01', 'toggle': 'UP', 'off': '00', 'query': 'QSTN'},
  'UPD': {'00': '00',
   '01': '01',
   'force': '02',
   'usb': 'USB',
   'd-nn': 'D**-nn',
   'e-xx-yy': 'E{xx}-yy',
   'query': 'QSTN',
   'net': 'NET',
   'cmp': 'CMP'},
  'STI': {'query': 'QSTN', 'title': 'nnnnnnnnnn'},
  'DSN': {'query': 'QSTN', 'station-name': u'xx\u2026xx'},
  'CST': {'status': 'prs', 'query': 'QSTN'},
  'HCN': {'query': 'QSTN', 'channel-name': 'nnnnnnnnnn'},
  'UPS': {'up': 'UP',
   'x8': '03',
   'x2': '01',
   'query': 'QSTN',
   'x1': '00',
   'x4': '02'},
  'LRA': {'down': 'Down',
   ValueRange(1, 7): (1, 7),
   'up': 'UP',
   'query': 'QSTN'},
  'XCH': {'down': 'DOWN',
   'query': 'QSTN',
   ValueRange(0, 597): (0, 597),
   'up': 'UP'},
  'XCN': {'query': 'QSTN', 'channel-name': 'nnnnnnnnnn'},
  'HTI': {'query': 'QSTN', 'title': 'nnnnnnnnnn'},
  'AVS': {'is-decreased': 'DOWN',
   'query': 'QSTN',
   'is-increased': 'UP',
   'offset': 'snnn'},
  'VWM': {'4-3': '01',
   'smart-zoom': '05',
   'auto': '00',
   'up': 'UP',
   'zoom': '04',
   'full': '02',
   'query': 'QSTN'},
  'HTS': {'query': 'QSTN', 'mmnnoo': 'mmnnoo'},
  'XCT': {'down': 'DOWN',
   'query': 'QSTN',
   'category-info': 'nnnnnnnnnn',
   'up': 'UP'},
  'VOS': {'query': 'QSTN', 'd4': '00', 'component': '01'},
  'DVL': {'on': '01',
   'off': '00',
   'up': 'UP',
   'mid': '02',
   'high': '03',
   'low': '01',
   'query': 'QSTN'},
  'CTW': {'down': 'DOWN',
   'query': 'QSTN',
   ValueRange(0, 7): (0, 7),
   'up': 'UP'},
  'ACE': {'query': 'QSTN', 'eq': 'aaabbbcccdddeeefffggghhhiii'},
  'CTS': {'center-off': '00',
   'toggle': 'TG',
   'center-on': '01',
   'query': 'QSTN'},
  'RES': {'480p': '02',
   '1080i': '04',
   '4k-upcaling': '08',
   'auto': '01',
   '720p': '03',
   'up': 'UP',
   '2560x1080p': '15',
   'source': '06',
   'through': '00',
   '24fs': '07',
   'query': 'QSTN',
   '1080p': '07',
   '1680x720p': '13'},
  'CTL': {'down': u'\u201cDOWN\u201d',
   ValueRange(-24, 24): (-24, 24),
   'query': 'QSTN',
   'up': u'\u201cUP\u201d',
   ValueRange(-12, 12): (-12, 12)},
  'CTM': {'query': 'QSTN', 'time-mm-ss-mm-ss': 'mm:ss/mm:ss'},
  'ISF': {'up': 'UP',
   'night': '02',
   'query': 'QSTN',
   'day': '01',
   'custom': '00'},
  'BCS': {'completed': '01',
   'high': '12',
   'middle': '11',
   'low': '10',
   'query': 'QSTN',
   'charging': '00'},
  'LFE': {'down': 'DOWN',
   'query': 'QSTN',
   '00-0db-01-1db-02-2db-03-3db-04-4db-05-5db-0a-10db-0f-15db-14-20db-ff-oodb': 'xx',
   'up': 'UP'},
  'DGF': {'sharp': '01',
   'slow': '00',
   'query': 'QSTN',
   'up': 'UP',
   'short': '02'},
  'ECO': {'volume-6db-down-and-dimmer-level-dark': '06',
   'volume-1db-down-and-dimmer-level-dark': '01',
   'volume-3db-down-and-dimmer-level-dark': '03'},
  'ASC': {'auto': '00', 'manual': '01', 'up': 'UP', 'query': 'QSTN'},
  'PMB': {'on': '01', 'toggle': 'TG', 'off': '00', 'query': 'QSTN'},
  'ADV': {'heavy': '03',
   'medium': '02',
   'off': '00',
   'light': '01',
   'up': 'UP',
   'query': 'QSTN'},
  'PRS': {'down': 'DOWN',
   ValueRange(1, 40): (1, 40),
   'query': 'QSTN',
   'up': 'UP',
   ValueRange(1, 30): (1, 30)},
  'RDS': {'02': u'\u201c02\u201d',
   '00': u'\u201c00\u201d',
   '01': u'\u201c01\u201d',
   'up': u'\u201cUP\u201d'},
  'ADQ': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'ADY': {'on': '01',
   'off': '00',
   'movie': '01',
   'up': 'UP',
   'music': '02',
   'query': 'QSTN'},
  'DMN': {'query': 'QSTN',
   'main': '00',
   'sub': '01',
   'main-sub': '02',
   'up': 'UP'},
  'MVL': {'level-down': 'DOWN',
   'level-up-1db-step': 'UP1',
   'level-up': 'UP',
   ValueRange(0, 50): (0, 50),
   'level-down-1db-step': 'DOWN1',
   ValueRange(0, 200): (0, 200),
   ValueRange(0, 80): (0, 80),
   'query': 'QSTN',
   ValueRange(0, 100): (0, 100)},
  'LTN': {'auto-dolby-truehd': '03',
   'off': '00',
   'on-dolby-truehd': '01',
   'up': 'UP',
   'low-dolbydigital': '01',
   'high-dolbydigital': '02',
   'query': 'QSTN'},
  'XTI': {'query': 'QSTN', 'title': 'nnnnnnnnnn'},
  'SBS': {'on': '01', 'toggle': 'UP', 'off': '00', 'query': 'QSTN'},
  'MEM': {'rcl': 'RCL', 'lock': 'LOCK', 'unlk': 'UNLK', 'str': 'STR'},
  'TUN': {'6-in-direct-mode': '6',
   'down': 'DOWN',
   '3-in-direct-mode': '3',
   '8-in-direct-mode': '8',
   'band': 'BAND',
   '5-in-direct-mode': '5',
   'direct': 'DIRECT',
   '0-in-direct-mode': '0',
   '9-in-direct-mode': '9',
   'up': 'UP',
   'freq-nnnnn': 'nnnnn',
   '4-in-direct-mode': '4',
   '1-in-direct-mode': '1',
   '7-in-direct-mode': '7',
   'query': 'QSTN',
   '2-in-direct-mode': '2'},
  'ITV': {'down': 'DOWN',
   ValueRange(-24, 24): (-24, 24),
   'up': 'UP',
   'query': 'QSTN'},
  'LMD': {'all-ch-stereo': '0C',
   'neo-6-cinema-dts-surround-sensation': '91',
   'dts-neural-x-thx-games': '8A',
   'multiplex': '12',
   'pliix-thx-music': '8B',
   'neo-6-music-dts-surround-sensation': '92',
   'unplugged': '09',
   'dts-x': '82',
   'game-rock': '06',
   'neural-surround-audyssey-dsx': 'A5',
   'pliix-music': '81',
   'game-sports': '0E',
   'thx-surround-ex': '43',
   'pliiz-height': '90',
   'auto': 'AUTO',
   'straight-decode': '40',
   'game': 'GAME',
   'dolby-atmos': '80',
   'whole-house': '1F',
   'plii-game-audyssey-dsx': 'A2',
   'neural-thx': '88',
   'neo-x-music': '83',
   'neural-digital-music': '93',
   'enhance': '0E',
   'neural-surround': '88',
   's-cinema': '50',
   'pliiz-height-thx-games': '96',
   'dolby-surround-thx-games': '89',
   'game-rpg': '03',
   'full-mono': '13',
   'direct': '01',
   'enhanced-7': '0E',
   'thx-u2': '52',
   'query': 'QSTN',
   'neural-x': '82',
   'neural-thx-music': '8E',
   'neo-6-cinema-audyssey-dsx': 'A3',
   's-games': '52',
   'movie': 'MOVIE',
   'orchestra': '08',
   'dolby-ex': '41',
   'pliiz-height-thx-music': '95',
   'neo-x-game': '9A',
   'neo-x-thx-cinema': '85',
   'pliix': 'A2',
   'dolby-surround-thx-music': '8B',
   'dts-surround-sensation': '15',
   'pure-audio': '11',
   'dolby-surround-thx-cinema': '84',
   'thx-cinema': '42',
   'mono': '0F',
   'pliiz-height-thx-u2': '99',
   'surround': '02',
   'mono-movie': '07',
   'surround-enhancer': '14',
   'cinema2': '50',
   'action': '25',
   'down': 'DOWN',
   'pliix-game': '86',
   'game-action': '05',
   'thx-musicmode': '51',
   'neo-6-music': '83',
   'thx-music': '44',
   'sports': '2E',
   'music': 'MUSIC',
   'pliix-thx-games': '89',
   'stage': '23',
   'neo-x-cinema': '82',
   's2-music': '98',
   'plii-music-audyssey-dsx': 'A1',
   'auto-surround': 'FF',
   'dts-neural-x-thx-music': '8C',
   'dolby-surround': '80',
   'theater-dimensional': '0D',
   'up': 'UP',
   'dolby-ex-audyssey-dsx': 'A7',
   'dts-neural-x-thx-cinema': '85',
   'neural-thx-cinema': '8D',
   'neo-6': '8C',
   'pliiz-height-thx-cinema': '94',
   'film': '03',
   'neo-x-thx-games': '8A',
   'studio-mix': '0A',
   'neural-digital-music-audyssey-dsx': 'A6',
   's2': '52',
   'tv-logic': '0B',
   'neo-6-cinema': '82',
   'dolby-virtual': '14',
   'plii': '8B',
   'thx-games': '52',
   'pliix-movie': '80',
   'neo-x-thx-music': '8C',
   's2-cinema': '97',
   'surr': 'SURR',
   's2-games': '99',
   's-music': '51',
   'neural-surr': '87',
   'stereo': 'STEREO',
   'thx': 'THX',
   'i': '52',
   'plii-movie-audyssey-dsx': 'A0',
   'neo-6-music-audyssey-dsx': 'A4',
   'audyssey-dsx': '16',
   'pliix-thx-cinema': '84',
   'neural-thx-games': '8F',
   'musical': '06'},
  'DIM': {'dim': 'DIM',
   'bright-led-off': '08',
   'dark': '02',
   'bright': '00',
   'query': 'QSTN',
   'shut-off': '03'},
  'SWL': {'down': u'\u201cDOWN\u201d',
   'up': u'\u201cUP\u201d',
   'query': 'QSTN',
   ValueRange(-30, 24): (-30, 24),
   ValueRange(-15, 12): (-15, 12)},
  'DIF': {'02': '02',
   '03': '03',
   'selector-listening-1line': '01',
   'toggle': 'TG',
   'query': 'QSTN',
   'default-2line': '00',
   'selector-volume-1line': '00'},
  'MOT': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'DIR': {'on': '01', 'toggle': 'TG', 'off': '00', 'query': 'QSTN'},
  'POP': {'t': 't----<.....>', 'ullt': 'Ullt<.....>'},
  'TGB': {'on': '01', 'off': '00', 'query': 'QSTN'},
  'TGC': {'on': '01', 'off': '00', 'query': 'QSTN'},
  'TGA': {'on': '01', 'off': '00', 'query': 'QSTN'},
  'VPM': {'isf-night': '06',
   'cinema': '02',
   'up': 'UP',
   'direct': '08',
   'custom': '01',
   'streaming': '07',
   'game': '03',
   'through': '00',
   'bypass': '08',
   'isf-day': '05',
   'query': 'QSTN',
   'standard': '00'},
  'DST': {'mp3-cd': '07',
   'unknown': 'FF',
   'none': '00',
   'query': 'QSTN',
   'cd': '04'},
  'XAT': {'artist-name': 'nnnnnnnnnn', 'query': 'QSTN'},
  'SLP': {'time-off': u'\u201cOFF\u201d',
   'up': u'\u201cUP\u201d',
   'qstn': u'\u201cQSTN\u201d',
   'time-1-90min': u'\u201c01\u201d-\u201c5A\u201d'},
  'FXP': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'SLR': {'xm': '31',
   'am': '25',
   'cd': '23',
   'query': 'QSTN',
   'tape2': '21',
   'internet-radio': '28',
   'source': '80',
   'tape': '20',
   'video5': '04',
   'video4': '03',
   'video7': '06',
   'video6': '05',
   'video1': '00',
   'video3': '02',
   'video2': '01',
   'phono': '22',
   'fm': '24',
   'multi-ch': '30',
   'off': '7F',
   'dvd': '10',
   'music-server': '27',
   'tuner': '26'},
  'HOI': {'query': 'QSTN',
   '2-for-zone-2': 'ab',
   '1-for-zone': 'ab',
   'a-1-for-zone-b-sub-0-none': 'ab'},
  'STW': {'on': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'SLI': {'bd': '10',
   'aiplay': '2D',
   'xm': '31',
   'am': '25',
   'cbl': '01',
   'cd': '23',
   'down': 'DOWN',
   'tv/cd': '23',
   'aux2': '04',
   'query': 'QSTN',
   'aux1': '03',
   'bluetooth': '2E',
   'network': '2B',
   'sat': '01',
   'hdmi-6': '56',
   '07': '07',
   'tape2': '21',
   'game/tv': '02',
   '08': '08',
   '09': '09',
   'line2': '42',
   'internet-radio': '28',
   'hdmi-7': '57',
   'pc': '05',
   'vcr': '00',
   'universal-port': '40',
   'game2': '04',
   'game1': '02',
   'net': '2B',
   'sirius': '32',
   'video5': '04',
   'video4': '03',
   'video7': '06',
   'video6': '05',
   'video1': '00',
   'video3': '02',
   'video2': '01',
   'hdmi-5': '55',
   'usb-dac-in': '2F',
   'game': '02',
   'optical': '44',
   'coaxial': '45',
   'line': '41',
   'phono': '22',
   'p4s': '27',
   'fm': '24',
   'usb': '2C',
   'tape-1': '20',
   'multi-ch': '30',
   'tv/tape': '20',
   'stb': '00',
   'dab': '33',
   'dvd': '10',
   'iradio-favorite': '28',
   'up': 'UP',
   'dlna': '27',
   'tv': '12',
   'strm-box': '11',
   'music-server': '27',
   'tuner': u'\u201c26\u201d',
   'dvr': '00'},
  'IRN': {'name-10-characters-ii-number-the-same-as-for-sli-command-xxxxxxxxxx-name': 'iixxxxxxxxxx'},
  'SLK': {'input': 'INPUT', 'wrong': 'WRONG', 'password': 'nnnn'},
  'SLA': {'opt': '05',
   'none': '0F',
   'coax': '05',
   'hdmi': '04',
   'auto': '00',
   'multi-channel': '01',
   'up': 'UP',
   'ilink': '03',
   'arc': '07',
   'query': 'QSTN',
   'balance': '06',
   'analog': '02'},
  'SLC': {'test': u'\u201cTEST\u201d',
   'down': u'\u201cDOWN\u201d',
   'test-tone-off': 'OFF',
   'up': u'\u201cUP\u201d',
   'chsel': u'\u201cCHSEL\u201d'},
  'MCM': {'memory-2': '02',
   'memory-3': '03',
   'memory-1': '01',
   'memory-6': '06',
   'memory-4': '04',
   'memory-5': '05',
   'up': 'UP',
   'down': 'DOWN',
   'query': 'QSTN'},
  'PWR': {'standby': '00',
   'on': '01',
   'standby-all': 'ALL',
   'off': '00',
   'query': 'QSTN'},
  'TFH': {'treble-up': 'TUP',
   'bass-up': 'BUP',
   'bass-down': 'BDOWN',
   'treble-down': 'TDOWN',
   'b-xx': 'B{xx}',
   'query': 'QSTN',
   't-xx': 'T{xx}'},
  'PNR': {'on': '01', 'toggle': 'TG', 'off': '00', 'query': 'QSTN'},
  'MCC': {'query': 'QSTN', '00': '00', '01': '01'},
  'ARC': {'auto': '01', 'off': '00', 'up': 'UP', 'query': 'QSTN'},
  'RST': {'all': 'ALL'},
  'TFW': {'treble-up': 'TUP',
   'bass-up': 'BUP',
   'bass-down': 'BDOWN',
   'treble-down': 'TDOWN',
   'b-xx': 'B{xx}',
   'query': 'QSTN',
   't-xx': 'T{xx}'},
  'SW2': {'down': u'\u201cDOWN\u201d',
   ValueRange(-30, 24): (-30, 24),
   'query': 'QSTN',
   ValueRange(-15, 12): (-15, 12),
   'up': u'\u201cUP\u201d'},
  'CFS': {'query': 'QSTN', ValueRange(1, 153): (1, 153)},
  'TFR': {'treble-up': 'TUP',
   'bass-up': 'BUP',
   'bass-down': 'BDOWN',
   'treble-down': 'TDOWN',
   'b-xx': 'B{xx}',
   'query': 'QSTN',
   't-xx': 'T{xx}'}},
 'dock': {'NAF': {'xx-xx': '{xx}{xx}'},
  'AAL': {'query': 'QSTN', 'album-name': 'nnnnnnn'},
  'ATM': {'mm-ss-mm-ss': 'mm:ss/mm:ss', 'query': 'QSTN'},
  'NAL': {'query': 'QSTN', 'album-name': 'nnnnnnn'},
  'ATI': {'query': 'QSTN', 'title': 'nnnnnnnnnn'},
  'AAT': {'query': 'QSTN'},
  'NAT': {'artist-name': 'nnnnnnnnnn', 'query': 'QSTN'},
  'NLS': {'info': 'tlpnnnnnnnnnn', 'ti': 'ti'},
  'NLU': {'xx-xx-yyyy': '{xx}{xx}yyyy'},
  'NLT': {'title-info': '{xx}uycccciiiillrraabbssnnn...nnn', 'query': 'QSTN'},
  'AST': {'query': 'QSTN', 'prs': 'prs'},
  'NBS': {'on': 'ON', 'off': 'OFF', 'query': 'QSTN'},
  'NSB': {'query': 'QSTN', 'is-off': 'OFF', 'is-on': 'ON'},
  'NSD': {'xx-xx-xx-xx-xx-x': '{xx}{xx}{xx}{xx}{xx}x'},
  'NBT': {'clear': 'CLEAR', 'pairing': 'PAIRING'},
  'NLA': {'lzzzzll-xx-xx-yyyy': 'Lzzzzll{xx}{xx}yyyy',
   'izzzzll-xx-xx': 'Izzzzll{xx}{xx}----',
   'tzzzzsurr': 'tzzzzsurr<.....>'},
  'NST': {'query': 'QSTN', 'prs': 'prs'},
  'NSV': {'service-id': u'ssiaaaa\u2026aaaabbbb\u2026bbbb'},
  'NJA': {'enable-and-image-type-link': 'LINK',
   'enable': 'ENA',
   'tp-xx-xx-xx-xx-xx-xx': 'tp{xx}{xx}{xx}{xx}{xx}{xx}',
   'enable-and-image-type-bmp': 'BMP',
   'req': 'REQ',
   'up': 'UP',
   'disable': 'DIS',
   'query': 'QSTN'},
  'NDS': {'query': 'QSTN', 'nfr': 'nfr'},
  'NRI': {'xml': u'<\u2026>',
   'query': 'QSTN',
   't': 't----<.....>',
   'ullt': 'Ullt<.....>'},
  'NMS': {'query': 'QSTN', 'maabbstii': 'maabbstii'},
  'NTS': {'mm-ss': 'mm:ss', 'hh-mm-ss': 'hh:mm:ss'},
  'NTR': {'cccc-tttt': 'cccc/tttt', 'query': 'QSTN'},
  'NRF': {ValueRange(1, 40): (1, 40)},
  'NPB': {'query': 'QSTN', 'pudtsrrr': 'pudtsrrr'},
  'NTI': {'query': 'QSTN', 'title': 'nnnnnnnnnn'},
  'NMD': {'std': 'STD', 'query': 'QSTN', 'ext': 'EXT', 'vdc': 'VDC'},
  'NTM': {'mm-ss-mm-ss': 'mm:ss/mm:ss',
   'query': 'QSTN',
   'hh-mm-ss-hh-mm-ss': 'hh:mm:ss/hh:mm:ss'},
  'NPU': {'popup': u'xaaa\u2026aaaybbb\u2026bbb'},
  'NKY': {'input': 'nnnnnnnnn', 'll': 'll'},
  'NTC': {'f1': 'F1',
   'f2': 'F2',
   'right': 'RIGHT',
   'chup': 'CHUP',
   'random': 'RANDOM',
   'rep-shf': 'REP/SHF',
   'return': 'RETURN',
   'select': 'SELECT',
   'trdn': 'TRDN',
   'playlist': 'PLAYLIST',
   'pause': 'PAUSE',
   'menu': 'MENU',
   'top': 'TOP',
   'rew': 'REW',
   '1': '1',
   '0': '0',
   '3': '3',
   '2': '2',
   '5': '5',
   '4': '4',
   '7': '7',
   '6': '6',
   '9': '9',
   '8': '8',
   'location': 'LOCATION',
   'album': 'ALBUM',
   'play': 'PLAY',
   'repeat': 'REPEAT',
   'p-p': 'P/P',
   'trup': 'TRUP',
   'memory': 'MEMORY',
   'stop': 'STOP',
   'caps': 'CAPS',
   'ff': 'FF',
   'genre': 'GENRE',
   'chdn': 'CHDN',
   'down': 'DOWN',
   'language': 'LANGUAGE',
   'artist': 'ARTIST',
   'setup': 'SETUP',
   'list': 'LIST',
   'up': 'UP',
   'mode': 'MODE',
   'delete': 'DELETE',
   'display': 'DISPLAY',
   'left': 'LEFT'},
  'NPR': {ValueRange(1, 40): (1, 40), 'set': 'SET'}},
 'zone4': {'PW4': {'standby': '00', 'on': '01', 'query': 'QSTN'},
  'PRS': {'down': 'DOWN',
   'up': 'UP',
   'query': 'QSTN',
   ValueRange(1, 40): (1, 40),
   ValueRange(1, 30): (1, 30)},
  'SL4': {'hidden3': '09',
   'hidden2': '08',
   'hidden1': '07',
   'xm': '31',
   'am': '25',
   'airplay': '2D',
   'cbl': '01',
   'cd': '23',
   'down': 'DOWN',
   'tv/cd': '23',
   'aux2': '04',
   'query': 'QSTN',
   'aux1': '03',
   'bluetooth': '2E',
   'sat': '01',
   'usb': '2C',
   'pc': '05',
   'game/tv': '02',
   'extra2': '08',
   'internet-radio': '28',
   'extra1': '07',
   'vcr': '00',
   'extra3': '09',
   'game2': '04',
   'game1': '02',
   'net': '2B',
   'bd': '10',
   'sirius': '32',
   'video5': '04',
   'video4': '03',
   'video7': '06',
   'video6': '05',
   'video1': '00',
   'video3': '02',
   'video2': '01',
   'game': '02',
   'phono': '22',
   'p4s': '27',
   'fm': '24',
   'network': '2B',
   'tape-1': '20',
   'multi-ch': '30',
   'universal-port': '40',
   'tv/tape': '20',
   'stb': '00',
   'dab': '33',
   'dvd': '10',
   'tape2': '21',
   'iradio-favorite': '28',
   'up': 'UP',
   'dlna': '27',
   'music-server': '27',
   'source': '80',
   'tuner': '26',
   'dvr': '00'},
  'TU4': {'6-in-direct-mode': '6',
   '3-in-direct-mode': '3',
   '8-in-direct-mode': '8',
   'up': 'UP',
   '5-in-direct-mode': '5',
   'direct': 'DIRECT',
   '0-in-direct-mode': '0',
   '9-in-direct-mode': '9',
   'down': 'DOWN',
   '4-in-direct-mode': '4',
   '1-in-direct-mode': '1',
   'freq-nnnnn,': 'nnnnn',
   '7-in-direct-mode': '7',
   'query': 'QSTN',
   '2-in-direct-mode': '2'},
  'NT4': {'trdn': 'TRDN',
   'play': 'PLAY',
   'pause': 'PAUSE',
   'return': 'RETURN',
   'trup': 'TRUP',
   'random': 'RANDOM',
   'stop': 'STOP',
   'rew': 'REW',
   'up': 'UP',
   'down': 'DOWN',
   'repeat': 'REPEAT',
   'right': 'RIGHT',
   'ff': 'FF',
   'display': 'DISPLAY',
   'select': 'SELECT',
   'left': 'LEFT'},
  'MT4': {'on': '01', 'toggle': 'TG', 'off': '00', 'query': 'QSTN'},
  'PR4': {'down': 'DOWN',
   ValueRange(1, 40): (1, 40),
   'query': 'QSTN',
   ValueRange(1, 30): (1, 30),
   'up': 'UP'},
  'TUN': {'down': 'DOWN', 'query': 'QSTN', 'up': 'UP', 'freq-nnnnn,': 'nnnnn'},
  'NP4': {ValueRange(1, 40): (1, 40)},
  'NTC': {'trupz': 'TRUPz',
   'trdnz': 'TRDNz',
   'playz': 'PLAYz',
   'pausez': 'PAUSEz',
   'stopz': 'STOPz'},
  'VL4': {'level-down': 'DOWN',
   ValueRange(0, 100): (0, 100),
   'query': 'QSTN',
   'level-up': 'UP',
   ValueRange(0, 80): (0, 80)}}}

_LOGGER = logging.getLogger(__name__)

DOMAIN = "onkyo"

DATA_MP_ENTITIES: HassKey[list[dict[str, OnkyoMediaPlayer]]] = HassKey(DOMAIN)

CONF_SOURCES = "sources"
CONF_MAX_VOLUME = "max_volume"
CONF_RECEIVER_MAX_VOLUME = "receiver_max_volume"

DEFAULT_NAME = "Onkyo Receiver"
SUPPORTED_MAX_VOLUME = 100
DEFAULT_RECEIVER_MAX_VOLUME = 80
ZONES = {"zone2": "Zone 2", "zone3": "Zone 3", "zone4": "Zone 4"}

SUPPORT_ONKYO_WO_VOLUME = (
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY_MEDIA
)
SUPPORT_ONKYO = (
    SUPPORT_ONKYO_WO_VOLUME
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.VOLUME_STEP
)

KNOWN_HOSTS: list[str] = []

DEFAULT_SOURCES = {
    "tv": "TV",
    "bd": "Bluray",
    "game": "Game",
    "aux1": "Aux1",
    "video1": "Video 1",
    "video2": "Video 2",
    "video3": "Video 3",
    "video4": "Video 4",
    "video5": "Video 5",
    "video6": "Video 6",
    "video7": "Video 7",
    "fm": "Radio",
}
DEFAULT_PLAYABLE_SOURCES = ("fm", "am", "tuner")

PLATFORM_SCHEMA = MEDIA_PLAYER_PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_MAX_VOLUME, default=SUPPORTED_MAX_VOLUME): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
        vol.Optional(
            CONF_RECEIVER_MAX_VOLUME, default=DEFAULT_RECEIVER_MAX_VOLUME
        ): cv.positive_int,
        vol.Optional(CONF_SOURCES, default=DEFAULT_SOURCES): {cv.string: cv.string},
    }
)

ATTR_HDMI_OUTPUT = "hdmi_output"
ATTR_PRESET = "preset"
ATTR_AUDIO_INFORMATION = "audio_information"
ATTR_VIDEO_INFORMATION = "video_information"
ATTR_VIDEO_OUT = "video_out"

AUDIO_VIDEO_INFORMATION_UPDATE_WAIT_TIME = 8

AUDIO_INFORMATION_MAPPING = [
    "audio_input_port",
    "input_signal_format",
    "input_frequency",
    "input_channels",
    "listening_mode",
    "output_channels",
    "output_frequency",
    "precision_quartz_lock_system",
    "auto_phase_control_delay",
    "auto_phase_control_phase",
]

VIDEO_INFORMATION_MAPPING = [
    "video_input_port",
    "input_resolution",
    "input_color_schema",
    "input_color_depth",
    "video_output_port",
    "output_resolution",
    "output_color_schema",
    "output_color_depth",
    "picture_mode",
]

ACCEPTED_VALUES = [
    "no",
    "analog",
    "yes",
    "out",
    "out-sub",
    "sub",
    "hdbaset",
    "both",
    "up",
]
ONKYO_SELECT_OUTPUT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(ATTR_HDMI_OUTPUT): vol.In(ACCEPTED_VALUES),
    }
)
SERVICE_SELECT_HDMI_OUTPUT = "onkyo_select_hdmi_output"


async def async_register_services(hass: HomeAssistant) -> None:
    """Register Onkyo services."""

    async def async_service_handle(service: ServiceCall) -> None:
        """Handle for services."""
        entity_ids = service.data[ATTR_ENTITY_ID]

        targets: list[OnkyoMediaPlayer] = []
        for receiver_entities in hass.data[DATA_MP_ENTITIES]:
            targets.extend(
                entity
                for entity in receiver_entities.values()
                if entity.entity_id in entity_ids
            )

        for target in targets:
            if service.service == SERVICE_SELECT_HDMI_OUTPUT:
                await target.async_select_output(service.data[ATTR_HDMI_OUTPUT])

    hass.services.async_register(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_SELECT_HDMI_OUTPUT,
        async_service_handle,
        schema=ONKYO_SELECT_OUTPUT_SCHEMA,
    )


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Onkyo platform."""
    await async_register_services(hass)

    receivers: dict[str, Receiver] = {}  # indexed by host
    all_entities = hass.data.setdefault(DATA_MP_ENTITIES, [])

    host = config.get(CONF_HOST)
    name = config.get(CONF_NAME)
    max_volume = config[CONF_MAX_VOLUME]
    receiver_max_volume = config[CONF_RECEIVER_MAX_VOLUME]
    sources = config[CONF_SOURCES]

    async def async_setup_receiver(
        info: ReceiverInfo, discovered: bool, name: str | None
    ) -> None:
        entities: dict[str, OnkyoMediaPlayer] = {}
        all_entities.append(entities)

        @callback
        def async_onkyo_update_callback(
            message: tuple[str, str, Any], origin: str
        ) -> None:
            """Process new message from receiver."""
            receiver = receivers[origin]
            _LOGGER.debug(
                "Received update callback from %s: %s", receiver.name, message
            )

            zone, _, value = message
            entity = entities.get(zone)
            if entity is not None:
                if entity.enabled:
                    entity.process_update(message)
            elif zone in ZONES and value != "N/A":
                # When we receive the status for a zone, and the value is not "N/A",
                # then zone is available on the receiver, so we create the entity for it.
                _LOGGER.debug("Discovered %s on %s", ZONES[zone], receiver.name)
                zone_entity = OnkyoMediaPlayer(
                    receiver, sources, zone, max_volume, receiver_max_volume
                )
                entities[zone] = zone_entity
                async_add_entities([zone_entity])

        @callback
        def async_onkyo_connect_callback(origin: str) -> None:
            """Receiver (re)connected."""
            receiver = receivers[origin]
            _LOGGER.debug(
                "Receiver (re)connected: %s (%s)", receiver.name, receiver.conn.host
            )

            for entity in entities.values():
                entity.backfill_state()

        _LOGGER.debug("Creating receiver: %s (%s)", info.model_name, info.host)
        connection = await Connection.create(
            host=info.host,
            port=info.port,
            update_callback=async_onkyo_update_callback,
            connect_callback=async_onkyo_connect_callback,
        )

        receiver = Receiver(
            conn=connection,
            model_name=info.model_name,
            identifier=info.identifier,
            name=name or info.model_name,
            discovered=discovered,
        )

        receivers[connection.host] = receiver

        # Discover what zones are available for the receiver by querying the power.
        # If we get a response for the specific zone, it means it is available.
        for zone in ZONES:
            receiver.conn.query_property(zone, "power")

        # Add the main zone to entities, since it is always active.
        _LOGGER.debug("Adding Main Zone on %s", receiver.name)
        main_entity = OnkyoMediaPlayer(
            receiver, sources, "main", max_volume, receiver_max_volume
        )
        entities["main"] = main_entity
        async_add_entities([main_entity])

    if host is not None:
        if host in KNOWN_HOSTS:
            return

        _LOGGER.debug("Manually creating receiver: %s (%s)", name, host)

        @callback
        async def async_onkyo_interview_callback(conn: Connection) -> None:
            """Receiver interviewed, connection not yet active."""
            info = ReceiverInfo(conn.host, conn.port, conn.name, conn.identifier)
            _LOGGER.debug("Receiver interviewed: %s (%s)", info.model_name, info.host)
            if info.host not in KNOWN_HOSTS:
                KNOWN_HOSTS.append(info.host)
                await async_setup_receiver(info, False, name)

        await Connection.discover(
            host=host,
            discovery_callback=async_onkyo_interview_callback,
        )
    else:
        _LOGGER.debug("Discovering receivers")

        @callback
        async def async_onkyo_discovery_callback(conn: Connection) -> None:
            """Receiver discovered, connection not yet active."""
            info = ReceiverInfo(conn.host, conn.port, conn.name, conn.identifier)
            _LOGGER.debug("Receiver discovered: %s (%s)", info.model_name, info.host)
            if info.host not in KNOWN_HOSTS:
                KNOWN_HOSTS.append(info.host)
                await async_setup_receiver(info, True, None)

        await Connection.discover(
            discovery_callback=async_onkyo_discovery_callback,
        )

    @callback
    def close_receiver(_event: Event) -> None:
        for receiver in receivers.values():
            receiver.conn.close()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_receiver)


class OnkyoMediaPlayer(MediaPlayerEntity):
    """Representation of an Onkyo Receiver Media Player (one per each zone)."""

    _attr_should_poll = False

    _supports_volume: bool = False
    _supports_audio_info: bool = False
    _supports_video_info: bool = False
    _query_timer: asyncio.TimerHandle | None = None

    def __init__(
        self,
        receiver: Receiver,
        sources: dict[str, str],
        zone: str,
        max_volume: int,
        volume_resolution: int,
    ) -> None:
        """Initialize the Onkyo Receiver."""
        self._receiver = receiver
        name = receiver.name
        identifier = receiver.identifier
        self._attr_name = f"{name}{' ' + ZONES[zone] if zone != 'main' else ''}"
        if receiver.discovered and zone == "main":
            # keep legacy unique_id
            self._attr_unique_id = f"{name}_{identifier}"
        else:
            self._attr_unique_id = f"{identifier}_{zone}"

        self._zone = zone
        self._source_mapping = sources
        self._reverse_mapping = {value: key for key, value in sources.items()}
        self._max_volume = max_volume
        self._volume_resolution = volume_resolution

        self._attr_source_list = list(sources.values())
        self._attr_extra_state_attributes = {}

    async def async_added_to_hass(self) -> None:
        """Entity has been added to hass."""
        self.backfill_state()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the query timer when the entity is removed."""
        if self._query_timer:
            self._query_timer.cancel()
            self._query_timer = None

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Return media player features that are supported."""
        if self._supports_volume:
            return SUPPORT_ONKYO
        return SUPPORT_ONKYO_WO_VOLUME

    @callback
    def _update_receiver(self, propname: str, value: Any) -> None:
        """Update a property in the receiver."""
        self._receiver.conn.update_property(self._zone, propname, value)

    @callback
    def _query_receiver(self, propname: str) -> None:
        """Cause the receiver to send an update about a property."""
        self._receiver.conn.query_property(self._zone, propname)

    async def async_turn_on(self) -> None:
        """Turn the media player on."""
        self._update_receiver("power", "on")

    async def async_turn_off(self) -> None:
        """Turn the media player off."""
        self._update_receiver("power", "standby")

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1.

        However full volume on the amp is usually far too loud so allow the user to
        specify the upper range with CONF_MAX_VOLUME. We change as per max_volume
        set by user. This means that if max volume is 80 then full volume in HA
        will give 80% volume on the receiver. Then we convert that to the correct
        scale for the receiver.
        """
        # HA_VOL * (MAX VOL / 100) * VOL_RESOLUTION
        self._update_receiver(
            "volume", int(volume * (self._max_volume / 100) * self._volume_resolution)
        )

    async def async_volume_up(self) -> None:
        """Increase volume by 1 step."""
        self._update_receiver("volume", "level-up")

    async def async_volume_down(self) -> None:
        """Decrease volume by 1 step."""
        self._update_receiver("volume", "level-down")

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute the volume."""
        self._update_receiver(
            "audio-muting" if self._zone == "main" else "muting",
            "on" if mute else "off",
        )

    async def async_select_source(self, source: str) -> None:
        """Select input source."""
        if self.source_list and source in self.source_list:
            source = self._reverse_mapping[source]
        self._update_receiver(
            "input-selector" if self._zone == "main" else "selector", source
        )

    async def async_select_output(self, hdmi_output: str) -> None:
        """Set hdmi-out."""
        self._update_receiver("hdmi-output-selector", hdmi_output)

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play radio station by preset number."""
        if self.source is not None:
            source = self._reverse_mapping[self.source]
            if media_type.lower() == "radio" and source in DEFAULT_PLAYABLE_SOURCES:
                self._update_receiver("preset", media_id)

    @callback
    def backfill_state(self) -> None:
        """Get the receiver to send all the info we care about.

        Usually run only on connect, as we can otherwise rely on the
        receiver to keep us informed of changes.
        """
        self._query_receiver("power")
        self._query_receiver("volume")
        self._query_receiver("preset")
        if self._zone == "main":
            self._query_receiver("hdmi-output-selector")
            self._query_receiver("audio-muting")
            self._query_receiver("input-selector")
            self._query_receiver("listening-mode")
            self._query_receiver("audio-information")
            self._query_receiver("video-information")
        else:
            self._query_receiver("muting")
            self._query_receiver("selector")

    @callback
    def process_update(self, update: tuple[str, str, Any]) -> None:
        """Store relevant updates so they can be queried later."""
        zone, command, value = update
        if zone != self._zone:
            return

        if command in ["system-power", "power"]:
            if value == "on":
                self._attr_state = MediaPlayerState.ON
            else:
                self._attr_state = MediaPlayerState.OFF
                self._attr_extra_state_attributes.pop(ATTR_AUDIO_INFORMATION, None)
                self._attr_extra_state_attributes.pop(ATTR_VIDEO_INFORMATION, None)
                self._attr_extra_state_attributes.pop(ATTR_PRESET, None)
                self._attr_extra_state_attributes.pop(ATTR_VIDEO_OUT, None)
        elif command in ["volume", "master-volume"] and value != "N/A":
            self._supports_volume = True
            # AMP_VOL / (VOL_RESOLUTION * (MAX_VOL / 100))
            self._attr_volume_level = value / (
                self._volume_resolution * self._max_volume / 100
            )
        elif command in ["muting", "audio-muting"]:
            self._attr_is_volume_muted = bool(value == "on")
        elif command in ["selector", "input-selector"]:
            self._parse_source(value)
            self._query_av_info_delayed()
        elif command == "hdmi-output-selector":
            self._attr_extra_state_attributes[ATTR_VIDEO_OUT] = ",".join(value)
        elif command == "preset":
            if self.source is not None and self.source.lower() == "radio":
                self._attr_extra_state_attributes[ATTR_PRESET] = value
            elif ATTR_PRESET in self._attr_extra_state_attributes:
                del self._attr_extra_state_attributes[ATTR_PRESET]
        elif command == "audio-information":
            self._supports_audio_info = True
            self._parse_audio_information(value)
        elif command == "video-information":
            self._supports_video_info = True
            self._parse_video_information(value)
        elif command == "fl-display-information":
            self._query_av_info_delayed()

        self.async_write_ha_state()

    @callback
    def _parse_source(self, source_raw: str | int | tuple[str]) -> None:
        # source is either a tuple of values or a single value,
        # so we convert to a tuple, when it is a single value.
        if isinstance(source_raw, str | int):
            source = (str(source_raw),)
        else:
            source = source_raw
        for value in source:
            if value in self._source_mapping:
                self._attr_source = self._source_mapping[value]
                return
        self._attr_source = "_".join(source)

    @callback
    def _parse_audio_information(
        self, audio_information: tuple[str] | Literal["N/A"]
    ) -> None:
        # If audio information is not available, N/A is returned,
        # so only update the audio information, when it is not N/A.
        if audio_information == "N/A":
            self._attr_extra_state_attributes.pop(ATTR_AUDIO_INFORMATION, None)
            return

        self._attr_extra_state_attributes[ATTR_AUDIO_INFORMATION] = {
            name: value
            for name, value in zip(
                AUDIO_INFORMATION_MAPPING, audio_information, strict=False
            )
            if len(value) > 0
        }

    @callback
    def _parse_video_information(
        self, video_information: tuple[str] | Literal["N/A"]
    ) -> None:
        # If video information is not available, N/A is returned,
        # so only update the video information, when it is not N/A.
        if video_information == "N/A":
            self._attr_extra_state_attributes.pop(ATTR_VIDEO_INFORMATION, None)
            return

        self._attr_extra_state_attributes[ATTR_VIDEO_INFORMATION] = {
            name: value
            for name, value in zip(
                VIDEO_INFORMATION_MAPPING, video_information, strict=False
            )
            if len(value) > 0
        }

    def _query_av_info_delayed(self) -> None:
        if self._zone == "main" and not self._query_timer:

            @callback
            def _query_av_info() -> None:
                if self._supports_audio_info:
                    self._query_receiver("audio-information")
                if self._supports_video_info:
                    self._query_receiver("video-information")
                self._query_timer = None

            self._query_timer = self.hass.loop.call_later(
                AUDIO_VIDEO_INFORMATION_UPDATE_WAIT_TIME, _query_av_info
            )
