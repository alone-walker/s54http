#! /usr/bin/env python
# -*- coding: utf-8 -*-


import re
import struct
import logging
import weakref

from twisted.names import client, dns
from twisted.internet import reactor, protocol
from twisted.internet.error import CannotListenError

from s54http.utils import (
        SSLCtxFactory, Cache,
        daemonize, parse_args, init_logger,
)


logger = logging.getLogger(__name__)
_IP = re.compile(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}')
config = {
        'daemon': False,
        'port': 8080,
        'ca': 'keys/ca.crt',
        'key': 'keys/server.key',
        'cert': 'keys/server.crt',
        'pidfile': 's54http.pid',
        'logfile': 'server.log',
        'loglevel': 'INFO',
        'dns': '8.8.8.8:53',
}


class RemoteProtocol(protocol.Protocol):

    def connectionMade(self):
        self.proxy = self.factory.proxy
        self.proxy.connectOk(self.transport)

    def dataReceived(self, data):
        self.proxy.recvRemote(data)


class RemoteFactory(protocol.ClientFactory):

    protocol = RemoteProtocol

    def __init__(self, proxy):
        self.proxy = proxy

    def clientConnectionFailed(self, connector, reason):
        message = reason.getErrorMessage()
        self.proxy.connectErr(message)

    def clientConnectionLost(self, connector, reason):
        try:
            self.proxy.connectionClosed()
        except ReferenceError:
            pass


class SockProxy:

    def __init__(self, sock_id, dispatcher, host, port):
        self.sock_id = sock_id
        self.dispatcher = dispatcher
        self.remote_host = host
        self.remote_port = port
        self.resolver = dispatcher.resolver
        self.addr_cache = dispatcher.addr_cache
        self.buffer = b''
        self.has_connect = False
        self.remote_addr = None
        self.transport = None
        self.resolveHost(host)

    @property
    def isConnected(self):
        return self.transport is not None

    def connectRemote(self):
        factory = RemoteFactory(weakref.proxy(self))
        reactor.connectTCP(
                self.remote_addr,
                self.remote_port,
                factory
        )
        self.has_connect = True

    def resolveOk(self, records):
        answers = records[0]
        for answer in answers:
            if answer.type != dns.A:
                continue
            addr = answer.payload.dottedQuad()
            self.addr_cache[self.remote_host] = addr
            self.remote_addr = addr
            break
        else:
            self.resolveErr('no ipv4 address found')
            return
        if not self.has_connect and len(self.buffer) > 0:
            self.connectRemote()

    def resolveErr(self, reason=''):
        logger.error(
                'sock_id[%u] resolve host[%s] failed[%s]',
                self.sock_id,
                self.remote_host,
                reason
        )
        self.dispatcher.handleConnect(self.sock_id, 1)

    def resolveHost(self, host):
        if _IP.match(host):
            self.remote_addr = host
        else:
            try:
                self.remote_addr = self.addr_cache[host]
            except KeyError:
                # getHostByName can't be used here, it may return ipv6 address
                self.resolver.lookupAddress(
                        host
                ).addCallbacks(
                        self.resolveOk,
                        self.resolveErr
                )

    def connectOk(self, transport):
        transport.write(self.buffer)
        self.buffer = b''
        self.transport = transport

    def connectErr(self, message):
        logger.error(
                'sock_id[%u] connect %s:%u failed[%s]',
                self.sock_id,
                self.remote_host,
                self.remote_port,
                message
        )
        self.dispatcher.handleConnect(self.sock_id, 1)

    def sendRemote(self, data):
        if self.isConnected:
            self.transport.write(data)
            return
        self.buffer += data
        if not self.has_connect and self.remote_addr is not None:
            self.connectRemote()

    def recvRemote(self, data):
        self.dispatcher.handleRemote(self.sock_id, data)

    def connectionClosed(self):
        logger.info(
                'sock_id[%u] connection[%s:%u] closed',
                self.sock_id,
                self.remote_host,
                self.remote_port
        )
        self.dispatcher.handleClose(self.sock_id)


class SocksDispatcher:

    def __init__(self, p):
        self.socks = {}
        self.transport = p.transport
        self.resolver = p.factory.resolver
        self.addr_cache = p.factory.addr_cache

    def dispatchMessage(self, message):
        type, = struct.unpack('!B', message[4:5])
        if 1 == type:
            self.connectRemote(message)
        elif 3 == type:
            self.sendRemote(message)
        elif 5 == type:
            self.closeRemote(message)
        else:
            logger.error('receive unknown message type=%u', type)

    def connectRemote(self, message):
        """
        type 1:
        +-----+------+----+------+------+
        | LEN | TYPE | ID | HOST | PORT |
        +-----+------+----+------+------+
        |  4  |   1  |  4 |      |   2  |
        +-----+------+----+------+------+
        """
        sock_id, = struct.unpack('!I', message[5:9])
        host = message[9:-2].tobytes().decode('utf-8').strip()
        port, = struct.unpack('!H', message[-2:])
        logger.info(
                'sock_id[%u] connect %s:%u',
                sock_id,
                host,
                port
        )
        self.socks[sock_id] = SockProxy(
                sock_id,
                self,
                host,
                port,
        )

    def handleConnect(self, sock_id, code):
        """
        type 2:
        +-----+------+----+------+
        | LEN | TYPE | ID | CODE |
        +-----+------+----+------+
        |  4  |   1  |  4 |   1  |
        +-----+------+----+------+
        """
        if 0 == code:
            return
        self.closeSock(sock_id, abort=True)
        message = struct.pack(
                '!IBIB',
                10,
                2,
                sock_id,
                code
        )
        self.transport.write(message)

    def sendRemote(self, message):
        """
        type 3:
        +-----+------+----+------+
        | LEN | TYPE | ID | DATA |
        +-----+------+----+------+
        |  4  |   1  |  4 |      |
        +-----+------+----+------+
        """
        sock_id, = struct.unpack('!I', message[5:9])
        data = message[9:]
        try:
            sock = self.socks[sock_id]
        except KeyError:
            logger.error('sock_id[%u] receive data after closed', sock_id)
        else:
            sock.sendRemote(data)

    def handleRemote(self, sock_id, data):
        """
        type 4:
        +-----+------+----+------+
        | LEN | TYPE | ID | DATA |
        +-----+------+----+------+
        |  4  |   1  |  4 |      |
        +-----+------+----+------+
        """
        total_length = 9 + len(data)
        header = struct.pack(
                '!IBI',
                total_length,
                4,
                sock_id,
        )
        self.transport.writeSequence([header, data])

    def closeSock(self, sock_id, *, abort=False):
        try:
            sock = self.socks[sock_id]
        except KeyError:
            logger.error('sock_id[%u] closed again', sock_id)
        else:
            del self.socks[sock_id]
            transport = sock.transport
            if transport is None:
                return
            if abort:
                transport.abortConnection()
            else:
                transport.loseConnection()

    def closeRemote(self, message):
        """
        type 5:
        +-----+------+----+
        | LEN | TYPE | ID |
        +-----+------+----+
        |  4  |   1  |  4 |
        +-----+------+----+
        """
        sock_id, = struct.unpack('!I', message[5:9])
        logger.info('sock_id[%u] remote closed', sock_id)
        self.closeSock(sock_id, abort=True)

    def handleClose(self, sock_id):
        """
        type 6:
        +-----+------+----+
        | LEN | TYPE | ID |
        +-----+------+----+
        |  4  |   1  |  4 |
        +-----+------+----+
        """
        if sock_id not in self.socks:
            return
        logger.info('sock_id[%u] local closed', sock_id)
        self.closeSock(sock_id)
        message = struct.pack(
                '!IBI',
                9,
                6,
                sock_id
        )
        self.transport.write(message)

    def tunnelClosed(self):
        if not self.socks:
            return
        old_socks = self.socks
        self.socks = {}
        for sock in old_socks.values():
            transport = sock.transport
            if transport is None:
                continue
            transport.abortConnection()


class TunnelProtocol(protocol.Protocol):

    def connectionMade(self):
        self.transport.setTcpNoDelay(True)
        self.transport.setTcpKeepAlive(True)
        self.buffer = b''
        self.dispatcher = SocksDispatcher(self)

    def connectionLost(self, reason=None):
        logger.info('proxy closed connection')
        self.dispatcher.tunnelClosed()

    def dataReceived(self, data):
        self.buffer += data
        if len(self.buffer) < 4:
            return
        length, = struct.unpack('!I', self.buffer[:4])
        if len(self.buffer) < length:
            return
        message = memoryview(self.buffer)[:length]
        self.dispatcher.dispatchMessage(message)
        self.buffer = self.buffer[length:]


def create_resolver(config):
    conf = config['dns'].strip()
    if ':' in conf:
        addr, port = conf.split(':')
        port = int(port)
    else:
        addr = conf
        port = 53
    return client.createResolver(servers=[(addr, port)])


def start_server(config):
    port = config['port']
    ca, key, cert = config['ca'], config['key'], config['cert']
    factory = protocol.ServerFactory()
    factory.protocol = TunnelProtocol
    factory.resolver = create_resolver(config)
    factory.addr_cache = Cache()
    ssl_ctx = SSLCtxFactory(
            False,
            ca,
            key,
            cert
    )
    try:
        reactor.listenSSL(port, factory, ssl_ctx)
    except CannotListenError:
        raise RuntimeError(
                f"couldn't listen on :{port}, address already in use"
        )
    logger.info('server start running...')
    reactor.run()


def main():
    parse_args(config)
    init_logger(config, logger)
    if config['daemon']:
        pidfile = config['pidfile']
        logfile = config['logfile']
        daemonize(
            pidfile,
            stdout=logfile,
            stderr=logfile
        )
    start_server(config)


if __name__ == '__main__':
    main()
