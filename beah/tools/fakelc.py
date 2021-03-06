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

import sys
import os
import os.path
import signal
import exceptions
import traceback
import pprint
import re
import time
from random import randint
import logging
from beah.misc.jsonenv import json
from optparse import OptionParser

from twisted.web import xmlrpc, server
from twisted.internet import reactor, defer
from twisted.internet.error import CannotListenError

import beah
import beah.config
from beah.wires.internals.twmisc import serveAnyChild, serveAnyRequest, twisted_logging
from beah import misc
from beah.misc import log_this, runtimes
import beah.tools

LOG_PATH = 'var/log'
VAR_PATH = 'var/beah'

def conf_opt(args):
    """
    Parses command line for common options.

    This seeks only the few most common options. For other options use your own
    parser.

    Returns tuple (options, args). For descritpin see
    optparse.OptionParser.parse_args and optparse.Values
    """
    opt = OptionParser()
    opt.add_option("-n", "--name", action="store", dest="name",
            help="Name of instance.")
    opt.add_option("-p", "--port", action="store", dest="port",
            help="TCP port to listen on.")
    opt.add_option("-i", "--interface", action="store", dest="interface",
            help="Network interface to listen on.")
    opt.add_option("-v", "--verbose", action="count", dest="verbose",
            help="Increase verbosity.")
    opt.add_option("-q", "--quiet", action="count", dest="quiet",
            help="Decrease verbosity.")
    opt.add_option("-j", "--job", action="store", dest="job_id", metavar="JOB_ID",
            help="Specify JOB_ID. Shall be filled in for multihost tasks.")
    opt.add_option("-s", "--recipeset", action="store", dest="recipeset_id",
            metavar="RECIPESET_ID",
            help="Specify RECIPESET_ID. Shall be filled in for multihost tasks.")
    opt.add_option("-r", "--recipe", action="store", dest="recipe",
            metavar="RECIPE",
            help="RECIPE is name of file which contains JSON definition of recipe.")
    opt.add_option("-R", "--root", action="store", dest="root_dir", metavar="ROOT",
            default='/',
            help="All files will be made relative to ROOT directory.")
    #opt.add_option("-S", "--recipes", action="store", dest="recipeset",
    #        metavar="RECIPESET",
    #        help="RECIPESET is name of file which contains JSON definition of recipe set.")
    opt.add_option("-D", "--define", action="append", dest="variables",
            metavar="VARIABLES",
            help="VARIABLES specify list of overrides.")
    opt.add_option("--timeout", action="store", dest="timeout", type="int",
            default=0,
            help="XMLRPC duration.")
    return opt.parse_args(args)

def conf_main(conf, args):
    (opts, _) = conf_opt(args)
    conf['name'] = opts.name or 'beah_fakelc'
    conf['root'] = opts.root_dir or '/'
    beah.config.proc_verbosity(opts, conf)
    conf['port'] = safe_int(opts.port, 5222)
    conf['interface'] = opts.interface or '::1'
    conf['timeout'] = safe_int(opts.timeout, 0)
    job_id = opts.job_id
    if job_id is None:
        job_id = 100 + randint(0, 99)
        conf.setdefault('job_id', job_id)
    else:
        conf['job_id'] = job_id
    recipeset_id = opts.recipeset_id
    if recipeset_id is None:
        recipeset_id = job_id*100 + randint(0, 99)
        conf.setdefault('recipeset_id', recipeset_id)
    else:
        conf['recipeset_id'] = recipeset_id
    if opts.recipe is None:
        #raise exceptions.Exception("RECIPE must be specified!")
        conf['recipe'] = 'recipes/recipe0'
    else:
        conf['recipe'] = opts.recipe
    varre = re.compile('''^([a-zA-Z_][a-zA-Z0-9_]*)=(['"]?)(.*)\\2$''')
    variables = {}
    if opts.variables:
        for pair in opts.variables:
            key, value = varre.match(pair).group(1,3)
            variables[key] = value
    conf['variables'] = variables
    return conf

log = logging.getLogger('beah_fakelc')

conf = dict(job_id=11, recipeset_id=11, recipe='recipes/recipe0', variables={})
recipes = {}
fqdn_recipes = {}
task_recipe = {}
fqdn_def_recipe = None
task_def_recipe = None

class NoTaskException(Exception): pass
class NoRecipeException(Exception): pass

def no_recipe(fqdn=None):
    return NoRecipeException("No recipe for this machine")

def no_task(id=None, task_id=None, fqdn=None):
    d = dict()
    if id is not None: d['id'] = id
    if fqdn is not None: d['fqdn'] = fqdn
    if task_id is not None: d['task_id'] = task_id
    return NoTaskException("No task matching %s" % d)

def get_recipe_(fqdn=None, id=None, task_id=None):
    log.info("get_recipe_(fqdn=%s, id=%s, task_id=%s)", fqdn, id, task_id)
    if fqdn is not None:
        id = build_recipe(fqdn)
    elif not task_recipe:
        id = build_recipe('')
    elif task_id is not None:
        task_id = int(task_id)
        if task_recipe.has_key(task_id):
            id = task_recipe[task_id]
        else:
            id = task_def_recipe
    if id is None:
        raise no_task(fqdn=fqdn, task_id=task_id, id=id)
    id = int(id)
    if recipes.has_key(id):
        return recipes[id]
    raise no_task(fqdn=fqdn, task_id=task_id, id=id)

def get_recipe_xml(**kwargs):
    rec = get_recipe_(**kwargs)
    return rec[0] % rec[1]

def get_recipe_args(**kwargs):
    return get_recipe_(**kwargs)[1]

def get_recipe_path(task_id):
    return "%(job_id)s/%(recipeset_id)s/%(recipe_id)s" % get_recipe_args(task_id=task_id)


RESULT_TYPE_ = ["Pass", "Warn", "Fail", "Panic"]

def print_(obj):
    log.info("%s", obj)
    return obj

def do_get_my_recipe(fname, system_name=None, recipe_id=None):
    log.info("%s(system_name=%r, recipe_id=%r)", fname, system_name, recipe_id)
    if recipe_id:
        return print_(get_recipe_xml(id=recipe_id))
    if system_name:
        return print_(get_recipe_xml(fqdn=system_name))
    raise NotImplementedError

def do_get_recipe(fname, fqdn):
    log.info("%s(fqdn=%r)", fname, fqdn)
    return print_(get_recipe_xml(fqdn=fqdn))

def do_task_info(fname, qtask):
    log.info("%s(qtask=%r)", fname, qtask)
    if qtask.startswith('T:'):
        task_id = qtask[2:]
        if not get_recipe_(task_id=task_id):
            recipe = get_recipe_(fqdn='')
            if not recipe:
                raise no_recipe()
        rec_args = get_recipe_args(task_id=task_id)
        if not rec_args:
            raise no_task(task_id=task_id)
        status = rec_args['task%s_stat' % task_id]
        return dict(
                is_finished=(status in ("Completed", "Aborted", "Cancelled")),
                state_label=status,
                )
    else:
        raise NotImplementedError


class TaskStarter(object):

    def __init__(self, n=0):
        self.n = n

    def check(self):
        self.n -= 1
        if self.n >= 0:
            raise Exception("Wait a minute!")

#task_starter = TaskStarter(3)
task_starter = TaskStarter(0)


try:
    getattr(__builtins__,'sorted')
except AttributeError:
    def sorted(l):
        l = list(l)
        l.sort()
        return l

class Element(object):
    def __init__(self, type, id=None, parent=None, name=None, classes=None, **kwargs):
        self.type = type
        self._id = id
        self._name = name
        self._parent = parent
        self.kwargs = kwargs
        self._classes = classes

    def ID(self):
        if self._id:
            return "%s-%s" % (self.type, self._id)
        else:
            return None

    def id(self):
        return "%s:%s" % (self.type, self._id or self._name or "???")

    def header(self):
        name = self._name
        id = self._id
        if id and name:
            return "%s:%s | %s" % (self.type, id, name)
        else:
            return self.id()

    def link(self):
        return """<A CLASS="%s" HREF="#%s">%s</A>""" % (self.type, self.ID(), self.id())

    def anchor(self):
        if self._id:
            return """ ID="%s" """ % self.ID()
        else:
            return ""

    def classes(self):
        clss = (self.type,) + (self._classes or ())
        return " ".join(clss)

    def format_args(self):
        a = ["""    <DIV CLASS="PAIR">
                        <SPAN CLASS="KEY">%s</SPAN>=<SPAN CLASS="VALUE">%s</SPAN>
                    </DIV>
                """ % (k, self.kwargs[k]) for k in sorted(self.kwargs.keys())]
        if a:
            return """
                %s""" % "".join(a)
        else:
            return ""

    def format_body(self):
        return self.format_args()

    def __str__(self):
        if self._parent:
            parent = self._parent.link()
        else:
            parent = ""
        return """
            <TR %s CLASS="%s">
                <TD CLASS="name">%s</TD>
                <TD CLASS="parent">%s</TD>
                <TD CLASS="body">%s</TD>
            </TR>""" % (self.anchor(), self.classes(), self.header(), parent, self.format_body())


def Job(id):
    return Element("job", id=id)


def RecipeSet(id):
    return Element("recipeset", id=id)


def Recipe(id):
    return Element("recipe", id=id)


def Task(id, **kwargs):
    return Element("task", id=id, **kwargs)


def Result(id, task=None):
    return Element("result", id=id, parent=task)


class File(Element):

    def __init__(self, parent, name='', path='', fullpath=''):
        pathname = os.path.join(path, name)
        self.fullpathname = os.path.join(fullpath, name)
        super(File, self).__init__("file", parent=parent, name=pathname)

    def header(self):
        return """<A HREF="%s">%s</A>""" % (self.fullpathname, super(File, self).header())

    def link(self):
        return """<A HREF="%s">%s</A>""" % (self.fullpathname, self.id())


class Results(object):

    def __init__(self, var_path, upload_path):
        result_filename = os.path.join(var_path, "results.txt")
        misc.pre_open(result_filename)
        self.f = open(result_filename, "a+")
        self.upload_path = upload_path

    def close(self):
        if self.f:
            self.f.close()
            self.f = None

    def write(self, obj):
        self.f.write(str(obj)+"\n")

    def start(self, id, kill_time):
        self.write(Task(id, kill_time=kill_time, method="start"))

    def stop(self, id, type_, msg):
        self.write(Element("stop", parent=Task(id), classes=(type_,), stop_type=type_, message=msg))

    def result(self, id, result_type, path, score, summary, result_id):
        self.write(Element("result", id=result_id, parent=Task(id), name=path, classes=(result_type,), result_type=result_type, path=path, score=score, summary=summary))

    def task_upload(self, id, path, name, size, offset, fullpath):
        longpath = os.path.join(self.upload_path, fullpath)
        file = File(name=name, path=path, parent=Task(id), fullpath=longpath)
        self.write(file)
        self.write(Element("task_upload", parent=file, size=size, offset=offset))

    def result_upload(self, id, result_id, path, name, size, offset, fullpath):
        longpath = os.path.join(self.upload_path, fullpath)
        file = File(name=name, path=path, parent=Result(result_id, task=Task(id)), fullpath=longpath)
        self.write(file)
        self.write(Element("result_upload", parent=file, size=size, offset=offset))

    def job_stop(self, id, stop_type, msg):
        self.write(Element("job_stop", parent=Job(id), classes=(stop_type,), stop_type=stop_type, message=msg))

    def recipeset_stop(self, id, stop_type, msg):
        self.write(Element("recipeset_stop", parent=RecipeSet(id), classes=(stop_type,), stop_type=stop_type, message=msg))

    def recipe_stop(self, id, stop_type, msg):
        self.write(Element("recipe_stop", parent=Recipe(id), classes=(stop_type,), stop_type=stop_type, message=msg))

    def extend_watchdog(self, id, kill_time):
        self.write(Element("extend_watchdog", parent=Task(id), kill_time=kill_time))

    #def ...(self, id, ...):
    #    self.write(("", ))


def do_task_start(fname, task_id, kill_time):
    global results
    results.start(task_id, kill_time)
    log.info("%s(task_id=%r, kill_time=%r)", fname, task_id, kill_time)
    # Fail on start, to check start repeating.
    task_starter.check()
    rec_args = get_recipe_args(task_id=task_id)
    if not rec_args:
        raise no_task(task_id=task_id)
    rec_args['task%s_stat' % task_id]='Running'
    misc.log_flush(log)
    return 0

STOP_TYPE = {'stop': 'Completed', 'abort': 'Aborted', 'cancel': 'Cancelled'}

def do_task_stop(fname, task_id, stop_type, msg):
    """
    Stop a task

    stop_type -- 'Stop'|'Abort'|'Cancel'

    return 0 on success, error message otherwise
    """
    global results
    results.stop(task_id, stop_type, msg)
    log.info("%s(task_id=%r, stop_type=%r, msg=%r)", fname, task_id, stop_type,
            msg)
    rec_args = get_recipe_args(task_id=task_id)
    if not rec_args:
        raise no_task(task_id=task_id)
    rec_args['task%s_stat' % task_id]=STOP_TYPE[stop_type]
    misc.log_flush(log)
    return 0

def do_task_result(fname, task_id, result_type, path, score, summary):
    """
    Report task result

    result_type -- 'Pass'|'Warn'|'Fail'|'Panic'

    return 0 on success, error message otherwise
    """
    global results
    try:
        log.info(
                "%s(task_id=%r, result_type=%r, path=%r, score=%r, summary=%r)",
                fname, task_id, result_type, path, score, summary)
        rec_args = get_recipe_args(task_id=task_id)
        if not rec_args:
            raise no_task(task_id=task_id)
        ix = 'task%s_res' % task_id
        result = rec_args.get(ix, "Pass")
        if RESULT_TYPE_.count(result) == 0 \
            or (RESULT_TYPE_.count(result_type) > 0 \
                    and RESULT_TYPE_.index(result) < RESULT_TYPE_.index(result_type)):
            rec_args[ix]=result_type
        result_id = "%s%s%.2d" % (task_id, time.strftime("%H%M%S"), randint(0, 99))
        add_result(task_id, result_id)
        results.result(task_id, result_type, path, score, summary, result_id)
        log.info("%s.RETURN: %s", fname, result_id)
        misc.log_flush(log)
        return result_id
    except:
        log.error("%s", misc.format_exc())
        results.result(task_id, result_type, path, score, summary, "ERROR")
        raise

tasks_by_results = {}

def add_result(task_id, result_id):
    tasks_by_results[result_id] = task_id

def get_task_by_result(result_id):
    return tasks_by_results.get(result_id)

### <STOLEN from="Beaker/Server">
import base64
import os.path
import fcntl
import stat
import errno
from beah.misc import digests


class BX(Exception):
    """Stub to keep code compatible with rhts/beaker version"""
    pass


def _(string):
    """Stub to keep code compatible with rhts/beaker version"""
    return string


def safe_int(n, default=None):
    try:
        return int(n)
    except:
        return default

def decode_int(n):
    """If n is not an integer, attempt to convert it"""
    if isinstance(n, (int, long)):
        return n
    return int(n)

def ensuredir(directory):
    """Create directory, if necessary."""
    if os.path.isdir(directory):
        return
    try:
        os.makedirs(directory)
    except OSError:
        #thrown when dir already exists (could happen in a race)
        if not os.path.isdir(directory):
            #something else must have gone wrong
            raise


class Uploader:
    def __init__(self, basepath):
        self.basepath = basepath

    def uploadFile(self, path, name, size, md5sum, offset, data):
        #path: the relative path to upload to
        #name: the name of the file
        #size: size of contents (bytes)
        #md5: md5sum (hex digest) of contents
        #data: base64 encoded file contents
        #offset: the offset of the chunk
        # files can be uploaded in chunks, if so the md5 and size describe
        # the chunk rather than the whole file. the offset indicates where
        # the chunk belongs
        # the special offset -1 is used to indicate the final chunk
        contents = base64.decodestring(data)
        del data
        # we will accept offset and size as strings to work around xmlrpc limits
        offset = decode_int(offset)
        size = decode_int(size)
        digest_method = digests.which_digest(md5sum)
        if digest_method:
            digest_constructor = digests.DigestConstructor(digest_method)
        else:
            digest_constructor = None
        if offset != -1:
            if size is not None:
                if size != len(contents): return False
            if digest_constructor:
                if md5sum != digest_constructor(contents).hexdigest():
                    return False
        uploadpath = self.basepath
        #XXX - have an incoming dir and move after upload complete
        # SECURITY - ensure path remains under uploadpath
        path = os.path.normpath(path)
        if path.startswith('..'):
            raise BX(_("Upload path not allowed: %s" % path))
        udir = "%s/%s" % (uploadpath,path)
        ensuredir(udir)
        fn = "%s/%s" % (udir,name)
        try:
            st = os.lstat(fn)
        except OSError, e:
            if e.errno == errno.ENOENT:
                pass
            else:
                raise
        else:
            if not stat.S_ISREG(st.st_mode):
                raise BX(_("destination not a file: %s" % fn))
        fd = os.open(fn, os.O_RDWR | os.O_CREAT, 0666)
        # log_error("fd=%r" %fd)
        try:
            if offset == 0 or (offset == -1 and size == len(contents)):
                #truncate file
                fcntl.lockf(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                try:
                    os.ftruncate(fd, 0)
                    # log_error("truncating fd %r to 0" %fd)
                finally:
                    fcntl.lockf(fd, fcntl.LOCK_UN)
            if offset == -1:
                os.lseek(fd,0,2)
            else:
                os.lseek(fd,offset,0)
            #write contents
            fcntl.lockf(fd, fcntl.LOCK_EX|fcntl.LOCK_NB, len(contents), 0, 2)
            try:
                os.write(fd, contents)
                # log_error("wrote contents")
            finally:
                fcntl.lockf(fd, fcntl.LOCK_UN, len(contents), 0, 2)
            if offset == -1:
                if size is not None:
                    #truncate file
                    fcntl.lockf(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                    try:
                        os.ftruncate(fd, size)
                        # log_error("truncating fd %r to size %r" % (fd,size))
                    finally:
                        fcntl.lockf(fd, fcntl.LOCK_UN)
                if digest_constructor:
                    #check final md5sum
                    sum = digest_constructor()
                    fcntl.lockf(fd, fcntl.LOCK_SH|fcntl.LOCK_NB)
                    try:
                        # log_error("checking md5sum")
                        os.lseek(fd,0,0)
                        while True:
                            block = os.read(fd, 819200)
                            if not block: break
                            sum.update(block)
                        if md5sum != sum.hexdigest():
                            # log_error("md5sum did not match")
                            #os.close(fd)
                            return False
                    finally:
                        fcntl.lockf(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return True
### </STOLEN>

def do_upload_file(path, name, size, digest, offset, data):
    global uploader
    log.info("do_upload_file(path=%r, name=%r, size=%r, digest=%r, offset=%r, data='...')",
            path, name, size, digest, offset)
    uploader.uploadFile(path, name, size, digest, offset, data)

def do_task_upload_file(fname, task_id, path, name, size, digest, offset, data):
    global results
    log.info("%s(task_id=%r, path=%r, name=%r, size=%r, digest=%r, offset=%r, data='...')",
            fname, task_id, path, name, size, digest, offset)
    fullpath = "task_%s/%s" % (task_id, path)
    results.task_upload(task_id, path, name, size, offset, fullpath)
    do_upload_file(fullpath, name, size, digest, offset,
            data)
    return 0

def do_result_upload_file(fname, result_id, path, name, size, digest, offset, data):
    global results
    log.info("%s(result_id=%r, path=%r, name=%r, size=%r, digest=%r, offset=%r, data='...')",
            fname, result_id, path, name, size, digest, offset)
    task_id = get_task_by_result(result_id)
    fullpath = "task_%s/result_%s/%s" % (task_id, result_id, path)
    results.result_upload(task_id, result_id, path, name, size, offset, fullpath)
    do_upload_file(fullpath, name, size, digest, offset,
            data)
    return 0

################################################################################
# XML-RPC HANDLERS:
################################################################################
class LCRecipes(xmlrpc.XMLRPC):

    _VERBOSE = (('return_recipe', staticmethod), 'xmlrpc_to_xml', 'xmlrpc_system_xml')

    def return_recipe(**kwargs):
        return get_recipe_xml(**kwargs)
    return_recipe = staticmethod(return_recipe)

    def xmlrpc_to_xml(self, recipe_id):
        log.info("recipes.to_xml(%r)", recipe_id)
        return self.return_recipe(id=recipe_id)

    def xmlrpc_system_xml(self, fqdn):
        #return self.return_recipe(fqdn=fqdn)
        return do_get_recipe("recipes.system_xml", fqdn)

class LCRecipeTasks(xmlrpc.XMLRPC):

    _VERBOSE = ('xmlrpc_Start', 'xmlrpc_Stop', 'xmlrpc_Result')

    def xmlrpc_Start(self, task_id, kill_time):
        return do_task_start("recipes.tasks.Start", task_id, kill_time)

    def xmlrpc_Stop(self, task_id, stop_type, msg):
        return do_task_stop("recipes.tasks.Stop", task_id, stop_type, msg)

    def xmlrpc_Result(self, task_id, result_type, path, score, summary):
        return do_task_result("recipes.tasks.Result", task_id, result_type,
                path, score, summary)

class LCHandler(xmlrpc.XMLRPC):

    """XMLRPC handler to handle requests to LC."""

    # FIXME: file upload: do not display the data...
    _VERBOSE = ('xmlrpc_get_recipe', 'xmlrpc_task_start', 'xmlrpc_task_stop',
            'xmlrpc_task_result', 'xmlrpc_task_upload_file', 'catch_xmlrpc',
            'xmlrpc_result_upload_file', 'xmlrpc_extend_watchdog',
            'xmlrpc_recipeset_stop', 'xmlrpc_recipe_stop', 'xmlrpc_job_stop',
            'xmlrpc_task_info',
            )

    def __init__(self, *args, **kwargs):
        xmlrpc.XMLRPC.__init__(self, *args, **kwargs)
        recipes = LCRecipes()
        recipes.putSubHandler('tasks', LCRecipeTasks())
        self.putSubHandler('recipes', recipes)
        self.XMLRPC_TIMEOUT = 0

    def Return(self, value):
        if self.XMLRPC_TIMEOUT:
            d = defer.Deferred()
            reactor.callLater(self.XMLRPC_TIMEOUT, d.callback, value)
            return d
        else:
            return value

    def xmlrpc_get_my_recipe(self, request):
        return self.Return(do_get_my_recipe("get_my_recipe", **request))

    def xmlrpc_get_recipe(self, fqdn):
        return self.Return(do_get_recipe("get_recipe", fqdn))

    def xmlrpc_task_info(self, qtask):
        return self.Return(do_task_info("task_info", qtask))

    def get_ids(self, task_id):
        recipe_args = get_recipe_args(task_id=task_id)
        return (recipe_args['job_id'], recipe_args['recipeset_id'],
                recipe_args['recipe_id'])

    def xmlrpc_task_start(self, task_id, kill_time):
        if kill_time:
            global runtime
            runtime.type_set('watchdog/' + get_recipe_path(task_id), task_id, kill_time-1)
        return self.Return(do_task_start("task_start", task_id, kill_time))

    def xmlrpc_task_stop(self, task_id, stop_type, msg=''):
        return self.Return(do_task_stop("task_stop", task_id, stop_type, msg))

    def xmlrpc_recipeset_stop(self, recipeset_id, stop_type, msg=''):
        global results
        log.info('recipeset_stop(%r, %r, %r)', recipeset_id, stop_type, msg)
        results.recipeset_stop(recipeset_id, stop_type, msg)
        return self.Return(0)

    def xmlrpc_recipe_stop(self, recipe_id, stop_type, msg=''):
        global results
        log.info('recipe_stop(%r, %r, %r)', recipe_id, stop_type, msg)
        results.recipe_stop(recipe_id, stop_type, msg)
        return self.Return(0)

    def xmlrpc_job_stop(self, job_id, stop_type, msg=''):
        global results
        log.info('job_stop(%r, %r, %r)', job_id, stop_type, msg)
        results.job_stop(job_id, stop_type, msg)
        return self.Return(0)

    def xmlrpc_task_result(self, task_id, result_type, path, score, summary):
        return self.Return(do_task_result("task_result", task_id, result_type, path, score,
                summary))

    def xmlrpc_extend_watchdog(self, task_id, kill_time):
        global results, runtime
        log.info('extend_watchdog(%r, %r)', task_id, kill_time)
        results.extend_watchdog(task_id, kill_time)
        runtime.type_set('watchdog/' + get_recipe_path(task_id), task_id, kill_time-1)
        return self.Return(kill_time-1)

    def xmlrpc_status_watchdog(self, task_id):
        global runtime
        return runtime.type_get('watchdog/' + get_recipe_path(task_id), task_id, -1)

    def xmlrpc_task_upload_file(self, task_id, path, name, size, digest,
            offset, data):
        return self.Return(do_task_upload_file("task_upload_file", task_id, path, name,
                size, digest, offset, data))

    def xmlrpc_result_upload_file(self, result_id, path, name, size, digest,
            offset, data):
        return self.Return(do_result_upload_file("result_upload_file", result_id, path, name,
                size, digest, offset, data))

    def catch_xmlrpc(self, method, *args):
        """Handler for unhandled requests."""
        log.error("Missing method: %r", [method] + list(args))
        # This is likely to break the test, but it does not matter now...
        return "--- ERROR: Server can not handle command %s" % method

serveAnyChild(LCHandler)
serveAnyRequest(LCHandler, 'catch_xmlrpc', xmlrpc.XMLRPC)

################################################################################
# RECIPE DEFINITIONS:
################################################################################
def build_recipe(fqdn):
    global conf
    log.debug("build_recipe(%r)", fqdn)
    if fqdn is None:
        return None
    if not fqdn:
        fqdn = os.environ["HOSTNAME"]
    if fqdn_recipes.has_key(fqdn):
        return fqdn_recipes[fqdn]
    return recipe_builder(conf['job_id'], conf['recipeset_id'], conf['recipe'], conf['variables'], fqdn)

def find_open(fname):
    fn = beah.tools.get_file(fname)
    if fn:
        return open(fn)
    raise exceptions.RuntimeError("Could not find file '%s'" % fname)

def recipe_builder(job_id, recipeset_id, recipefile, overrides, fqdn):
    global runtime
    log.debug("recipe_builder(%r, %r, %r, %r, %r)" % (job_id, recipeset_id,
        recipefile, overrides, fqdn))
    f = find_open(recipefile)
    try:
        recipexml, machines, tasks, args = json.load(f)
    finally:
        f.close()
    f = find_open(recipexml)
    try:
        recipe = f.read()
    finally:
        f.close()
    args['beah_root'] = beah.tools.get_data_root().next() # pylint: disable=E1101
    args['beah_py_root'] = beah.tools.get_root()
    args.update(overrides)
    args['job_id'] = job_id
    args['recipeset_id'] = recipeset_id
    recipe_id = args.setdefault('recipe_id', 99)
    for task in tasks:
        args.setdefault('task%d_stat' % task, 'Waiting')
        args.setdefault('task%d_res' % task, 'None')
    for machine_ix in range(len(machines)):
        machine = machines[machine_ix]
        args.setdefault('machine%d' % machine_ix, machine)
        args.setdefault('machine%d_stat' % machine_ix, 'None')
    rtargs = runtimes.TypeDict(runtime, 'args/%s/%s/%s' % (job_id,
        recipeset_id, recipe_id))
    for k, v in args.items():
        rtargs.setdefault(k, v)
    for machine_ix in range(len(machines)):
        if rtargs.get('machine%d' % machine_ix, '') == fqdn:
            log.debug("recipe_builder: found %r as %r" % (fqdn, machine_ix))
            break
    else:
        log.debug("recipe_builder: %r not found, using machine 0" % (fqdn,))
        rtargs['machine0'] = fqdn
    log.debug("recipe_builder: args=%r" % (dict(rtargs),))
    for machine_ix in range(len(machines)):
        machine = rtargs.get('machine%d' % machine_ix, '')
        if machine == '' or machine == 'machine%d' % machine_ix:
            log.warning("Machine %d was not specified.", machine_ix)
    schedule(fqdn, recipe_id, recipe, rtargs, tasks)
    return recipe_id

def schedule(machine, recipe_id, recipe, args, tasks):
    log.debug("schedule(%r, %r, %r, %r, %r)" % (machine, recipe_id,
        recipe, dict(args), tasks))
    global recipes, task_recipe, fqdn_recipes
    recipes[recipe_id] = (recipe, args)
    log.info("%s", pprint.pformat(recipes))
    fqdn_recipes[machine] = recipe_id
    for task in tasks:
        task_recipe[task] = recipe_id


def close(log):
    global runtime, results
    runtime.close()
    results.close()
    log.info("runtime closed.")
    misc.log_flush(log)


def main():
################################################################################
# EXECUTE:
################################################################################
    global conf, runtime, uploader, results
    conf = conf_main({}, sys.argv[1:])
    name = conf['name']
    var_path = os.path.join(conf['root'], VAR_PATH, name)
    runtime = runtimes.ShelveRuntime(os.path.join(var_path, "runtime"))
    log = logging.getLogger('beah_fakelc')
    twisted_logging(log, level=logging.WARNING)
    # FIXME: redirect to console or syslog?
    misc.make_log_handler(log, os.path.join(conf['root'], LOG_PATH), "%s.log" % name)
    log.setLevel(misc.str2log_level(conf.get('LOG', 'warning')))
    if conf.get('DEVEL', False):
        print_this = log_this.log_this(lambda s: log.debug(s), log_on=True)
        # make the classes verbose:
        misc.make_class_verbose(LCRecipes, print_this)
        misc.make_class_verbose(LCRecipeTasks, print_this)
        misc.make_class_verbose(LCHandler, print_this)
    lc = LCHandler()
    lc.XMLRPC_TIMEOUT = safe_int(conf['timeout'], 0)
    s = server.Site(lc, None, 60*60*12)
    reactor.listenTCP(conf['port'], s, interface='')
    # To support testing in IPv6 and mixed IPv4/IPv6 environments, 
    # we attempt to listen on the specified (defaults to IPv6) 
    # interface as well.
    try:
        reactor.listenTCP(conf['port'], s, interface=conf['interface'])
    except CannotListenError:
        pass

    reactor.addSystemEventTrigger("before", "shutdown", close, log)
    upload_path = os.path.join(var_path, "fakelc-uploads")
    results = Results(var_path, upload_path)
    uploader = Uploader(upload_path)
    reactor.run()

################################################################################
# RUN:
################################################################################
if __name__ == '__main__':
    main()

