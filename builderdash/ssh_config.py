"""OpenSSH config file for reaching a KubeVirt builder VM by hand.

With the virtctl transport, builderdash writes one file per build so a
developer can run ``ssh -F <file> <alias>``, also after a failed build's VM
is started again with ``virtctl start``. The ProxyCommand is the same
``virtctl port-forward --stdio=true vmi/<name>`` command builderdash uses.

Host keys: builderdash accepts the builder's host key without checking it
(paramiko AutoAddPolicy, nothing saved), because every builder is a fresh VM.
The file matches that with ``StrictHostKeyChecking no`` and
``UserKnownHostsFile /dev/null``.

The file names the private key path (IdentityFile); it never contains key
material. It is written with mode 0600.
"""

import os
import shlex

UNSET = (None, "", "null", "none", "None")


def _ssh_value(value):
    """One OpenSSH config value: % escaped, double-quoted when it has whitespace or quotes."""
    text = str(value).replace("%", "%%")
    if any(c.isspace() for c in text) or '"' in text:
        if '"' in text:
            raise ValueError(f"ssh config value cannot contain a double quote: {value!r}")
        return f'"{text}"'
    return text


def ssh_alias(my_build):
    """Host alias: the builder VM name (it already starts with builderdash-)."""
    name = str(my_build.instancename)
    return name if name.startswith("builderdash-") else f"builderdash-{name}"


def default_ssh_config_path(my_build):
    return os.path.join(os.getcwd(), f"{ssh_alias(my_build)}.ssh_config")


def ssh_config_path(my_build):
    """k8s_ssh_config_path (a leading ~ is expanded), else <cwd>/<alias>.ssh_config."""
    path = getattr(my_build, "k8s_ssh_config_path", None)
    if path in UNSET:
        return default_ssh_config_path(my_build)
    return os.path.abspath(os.path.expanduser(str(path)))


def ssh_config_text(alias, user, identity_file, proxy_argv, note=None):
    proxy_command = " ".join(shlex.quote(str(a)) for a in proxy_argv)
    lines = [
        "# Written by builderdash. Connect with: ssh -F <this file> " + alias,
    ]
    if note:
        lines.append(f"# {note}")
    lines += [
        f"Host {_ssh_value(alias)}",
        f"  HostName {_ssh_value(alias)}",
        f"  User {_ssh_value(user)}",
    ]
    if identity_file not in UNSET:
        lines += [
            f"  IdentityFile {_ssh_value(os.path.expanduser(str(identity_file)))}",
            "  IdentitiesOnly yes",
        ]
    lines += [
        # ProxyCommand is run by the shell; arguments are shell-quoted, % escaped.
        f"  ProxyCommand {proxy_command.replace('%', '%%')}",
        "  StrictHostKeyChecking no",
        "  UserKnownHostsFile /dev/null",
        "  LogLevel ERROR",
        "",
    ]
    return "\n".join(lines)


def write_ssh_config(path, text):
    """Write text to path with mode 0600 (also when the file already exists)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            stream.write(text)
    finally:
        if fd is not None:
            os.close(fd)
    return path
