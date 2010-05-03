%{!?python_sitelib: %global python_sitelib %(%{__python} -c "from distutils.sysconfig import get_python_lib; print get_python_lib()")}
%{!?pyver: %global pyver %(%{__python} -c "import sys ; print sys.version[:3]")}

Summary: Beah - Beaker Test Harness. Part of Beaker project - http://fedorahosted.org/beaker/wiki.
Name: beah
Version: 0.2
Release: 1%{?dist}
URL: http://fedorahosted.org/beah
Source0: http://fedorahosted.org/releases/b/e/%{name}-%{version}.tar.gz
License: GPLv2+
Group: Development/Libraries
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-buildroot
Prefix: %{_prefix}
BuildArch: noarch
Vendor: Marian Csontos <mcsontos@redhat.com>
Packager: Marian Csontos <mcsontos@redhat.com>
Requires: python python-hashlib python-setuptools python-simplejson 
Requires: python-twisted-core python-twisted-web python-uuid python-zope-interface
BuildRequires: python-devel python-setuptools

%description
Beah - Beaker Test Harness.

Ultimate Test Harness, with goal to serve any tests and any test scheduler
tools. Harness consist of a server and two kinds of clients - backends and
tasks.

Backends issue commands to Server and process events from tasks.
Tasks are mostly events producers.

Powered by Twisted.


%prep
%setup -q

%build
%{__python} setup.py build

%install
%{__python} setup.py install --optimize=1 --root=$RPM_BUILD_ROOT $PREFIX

%clean
rm -rf $RPM_BUILD_ROOT

%files
%defattr(-,root,root)
%{_sysconfdir}/%{name}*
%{_sysconfdir}/init.d/%{name}*
%attr(0755, root, root)%{_bindir}/%{name}*
%attr(0755, root, root)%{_bindir}/beat_tap_filter
%{python_sitelib}/%{name}-%{version}-*
%{python_sitelib}/%{name}/
%{python_sitelib}/beahlib.py*
%{_datadir}/%{name}

%changelog
* Mon May 03 2010 Bill Peck <bpeck@redhat.com> 0.2-1
 - Initial spec file and use of tito for tagging and building.