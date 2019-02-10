#! /usr/bin/env python


import re
import struct
import logging

from twisted.names import client, dns
from twisted.internet import reactor, protocol

from utils import (
        daemonize, parse_args, ssl_ctx_factory, init_logger
)

config = {
        'daemon': False,
        'saddr': '',
        'sport': 8080,
        'port': 8080,
        'ca': 'keys/ca.crt',
        'key': 'keys/client.key',
        'cert': 'keys/client.crt',
        'pidfile': 'socks.pid',
        'logfile': 'socks.log',
        'loglevel': 'INFO'
}


logger = logging.getLogger(__name__)

_dns_server = client.createResolver(servers=[('127.0.0.1', 53)])
_IP = re.compile(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}')


def verify(conn, x509, errno, errdepth, ok):
    if not ok:
        cn = x509.get_subject().commonName
        logger.error(
                'server verify failed errno=%d cn=%s',
                errno,
                cn
        )
    return ok


class sock_remote_protocol(protocol.Protocol):

    def __init__(self):
        self.buffer = b''

    def connectionMade(self):
        self.dispatcher.transport = self.transport

    def dataReceived(self, data):
        self.buffer += data
        if len(self.buffer) < 4:
            return
        length, = struct.unpack('!I', self.buffer[:4])
        logger.info(
                'receive message length=%u:%u',
                length,
                len(self.buffer)
        )
        if len(self.buffer) < length:
            return
        self.dispatcher.dispatchMessage(self.buffer, length)
        self.buffer = self.buffer[length:]


class sock_remote_factory(protocol.ClientFactory):

    def __init__(self, dispatcher):
        self.protocol = sock_remote_protocol
        self.dispatcher = dispatcher

    def buildProtocol(self, addr):
        p = protocol.ClientFactory.buildProtocol(self, addr)
        p.dispatcher = self.dispatcher
        return p

    def clientConnectionFailed(self, connector, reason):
        message = reason.getErrorMessage()
        logger.error('connect server failed[%s]', message)
        raise RuntimeError(f'connect server failed[{message}]')

    def clientConnectionLost(self, connector, reason):
        logger.info(
                'connetion closed: %s',
                reason.getErrorMessage()
        )


class socks_dispatcher:

    def __init__(self, remote_addr, remote_port, ssl_ctx):
        remote_factory = sock_remote_factory(self)
        reactor.connectSSL(
                remote_addr,
                remote_port,
                remote_factory,
                ssl_ctx
        )
        self.socks = {}

    def dispatchMessage(self, message, total_length):
        type, = struct.unpack('!B', message[4:5])
        logger.info(
                'receive message type=%u, length=%u',
                type,
                total_length
        )
        assert type in (2, 4, 6)
        if 2 == type:
            self.handleConnect(message)
        elif 4 == type:
            self.handleRemote(message, total_length)
        elif 6 == type:
            self.handleClose(message)

    def connectRemote(self, sock, remote_addr, remote_port):
        """
        type 1:
        +-----+------+----+------+------+
        | LEN | TYPE | ID | ADDR | PORT |
        +-----+------+----+------+------+
        |  4  |   1  |  8 |   4  |   2  |
        +-----+------+----+------+------+
        """
        sock_id = id(sock)
        self.socks[sock_id] = sock
        ip = [int(item) for item in remote_addr.split('.')]
        message = struct.pack(
                '!IBQBBBBH',
                19,
                1,
                sock_id,
                ip[0],
                ip[1],
                ip[2],
                ip[3],
                remote_port
        )
        self.transport.write(message)

    def handleConnect(self, message):
        """
        type 2:
        +-----+------+----+------+
        | LEN | TYPE | ID | CODE |
        +-----+------+----+------+
        |  4  |   1  |  8 |   1  |
        +-----+------+----+------+
        """
        sock_id, code = struct.unpack('!QB', message[5:14])
        try:
            sock = self.socks[sock_id]
        except KeyError:
            logger.error('receive from unknown sock %u', sock_id)
        else:
            if 0 != code:
                sock.remoteConnectionFailed()
                del self.socks[sock_id]

    def sendRemote(self, sock, data):
        """
        type 3:
        +-----+------+----+------+
        | LEN | TYPE | ID | DATA |
        +-----+------+----+------+
        |  4  |   1  |  8 |      |
        +-----+------+----+------+
        """
        sock_id = id(sock)
        data_length = len(data)
        total_length = 13 + data_length
        message = struct.pack(
                f'!IBQ{data_length}s',
                total_length,
                3,
                sock_id,
                data
        )
        logger.info(
                'send message length=%d:%u',
                total_length,
                len(message)
        )
        self.transport.write(message)

    def handleRemote(self, message, total_length):
        """
        type 4:
        +-----+------+----+------+
        | LEN | TYPE | ID | DATA |
        +-----+------+----+------+
        |  4  |   1  |  8 |      |
        +-----+------+----+------+
        """
        data_length = total_length - 13
        sock_id, data = struct.unpack(
                f'!Q{data_length}s',
                message[5:total_length]
        )
        try:
            sock = self.socks[sock_id]
        except KeyError:
            pass
        else:
            sock.transport.write(data)

    def closeRemote(self, sock):
        """
        type 5:
        +-----+------+----+
        | LEN | TYPE | ID |
        +-----+------+----+
        |  4  |   1  |  8 |
        +-----+------+----+
        """
        sock_id = id(sock)
        message = struct.pack(
                '!IBQ',
                13,
                5,
                sock_id
        )
        self.transport.write(message)
        try:
            del self.socks[sock_id]
        except KeyError:
            logger.error('send to unknown sock %d', sock_id)

    def handleClose(self, message):
        """
        type 6:
        +-----+------+----+
        | LEN | TYPE | ID |
        +-----+------+----+
        |  4  |   1  |  8 |
        +-----+------+----+
        """
        sock_id, = struct.unpack('!Q', message[5:13])
        try:
            sock = self.socks[sock_id]
        except KeyError:
            logger.error('receive from unknown sock %u', sock_id)
        else:
            sock.transport.loseConnection()
            del self.socks[sock_id]


class sock_local_protocol(protocol.Protocol):

    def __init__(self):
        self.remote_addr = None
        self.remote_port = None
        self.buffer = None
        self.state = None
        self.dispatcher = None

    def connectionMade(self):
        self.state = 'waitHello'
        self.buffer = b''

    def connectionLost(self, reason=None):
        logger.info('local connection closed')
        self.dispatcher.closeRemote(self)

    def dataReceived(self, data):
        method = getattr(self, self.state)
        method(data)

    def waitHello(self, data):
        self.buffer += data
        if len(self.buffer) < 2:
            return
        version, nmethods = struct.unpack('!BB', self.buffer[:2])
        logger.info('version=%d, nmethods=%d', version, nmethods)
        if version != 5:
            logger.error('unsupported version %u', version)
            self.sendHelloReply(0xFF)
            self.transport.loseConnection()
            return
        if nmethods < 1:
            logger.error('no methods found')
            self.sendHelloReply(0xFF)
            self.transport.loseConnection()
            return
        if len(self.buffer) < nmethods + 2:
            return
        for method in self.buffer[2:2+nmethods]:
            if method == 0:
                self.buffer = b''
                self.state = 'waitConnectRemote'
                logger.info('state: waitConnectRemote')
                self.sendHelloReply(0)
                return
        self.sendHelloReply(0xFF)
        self.transport.loseConnection()

    def sendHelloReply(self, rep):
        response = struct.pack('!BB', 5, rep)
        self.transport.write(response)

    def waitConnectRemote(self, data):
        self.buffer += data
        if len(self.buffer) < 4:
            return
        version, command, reserved, atyp = struct.unpack(
                '!BBBB',
                self.buffer[:4]
        )
        if version != 5:
            logger.error('unsupported version %u', version)
            self.transport.loseConnection()
            return
        if reserved != 0:
            logger.error('reserved value not 0')
            self.transport.loseConnection()
            return
        if command != 1:
            logger.error('unsupported command %u', command)
            self.sendConnectReply(7)
            self.transport.loseConnection()
            return
        if atyp not in (1, 3):
            logger.error('unsupported atyp %u', atyp)
            self.sendConnectReply(8)
            self.transport.loseConnection()
            return
        if atyp == 1:
            if len(self.buffer) < 10:
                return
            b1, b2, b3, b4 = struct.unpack('!BBBB', self.buffer[4:8])
            host = '{}.{}.{}.{}'.format(b1, b2, b3, b4)
            port, = struct.unpack('!H', self.buffer[8:10])
            self.connectRemote(host, port)
            return
        elif atyp == 3:
            if len(self.buffer) < 5:
                return
            length, = struct.unpack('!B', self.buffer[4:5])
            if len(self.buffer) < 5 + length + 2:
                return
            host = self.buffer[5:5+length].decode('utf-8').strip()
            port, = struct.unpack('!H', self.buffer[5+length:7+length])
            self.buffer = b''

            if _IP.match(host):
                self.connectRemote(host, port)
                return

            d = _dns_server.lookupAddress(host)

            def resolve_ok(records, host, port):
                answers, *_ = records
                for answer in answers:
                    if answer.type != dns.A:
                        continue
                    addr = answer.payload.dottedQuad()
                    self.connectRemote(addr, port)
                    break
                else:
                    logger.error('no ip4 address found[%s]', host)
                    self.sendConnectReply(5)
                    self.transport.loseConnection()

            d.addCallback(resolve_ok, host, port)

            def resolve_err(res):
                logger.error('resolve host failed[%s]', host)
                self.sendConnectReply(5)
                self.transport.loseConnection()

            d.addErrback(resolve_err)
            self.state = 'waitNameResolve'
            logger.info('state: waitNameResolve')
            return

    def sendConnectReply(self, rep):
        logger.info('reply code %u', rep)
        message = struct.pack(
                '!BBBBBBBBH',
                5,
                rep,
                0,
                1,
                0,
                0,
                0,
                0,
                0
        )
        self.transport.write(message)

    def waitNameResolve(self, data):
        logger.error('receive data when waitNameResolve')

    def connectRemote(self, host, port):
        self.remote_addr = host
        self.remote_port = port
        logger.info('connect to %s:%d', host, port)
        self.dispatcher.connectRemote(self, host, port)
        self.buffer = b''
        self.state = 'sendRemote'
        logger.info('state: sendRemote')
        self.sendConnectReply(0)

    def remoteConnectionFailed(self):
        logger.error(
                'connect failed %s:%d',
                self.remote_addr,
                self.remote_port
        )
        self.transport.loseConnection()

    def sendRemote(self, data):
        logger.info(
                'send data %s:%u',
                self.remote_addr,
                self.remote_port
        )
        self.dispatcher.sendRemote(self, data)


class sock_local_factory(protocol.ServerFactory):

    def __init__(self, dispatcher):
        self.protocol = sock_local_protocol
        self.dispatcher = dispatcher

    def buildProtocol(self, addr):
        p = protocol.ServerFactory.buildProtocol(self, addr)
        p.dispatcher = self.dispatcher
        return p


def start_server(config):
    local_port = config['port']
    remote_addr, remote_port = config['saddr'], config['sport']
    ca, key, cert = config['ca'], config['key'], config['cert']
    ssl_ctx = ssl_ctx_factory(
            True,
            ca,
            key,
            cert,
            verify
    )
    dispatcher = socks_dispatcher(remote_addr, remote_port, ssl_ctx)
    local_factory = sock_local_factory(dispatcher)
    reactor.listenTCP(
            local_port,
            local_factory,
            interface='127.0.0.1'
    )
    reactor.run()


def main():
    parse_args(config)
    if not config['saddr']:
        raise RuntimeError('no server address found')
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
