import os
import shlex
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from builderdash import ssh_config


class SshConfigTextTests(unittest.TestCase):
    def test_text(self):
        text = ssh_config.ssh_config_text(
            "builderdash-x", "rocky", "/home/u/.ssh/key",
            ["/usr/local/bin/virtctl", "--kubeconfig", "/home/u/my kube", "--context", "ctx", "--namespace",
             "omnibond", "port-forward", "--stdio=true", "vmi/builderdash-x", "22"])
        self.assertIn("Host builderdash-x\n", text)
        self.assertIn("  User rocky\n", text)
        self.assertIn("  IdentityFile /home/u/.ssh/key\n", text)
        self.assertIn("  StrictHostKeyChecking no\n", text)
        self.assertIn("  UserKnownHostsFile /dev/null\n", text)
        line = [l for l in text.splitlines() if l.startswith("  ProxyCommand ")][0]
        argv = shlex.split(line[len("  ProxyCommand "):])
        self.assertEqual(argv[2], "/home/u/my kube")
        self.assertEqual(argv[-2:], ["vmi/builderdash-x", "22"])

    def test_percent_and_spaces_are_escaped(self):
        text = ssh_config.ssh_config_text("a", "u", "/k dir/%key", ["v", "100%"])
        self.assertIn('  IdentityFile "/k dir/%%key"\n', text)
        self.assertIn("  ProxyCommand v 100%%\n", text)

    def test_no_identity_when_unset(self):
        self.assertNotIn("IdentityFile", ssh_config.ssh_config_text("a", "u", None, ["v"]))

    def test_write_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "b.ssh_config")
            ssh_config.write_ssh_config(path, "Host a\n")
            os.chmod(path, 0o644)
            ssh_config.write_ssh_config(path, "Host b\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path) as f:
                self.assertEqual(f.read(), "Host b\n")

    def test_paths(self):
        build = SimpleNamespace(instancename="builderdash-x")
        self.assertEqual(ssh_config.ssh_config_path(build), os.path.join(os.getcwd(), "builderdash-x.ssh_config"))
        build.k8s_ssh_config_path = "~/c/x.conf"
        self.assertEqual(ssh_config.ssh_config_path(build), os.path.expanduser("~/c/x.conf"))
        self.assertEqual(ssh_config.ssh_alias(SimpleNamespace(instancename="n1")), "builderdash-n1")


class WriteForBuildTests(unittest.TestCase):
    def setUp(self):
        import builderdash.main as main
        self.main = main
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, **kw):
        attrs = dict(instancename="builderdash-x", k8s_namespace="omnibond", sshkeyuser="rocky",
                     sshkey="/home/u/.ssh/key", build_host_ssh_port=22, k8s_virtctl_bin="/usr/local/bin/virtctl",
                     k8s_kubeconfig_path="/kc", k8s_kubeconfig_context="ctx",
                     k8s_ssh_config_path=os.path.join(self.tmp.name, "x.ssh_config"),
                     k8s_ssh_transport_resolved="virtctl")
        attrs.update(kw)
        return SimpleNamespace(**attrs)

    def test_virtctl_writes_the_same_proxy_command(self):
        build = self.build()
        with self.assertLogs(level="INFO") as logs:
            path = self.main.kubevirt_write_ssh_config(build)
        self.assertEqual(path, build.k8s_ssh_config_file)
        with open(path) as f:
            text = f.read()
        self.assertIn("  ProxyCommand /usr/local/bin/virtctl --kubeconfig /kc --context ctx --namespace omnibond "
                      "port-forward --stdio=true vmi/builderdash-x 22\n", text)
        self.assertIn(f"ssh -F {path} builderdash-x", "\n".join(logs.output))
        self.assertNotIn("PRIVATE KEY", text)

    def test_other_transports_write_nothing(self):
        for transport in ("kubectl", "direct", "proxy"):
            build = self.build(k8s_ssh_transport_resolved=transport)
            self.assertIsNone(self.main.kubevirt_write_ssh_config(build))
            self.assertIsNone(build.k8s_ssh_config_file)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_write_error_is_a_warning(self):
        build = self.build()
        with mock.patch.object(self.main, "write_ssh_config", side_effect=OSError("read-only")), \
                self.assertLogs(level="WARNING"):
            self.assertIsNone(self.main.kubevirt_write_ssh_config(build))


if __name__ == "__main__":
    unittest.main()
