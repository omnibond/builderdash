SSH_TRANSPORT_KUBECTL = "kubectl"
SSH_TRANSPORT_VIRTCTL = "virtctl"
SSH_TRANSPORT_DIRECT = "direct"
SSH_TRANSPORT_PROXY = "proxy"
SSH_TRANSPORTS = (SSH_TRANSPORT_KUBECTL, SSH_TRANSPORT_VIRTCTL, SSH_TRANSPORT_DIRECT, SSH_TRANSPORT_PROXY)

_UNSET = (None, "", "null", "none", "None")


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


def resolve_ssh_transport(my_build, is_kubevirt):
    """How builderdash reaches the build host over SSH.

    Explicit ``k8s_ssh_transport`` (kubectl|virtctl|direct|proxy) wins. When it
    is unset, existing configs keep their behavior: a ``proxy_conf_path`` means
    ``proxy`` (jump host) and a ``build_host_ssh_hostname`` override means
    ``direct`` (the caller runs its own forward). Otherwise KubeVirt builds use
    ``virtctl`` (``virtctl port-forward --stdio`` as a ProxyCommand) and other
    providers use ``direct``. ``kubectl`` (port-forward to the virt-launcher
    pod, for accounts with only ``pods/portforward``) must be chosen explicitly.
    """
    transport = getattr(my_build, "k8s_ssh_transport", None)
    if transport not in _UNSET:
        transport = str(transport).strip().lower()
        if transport not in SSH_TRANSPORTS:
            raise ValueError(
                f"k8s_ssh_transport must be one of {', '.join(SSH_TRANSPORTS)}; got {transport!r}")
        if transport in (SSH_TRANSPORT_KUBECTL, SSH_TRANSPORT_VIRTCTL) and not is_kubevirt:
            raise ValueError(f"k8s_ssh_transport={transport} only applies to kubevirt builds")
        if transport == SSH_TRANSPORT_PROXY and getattr(my_build, "proxy_conf_path", None) in _UNSET:
            raise ValueError("k8s_ssh_transport=proxy requires proxy_conf_path")
        return transport
    if getattr(my_build, "proxy_conf_path", None) not in _UNSET:
        return SSH_TRANSPORT_PROXY
    if getattr(my_build, "build_host_ssh_hostname", None) not in _UNSET:
        return SSH_TRANSPORT_DIRECT
    return SSH_TRANSPORT_VIRTCTL if is_kubevirt else SSH_TRANSPORT_DIRECT


def ssh_transport_is_explicit(my_build):
    return getattr(my_build, "k8s_ssh_transport", None) not in _UNSET
