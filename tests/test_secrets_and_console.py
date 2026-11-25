import logging
import unittest
from unittest import mock

import yaml

from builderdash import kubevirt_operations as ops
from builderdash import main as bd_main
from builderdash import ssher
from builderdash.redaction import REDACTED, is_secret_key, redact_mapping

from tests.helpers import make_kubevirt_build


def user_data(build):
    vm = ops.generate_vm_manifest(build)
    volumes = vm["spec"]["template"]["spec"]["volumes"]
    cloud_init = [v for v in volumes if "cloudInitNoCloud" in v][0]["cloudInitNoCloud"]["userData"]
    return cloud_init, yaml.safe_load(cloud_init)["users"][0]


class OptionalPasswordTests(unittest.TestCase):
    def test_unset_password_locks_the_account(self):
        for value in (None, "", "  ", "null", "None"):
            with self.subTest(value=value):
                text, user = user_data(make_kubevirt_build(kubevirt_plain_text_passwd=value))
                self.assertIs(user["lock_passwd"], True)
                self.assertNotIn("plain_text_passwd", user)
                self.assertNotIn("plain_text_passwd", text)
                self.assertNotIn("None", text)
                self.assertEqual(user["ssh_authorized_keys"], ["ssh-ed25519 AAAATEST builder@test"])

    def test_missing_password_attribute(self):
        build = make_kubevirt_build()
        del build.kubevirt_plain_text_passwd
        _, user = user_data(build)
        self.assertIs(user["lock_passwd"], True)
        self.assertNotIn("plain_text_passwd", user)

    def test_set_password_is_dev_only_and_warned(self):
        with self.assertLogs(level="WARNING") as logs:
            _, user = user_data(make_kubevirt_build(kubevirt_plain_text_passwd='p@ss: "x" #1'))
        self.assertIs(user["lock_passwd"], False)
        self.assertEqual(user["plain_text_passwd"], 'p@ss: "x" #1')
        self.assertTrue(any("development only" in m for m in logs.output))
        self.assertFalse(any('p@ss' in m for m in logs.output))

    def test_set_password_redacted_in_rendered_log(self):
        rendered = ops.generate_rendered_vm_yaml_manifest(make_kubevirt_build(kubevirt_plain_text_passwd="pw-123"))
        self.assertNotIn("pw-123", ops.redact_manifest_for_log(rendered))


class LogSerialConsoleTests(unittest.TestCase):
    def devices(self, **kw):
        return ops.generate_vm_manifest(make_kubevirt_build(**kw))["spec"]["template"]["spec"]["domain"]["devices"]

    def test_default_leaves_kubevirt_default(self):
        self.assertNotIn("logSerialConsole", self.devices())
        self.assertNotIn("logSerialConsole", self.devices(k8s_log_serial_console=None))

    def test_false_and_true(self):
        self.assertIs(self.devices(k8s_log_serial_console=False)["logSerialConsole"], False)
        self.assertIs(self.devices(k8s_log_serial_console="false")["logSerialConsole"], False)
        self.assertIs(self.devices(k8s_log_serial_console=True)["logSerialConsole"], True)
        # interfaces and disks are kept
        self.assertIn("interfaces", self.devices(k8s_log_serial_console=False))


class RedactionTests(unittest.TestCase):
    def test_keys(self):
        for key in ("kubevirt_plain_text_passwd", "target_password", "proxy_passphrase", "api_token", "client_secret"):
            self.assertTrue(is_secret_key(key), key)
        for key in ("sshkey", "pubkeypath", "instancename", "k8s_kubeconfig_path"):
            self.assertFalse(is_secret_key(key), key)

    def test_mapping(self):
        out = redact_mapping({"proxy_password": "pw", "proxy_passphrase": None, "proxy_hostname": "h",
                              "nested": {"password": "x"}})
        self.assertEqual(out, {"proxy_password": REDACTED, "proxy_passphrase": None, "proxy_hostname": "h",
                               "nested": {"password": REDACTED}})

    def test_build_setup_does_not_log_password(self):
        section = {"start": [{"cloudservice": "aws"}, {"ostype": "almalinux"}, {"buildtype": "control"},
                             {"kubevirt_plain_text_passwd": "very-secret-pw"}, {"instancename": "x"}]}
        build = bd_main.Build()
        with self.assertLogs(level="INFO") as logs:
            build.setup(section)
        self.assertEqual(build.kubevirt_plain_text_passwd, "very-secret-pw")
        joined = "\n".join(logs.output)
        self.assertNotIn("very-secret-pw", joined)
        self.assertIn("kubevirt_plain_text_passwd: <redacted>", joined)
        self.assertIn("instancename: x", joined)

    def test_ssh_connection_logs_redacted(self):
        conn = ssher.SSHConnection("10.0.0.1", target_username="u", target_password="tpw", target_passphrase="tpp",
                                   proxy_hostname="jump", proxy_password="ppw", proxy_passphrase="ppp")
        fake = mock.MagicMock()
        with mock.patch.object(ssher.paramiko, "SSHClient", return_value=fake), \
                self.assertLogs(ssher.logger, level="INFO") as logs:
            conn._SSHConnection__connect_proxy()
            conn.proxy_to_target_channel = object()
            conn._SSHConnection__connect_target()
        joined = "\n".join(logs.output)
        for secret in ("tpw", "tpp", "ppw", "ppp"):
            self.assertNotIn(f"'{secret}'", joined)
        self.assertIn(REDACTED, joined)
        # the real values still reach paramiko
        kwargs = [c.kwargs for c in fake.connect.call_args_list]
        self.assertEqual(kwargs[0]["password"], "ppw")
        self.assertEqual(kwargs[1]["password"], "tpw")


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
