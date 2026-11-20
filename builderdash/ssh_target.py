def resolve_ssh_target_hostname(my_build):
    """SSH destination host.

    Optional start-YAML key ``build_host_ssh_hostname`` replaces
    ``remoteIp`` (KubeVirt: VMI pod IP). Unset, empty, or ``null`` keeps
    today's GCE/AWS/KubeVirt-on-GCE path. This is a destination replace,
    not ``proxy_conf`` (that remains a jump to the target host).
    """
    hostname = getattr(my_build, "build_host_ssh_hostname", None)
    if hostname in (None, "", "null"):
        return my_build.remoteIp
    return hostname
