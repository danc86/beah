from beah.system.dist_rhel import *

def install_rpm(self, pkg_name): # pylint: disable=E0102
    self.write_line("yum -y install %s" % pkg_name)

ShExecutable.install_rpm = install_rpm # pylint: disable=E0602

