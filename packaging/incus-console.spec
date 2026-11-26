# Built from files already laid out under %{stage} (packaging/inside-rpm.sh).
Name:           incus-console
Version:        %{version}
Release:        1%{?dist}
Summary:        Web console for Incus
License:        Apache-2.0
URL:            https://github.com/openrds-io/incus-console

# The bundled Python environment is self-contained: do not derive dependencies
# from it, strip it, or rewrite its scripts.
AutoReqProv:    no
%global debug_package %{nil}
%global __os_install_post %{nil}
%global __brp_mangle_shebangs %{nil}

Requires:       python(abi) = %{python_minor}
Requires:       nginx
Requires:       openssl
Requires(pre):  shadow-utils
%{?systemd_requires}

%description
Incus Console manages Incus containers, storage, networks, projects and
clusters from a browser, with users, roles and an audit log.

After installing, run incus-console-setup to put it behind nginx and create
the first admin.

%install
mkdir -p %{buildroot}
cp -a %{stage}/. %{buildroot}/
install -d -m 0700 %{buildroot}/var/lib/incus-console

%pre
getent group incus-console >/dev/null || groupadd --system incus-console
getent passwd incus-console >/dev/null || useradd --system --gid incus-console \
  --home-dir /var/lib/incus-console --no-create-home --shell /sbin/nologin \
  --comment "Incus Console" incus-console

%post
%systemd_post incus-console.service

%preun
%systemd_preun incus-console.service

%postun
%systemd_postun_with_restart incus-console.service

%files
/opt/incus-console
%dir %attr(0750,root,incus-console) /etc/incus-console
%config(noreplace) %attr(0640,root,incus-console) /etc/incus-console/config.py
/usr/lib/systemd/system/incus-console.service
/usr/bin/incus-console
/usr/sbin/incus-console-setup
/usr/share/incus-console
%doc /usr/share/doc/incus-console
%dir %attr(0700,incus-console,incus-console) /var/lib/incus-console
