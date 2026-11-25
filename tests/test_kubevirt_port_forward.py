import os
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from builderdash import kubevirt_port_forward as pf


def pod(name, phase="Running", deleting=False):
    return SimpleNamespace(metadata=SimpleNamespace(name=name, deletion_timestamp="now" if deleting else None),
                           status=SimpleNamespace(phase=phase))


class FakeProcess:
    def __init__(self, alive=True):
        self.alive = alive
        self.terminated = False

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.terminated = True
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


class LauncherPodTests(unittest.TestCase):
    def test_picks_running_not_terminating(self):
        self.assertEqual(pf.running_launcher_pod([pod("a", "Pending"), pod("b", deleting=True), pod("c")]), "c")
        self.assertIsNone(pf.running_launcher_pod([pod("a", "Succeeded")]))

    def test_selector_and_uid_fallback(self):
        core = mock.Mock()
        core.list_namespaced_pod.side_effect = [SimpleNamespace(items=[]), SimpleNamespace(items=[pod("launcher")])]
        self.assertEqual(pf.find_launcher_pod(core, "ns", "vm1", vmi_uid="uid-1"), "launcher")
        selectors = [c.kwargs["label_selector"] for c in core.list_namespaced_pod.call_args_list]
        self.assertEqual(selectors, ["kubevirt.io=virt-launcher,vm.kubevirt.io/name=vm1",
                                     "kubevirt.io/created-by=uid-1"])

    def test_wait_times_out(self):
        core = mock.Mock()
        core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
        clock = iter([0, 0, 100, 200])
        with self.assertRaises(RuntimeError):
            pf.wait_for_launcher_pod(core, "ns", "vm1", timeout=50, sleep=lambda s: None, clock=lambda: next(clock))


class PortForwardTests(unittest.TestCase):
    def make(self, procs, probe=lambda port: True, resolver=None):
        popen = mock.Mock(side_effect=procs)
        forward = pf.PortForward(["kubectl", "--context", "c"], "ns", "launcher-1", local_port=40022, remote_port=22,
                                 log_path=os.devnull, pod_resolver=resolver, popen=popen, restart_delay=0.01,
                                 probe=probe)
        return forward, popen

    def test_command(self):
        forward, _ = self.make([])
        self.assertEqual(forward.command, ["kubectl", "--context", "c", "-n", "ns", "port-forward", "--address",
                                           "127.0.0.1", "pod/launcher-1", "40022:22"])

    def test_free_port_when_unset(self):
        forward = pf.PortForward(["kubectl"], "ns", "p", local_port=0)
        self.assertGreater(forward.local_port, 0)

    def test_start_supervise_restart_and_stop(self):
        first, second = FakeProcess(), FakeProcess()
        forward, popen = self.make([first, second], resolver=lambda: "launcher-2")
        forward.start(wait=5)
        first.alive = False                      # kubectl exits while the build still needs it
        deadline = time.time() + 5
        while popen.call_count < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(popen.call_count, 2)
        self.assertIn("pod/launcher-2", popen.call_args.args[0])   # re-resolved launcher pod
        forward.stop()
        self.assertTrue(second.terminated)
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_start_times_out_and_cleans_up(self):
        proc = FakeProcess()
        forward, _ = self.make([proc], probe=lambda port: False)
        with self.assertRaises(RuntimeError):
            forward.start(wait=0, sleep=lambda s: None)
        self.assertTrue(proc.terminated)


class HelpersTests(unittest.TestCase):
    def test_kubectl_base_args(self):
        self.assertEqual(pf.kubectl_base_args("kubectl"), ["kubectl"])
        args = pf.kubectl_base_args("/bin/kubectl", "~/.kube/x", "ctx")
        self.assertEqual(args[:2], ["/bin/kubectl", "--kubeconfig"])
        self.assertEqual(args[2], os.path.expanduser("~/.kube/x"))
        self.assertEqual(args[3:], ["--context", "ctx"])

    def test_find_binary(self):
        self.assertIsNone(pf.find_binary("/nonexistent/kubectl", "kubectl"))
        self.assertEqual(pf.find_binary(None, "kubectl", which=lambda n: "/usr/bin/" + n), "/usr/bin/kubectl")
        self.assertEqual(pf.find_binary("kubectl-1.30", "kubectl", which=lambda n: "/opt/" + n), "/opt/kubectl-1.30")

    def test_virtctl_proxy_command(self):
        self.assertEqual(pf.virtctl_proxy_command("virtctl", "ns", "vm1"),
                         ["virtctl", "--namespace", "ns", "port-forward", "--stdio=true", "vmi/vm1", "22"])


if __name__ == "__main__":
    unittest.main()
