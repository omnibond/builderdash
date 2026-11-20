import unittest
from types import SimpleNamespace
from unittest import mock

import paramiko

from builderdash.ssh_target import resolve_ssh_transport


class ResolveSshTransportTests(unittest.TestCase):
    def test_kubevirt_default_is_virtctl(self):
        self.assertEqual(resolve_ssh_transport(SimpleNamespace(), is_kubevirt=True), "virtctl")

    def test_kubectl_is_still_selectable(self):
        build = SimpleNamespace(k8s_ssh_transport="kubectl")
        self.assertEqual(resolve_ssh_transport(build, is_kubevirt=True), "kubectl")

    def test_other_providers_default_direct(self):
        self.assertEqual(resolve_ssh_transport(SimpleNamespace(), is_kubevirt=False), "direct")

    def test_existing_proxy_config_keeps_jump_host(self):
        for kubevirt in (True, False):
            build = SimpleNamespace(proxy_conf_path="~/proxy.yaml")
            self.assertEqual(resolve_ssh_transport(build, is_kubevirt=kubevirt), "proxy")

    def test_pr6_hostname_override_keeps_direct(self):
        build = SimpleNamespace(build_host_ssh_hostname="127.0.0.1")
        self.assertEqual(resolve_ssh_transport(build, is_kubevirt=True), "direct")

    def test_explicit_transport_wins(self):
        build = SimpleNamespace(k8s_ssh_transport="VirtCtl", proxy_conf_path="~/p.yaml")
        self.assertEqual(resolve_ssh_transport(build, is_kubevirt=True), "virtctl")
        build = SimpleNamespace(k8s_ssh_transport="direct")
        self.assertEqual(resolve_ssh_transport(build, is_kubevirt=True), "direct")

    def test_null_transport_is_unset(self):
        for value in (None, "", "null"):
            build = SimpleNamespace(k8s_ssh_transport=value)
            self.assertEqual(resolve_ssh_transport(build, is_kubevirt=True), "virtctl")

    def test_invalid_values(self):
        with self.assertRaises(ValueError):
            resolve_ssh_transport(SimpleNamespace(k8s_ssh_transport="telnet"), is_kubevirt=True)
        with self.assertRaises(ValueError):
            resolve_ssh_transport(SimpleNamespace(k8s_ssh_transport="kubectl"), is_kubevirt=False)
        with self.assertRaises(ValueError):
            resolve_ssh_transport(SimpleNamespace(k8s_ssh_transport="proxy"), is_kubevirt=True)


class SshConnectTests(unittest.TestCase):
    def setUp(self):
        import builderdash.main as main
        self.main = main
        self.conn = mock.Mock()
        self.conn.is_alive.return_value = True
        patcher = mock.patch.object(main, "SSHConnection", return_value=self.conn)
        self.ssh_cls = patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, **kw):
        attrs = dict(env_provider=self.main.EnvProvider.K8S_VM, remoteIp="10.244.0.7", build_host_ssh_port=22,
                     sshkeyuser="rocky", sshkey="/k", instancename="builderdash-x", k8s_namespace="omnibond")
        attrs.update(kw)
        return SimpleNamespace(**attrs)

    def test_kubectl_uses_local_forward(self):
        self.main.ssh_connect(self.build(k8s_ssh_transport="kubectl", k8s_ssh_target=("127.0.0.1", 40022)))
        kwargs = self.ssh_cls.call_args.kwargs
        self.assertEqual((kwargs["target_hostname"], kwargs["target_port"]), ("127.0.0.1", 40022))
        self.assertIsNone(kwargs["target_sock_factory"])
        self.assertNotIn("proxy_hostname", kwargs)

    def test_proxy_jump_host_still_works(self):
        proxy_conf = dict(proxy_hostname="jump.example", proxy_port=22, proxy_username="u", proxy_key_filename="/pk",
                          proxy_timeout=None, proxy_attempt_limit=3, proxy_retry_delay=5,
                          proxy_missing_host_key_policy=None, proxy_channel_alt_src_hostname=None)
        self.main.ssh_connect(self.build(proxy_conf_path="/p.yaml", proxy_conf=proxy_conf))
        kwargs = self.ssh_cls.call_args.kwargs
        self.assertEqual(kwargs["proxy_hostname"], "jump.example")
        self.assertEqual((kwargs["target_hostname"], kwargs["target_port"]), ("10.244.0.7", 22))

    def test_direct_with_pr6_override(self):
        self.main.ssh_connect(self.build(build_host_ssh_hostname="127.0.0.1", build_host_ssh_port=2222))
        kwargs = self.ssh_cls.call_args.kwargs
        self.assertEqual((kwargs["target_hostname"], kwargs["target_port"]), ("127.0.0.1", 2222))
        self.assertIsNone(kwargs["target_sock_factory"])

    def test_virtctl_uses_proxy_command(self):
        build = self.build(k8s_ssh_transport="virtctl", k8s_virtctl_bin="/usr/local/bin/virtctl",
                           k8s_kubeconfig_path="/kc", k8s_kubeconfig_context="ctx")
        self.main.ssh_connect(build)
        kwargs = self.ssh_cls.call_args.kwargs
        factory = kwargs["target_sock_factory"]
        with mock.patch.object(paramiko, "ProxyCommand") as proxy_command:
            factory()
        self.assertEqual(proxy_command.call_args.args[0],
                         "/usr/local/bin/virtctl --kubeconfig /kc --context ctx --namespace omnibond "
                         "port-forward --stdio=true vmi/builderdash-x 22")

    def test_default_virtctl_uses_proxy_command(self):
        self.main.ssh_connect(self.build(k8s_virtctl_bin="/usr/local/bin/virtctl"))
        kwargs = self.ssh_cls.call_args.kwargs
        self.assertIsNotNone(kwargs["target_sock_factory"])
        self.assertEqual(kwargs["target_hostname"], "builderdash-x")

    def test_preflight_default_virtctl_missing_suggests_alternatives(self):
        build = self.build()
        with mock.patch.object(self.main, "find_binary", return_value=None):
            with self.assertRaises(ValueError) as caught:
                self.main.kubevirt_preflight_ssh_transport(build)
        message = str(caught.exception)
        self.assertIn("k8s_virtctl_path", message)
        self.assertIn("k8s_ssh_transport: kubectl", message)
        with mock.patch.object(self.main, "find_binary", return_value="/usr/local/bin/virtctl"):
            self.assertEqual(self.main.kubevirt_preflight_ssh_transport(build), "virtctl")
        self.assertEqual(build.k8s_virtctl_bin, "/usr/local/bin/virtctl")

    def test_preflight_requires_kubectl(self):
        build = self.build(k8s_ssh_transport="kubectl")
        with mock.patch.object(self.main, "find_binary", return_value=None):
            with self.assertRaises(ValueError):
                self.main.kubevirt_preflight_ssh_transport(build)
        with mock.patch.object(self.main, "find_binary", return_value="/usr/bin/kubectl"):
            self.assertEqual(self.main.kubevirt_preflight_ssh_transport(build), "kubectl")
        self.assertEqual(build.k8s_kubectl_bin, "/usr/bin/kubectl")

    def test_preflight_virtctl_and_proxy(self):
        build = self.build(k8s_ssh_transport="virtctl")
        with mock.patch.object(self.main, "find_binary", return_value="/usr/local/bin/virtctl"):
            self.assertEqual(self.main.kubevirt_preflight_ssh_transport(build), "virtctl")
        build = self.build(proxy_conf_path="/p.yaml")
        with mock.patch.object(self.main, "find_binary", side_effect=AssertionError("no CLI needed")):
            self.assertEqual(self.main.kubevirt_preflight_ssh_transport(build), "proxy")


class SshConnectionSockFactoryTests(unittest.TestCase):
    def test_sock_factory_used_and_closed_on_failure(self):
        from builderdash.ssher import SSHConnection
        socks = []

        def factory():
            sock = mock.Mock()
            socks.append(sock)
            return sock

        client = mock.Mock()
        client.connect.side_effect = [OSError("refused"), None]
        client.open_sftp.return_value = mock.Mock()
        with mock.patch("builderdash.ssher.paramiko.SSHClient", return_value=client):
            conn = SSHConnection("builderdash-x", 22, target_username="rocky", target_attempt_limit=2,
                                 target_retry_delay=0, target_sock_factory=factory)
            conn.connect()
        self.assertEqual(len(socks), 2)
        socks[0].close.assert_called_once()
        self.assertIs(client.connect.call_args.kwargs["sock"], socks[1])
        conn.disconnect()
        socks[1].close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
