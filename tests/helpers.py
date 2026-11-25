import os
import tempfile
from types import SimpleNamespace


def make_kubevirt_build(**overrides):
    fd, pubkey = tempfile.mkstemp(suffix=".pub")
    with os.fdopen(fd, "w") as f:
        f.write("ssh-ed25519 AAAATEST builder@test")
    attrs = dict(
        instancename="builderdash-nrp-control-20261119-abcd",
        k8s_namespace="omnibond",
        instancetype="my-instancetype",
        sshkeyuser="rocky",
        pubkeypath=pubkey,
        disksize="35Gi",
        sourceimage={"http": {"url": "https://example.invalid/alma9.qcow2"}},
        kubevirt_plain_text_passwd="s3cret-pass",
        k8s_storage_class="rook-ceph-block-central",
        buildtype="control",
        k8s_build_run_id="run123",
    )
    attrs.update(overrides)
    return SimpleNamespace(**attrs)
