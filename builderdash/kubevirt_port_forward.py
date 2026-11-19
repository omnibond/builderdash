"""Reach a KubeVirt builder VM's sshd through ``kubectl port-forward``.

``kubectl port-forward pod/<virt-launcher> LOCAL:22`` uses the
``pods/portforward`` subresource, which namespaced service accounts usually
have (``virtctl port-forward`` needs ``virtualmachineinstances/portforward``
instead). With masquerade pod networking, a connection to the launcher pod's
port reaches the guest, so builderdash can SSH to ``127.0.0.1:LOCAL`` without
a jump host or a route to the pod network.

``PortForward`` runs kubectl as a child process, waits for the local port to
open, restarts kubectl if it exits while the build still needs it (re-resolving
the launcher pod first, in case it changed), and terminates it on ``stop()``.
"""

import logging
import os
import shutil
import socket
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

LAUNCHER_SELECTOR = "kubevirt.io=virt-launcher,vm.kubevirt.io/name={name}"


def free_port(host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_binary(configured, default_name, which=shutil.which):
    """Absolute path of a CLI (kubectl/virtctl), or None if it cannot be found."""
    candidate = os.path.expanduser(str(configured)) if configured else default_name
    if os.path.sep in candidate:
        return candidate if os.access(candidate, os.X_OK) else None
    return which(candidate)


def kubectl_base_args(kubectl, kubeconfig=None, context=None):
    args = [kubectl]
    if kubeconfig:
        args += ["--kubeconfig", os.path.expanduser(str(kubeconfig))]
    if context:
        args += ["--context", str(context)]
    return args


def running_launcher_pod(pods):
    """Name of the first Running, not-terminating pod in a V1PodList.items-like list."""
    for pod in pods:
        metadata = getattr(pod, "metadata", None)
        status = getattr(pod, "status", None)
        if metadata is None or getattr(metadata, "deletion_timestamp", None):
            continue
        if status is not None and getattr(status, "phase", None) == "Running":
            return metadata.name
    return None


def find_launcher_pod(core_v1_api, namespace, vm_name, vmi_uid=None):
    """Running virt-launcher pod for VM ``vm_name`` (falls back to the VMI uid label)."""
    selectors = [LAUNCHER_SELECTOR.format(name=vm_name)]
    if vmi_uid:
        selectors.append(f"kubevirt.io/created-by={vmi_uid}")
    for selector in selectors:
        pods = core_v1_api.list_namespaced_pod(namespace, label_selector=selector).items
        name = running_launcher_pod(pods)
        if name:
            return name
    return None


def wait_for_launcher_pod(core_v1_api, namespace, vm_name, vmi_uid=None, timeout=600, interval=5,
                          sleep=time.sleep, clock=time.time):
    deadline = clock() + timeout
    while True:
        name = find_launcher_pod(core_v1_api, namespace, vm_name, vmi_uid=vmi_uid)
        if name:
            return name
        if clock() >= deadline:
            raise RuntimeError(f"no Running virt-launcher pod found for VM '{namespace}/{vm_name}'")
        logger.info("waiting for a Running virt-launcher pod for VM %s/%s ...", namespace, vm_name)
        sleep(interval)


class PortForward:
    """Supervised ``kubectl port-forward pod/POD LOCAL:REMOTE`` bound to 127.0.0.1."""

    def __init__(self, kubectl_args, namespace, pod, local_port=0, remote_port=22,
                 log_path="port-forward.log", pod_resolver=None, popen=subprocess.Popen,
                 restart_delay=2.0, probe=port_open):
        self.kubectl_args = list(kubectl_args)
        self.namespace = namespace
        self.pod = pod
        self.local_port = int(local_port or 0) or free_port()
        self.remote_port = int(remote_port or 22)
        self.log_path = log_path
        self.pod_resolver = pod_resolver
        self.popen = popen
        self.restart_delay = restart_delay
        self.probe = probe
        self.process = None
        self.restarts = 0
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    @property
    def command(self):
        return self.kubectl_args + ["-n", self.namespace, "port-forward", "--address", "127.0.0.1",
                                    f"pod/{self.pod}", f"{self.local_port}:{self.remote_port}"]

    def describe(self):
        return " ".join(self.command)

    def _spawn(self):
        log = open(self.log_path, "a") if self.log_path else open(os.devnull, "w")
        try:
            self.process = self.popen(self.command, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        finally:
            log.close()

    def _respawn(self):
        if self.pod_resolver is not None:
            try:
                pod = self.pod_resolver()
            except Exception as e:  # keep the old pod name and let kubectl report the problem
                logger.warning("could not re-resolve virt-launcher pod: %s", e)
                pod = None
            if pod and pod != self.pod:
                logger.info("virt-launcher pod changed from %s to %s", self.pod, pod)
                self.pod = pod
        self.restarts += 1
        logger.info("restarting kubectl port-forward (restart %d): %s", self.restarts, self.describe())
        self._spawn()

    def _supervise(self):
        while not self._stopping.is_set():
            with self._lock:
                alive = self.process is not None and self.process.poll() is None
            if alive:
                self._stopping.wait(1.0)
                continue
            if self._stopping.wait(self.restart_delay):
                break
            with self._lock:
                if not self._stopping.is_set():
                    self._respawn()

    def start(self, wait=60, interval=0.5, sleep=time.sleep, clock=time.time):
        logger.info("starting kubectl port-forward: %s", self.describe())
        with self._lock:
            self._spawn()
        deadline = clock() + wait
        while not self.probe(self.local_port):
            if clock() >= deadline:
                self.stop()
                raise RuntimeError(
                    f"kubectl port-forward to 127.0.0.1:{self.local_port} did not open within {wait}s "
                    f"(see {self.log_path or 'kubectl output'})")
            with self._lock:
                if self.process.poll() is not None:
                    self._respawn()
            sleep(interval)
        self._thread = threading.Thread(target=self._supervise, name="builderdash-port-forward", daemon=True)
        self._thread.start()
        logger.info("kubectl port-forward is listening on 127.0.0.1:%d -> pod/%s:%d",
                    self.local_port, self.pod, self.remote_port)
        return self

    def stop(self):
        self._stopping.set()
        with self._lock:
            process = self.process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except Exception:
                    process.kill()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        logger.info("kubectl port-forward stopped (127.0.0.1:%d)", self.local_port)


def virtctl_proxy_command(virtctl, namespace, vmi_name, remote_port=22, kubeconfig=None, context=None):
    """argv for a paramiko.ProxyCommand that tunnels SSH over ``virtctl port-forward --stdio``."""
    args = [virtctl]
    if kubeconfig:
        args += ["--kubeconfig", os.path.expanduser(str(kubeconfig))]
    if context:
        args += ["--context", str(context)]
    args += ["--namespace", str(namespace), "port-forward", "--stdio=true", f"vmi/{vmi_name}",
             str(int(remote_port or 22))]
    return args
