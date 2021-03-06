# Beah - Test harness. Part of Beaker project.
#
# Copyright (C) 2009 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

from beah.misc.jsonenv import json
from twisted.protocols import basic
from twisted.internet import reactor
from twisted.internet.error import DNSLookupError, NoRouteError, CannotListenError
import exceptions
import logging

USE_DEFAULT = object()

class JSONProtocol(basic.LineReceiver):

    """Protocol to send and receive new-line delimited JSON objects"""

    delimiter = "\n"
    MAX_LENGTH = 0x100000 # 1 MiB

    ########################################
    # METHODS TO REIMPLEMENT:
    ########################################
    def proc_input(self, cmd):
        """Process object received in a message"""
        raise exceptions.NotImplementedError

    def lose_item(self, data):
        raise

    ########################################
    # OWN METHODS:
    ########################################
    def send_cmd(self, obj):
        """Send an object as a message"""
        self.transport.write(self.format(obj))

    def format(obj):
        """Create a message from an object"""
        return json.dumps(obj) + "\n"
    # AVOID DECORATORS - KEEP PYTHON 2.2 COMPATIBILITY
    format = staticmethod(format)

    ########################################
    # INHERITED METHODS:
    ########################################
    def lineReceived(self, data):
        try:
            obj = json.loads(data)
        except:
            obj = self.lose_item(data)
        self.proc_input(obj)

import os
from twisted.internet import abstract
from twisted.internet.error import ConnectionDone
class OsFifo(abstract.FileDescriptor):
    def __init__(self, reactor, fifo_name, protocol, keep_alive=False):
        abstract.FileDescriptor.__init__(self, reactor)
        self.keep_alive = keep_alive
        self.fd = os.open(fifo_name, os.O_NONBLOCK | os.O_RDONLY)
        self.protocol = protocol
        self.protocol.makeConnection(self)

    def doRead(self):
        data = os.read(self.fd, 4096)
        if data:
            self.protocol.dataReceived(data)
        elif not self.keep_alive:
            self.connectionLost(ConnectionDone)

    def connectionLost(self, reason=ConnectionDone):
        self.stopReading()
        os.close(self.fd)
        self.protocol.connectionLost(reason)

    def fileno(self):
        return self.fd

def serveAnyChild(cls):
    """
    Modify cls, a subclass of twisted.web.xmlrpc.Proxy, to handle any child's
    requests.
    """
    def getChild(self, path, request):
        """Will return self for any child request."""
        return self
    cls.getChild = getChild

def serveAnyRequest(cls, by, base=USE_DEFAULT):
    """
    Modify cls, a subclass of twisted.web.xmlrpc.Proxy, to handle requests for
    any path.

    Parameters:
    by: function handling RPC
    base: how to handle baseclass calls
    - None: do not pass calls to baseclass
    - USE_DEFAULT: use first baseclass to handle RPC
    - class: use given baseclass to handle RPC

    """
    if base is USE_DEFAULT:
        base = cls.__base__

    if base is None:
        def _getFunction(self, functionPath):
            """Will return handler for all requests."""
            def tempf(*args):
                return getattr(self, by)(functionPath, *args)
            return tempf
    else:
        if not issubclass(cls, base):
            raise exceptions.RuntimeError('%s is not a baseclass of %s' % (base, cls))
        getter = getattr(base, 'lookupProcedure', None) or getattr(base, '_getFunction')
        def _getFunction(self, functionPath):
            """Will return handler for all unhandled requests."""
            try:
                return getter(self, functionPath)
            except:
                def tempf(*args):
                    return getattr(self, by)(functionPath, *args)
                return tempf
    cls._getFunction = _getFunction
    cls.lookupProcedure = _getFunction


def make_logging_proxy(proxy):
    """
    Override proxy's callRemote method to log results.

    Parameters:
    - proxy is an instance of Proxy class to be "decorated".

    Return:
    - will return proxy

    Customize by overriding proxy's logging_print, logging_log or
    logging_log_err.

    Function will refuse to add logging trait twice.
    """

    if getattr(proxy, 'trait_logging_proxy', False):
        return proxy

    proxy.trait_logging_proxy = True

    def logging_print(fmt, *args, **kwargs):
        print fmt % args, kwargs
    proxy.logging_print = logging_print

    def logging_log(result, message, method, args, kwargs):
        proxy.logging_print("XML-RPC call %s %s: %s", method, message, result)
        proxy.logging_print("original call: %s(*%r, **%r)", method, args, kwargs)
        return result
    proxy.logging_log = logging_log
    proxy.logging_log_err = logging_log

    proxy_callRemote = proxy.callRemote
    def callRemote(method, *args, **kwargs):
        return proxy_callRemote(method, *args, **kwargs) \
                .addCallbacks(proxy.logging_log, proxy.logging_log_err,
                        callbackArgs=["returned", method, args, kwargs],
                        errbackArgs=["failed", method, args, kwargs])
    proxy.callRemote = callRemote

    return proxy

def twisted_logging(logger, level=None):
    from twisted.python.log import PythonLoggingObserver
    try:
        if level is not None:
            logger.getChild('twisted').setLevel(level)
        observer = PythonLoggingObserver(logger.name + '.twisted')
        observer.start()
    except:
        logger.critical("Could not add twisted observer!", exc_info=True)

def reason2rc(end_reason):
    sig = end_reason.value.signal
    if sig:
        return 128+sig
    else:
        return end_reason.value.exitCode

# To support testing in IPv6 only, IPv4 only and mixed IPv4/6 environments

def connect_loopback(port, factory, ipv6_disabled=False):
    # We prefer connecting over IPv6. However, if the server is not
    # listening on IPv6 (e.g. because the operating system or Twisted
    # doesn't support IPv6, or has been disabled), 
    # we fallback to connecting over IPv4.
    def connect_tcp(interface):
        return reactor.connectTCP(interface, port, factory)

    if not ipv6_disabled:
        try:
            return connect_tcp('::1')
        except (NoRouteError, DNSLookupError):
            return connect_tcp('127.0.0.1')
    else:
        return connect_tcp('127.0.0.1')

def listen_all_tcp(port, listener, ipv6_disabled=False):

    def listen_tcp(interface):
        return reactor.listenTCP(port, listener, interface=interface)

    # We can listen on '::' (in6addr_any) and the kernel will give us
    # connections that came in over IPv6 or IPv4. But if IPv6 is disabled in
    # the kernel we need to listen on '' (INADDR_ANY) instead.
    if not ipv6_disabled:
        try:
            return listen_tcp('::')
        except CannotListenError:
            return listen_tcp('')
    else:
        return listen_tcp('')

def listen_loopback_tcp(port, listener, ipv6_disabled=False):

    def listen_tcp(interface):
        return reactor.listenTCP(port, listener, interface=interface)

    # We need to listen on both IPv4 and IPv6 loopback addresses. Either one 
    # can fail, but if both fails we have hit a real error.
    listening6 = None
    if not ipv6_disabled:
        try:
            listening6 = listen_tcp('::1')
        except CannotListenError:
            pass
    try:
        listening4 = listen_tcp('127.0.0.1')
    except CannotListenError:
        if not listening6:
            raise
    return listening6 or listening4
