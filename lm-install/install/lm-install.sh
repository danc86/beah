#!/bin/bash -x

FAKELC_SERVICE=1

function warning() { echo "--- WARNING: $*" >&2; }
function soft_error() { echo "--- ERROR: $*" >&2; return 1; }
function error() { echo "--- ERROR: $*" >&2; exit 1; }

function hgrep()
{
  history | grep "$@"
}

function psgrep()
{
  ps -ef | grep "$@"
}

function kill3()
{
  local pids=$(ps -o pid --ppid $1 | grep -v PID)
  kill $1
  for pid in $pids; do kill3 $pid; done
}

function lm_env_check()
{
  local _answ=0
  if [[ -z "$BEAH_DEV" ]]; then
    soft_error "env.variable BEAH_DEV is not defined."
    _answ=1
  fi
  if [[ -z "$LM_INSTALL_ROOT" ]]; then
    soft_error "env.variable LM_INSTALL_ROOT is not defined."
    _answ=1
  fi
  if [[ -z "$LM_YUM_PATH" ]]; then
    warning "env.variable LM_YUM_PATH is not defined."
    _answ=0
  fi
  if [[ -z "$LM_YUM_FILE" ]]; then
    warning "env.variable LM_YUM_FILE is not defined."
    _answ=0
  fi
  if [[ -z "$LM_NO_RHTS" ]]; then
    if [[ -z "$LM_RHTS_DEVEL_REPO" ]]; then
      warning "env.variable LM_RHTS_DEVEL_REPO is not defined."
      _answ=0
    fi
    if [[ -z "$LM_RHTS_REPO" ]]; then
      warning "env.variable LM_RHTS_REPO is not defined."
      _answ=0
    fi
  fi
  return $_answ
}
function lm_check()
{
  if lm_env_check; then
    if [[ ! -d "$LM_INSTALL_ROOT" ]]; then
      soft_error "Directory LM_INSTALL_ROOT does not exist!"
      return 2
    fi
    if [[ ! -r "$LM_INSTALL_ROOT/main.sh" ]]; then
      soft_error "File \$LM_INSTALL_ROOT/main.sh does not exist!"
      return 2
    fi
    true
  else
    return $?
  fi
}

function lm_pushd()
{
  mkdir -p $LM_INSTALL_ROOT/temp
  pushd $LM_INSTALL_ROOT/temp
}

function lm_install_yum()
{
  if rpm -q yum; then
    return 0
  fi
  lm_pushd
  /usr/bin/wget -N $LM_YUM_PATH/$LM_YUM_FILE
  /bin/rpm -Uvh $LM_YUM_FILE
  popd
  rpm -q yum
}

function yumi()
{
  if rpm -q "$1"; then
    return 0
  fi
  yum -y install "$1"
}
function yummie()
{
  yum -y install "$@"
}

function lm_install_additional_packages()
{
  yummie vim-enhanced python python-devel rpm-build
}

function lm_install_setuptools()
{
  lm_pushd
  case "${1:-"yum"}" in
  yum)
    yumi python-setuptools
    ;;
  src)
    wget http://pypi.python.org/packages/source/s/setuptools/setuptools-0.6c9.tar.gz
    tar xvzf setuptools-0.6c9.tar.gz
    pushd setuptools-0.6c9
    python setup.py install
    popd
    ;;
  egg)
    wget http://pypi.python.org/packages/2.6/s/setuptools/setuptools-0.6c9-py2.6.egg
    sh setuptools-0.6c9-py2.6.egg
    ;;
  *)
    soft-error "lm_install_setuptools do not understand '$1'"
    popd
    return 1
    ;;
  esac
  popd
}

function lm_unpack()
{
  # define BEAH_SRC_DIR local in caller!
  lm_pushd || return 1
    export BEAH_DEV

    BEAH_SRC_DIR="$PWD/beah-${BEAH_VER}${BEAH_DEV}"
    if [[ -d "$BEAH_SRC_DIR" ]]; then
      echo "Directory \"beah-${BEAH_VER}${BEAH_DEV}\" exists."
    else
      tar xvzf ${LM_INSTALL_ROOT}/install/beah-${BEAH_VER}${BEAH_DEV}.tar.gz
    fi
  popd
  [[ -d "$BEAH_SRC_DIR" ]]
}

function lm_build_rpm()
{
  # define BEAH_RPM local in caller!
  export BEAH_DEV
  local BEAH_SRC_DIR=
  lm_unpack || return 1;
  pushd $BEAH_SRC_DIR || return 1;
    BEAH_RPM="${PWD}/dist/beah-${BEAH_VER}${BEAH_DEV}-1.noarch.rpm"
    if [[ -e "$BEAH_RPM" ]]; then
      echo "Nothing to do: RPM file \"$BEAH_RPM\" already exists."
    else
      local deps
      if [[ "$1" == "-d" ]]; then
        deps="--requires \"$(echo python{,-{hashlib,setuptools,simplejson,twisted-{core,web},uuid,zope-interface}})\""
      else
        deps=""
      fi
      python setup.py bdist_rpm $deps
    fi
  popd
  [[ -e "$BEAH_RPM" ]]
}

function lm_build_egg()
{
  # define BEAH_EGG local in caller!
  export BEAH_DEV
  local BEAH_SRC_DIR=
  lm_unpack || return 1
  pushd $BEAH_SRC_DIR || return 1;
    local egg_ver="$(python -V 2>&1 | cut -d " " -f 2 -s)"
    BEAH_EGG="${PWD}/dist/beah-${BEAH_VER}${BEAH_DEV}-py$egg_ver.egg"
    if [[ -e "$BEAH_EGG" ]]; then
      echo "Nothing to do: Egg file \"$BEAH_EGG\" already exists."
    else
      python setup.py bdist_egg
    fi
  popd
  [[ -e "$BEAH_EGG" ]]
}

# Other Dependencies:

# uuid - http://zesty.ca/python/uuid.py - http://zesty.ca/python/uuid.html
# http://pypi.python.org/pypi/uuid/1.30
# http://pypi.python.org/packages/source/u/uuid/uuid-1.30.tar.gz#md5=639b310f1fe6800e4bf8aa1dd9333117
# - only on Py 2.4 and older

# newer uuid (from 2.5) requires ctype
# http://pypi.python.org/pypi/ctypes/1.0.2

# hashlib - http://code.krypto.org/python/hashlib/hashlib-20081119.tar.gz
# http://pypi.python.org/pypi/hashlib/20081119
# - only on Py 2.4 and older

function lm_install_beah()
{
  local BEAH_SRC_DIR=
  lm_unpack || return 1
  pushd "$BEAH_SRC_DIR" || return 1
    case "${1:-"src"}" in
      rpm|-r|--rpm)
        local BEAH_RPM=
        if lm_build_rpm; then
          yum -y install python-{zope-interface,twisted-{core,web},simplejson}
          rpm -iF "$BEAH_RPM"
        else
          false
        fi
        ;;
      yum|-y|--yum)
        local BEAH_RPM=
        if lm_build_rpm; then
          yum -y install --nogpgcheck "$BEAH_RPM"
        else
          false
        fi
        ;;
      egg|-e|--egg)
        local BEAH_EGG=
        if lm_build_egg; then
          easy_install "$BEAH_EGG"
        else
          false
        fi
        ;;
      src-nodep|-n|--src-nodep)
        python setup.py install
        ;;
      src|-s|--src)
        yum -y install python-{zope-interface,twisted-{core,web},simplejson} && \
        python setup.py install
        ;;
      build|-b|--build)
        lm_build_rpm && \
        lm_build_egg
        ;;
      help|-h|-?|--help)
        echo "USAGE: $0 [src|egg|rpm|build|help]"
        ;;
      *)
        soft-error "lm_install_beah does not understand '$1'"
        echo "USAGE: $0 [src|egg|rpm|build|help]" >&2
        false
        ;;
    esac
    local _answ=$?
  popd
  if which beah-root; then
    rm -f ~/beah
    ln -s $(beah-root) ~/beah
    rm -f ~/beah-libexec
    ln -s $(dirname $(dirname $(beah-data-root)))/libexec/beah ~/beah-libexec
    rm -f ~/beah-data
    ln -s $(beah-data-root) ~/beah-data
  fi
  echo "lm_install_beah: return $_answ"
  return $_answ
}

function lm_the_recipe()
{
  if [[ -f $1 ]]; then
    rm -f /root/the_recipe
    ln -s $1 /root/the_recipe
  else
    echo "No such file '$1'"
  fi
}

function lm_config_beah()
{
  cat > /etc/beah_beaker.conf <<END
[DEFAULT]
LAB_CONTROLLER=${LAB_CONTROLLER:-"http://127.0.0.1:5222/client"}

# PRETEND TO BE ANOTHER MACHINE:
HOSTNAME=${BEAKER_HOSTNAME:-"$HOSTNAME"}

RUNTIME_FILE_NAME=%(VAR_ROOT)s/beah_beaker.runtime

DIGEST=no
END

  rm -f /etc/beah.conf.orig
  mv /etc/beah.conf /etc/beah.conf.orig
  sed -e 's/^DEVEL=.*$/DEVEL=True/' /etc/beah.conf.orig > /etc/beah.conf || true
}

LM_LOGS="/mnt/testarea/beah*.out /var/log/beah*.log /mnt/testarea/var/log/rhts_task*.log /var/log/rhts/*"
function lm_tar_logs()
{
  tar czf $LM_INSTALL_ROOT/lm-logs-$(date +%Y%m%d-%H%M%S).tar.gz $LM_LOGS
}

function lm_logs()
{
  vim -o $LM_LOGS
}

function lm_results()
{
  local uploads=/var/beah/beah_fakelc/fakelc-uploads
  vim -o $uploads/ $uploads/task_*/debug/task_log $uploads/../results.txt
}

function lm_rm_logs()
{
  rm -f $LM_LOGS
  rm -rf /mnt/testarea/beah-fakelc-logs
}

function lm_rm_runtime()
{
  rm -rf /var/beah/*
  rm -rf /mnt/testarea/beah-fakelc-logs/*
  rm -rf /var/run/beah*
}

function ls_egg()
{
  if [[ -z "$1" ]]; then
    soft_error "ls_egg PKG"
    return 1
  fi
  for lib_dir in /usr/lib*; do
    for py_dir in $lib_dir/python*; do
      for egg_file in $py_dir/site-packages/$1-*.egg; do
        if [[ -d "$egg_file" ]]; then
          echo $egg_file
        fi
      done
    done
  done
}

function rm_egg()
{
  rm -rf $(ls_egg $1)
}

function lm_rm_beah_eggs()
{
  rm_egg beah
  rm -f /usr/bin/beah*
}

function lm_rm_all()
{
  lm_rm_logs
  lm_rm_beah_eggs
  lm_rm_runtime
  pushd $LM_INSTALL_ROOT
  rm -rf *
  popd
}

function lm_view_logs()
{
  view -o $LM_LOGS
}

function lm_mon()
{
  local file1=$(mktemp) file2=$(mktemp)
  # rm -f $file1 $file2 &>/dev/null
  lm_ps &> $file2
  cat $file2
  while true; do
    lm_ps &> $file1
    if ! diff $file1 $file2 &>/dev/null; then
      echo
      echo "-------------------"
      cat $file1
    fi
    echo -n "."
    sleep ${1:-2}
    lm_ps &> $file2
    if ! diff $file1 $file2 &>/dev/null; then
      echo
      echo "-------------------"
      cat $file2
    fi
    echo -n "."
    sleep ${1:-2}
  done
}

function lm_ps()
{
  for pid in $(pgrep beah) $(pgrep -f rhts-compat) $(pgrep rhts-test-runner.sh); do
    pstree -lacpnu $pid | grep -v 'sleep 1$'
  done
}

function lm_srv()
{
  service beah-srv ${1:-restart}
}

function lm_beaker()
{
  service beah-beaker-backend ${1:-restart}
}

function lm_watchdog()
{
  service beah-watchdog-backend ${1:-restart}
}

function lm_fakelc()
{
  service beah-fakelc ${1:-restart}
}

function lm_stop()
{
  service beah-beaker-backend stop
  chkconfig --level 345 beah-beaker-backend off
  service beah-watchdog-backend stop
  chkconfig --level 345 beah-watchdog-backend off
  service beah-fwd-backend stop
  chkconfig --level 345 beah-fwd-backend off
  service beah-fakelc stop
  chkconfig --level 345 beah-fakelc off
  if [[ -n "$LM_FAKELC" ]]; then
    if [[ -f /mnt/testarea/beah-fakelc.pid && -n "$(cat /mnt/testarea/beah-fakelc.pid)" ]]; then
      sleep 2
      kill -2 $(cat /mnt/testarea/beah-fakelc.pid)
    fi
  fi
  service beah-srv stop
  chkconfig --level 345 beah-srv off
}

function lm_clean()
{
  lm_rm_logs
  lm_rm_runtime
  rm -rf /var/cache/rhts
}

function lm_stop_test()
{
  service beah-fakelc stop
}

function lm_clean_test()
{
  lm_stop_test
  #lm_rm_logs
  rm -f /mnt/testarea/beah-fakelc*.out /var/log/beah-fakelc*.log /mnt/testarea/var/log/rhts_task*.log
  rm -rf /mnt/testarea/beah-fakelc-logs
  #lm_rm_runtime
  rm -rf /var/run/beah
  rm -rf /var/beah/beah_fakelc*
  rm -rf /var/beah/rhts_task_*
  rm -rf /mnt/testarea/beah-fakelc-logs/*
  rm -rf /var/cache/rhts
}

function lm_start_test()
{
  service beah-fakelc start
  lm_mon
}

function lm_restart_test()
{
  lm_clean_test
  lm_start_test
}

function lm_start_debug_srv()
{
  rm -rf /var/cache/rhts
  if [[ -z "$NO_FAKELC" ]]; then
    if [[ -n "$FAKELC_SERVICE" ]]; then
      chkconfig --level 345 beah-fakelc on
      service beah-fakelc start
    else
      if [[ -n "$LM_FAKELC" ]]; then
        beah-fakelc &> /mnt/testarea/beah-fakelc.out &
        echo "$!" > /mnt/testarea/beah-fakelc.pid
        sleep 2
      fi
    fi
  fi
  if [[ -z "$NO_WATCHDOG" ]]; then
    chkconfig --level 345 beah-watchdog-backend on
    service beah-watchdog-backend start
  fi
  chkconfig --level 345 beah-fwd-backend on
  service beah-fwd-backend start
  chkconfig --level 345 beah-beaker-backend on
  service beah-beaker-backend start
  chkconfig --level 345 beah-srv off
  BEAH_SRV_DEBUGGER=pdb beah-srv
}

function lm_start_debug_beaker()
{
  rm -rf /var/cache/rhts
  chkconfig --level 345 beah-srv on
  service beah-srv start
  if [[ -z "$NO_FAKELC" ]]; then
    if [[ -n "$FAKELC_SERVICE" ]]; then
      chkconfig --level 345 beah-fakelc on
      service beah-fakelc start
    else
      if [[ -n "$LM_FAKELC" ]]; then
        beah-fakelc &> /mnt/testarea/beah-fakelc.out &
        echo "$!" > /mnt/testarea/beah-fakelc.pid
        sleep 2
      fi
    fi
  fi
  if [[ -z "$NO_WATCHDOG" ]]; then
    chkconfig --level 345 beah-watchdog-backend on
    service beah-watchdog-backend start
  fi
  chkconfig --level 345 beah-fwd-backend on
  service beah-fwd-backend start
  chkconfig --level 345 beah-beaker-backend off
  BEAH_BEAKER_DEBUGGER=pdb beah-beaker-backend
}

function lm_start_()
{
  rm -rf /var/cache/rhts
  chkconfig --level 345 beah-srv on
  service beah-srv start
  if [[ -z "$NO_FAKELC" ]]; then
    if [[ -n "$FAKELC_SERVICE" ]]; then
      chkconfig --level 345 beah-fakelc on
      service beah-fakelc start
    else
      if [[ -n "$LM_FAKELC" ]]; then
        beah-fakelc &> /mnt/testarea/beah-fakelc.out &
        echo "$!" > /mnt/testarea/beah-fakelc.pid
        sleep 2
      fi
    fi
  fi
  if [[ -z "$NO_WATCHDOG" ]]; then
    chkconfig --level 345 beah-watchdog-backend on
    service beah-watchdog-backend start
  fi
  chkconfig --level 345 beah-fwd-backend on
  service beah-fwd-backend start
  chkconfig --level 345 beah-beaker-backend on
  service beah-beaker-backend start
}

function lm_start()
{
  lm_start_
  lm_mon
}

function lm_restart()
{
  lm_stop
  lm_start
}

function lm_kill()
{
  beah kill
  if [[ -n "$FAKELC_SERVICE" ]]; then
    service beah-fakelc stop
  else
    if [[ -n "$LM_FAKELC" ]]; then
      kill -2 $(cat /mnt/testarea/beah-fakelc.pid)
    fi
  fi
}

function lm_iptables()
{
  local port=${1:-12432}
  iptables -D INPUT -p tcp -m state --state NEW -m tcp --dport $port -j ACCEPT &>/dev/null
  iptables -I INPUT -p tcp -m state --state NEW -m tcp --dport $port -j ACCEPT
  service iptables save
}

function lm_chkconfig_del()
{
  if chkconfig beah-srv; then
    chkconfig --del beah-srv
  fi
  if chkconfig beah-watchdog-backend; then
    chkconfig --del beah-watchdog-backend
  fi
  if chkconfig beah-beaker-backend; then
    chkconfig --del beah-beaker-backend
  fi
  if chkconfig beah-fwd-backend; then
    chkconfig --del beah-fwd-backend
  fi
  if chkconfig beah-fakelc; then
    chkconfig --del beah-fakelc
  fi
}

function lm_chkconfig_add()
{
  if ! chkconfig beah-srv; then
    chkconfig --add beah-srv
  fi
  if ! chkconfig beah-watchdog-backend; then
    chkconfig --add beah-watchdog-backend
  fi
  if ! chkconfig beah-beaker-backend; then
    chkconfig --add beah-beaker-backend
  fi
  if ! chkconfig beah-fwd-backend; then
    chkconfig --add beah-fwd-backend
  fi
  if [[ -z $NO_FAKELC && -n $FAKELC_SERVICE ]]; then
    if ! chkconfig beah-fakelc; then
      chkconfig --add beah-fakelc
    fi
  else
    if chkconfig beah-fakelc; then
      chkconfig --del beah-fakelc
    fi
  fi
}

function lm_main_beah()
{
  lm_install_beah ${1:-yum} && \
  lm_config_beah || return 1
  lm_iptables
  lm_chkconfig_add
}

function lm_install_rhts_repo()
{
## THIS IS DEFINED IN XML RECIPE
#  # Add yum repository containing RHTS tests:
#  if [[ -z "LM_NO_RHTS" ]]; then
#  cat > /etc/yum.repos.d/rhts-tests.repo << REPO_END
#[rhts-noarch]
#name=rhts tests development
#baseurl=$LM_RHTS_REPO
#enabled=0
#gpgcheck=0
#REPO_END
# fi
  true
}

function lm_install_rhts_deps()
{
  yummie yum-utils
  if [[ -z "$LM_NO_RHTS" ]]; then
  cat > /etc/yum.repos.d/rhts.repo << REPO_END
[rhts]
name=rhts scripts
baseurl=$LM_RHTS_DEVEL_REPO
enabled=0
gpgcheck=0
REPO_END
  yum -y --enablerepo=rhts install rhts-test-env-lab rhts-legacy
  lm_install_rhts_repo
  else
    true
  fi
}

function beah_pushd()
{
  local pth=$(beah-root)
  if [[ ! -z "$pth" ]]; then
    pushd $pth/..
  fi
}

function lm_main_install()
{
  lm_install_yum && \
  lm_install_additional_packages && \
  lm_install_setuptools yum && \
  lm_main_beah "$@" && \
  if [[ -z "$LM_NO_RHTS" ]]; then
    lm_install_rhts_deps
  else
    true
  fi
}

function lm_main_run()
{
  lm_main_install "$@" && \
  lm_restart
  echo "Now you can run e.g. 'lm_kill', 'lm_restart' or 'lm_view_logs'."
  echo "Call 'lm_help' to display a help for these functions."
}

function lm_help()
{
cat <<END
lm_main_run
    Install all dependencies and run harness.
lm_restart
    Run harness. It runs lm_mon as the last command to monitor running procs.
lm_kill
    Send kill command to harness.
lm_stop
    Stops harness services. Use this if lm_kill does not work.
lm_logs
    View log files (uses vim).

OTHER FUNCTIONS:
lm_main [OPTION]
    Subroutine called on sourcing the script. See lm_main_help for usage.
lm_env_check
    Check environment variables.
lm_check
    Check environment including existence of $LM_INSTALL_ROOT/main.sh
lm_main_install [OPTION]
    Install dependencies, harness and rhts dependencies.
    Options: see lm_install_beah
lm_main_beah [OPTION]
    Install and configure harness.
    Options: see lm_install_beah
lm_install_beah [OPTION]
    Install beah package.
    Options:
      rpm -r --rpm   Install from built RPM file.
      yum -y --yum   Install from built RPM file using yum.
      egg -e --egg   Install from egg file.
      src -s --src   Install from source .tar.gz package.
END
}


function lm_main_help()
{
  cat <<END
Usage: $0 [OPTION]...

Options:
run | --run | -r
        install harness and dependencies and start services
check | --check | -c
        check environment variables and directories
env-check | --env-check | -e
        check environment variables
help | --help | -h | -?
        print this message

When used as:
  . $0 [ARGS]

END
echo "Now you can run e.g. 'lm_main_run', 'lm_main_beah' or 'lm_install_beah rpm'"
echo "Call 'lm_help' to display a help for these functions."
}

function lm_main()
{
case "${1:-"help"}" in
  env-check|--env-check|-e)
    lm_env_check
    ;;
  check|--check|-c)
    lm_check
    ;;
  install|--install|-i)
    if [[ -n "$LM_INSTALL_ROOT" && -d "$LM_INSTALL_ROOT/install" && -f "$LM_INSTALL_ROOT/install/env.sh" ]]; then
      . $LM_INSTALL_ROOT/install/env.sh
    fi
    lm_check && lm_main_install
    ;;
  run|--run|-r)
    if [[ -n "$LM_INSTALL_ROOT" && -d "$LM_INSTALL_ROOT/install" && -f "$LM_INSTALL_ROOT/install/env.sh" ]]; then
      . $LM_INSTALL_ROOT/install/env.sh
    fi
    lm_check && lm_main_run
    ;;
  help|--help|-h|-?)
    lm_main_help
    ;;
  *)
    soft_error "$0: Unrecognized option '$1'"
    lm_main_help
    ;;
esac
}

lm_main "$@"

