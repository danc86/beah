# Beah - Test harness. Part of Beaker project.
#
# Copyright (C) 2009 Marian Csontos <mcsontos@redhat.com>
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

"""
Backend translating beah events to XML-RPCs understood by beaker's Lab
Controller.
"""

# Beaker Backend should invoke these XML-RPC:
#  1. recipes.to_xml(recipe_id)
#     recipes.system_xml(fqdn)
#  2. parse XML
#  3. recipes.tasks.Start(task_id, kill_time)
#  *. recipes.tasks.Result(task_id, result_type, path, score, summary)
#     - result_type: pass_|warn|fail|panic
#  4. recipes.tasks.Stop(task_id, stop_type, msg)
#     - stop_type: stop|abort|cancel

import sys
import os
import os.path
import re
import traceback
import exceptions
import base64
import hashlib
import simplejson as json
import logging
from xml.dom import minidom

from twisted.web.xmlrpc import Proxy
from twisted.internet import reactor

from beah import config
from beah.core import command, event, addict
from beah.core.backends import SerializingBackend
from beah.core.constants import ECHO, RC, LOG_LEVEL
from beah.misc import format_exc, dict_update, log_flush, writers, runtimes, \
        make_class_verbose, is_class_verbose, pre_open, digests
from beah.misc.log_this import log_this
import beah.system
# FIXME: using rpm's, yum - too much Fedora centric(?)
from beah.system.dist_fedora import RPMInstaller
from beah.system.os_linux import ShExecutable
from beah.wires.internals.repeatingproxy import RepeatingProxy
from beah.wires.internals.twbackend import start_backend, log_handler
from beah.wires.internals.twmisc import make_logging_proxy

log = logging.getLogger('backend')

class RHTSTask(ShExecutable):

    def __init__(self, env_, repos, repof):
        self.__env = env_
        self.__repos = repos
        self.__repof = repof
        ShExecutable.__init__(self)

    def content(self):
        self.write_line("""
# This wrapper should prevent rhts-test-runner.sh to install rpm from default
# rhts repository and use repositories defined in recipe
#mkdir -p $TESTPATH
if [[ -n "$BEAH_MAKE_REPOS" ]]; then
cat >/etc/yum.repos.d/beaker-tests.repo <<REPO_END
%s
REPO_END
fi
if rpm -q "$TESTRPMNAME"; then
    beahsh INFO -H wrapper "$TESTRPMNAME is already installed."
else
    # This will happen only on first run so it is safe to override
    # watchdog as it will be reset by task...
    beahsh INFO -H wrapper "Installing the task $TESTRPMNAME"
    beahsh extend_watchdog 20m
    yum -y --disablerepo=* --enablerepo=beaker-* install "$TESTRPMNAME"
    for iteration in $(seq 6); do
        if ! rpm -q "$TESTRPMNAME"; then
            beahsh INFO -H wrapper "$TESTRPMNAME not installed. Will retry in 300s..."
            sleep 300
            beahsh extend_watchdog 20m
            beahsh INFO -H wrapper "Cleaning metadata and trying to get the task again..."
            yum -y clean metadata
            yum -y --disablerepo=* --enablerepo=beaker-* install "$TESTRPMNAME"
        else
            break
        fi
    done
    if ! rpm -q "$TESTRPMNAME"; then
        beahsh fail -H wrapper "$TESTRPMNAME was not installed."
        exit
    fi
fi
# This is a workaround for /distribution/reservesys test:
touch /mnt/tests/runtests.sh
chmod a+x /mnt/tests/runtests.sh
beahsh INFO -H wrapper "Running the task..."
exec beah-rhts-task
#%s -m beah.tasks.rhts_xmlrpc
""" % (self.__repof,
                sys.executable))


def mk_rhts_task(env_, repos, repof):
    # FIXME: proper RHTS launcher shold go here.
    # create a script to: check, install and run a test
    # should task have an "envelope" - e.g. binary to run...
    e = RHTSTask(env_, repos, repof)
    e.make()
    return e.executable

def normalize_rpm_name(rpm_name):
    if rpm_name[-4:] != '.rpm':
        return rpm_name
    return rpm_name[:-4]

def xml_attr(node, key, default=None):
    try:
        return str(node.attributes[key].value)
    except:
        return default

def xml_get_nodes(node, tag):
    return [n for n in node.childNodes if n.nodeName == tag]

def xml_first_node(node, tag):
    for n in node.childNodes:
        if n.nodeName == tag:
            return n
    return None

def role_empty(role):
    return not role or role == 'None'

def proc_role(systems, role_node):
    for system_node in xml_get_nodes(role_node, 'system'):
        system = xml_attr(system_node, 'value')
        if system not in systems:
            systems.append(system)

def proc_roles(roles, roles_node):
    for role_node in xml_get_nodes(roles_node, 'role'):
        role = 'RECIPE_MEMBERS'
        systems = roles.setdefault(role, [])
        proc_role(systems, role_node)
        role = xml_attr(role_node, 'value')
        if role_empty(role) or role == 'RECIPE_MEMBERS':
            continue
        systems = roles.setdefault(role, [])
        proc_role(systems, role_node)

def parse_recipe_xml(input_xml, hostname):

    task_env = {}
    root = minidom.parseString(input_xml)
    for er in root.getElementsByTagName('recipe'):
        system = xml_attr(er, 'system')
        if system == hostname:
            task_env['RECIPETYPE'] = 'machine'
            break
    else:
        for er in root.getElementsByTagName('guestrecipe'):
            system = xml_attr(er, 'system')
            if system == hostname:
                task_env['RECIPETYPE'] = 'guest'
                break
        else:
            log.info("parse_recipe_xml: No recipe for %s." % hostname)
            return None

    rs = xml_attr(er, 'status')
    if rs not in ['Running', 'Waiting']:
        log.info("parse_recipe_xml: This recipe has finished.")
        return None

    variant = xml_attr(er, 'variant', '')
    if variant == 'None':
        variant = ''
    dict_update(task_env,
            ARCH=xml_attr(er, 'arch'),
            RECIPEID=xml_attr(er, 'id'),
            JOBID=xml_attr(er, 'job_id'),
            RECIPESETID=xml_attr(er, 'recipe_set_id'),
            DISTRO=xml_attr(er, 'distro', ''),
            FAMILY=xml_attr(er, 'family', ''),
            VARIANT=variant,
            HOSTNAME=hostname)

    # The following is necessary for Virtual Workflows:
    GUEST_ATTRS = ('system', 'mac_address', 'location', 'guestargs', 'guestname')
    task_env['GUESTS'] = '|'.join([
        ';'.join([xml_attr(gr, a, '') for a in GUEST_ATTRS])
            for gr in xml_get_nodes(er, 'guestrecipe')])

    for job in root.getElementsByTagName('job'):
        submitter = xml_attr(job, 'owner')
        if submitter:
            task_env['SUBMITTER'] = submitter
        break

    # FIXME: This will eventually need to be replaced by sth RPM independent...
    repos = []
    repof = ''
    for r in xml_get_nodes(xml_first_node(er, 'repos'), 'repo'):
        name = xml_attr(r, 'name')
        repos.append(name)
        repof += "[%s]\nname=beaker provided '%s' repo\nbaseurl=%s\nenabled=1\ngpgcheck=0\n\n" \
                % (name, name, xml_attr(r, 'url'))
    task_env['BEAKER_REPOS']=':'.join(repos)

    task_env['RECIPE_ROLE'] = xml_attr(er, 'role', '')
    roles = {}
    for roles_node in xml_get_nodes(er, 'roles'):
        proc_roles(roles, roles_node)
        break

    test_order = 0

    for task in xml_get_nodes(er, 'task'):

        to = xml_attr(task, 'testorder')
        if to is not None:
            test_order = int(to)
        else:
            test_order += 1

        ts = xml_attr(task, 'status')

        if ts not in ['Waiting', 'Running']:
            log.debug("task id: %r status: %r", xml_attr(task, 'id'), ts)
            continue

        task_id = xml_attr(task, 'id')
        task_name = xml_attr(task, 'name')
        dict_update(task_env,
                TASKID=str(task_id),
                RECIPETESTID=str(task_id),
                TESTID=str(task_id),
                TASKNAME=task_name,
                ROLE=xml_attr(task, 'role', ''))

        # FIXME: Anything else to save?

        for p in task.getElementsByTagName('param'):
            task_env[xml_attr(p, 'name')]=xml_attr(p, 'value')

        for roles_node in xml_get_nodes(task, 'roles'):
            proc_roles(roles, roles_node)
            break

        for role_str in roles.keys():
            task_env[role_str]=' '.join(roles[role_str])

        ewd = xml_attr(task, 'avg_time')
        task_env['KILLTIME'] = ewd

        executable = ''
        args = []
        while not executable:

            rpm_tags = task.getElementsByTagName('rpm')
            log.debug("parse_recipe_xml: rpm tag: %s", rpm_tags)
            if rpm_tags:
                rpm_name = xml_attr(rpm_tags[0], 'name')
                dict_update(task_env,
                        TEST=task_name,
                        TESTRPMNAME=normalize_rpm_name(rpm_name),
                        TESTPATH="/mnt/tests"+task_name ,
                        KILLTIME=str(ewd))
                executable = mk_rhts_task(task_env, repos, repof)
                args = [rpm_name]
                log.info("parse_recipe_xml: RPMTest %s - %s %s", rpm_name, executable, args)
                break

            exec_tags = task.getElementsByTagName('executable')
            log.debug("parse_recipe_xml: executable tag: %s", exec_tags)
            if exec_tags:
                if repof:
                    f = open('/etc/yum.repos.d/beaker-tests.repo', 'w+')
                    f.write(repof)
                    f.close()
                executable = xml_attr(exec_tag[0], 'url')
                for arg in exec_tag[0].getElementsByTagName('arg'):
                    args.append(xml_attr(arg, 'value'))
                log.info("parse_recipe_xml: ExecutableTest %s %s", executable, args)
                break

            break

        proto_len = executable.find(':')
        if proto_len >= 0:
            proto = executable[:proto_len]
            if proto == "file" and executable[proto_len+1:proto_len+3] == '//':
                executable = executable[proto_len+3:]
            else:
                # FIXME: retrieve a file and set an executable bit.
                log.warning("parse_recipe_xml: Feature not implemented yet. proto=%s",
                        proto)
                continue
        else:
            executable = os.path.abspath(executable)

        if not executable:
            log.warning("parse_recipe_xml: Task %s(%s) does not have an executable associated!",
                    task_name, task_id)
            continue

        if task_env.has_key('TESTORDER'):
            task_env['TESTORDER'] = str(8*int(task_env['TESTORDER']) + 4)
        else:
            task_env['TESTORDER'] = str(8*test_order)
        return dict(task_env=task_env, executable=executable, args=args,
                ewd=ewd)

    return None

def handle_error(result, *args, **kwargs):
    log.warning("Deferred Failed(%r, *%r, **%r)", result, args, kwargs)
    return result


def jsonln(obj):
    return "%s\n" % json.dumps(obj)


class BeakerWriter(writers.JournallingWriter):

    _VERBOSE = ('write', 'send')

    def __init__(self, journal=None, offs=None, proxy=None, method=None, id=None, path=None,
            filename=None, repr=None):
        if offs is None:
            raise exceptions.RuntimeError("empty offs passed!")
        for var in ('journal', 'proxy', 'id', 'filename'):
            if not locals()[var]:
                raise exceptions.RuntimeError("empty %s passed!" % (var,))
        self.proxy = proxy
        self.method = method or 'task_upload_file'
        self.id = id
        self.path = path or '/'
        self.filename = filename
        if repr:
            self.repr = repr
        writers.JournallingWriter.__init__(self, journal, offs, capacity=4096, no_split=True)

    def send(self, cdata):
        """
        Calculate necessary fields and send.
        """
        size = len(cdata)
        offs = self.get_offset()
        d = hashlib.md5()
        d.update(cdata)
        digest = d.hexdigest()
        data = event.encode("base64", cdata)
        # FIXME? I would like to be able to append to the file. *_upload_file
        # calls require offset but the file is remote and there is no way to
        # find the file's real size
        return self.proxy.callRemote(self.method, self.id, self.path,
                self.filename, str(size), digest, offs, data)

def open_(name, mode):
    pre_open(name)
    return open(name, mode)

class BeakerLCBackend(SerializingBackend):

    GET_RECIPE = 'get_recipe'
    TASK_START = 'task_start'
    TASK_STOP = 'task_stop'
    TASK_RESULT = 'task_result'

    _VERBOSE = ['on_idle', 'on_lc_failure', 'set_controller',
            'handle_new_task', 'save_command', 'get_command', 'get_writer',
            'close_writers', 'pre_proc', 'proc_evt_output',
            'proc_evt_lose_item', 'proc_evt_log', 'proc_evt_echo',
            'proc_evt_start', 'proc_evt_end', 'proc_evt_result',
            'proc_evt_relation', 'proc_evt_file', 'proc_evt_file_meta',
            'proc_evt_file_write', 'handle_Stop', 'get_file_info',
            'set_file_info', 'get_result_id', 'handle_Result', 'set_task_info',
            'get_task_info']

    def __init__(self):
        self.conf = config.get_conf('beah-backend')
        self.hostname = self.conf.get('DEFAULT', 'HOSTNAME')
        self.waiting_for_lc = False
        self.runtime = runtimes.ShelveRuntime(self.conf.get('DEFAULT', 'RUNTIME_FILE_NAME'))
        self.__commands = {}
        self.__results_by_uuid = runtimes.TypeDict(self.runtime, 'results_by_uuid')
        self.__file_info = {}
        self.__writers = {}
        self.__offsets = {}
        self.__writer_args = {}
        self.__tasks_by_id = runtimes.TypeDict(self.runtime, 'tasks_by_id')
        self.__tasks_by_uuid = runtimes.TypeDict(self.runtime, 'tasks_by_uuid')
        self.__task_info = {}
        self.__journal_file = None
        self.__len_queue = []
        offs = self.__journal_offs = self.runtime.type_get('', 'journal_offs', 0)
        SerializingBackend.__init__(self)
        f = self.get_journal()
        f.seek(offs, 1)
        while True:
            ln = f.readline()
            if ln == '':
                break
            try:
                evt, flags = json.loads(ln)
                evt = event.Event(evt)
                SerializingBackend._queue_evt(self, evt, **flags)
                self.__len_queue.append(len(ln))
            except:
                self.on_exception("Can not parse a line from journal.", line=ln)

    def get_journal(self):
        if self.__journal_file is None:
            jname = self.conf.get('DEFAULT', 'VAR_ROOT') + '/journals/beakerlc.journal'
            self.__journal_file = open_(jname, 'ab+')
        return self.__journal_file

    def _queue_evt(self, evt, **flags):
        data = jsonln((evt, flags))
        self.__len_queue.append(len(data))
        self.__journal_file.write(data)
        self.__journal_file.flush()
        SerializingBackend._queue_evt(self, evt, **flags)

    def _pop_evt(self):
        self.__journal_offs += self.__len_queue.pop(0)
        self.runtime.type_set('', 'journal_offs', self.__journal_offs)
        return SerializingBackend._pop_evt(self)

    def on_lc_failure(self, result):
        self.waiting_for_lc = False
        log.error(traceback.format_tb(result.getTracebackObject()))
        reactor.callLater(120, self.on_idle)
        return None

    def on_idle(self):
        if self.waiting_for_lc:
            self.on_error("on_idle called with waiting_for_lc already set.")
            return

        self.proxy.callRemote(self.GET_RECIPE, self.hostname) \
                .addCallback(self.handle_new_task) \
                .addErrback(self.on_lc_failure)
        self.waiting_for_lc = True

    def idle(self):
        return self.proxy.is_idle()

    def set_controller(self, controller=None):
        SerializingBackend.set_controller(self, controller)
        if controller:
            url = self.conf.get('DEFAULT', 'LAB_CONTROLLER')
            self.proxy = RepeatingProxy(url)
            self.proxy.serializing = True
            self.proxy.on_idle = self.set_idle
            if is_class_verbose(self):
                make_logging_proxy(self.proxy)
                self.proxy.logging_print = log.info
            self.on_idle()

    def handle_new_task(self, result):

        self.waiting_for_lc = False

        log.debug("handle_new_task(%s)", result)

        self.recipe_xml = result

        try:
            self.task_data = parse_recipe_xml(self.recipe_xml, self.hostname)
        except:
            self.on_exception("parse_recipe_xml Failed.")
            raise

        log.debug("handle_new_task: task_data = %r", self.task_data)

        if self.task_data is None:
            log.info("* Recipe done. Nothing to do...")
            reactor.callLater(60, self.on_idle)
            return

        task_id = self.task_data['task_env']['TASKID']
        self.task_data['task_env']['LAB_CONTROLLER'] = config.get_conf('beah-backend').get('DEFAULT', 'COBBLER_SERVER')
        run_cmd, _ = self.__tasks_by_id.get(task_id, (None, None))
        new_cmd = not run_cmd
        if new_cmd:
            task_name = self.task_data['task_env']['TASKNAME'] or None
            run_cmd = command.run(self.task_data['executable'],
                    name=task_name,
                    env=self.task_data['task_env'],
                    args=self.task_data['args'])
        self.controller.proc_cmd(self, run_cmd)
        self.save_command(run_cmd)
        if new_cmd:
            self.save_task(run_cmd, task_id)

        # Persistent env (handled by Controller?) - env to run task under,
        # task can change it, and when restarted will continue with same
        # env(?) Task is able to handle this itself. Provide a library...

    RESULT_TYPE = {
            RC.PASS:("pass_", "Pass"),
            RC.WARNING:("warn", "Warning"),
            RC.FAIL:("fail", "Fail"),
            RC.CRITICAL:("panic", "Panic - Critical"),
            RC.FATAL:("panic", "Panic - Fatal"),
            }

    def result_type(rc):
        return BeakerLCBackend.RESULT_TYPE.get(rc,
                ("warn", "Warning: Unknown Code (%s)" % rc))
    result_type = staticmethod(result_type)

    LOG_TYPE = {
            LOG_LEVEL.DEBUG3: "DEBUG3",
            LOG_LEVEL.DEBUG2: "DEBUG2",
            LOG_LEVEL.DEBUG1: "DEBUG",
            LOG_LEVEL.INFO: "INFO",
            LOG_LEVEL.WARNING: "WARNING",
            LOG_LEVEL.ERROR: "ERROR",
            LOG_LEVEL.CRITICAL: "CRITICAL",
            LOG_LEVEL.FATAL: "FATAL",
            }

    def log_type(log_level):
        return BeakerLCBackend.LOG_TYPE.get(log_level,
                "WARNING(%s)" % (log_level,))
    log_type = staticmethod(log_type)

    def mk_msg(self, **kwargs):
        return json.dumps(kwargs)

    def save_task(self, run_cmd, tid):
        tuuid = run_cmd.id()
        self.__tasks_by_uuid[tuuid] = (run_cmd, tid)
        self.__tasks_by_id[tid] = (run_cmd, tuuid)

    def get_evt_task_id(self, evt):
        tuuid = self.get_evt_task_uuid(evt)
        if tuuid is None:
            return None
        return self.__tasks_by_uuid.get(tuuid, (None, None))[1]

    def get_evt_task_uuid(self, evt):
        evev = evt.event()
        if evev in ('start', 'end'):
            return evt.arg('task_id')
        if evev == 'echo':
            cid = evt.arg('cmd_id')
            cmd = self.get_command(cid)
            if (cmd is not None and cmd.command()=='run'):
                return cid
            else:
                return None
        return evt.origin().get('id',None)

    def save_command(self, cmd):
        self.__commands[cmd.id()] = cmd

    def get_command(self, cmd_id):
        return self.__commands.get(cmd_id, None)

    def get_writer(self, id, name, args=(), kwargs={}):
        writer = self.__writers.setdefault(id, {}).get(name, None)
        if writer is None:
            offss = self.__offsets.get(id, None)
            if offss is None:
                offss = self.__offsets[id] = runtimes.TypeDict(self.runtime, 'offsets/%s' % id)
            offs = offss.get(name, 0)
            wrargs = self.__writer_args.get(id, None)
            if wrargs is None:
                wrargs = self.__writer_args[id] = runtimes.TypeDict(self.runtime, 'writer_args/%s' % id)
            wrargs[name] = (args, kwargs)
            jname = self.conf.get('DEFAULT', 'VAR_ROOT') + "/journals/%s/%s" % (id, name)
            journal = open_(jname, "ab+")
            writer = BeakerWriter(journal, offs, id=id,
                    filename=os.path.basename(name),
                    path=os.path.dirname(name),
                    proxy=self.proxy, *args, **kwargs)
            writer_set_offset = writer.set_offset
            def wroff(offs):
                offss[name] = offs
                writer_set_offset(offs)
            writer.set_offset = wroff
            self.__writers[id][name] = writer
        return writer

    def close_writers(self, id):
        wrargs = self.__writer_args.get(id, None)
        if wrargs is not None:
            writers = self.__writers.get(id, {})
            for name in list(wrargs.keys()):
                args = wrargs[name]
                writer = self.get_writer(id, name, args[0], args[1])
                if not writer:
                    self.on_error("Can not open a writer(%r, %r)" % (id, name))
                else:
                    writer.close()
                    del writers[name]
                del wrargs[name]
            del self.__writer_args[id]
        writers = self.__writers.get(id, None)
        if writers is not None:
            for name in list(writers.keys()):
                log.warning("close_writers: writer(%r, %r) without args record", id, name)
                writers[name].close()
                del writers[name]
            del self.__writers[id]
        offss = self.__offsets.get(id, None)
        if offss is not None:
            for name in list(offss.keys()):
                del offss[name]
            del self.__offsets[id]

    def pre_proc(self, evt):
        id = evt.task_id = self.get_evt_task_id(evt)
        if id is None:
            return True
        if evt.event() == 'file_write':
            evt = event.Event(evt)
            evt.args()['data'] = '...hidden...'
        self.get_writer(id, 'debug/.task_beah_raw').write(jsonln(evt))
        return False

    def proc_evt_output(self, evt):
        self.get_writer(evt.task_id, 'debug/task_output_%s' % evt.arg('out_handle')) \
                        .write(str(evt.arg('data')))

    def proc_evt_lose_item(self, evt):
        f = self.get_writer(evt.task_id, 'debug/task_beah_unexpected')
        f.write(str(evt.arg('data')) + "\n")

    def proc_evt_log(self, evt):
        message = evt.arg('message', '')
        reason = evt.arg('reason', '')
        join = ''
        if reason:
            reason = 'reason=%s' % reason
            if message:
                message = "%s; %s" % (message, reason)
            else:
                message = reason
        message = "LOG:%s(%s): %s\n" % (evt.arg('log_handle', ''),
                self.log_type(evt.arg('log_level')), message)
        self.get_writer(evt.task_id, 'debug/task_log').write(message)

    def proc_evt_echo(self, evt):
        cmd = self.get_command(evt.arg('cmd_id'))
        if (cmd is not None and cmd.command()=='run'):
            rc = evt.arg('rc')
            if rc not in (ECHO.OK, ECHO.DUPLICATE):
                id = evt.task_id
                # FIXME: Start was not issued. Is it OK?
                self.task_set_finished(id)
                message = ("Harness could not run the task: %s rc=%s"
                        % (evt.arg('message', 'no info'), rc))
                self.proxy.callRemote(self.TASK_RESULT, id, "fail",
                        "harness/run", 1, message)
                self.proxy.callRemote(self.TASK_STOP, id, "stop", message) \
                                    .addCallback(self.handle_Stop) \
                                    .addErrback(self.on_lc_failure)

    def proc_evt_extend_watchdog(self, evt):
        timeout = evt.arg('timeout')
        self.proxy.callRemote('extend_watchdog', evt.task_id, timeout)

    def proc_evt_start(self, evt):
        id = evt.task_id
        if not self.task_has_started(id):
            self.task_set_started(id)
            self.proxy.callRemote(self.TASK_START, id, 0)
            # FIXME: start local watchdog

    def proc_evt_end(self, evt):
        id = evt.task_id
        self.close_writers(id)
        self.task_set_finished(id)
        rc = evt.arg("rc", None)
        if rc is None:
            score = 999
        else:
            score = int(rc)
        if score != 0:
            message = "Task exited with non zero exit code. rc=%s" % rc
            self.proxy.callRemote(self.TASK_RESULT, id, "fail",
                    "task/exit", score, message)
        else:
            message = "OK"
        self.proxy.callRemote(self.TASK_STOP, id, "stop", message) \
                        .addCallback(self.handle_Stop) \
                        .addErrback(self.on_lc_failure)

    def find_job_id(self, id):
        return id

    def find_recipe_id(self, id):
        return id

    def proc_evt_abort(self, evt):
        type = evt.arg('type', '')
        if not type:
            log.error("No abort type specified.")
            raise exceptions.RuntimeError("No abort type specified.")
        target = evt.arg('target', None)
        d = None
        if type == 'job':
            target = self.find_job_id(target)
            if target is not None:
                d = self.proxy.callRemote('job_stop', target, 'abort', 'Aborted by task.')
        elif type == 'recipe':
            target = self.find_recipe_id(target)
            if target is not None:
                d = self.proxy.callRemote('recipe_stop', target, 'abort', 'Aborted by task.')

    def proc_evt_result(self, evt):
        try:
            type = self.result_type(evt.arg("rc", None))
            handle = evt.arg("handle", "%s/%s" % \
                    (self.task_data['task_env']['TASKNAME'], evt.id()))
            score = evt.arg("statistics", {}).get("score", 0)
            message = evt.arg('message', '') or self.mk_msg(event=evt)
            log_msg = "%s:%s: %s score=%s\n" % (type[1], handle, message, score)
            self.get_writer(evt.task_id, 'debug/task_log').write(log_msg)
            self.proxy.callRemote(self.TASK_RESULT, evt.task_id,
                    type[0], handle, score, message) \
                            .addCallback(self.handle_Result, event_id=evt.id())
            self.__results_by_uuid[evt.id()] = ""
        except:
            s = format_exc()
            log.error("Exception in proc_evt_result: %s", s)
            print s
            raise

    def __on_error(self, level, msg, tb, *args, **kwargs):
        if args: msg += '; *args=%r' % (args,)
        if kwargs: msg += '; **kwargs=%r' % (kwargs,)
        log.error("--- %s: %s at %s", level, msg, tb)

    def on_exception(self, msg, *args, **kwargs):
        self.__on_error("EXCEPTION", msg, format_exc(),
                *args, **kwargs)

    def on_error(self, msg, *args, **kwargs):
        self.__on_error("ERROR", msg, traceback.format_stack(), *args, **kwargs)

    def proc_evt_relation(self, evt):
        if evt.arg('handle') == 'result_file':
            rid = evt.arg('id1')
            result_id = self.get_result_id(rid)
            if result_id is None:
                self.on_error("Result with given id (%s) does not exist." % rid)
                return
            if result_id == '':
                self.on_error("Waiting for result_id from LC for given id (%s)." % rid)
                return
            fid = evt.arg('id2')
            finfo = self.get_file_info(fid)
            if finfo is None:
                self.on_error("File with given id (%s) does not exist." % fid)
                return
            finfo['be:upload_as'] = ('result_file', result_id, evt.arg('title2'))
            log.debug("relation result_file processed. finfo updated: %r", finfo)

    def proc_evt_file(self, evt):
        fid = evt.id()
        if self.get_file_info(fid) is not None:
            self.on_error("File with given id (%s) already exists." % fid)
            return
        # FIXME: Check what's submitted:
        self.set_file_info(fid, evt.args())

    def proc_evt_file_meta(self, evt):
        fid = evt.arg('file_id')
        # FIXME: Check what's submitted:
        self.set_file_info(fid, evt.args())

    def proc_evt_file_write(self, evt):
        fid = evt.arg('file_id')
        finfo = self.get_file_info(fid)
        if finfo is None:
            self.on_error("File with given id (%s) does not exist." % fid)
            return
        # NOTE: be careful here. finfo is not ordinary dict! It never rewrites
        # existing items with None's.
        finfo['codec'] = evt.arg('codec', None)
        codec = finfo.get('codec', None)
        offset = evt.arg('offset', None)
        seqoff = finfo.get('offset', 0)
        if offset is None:
            offset = seqoff
        elif offset != seqoff:
            if offset == 0:
                # task might want to re-upload file from offset 0.
                log.info('Rewriting file %s.' % fid)
            else:
                log.warning("Given offset (%s) does not match calculated (%s).",
                        offset, seqoff)
        data = evt.arg('data')
        try:
            cdata = event.decode(codec, data)
        except:
            self.on_exception("Unable to decode data.")
            return
        if cdata is None:
            self.on_error("No data found.")
            return
        size = len(cdata)
        self.set_file_info(fid, offset=offset+size)
        # FIXME: make this config.option
        digest_method = 'md5'
        dm, digest = digests.make_digest(evt.arg('digest', None)) or (None, None)
        if dm != digest_method:
            digest = digests.DigestConstructor(digest_method)(cdata).hexdigest()
        if codec != "base64":
            data = event.encode("base64", cdata)
        if finfo.has_key('be:uploading_as'):
            method, id, path, filename = finfo['be:uploading_as']
        else:
            filename = finfo.get('name',
                    self.task_data['task_env']['TASKNAME'] + '/' + fid)
            method = 'task_upload_file'
            id = evt.task_id
            if finfo.has_key('be:upload_as'):
                upload_as = finfo['be:upload_as']
                if upload_as[0] == 'result_file':
                    if upload_as[2]:
                        filename = upload_as[2]
                    id = upload_as[1]
                    method = 'result_upload_file'
            # I would prefer following, but rsplit is not in python2.3:
            #   (path, filename) = ('/' + filename).rsplit('/', 1)
            filename = '/' + filename
            sep_ix = filename.rfind('/')
            (path, filename) = (filename[:sep_ix], filename[sep_ix+1:])
            path = path[1:] or '/'
            finfo['be:uploading_as'] = (method, id, path, filename)

        self.proxy.callRemote(method, id, path, filename,
                str(size), digest, str(offset), data)

    def handle_Stop(self, result):
        """Handler for task_stop XML-RPC return."""
        log_flush(log)
        self.on_idle()

    def get_task_info(self, id):
        """Get data associated with task. Find task by UUID."""
        tinfo = self.__task_info.get(id, None)
        if tinfo:
            return tinfo
        tinfo = runtimes.TypeAddict(self.runtime, 'task_info/%s' % id)
        if tinfo.has_key('__id'):
            self.__task_info[id] = tinfo
            return tinfo
        return None

    def set_task_info(self, id, *args, **kwargs):
        """Attach data to task. Find task by UUID."""
        tinfo = runtimes.TypeAddict(self.runtime, 'task_info/%s' % id)
        tinfo.update(*args, **kwargs)
        tinfo['__id'] = id
        self.__task_info[id] = tinfo

    def task_has_started(self, id):
        tinfo = self.get_task_info(id)
        return tinfo and tinfo.get('state', 0) >= 1

    def task_has_finished(self, id):
        tinfo = self.get_task_info(id)
        return tinfo and tinfo.get('state', 0) >= 2

    def task_set_started(self, id):
        self.set_task_info(id, state=1)

    def task_set_finished(self, id):
        self.set_task_info(id, state=1)

    def get_file_info(self, id):
        """Get data associated with file. Find file by UUID."""
        finfo = self.__file_info.get(id, None)
        if finfo:
            return finfo
        finfo = runtimes.TypeAddict(self.runtime, 'file_info/%s' % id)
        if finfo.has_key('__id'):
            self.__file_info[id] = finfo
            return finfo
        return None

    def set_file_info(self, id, *args, **kwargs):
        """Attach data to file. Find file by UUID."""
        finfo = runtimes.TypeAddict(self.runtime, 'file_info/%s' % id)
        finfo.update(*args, **kwargs)
        finfo['__id'] = id
        self.__file_info[id] = finfo

    def get_result_id(self, event_id):
        """Get data associated with result. Find result by UUID."""
        return self.__results_by_uuid.get(event_id, None)

    def handle_Result(self, result_id, event_id=None):
        """Attach data to a result. Find result by UUID."""
        log.debug("%s.RETURN: %s (original event_id %s)",
                self.TASK_RESULT, result_id, event_id)
        self.__results_by_uuid[event_id] = result_id

    def close(self):
        # FIXME: send a bye to server? (Should this be considerred an abort?)
        reactor.callLater(1, reactor.stop)

def start_beaker_backend():
    if config.parse_bool(config.get_conf('beah-backend').get('DEFAULT', 'DEVEL')):
        print_this = log_this(lambda s: log.debug(s), log_on=True)
        make_class_verbose(BeakerLCBackend, print_this)
        make_class_verbose(BeakerWriter, print_this)
        make_class_verbose(RepeatingProxy, print_this)
    backend = BeakerLCBackend()
    # Start a default TCP client:
    start_backend(backend)

def beakerlc_opts(opt, conf):
    def lc_cb(option, opt_str, value, parser):
        # FIXME!!! check value
        conf['LAB_CONTROLLER'] = value
    opt.add_option("-l", "--lab-controller", "--lc",
            action="callback", callback=lc_cb, type='string',
            help="Specify lab controller's URL.")
    def cs_cb(option, opt_str, value, parser):
        # FIXME!!! check value
        conf['COBBLER_SERVER'] = value
    opt.add_option("-S", "--cobbler-server", "--cs",
            action="callback", callback=cs_cb, type='string',
            help="Cobbler server's host name.")
    def hostname_cb(option, opt_str, value, parser):
        # FIXME!!! check value
        conf['HOSTNAME'] = value
    opt.add_option("-H", "--hostname",
            action="callback", callback=hostname_cb, type='string',
            help="Identify as HOSTNAME when talking to Lab Controller.")
    return opt

def defaults():
    d = config.backend_defaults()
    cs = os.getenv('COBBLER_SERVER', '')
    lc = os.getenv('LAB_CONTROLLER', '')
    if not lc:
        if cs:
            lc = 'http://%s:8000/server' % cs
        else:
            cs = 'localhost'
            lc = 'http://localhost:5222/'
    if not cs:
        cs = re.compile('^(https?://)?([^/:]+?)(:[0-9]+)?(/.*)?$').match(lc).group(2)
    d.update({
            'NAME':'beah_beaker_backend',
            'LAB_CONTROLLER':lc,
            'COBBLER_SERVER':cs,
            'HOSTNAME':os.getenv('HOSTNAME')
            })
    return d

def configure():
    config.backend_conf(env_var='BEAH_BEAKER_CONF', filename='beah_beaker.conf',
            defaults=defaults(), overrides=config.backend_opts(option_adder=beakerlc_opts))

def main():
    configure()
    log_handler()
    start_beaker_backend()
    reactor.run()

def test_configure():
    configure()
    cfg = config._get_config('beah-backend')
    conf = config.get_conf('beah-backend')
    #cfg.print_()
    #conf.write(sys.stdout)
    assert conf.has_option('DEFAULT', 'NAME')
    assert conf.has_option('DEFAULT', 'LAB_CONTROLLER')
    assert conf.has_option('DEFAULT', 'HOSTNAME')
    assert conf.has_option('DEFAULT', 'INTERFACE')
    assert conf.has_option('DEFAULT', 'PORT')
    assert conf.has_option('DEFAULT', 'LOG')
    assert conf.has_option('DEFAULT', 'DEVEL')
    assert conf.has_option('DEFAULT', 'VAR_ROOT')
    assert conf.has_option('DEFAULT', 'LOG_PATH')
    assert conf.has_option('DEFAULT', 'RUNTIME_FILE_NAME')

def test():
    # FIXME!!! Implement self-test
    test_configure()
    raise exceptions.NotImplementedError("More test to be added here!")

if __name__ == '__main__':
    test()

