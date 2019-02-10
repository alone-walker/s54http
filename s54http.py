#! /usr/bin/env python


import struct
import logging

from twisted.internet import reactor, protocol

from utils import (
        daemonize, parse_args, ssl_ctx_factory, init_logger,
)


logger = logging.getLogger(__name__)


config = {
        'daemon': False,
        'port': 6666,
        'ca': 'keys/ca.crt',
        'key': 'keys/server.key',
        'cert': 'keys/server.crt',
        'pidfile': 'socks.pid',
        'logfile': 'socks.log',
        'loglevel': 'INFO'
}


def verify(conn, x509, errno, errdepth, ok):
    if not ok:
        cn = x509.get_subject().commonName
        logger.error('client verify failed errno=%d cn=%s', errno, cn)
    return ok


class remote_protocol(protocol.Protocol):

    def connectionMade(self):
        logger.info(
                'connect success %s:%u',
                self.factory.host,
                self.factory.port
        )
        self.dispatcher.handleConnect(
                self.sock_id,
                0,
                sock=self
        )

    def dataReceived(self, data):
        self.dispatcher.handleRemote(self.sock_id, data)


class remote_factory(protocol.ClientFactory):

    def __init__(self, dispatcher, sock_id, host, port):
        self.protocol = remote_protocol
        self.dispatcher = dispatcher
        self.sock_id = sock_id
        self.host = host
        self.port = port

    def buildProtocol(self, addr):
        p = protocol.ClientFactory.buildProtocol(self, addr)
        p.dispatcher = self.dispatcher
        p.sock_id = self.sock_id
        p.factory = self
        return p

    def clientConnectionFailed(self, connector, reason):
        logger.error('connect failed[%s]', reason.getErrorMessage())
        self.dispatcher.handleConnect(self.sock_id, 1)

    def clientConnectionLost(self, connector, reason):
        logger.info('connetion closed[%s]', reason.getErrorMessage())
        self.dispatcher.handleClose(self.sock_id)


class socks_dispatcher:

    def __init__(self, transport):
        self.socks = {}
        self.factories = {}
        self.bufferes = {}
        self.connected = {}
        self.transport = transport

    def dispatchMessage(self, message, total_length):
        type, = struct.unpack('!B', message[4:5])
        logger.info(
                'receive message type=%u length=%u',
                type,
                total_length
        )
        assert type in (1, 3, 5)
        if 1 == type:
            self.connectRemote(message)
        elif 3 == type:
            self.sendRemote(message, total_length)
        elif 5 == type:
            self.closeRemote(message)

    def connectRemote(self, message):
        """
        type 1:
        +-----+------+----+------+------+
        | LEN | TYPE | ID | ADDR | PORT |
        +-----+------+----+------+------+
        |  4  |   1  |  8 |   4  |   2  |
        +-----+------+----+------+------+
        """
        sock_id, = struct.unpack('!Q', message[5:13])
        assert sock_id not in self.socks
        ip = struct.unpack('!BBBB', message[13:17])
        host = f'{ip[0]}.{ip[1]}.{ip[2]}.{ip[3]}'
        port, = struct.unpack('!H', message[17:19])
        logger.info('connect to %s:%d', host, port)
        self.factories[sock_id] = remote_factory(self, sock_id, host, port)
        self.connected[sock_id] = False
        self.bufferes[sock_id] = b''

    def handleConnect(self, sock_id, code, *, sock=None):
        """
        type 2:
        +-----+------+----+------+
        | LEN | TYPE | ID | CODE |
        +-----+------+----+------+
        |  4  |   1  |  8 |   1  |
        +-----+------+----+------+
        """
        if code == 0:
            self.socks[sock_id] = sock
            try:
                data = self.bufferes[sock_id]
            except KeyError:
                pass
            else:
                if len(data) > 0:
                    sock.transport.write(data)
                    self.bufferes[sock_id] = b''
        else:
            message = struct.pack(
                    '!IBQB',
                    14,
                    2,
                    sock_id,
                    code
            )
            self.transport.write(message)
            self.closeSock(sock_id)

    def sendRemote(self, message, total_length):
        """
        type 3:
        +-----+------+----+------+
        | LEN | TYPE | ID | DATA |
        +-----+------+----+------+
        |  4  |   1  |  8 |      |
        +-----+------+----+------+
        """
        sock_id, = struct.unpack('!Q', message[5:13])
        data_length = total_length - 13
        data, = struct.unpack(f'!{data_length}s', message[13:total_length])
        try:
            sock = self.socks[sock_id]
        except KeyError:
            try:
                factory = self.factories[sock_id]
            except KeyError:
                logger.error('receive unknown factory %u', sock_id)
            else:
                self.bufferes[sock_id] += data
                if not self.connected[sock_id]:
                    logger.info(
                            'connect to %s:%u',
                            factory.host,
                            factory.port
                    )
                    reactor.connectTCP(
                            factory.host,
                            factory.port,
                            factory
                    )
                    self.connected[sock_id] = True
        else:
            sock.transport.write(data)

    def handleRemote(self, sock_id, data):
        """
        type 4:
        +-----+------+----+------+
        | LEN | TYPE | ID | DATA |
        +-----+------+----+------+
        |  4  |   1  |  8 |      |
        +-----+------+----+------+
        """
        data_length = len(data)
        total_length = 13 + data_length
        message = struct.pack(
                f'!IBQ{data_length}s',
                total_length,
                4,
                sock_id,
                data
        )
        logger.info(
                'send message length=%d:%d',
                total_length,
                len(message)
        )
        self.transport.write(message)

    def closeSock(self, sock_id):
        try:
            sock = self.socks[sock_id]
            del self.socks[sock_id]
            del self.factories[sock_id]
            del self.connected[sock_id]
            del self.bufferes[sock_id]
        except KeyError:
            logger.error('close unknown sock %u', sock_id)
        else:
            try:
                sock.transport.loseConnection()
            except Exception:
                pass

    def closeRemote(self, message):
        """
        type 5:
        +-----+------+----+
        | LEN | TYPE | ID |
        +-----+------+----+
        |  4  |   1  |  8 |
        +-----+------+----+
        """
        sock_id, = struct.unpack('!Q', message[5:13])
        self.closeSock(sock_id)

    def handleClose(self, sock_id):
        """
        type 6:
        +-----+------+----+
        | LEN | TYPE | ID |
        +-----+------+----+
        |  4  |   1  |  8 |
        +-----+------+----+
        """
        message = struct.pack(
                '!IBQ',
                13,
                6,
                sock_id
        )
        self.transport.write(message)
        self.closeSock(sock_id)


class socks5_protocol(protocol.Protocol):

    def connectionMade(self):
        self.buffer = b''
        self.dispatcher = socks_dispatcher(self.transport)

    def connectionLost(self, reason=None):
        logger.info('client closed connection')

    def dataReceived(self, data):
        self.buffer += data
        if len(self.buffer) < 4:
            return
        length, = struct.unpack('!I', self.buffer[:4])
        if len(self.buffer) < length:
            return
        self.dispatcher.dispatchMessage(self.buffer, length)
        self.buffer = self.buffer[length:]


def start_server(config):
    port = config['port']
    ca, key, cert = config['ca'], config['key'], config['cert']
    factory = protocol.ServerFactory()
    factory.protocol = socks5_protocol
    ssl_ctx = ssl_ctx_factory(
            False,
            ca,
            key,
            cert,
            verify
    )
    reactor.listenSSL(port, factory, ssl_ctx)
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
