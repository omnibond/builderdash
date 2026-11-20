import unittest
from types import SimpleNamespace

from builderdash.ssh_target import resolve_ssh_target_hostname


class ResolveSshTargetHostnameTests(unittest.TestCase):
    def test_unset_uses_remote_ip(self):
        build = SimpleNamespace(remoteIp="10.1.2.3")
        self.assertEqual(resolve_ssh_target_hostname(build), "10.1.2.3")

    def test_empty_and_null_use_remote_ip(self):
        for value in ("", "null", None):
            build = SimpleNamespace(remoteIp="10.1.2.3", build_host_ssh_hostname=value)
            self.assertEqual(resolve_ssh_target_hostname(build), "10.1.2.3", msg=repr(value))

    def test_override_replaces_remote_ip(self):
        build = SimpleNamespace(
            remoteIp="10.244.1.9",
            build_host_ssh_hostname="127.0.0.1",
        )
        self.assertEqual(resolve_ssh_target_hostname(build), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
