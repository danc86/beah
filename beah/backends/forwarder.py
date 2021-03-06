# -*- test-case-name: beah.backends.test.test_forwarder -*-

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

from beah.wires.internals.twbackend import start_backend, log_handler
from beah.core.backends import ExtBackend
from beah.core import command, event
from beah.core.constants import ECHO
from beah.misc.log_this import log_this
from beah.misc import localhost, make_class_verbose, test_loop, \
    test_loop_port, TEST_LOOP, parse_bool
from beah import config

from twisted.internet.defer import Deferred
from twisted.internet import reactor
import logging
import exceptions
import sys
import os
from optparse import OptionParser

log = logging.getLogger('backend')

class ForwarderBackend(ExtBackend):

    RETRY_IN = 10

    def __init__(self):
        self.__remotes = {}
        ExtBackend.__init__(self)
        self.def_port = config.get_conf('beah-backend').get('DEFAULT', 'PORT')

    _VERBOSE = ('stop_remote', 'remote_backend', 'remote_call',
            'proc_evt_variable_get', 'handle_evt_variable_get')

    def stop_remote(self, dest):
        dest_s = '%s:%s' % dest
        rb = self.__remotes.get(dest_s, None)
        if rb:
            if not rb.idle():
                log.warning("Can not stop RemoteBackend for %s, which is not idle!", dest_s)
            else:
                log.info("Closing RemoteBackend for %s.", dest_s)
                rb.proc_evt_bye(event.Event('bye', message='Idle timeout.'))
                del self.__remotes[dest_s]
        else:
            log.warning("No RemoteBackend for %s", dest_s)

    def remote_backend(self, dest):
        dest_s = '%s:%s' % dest
        rb = self.__remotes.get(dest_s, None)
        if rb is None:
            rb = _RemoteBackend(self, dest)
            start_backend(rb, host=dest[0], port=dest[1],
                    byef=lambda evt: None)
            self.__remotes[dest_s] = rb
        return rb

    def remote_call(self, cmd, host, port=None):
        if port is None:
            port = self.def_port
        port = int(port)
        # create a backend if necessary
        rem = self.remote_backend((host, port))
        return rem.send_cmd(cmd)

    def handle_evt_variable_get(self, result, evt):
        type, pass_, remote_be, cmd_id, rest = result
        if type == 'timeout':
            log.error("Timeout.")
            remote_be.done(cmd_id)
            reactor.callLater(self.RETRY_IN, self.proc_evt_variable_get, evt)
            return
        if type == 'forward_response' and pass_ and rest.command() == 'variable_value':
            dest = rest.arg('dest')
            if TEST_LOOP(dest):
                rest.args()['dest'] = dest.lower()
            self.controller.proc_cmd(self, rest)
            remote_be.done(cmd_id)
            return
        if type != 'echo':
            log.warning("Unexpected answer. Waiting more... (diagnostic=%r)", result)
            remote_be.more(cmd_id).addCallback(self.handle_evt_variable_get, evt)
            return
        log.error("Remote end send echo without any answer! (diagnostic=%r)", result)
        remote_be.done(cmd_id)
        reactor.callLater(self.RETRY_IN, self.proc_evt_variable_get, evt)

    def proc_evt_variable_get(self, evt):
        host = evt.arg('dest')
        if localhost(host):
            return
        # FIXME? remote Controller could listen on another port:
        port = self.def_port
        # loop for testing:
        if test_loop(host):
            # test.loop for testing forwarder on single machine.
            # could be used with port number test.loop:11432
            # Modify the dest field to avoid inifinite loop:
            evt.args()['dest'] = host.upper()
            port = int(test_loop_port(host) or port)
        cmd = command.forward(event=evt)
        d = self.remote_call(cmd, host, port)
        if d:
            d.addCallback(self.handle_evt_variable_get, evt)

class _RemoteBackend(ExtBackend):

    """
    Backend interacting with remote Controller.
    """

    ALLOWED_COMMANDS = ['variable_value']
    TIMEOUT = 5
    IDLE_TIMEOUT = 10

    _CONNECTED=object()
    _NEW=object()
    _IDLE=object()

    def __init__(self, caller, dest, queue=None, pending=None):
        self.__caller = caller
        self.__dest = dest
        self.__idle = None
        self.__queue = queue or []
        self.__pending = pending or {}
        self.__status = self._NEW
        ExtBackend.__init__(self)

    _VERBOSE = ('send_cmd', 'done', 'more', 'set_controller',
            'proc_evt_forward_response', 'proc_evt_echo')

    def idle(self):
        return not self.__queue

    def dest(self):
        return self.__dest

    def send_cmd(self, cmd):
        for c, _ in self.__pending.values():
            if cmd.same_as(c):
                return None
        d = Deferred()
        cid = cmd.id()
        self.__queue.append(cid)
        if self.__idle:
            if self.__idle.active():
                self.__idle.cancel()
            self.__idle = None
        self.__pending[cid] = (cmd, d)
        if self.__status is self._CONNECTED:
            self.controller.proc_cmd(self, cmd)
            reactor.callLater(self.TIMEOUT, self.timeout, cid)
        return d

    def __replaced(self, cmd_id, d):
        c, dd = self.__pending.get(cmd_id, (None, None))
        if c is None:
            return None
        self.__pending[cmd_id] = (c, d)
        return dd

    def timeout(self, cmd_id):
        d = self.__replaced(cmd_id, None)
        if d:
            d.callback(('timeout', False, self, cmd_id, None))

    def more(self, cmd_id):
        d = Deferred()
        if self.__replaced(cmd_id, d):
            raise exceptions.RuntimeError('more should be called from callbacks only!')
        return d

    def done(self, cmd_id):
        if self.__pending[cmd_id][1]:
            raise exceptions.RuntimeError('done should be called from callbacks only!')
        del self.__pending[cmd_id]
        self.__queue = list([cid for cid in self.__queue if cid != cmd_id])
        if self.idle():
            self.__idle = reactor.callLater(self.IDLE_TIMEOUT, self.__caller.stop_remote, self.__dest)

    def set_controller(self, controller=None):
        ExtBackend.set_controller(self, controller)
        if controller:
            self.__status = self._CONNECTED
            # FIXME: implement proper output filterring instead of no_output:
            controller.proc_cmd(self, command.no_output())
            for cid in self.__queue:
                cmd, d = self.__pending[cid]
                controller.proc_cmd(self, cmd)
                reactor.callLater(self.TIMEOUT, self.timeout, cid)
        else:
            self.__status = self._IDLE

    def proc_evt_forward_response(self, evt):
        cmd = command.command(evt.arg('command'))
        cid = evt.arg('forward_id')
        d = self.__replaced(cid, None)
        if d:
            cmd_ok = cmd.command() in self.ALLOWED_COMMANDS
            if not cmd_ok:
                log.warning("Command %s not allowed! cmd=%r", cmd.command(), cmd)
            d.callback(('forward_response', cmd_ok, self, cid, cmd))

    def proc_evt_echo(self, evt):
        cid = evt.arg('cmd_id')
        d = self.__replaced(cid, None)
        if d:
            d.callback(('echo', evt.arg('rc') == ECHO.OK, self, cid, evt))

def start_forwarder_backend():
    if parse_bool(config.get_conf('beah-backend').get('DEFAULT', 'DEVEL')):
        print_this = log_this(lambda s: log.debug(s), log_on=True)
        make_class_verbose(ForwarderBackend, print_this)
        make_class_verbose(_RemoteBackend, print_this)
    backend = ForwarderBackend()
    # Start a default TCP client:
    start_backend(backend)
    return backend

def main(args=None):
    config.backend_conf(
            defaults={'NAME':'beah_forwarder_backend'},
            overrides=config.backend_opts(args=args))
    log_handler()
    start_forwarder_backend()
    reactor.run()

