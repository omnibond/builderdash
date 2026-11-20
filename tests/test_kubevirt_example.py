import os
import tempfile
import unittest
from types import SimpleNamespace

import yaml

from builderdash import kubevirt_operations as ops
from builderdash.ssh_target import resolve_ssh_transport

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "startKubeVirt.yaml.example")


class KubeVirtExampleTests(unittest.TestCase):
    def test_example_renders(self):
        with open(EXAMPLE) as f:
            config = yaml.safe_load(f)
        attrs = {}
        for option in config[0]["start"]:
            attrs.update(option)
        fd, pub = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.remove, pub)
        attrs.update(pubkeypath=pub, instancename="builderdash-nrp-rel10-control-20261119-abcd",
                     k8s_build_run_id="r1")
        build = SimpleNamespace(**attrs)
        vm = ops.generate_vm_manifest(build)
        dv = ops.generate_data_volume_manifest(build)
        self.assertEqual(vm["spec"]["instancetype"],
                         {"kind": "VirtualMachineClusterInstancetype", "name": "u1.xlarge"})
        self.assertIn("nodeAffinity", vm["spec"]["template"]["spec"]["affinity"])
        self.assertEqual(vm["spec"]["template"]["spec"]["tolerations"][0]["effect"], "PreferNoSchedule")
        self.assertEqual(vm["spec"]["template"]["spec"]["domain"]["resources"]["limits"]["ephemeral-storage"], "5Gi")
        self.assertEqual(dv["spec"]["pvc"]["volumeMode"], "Filesystem")
        self.assertEqual(dv["spec"]["pvc"]["storageClassName"], "rook-ceph-block-central")
        self.assertEqual(vm["metadata"]["labels"]["eureka.omnibond.com/build"], "nrp-rel10")
        self.assertEqual(resolve_ssh_transport(build, is_kubevirt=True), "virtctl")
        self.assertIs(vm["spec"]["template"]["spec"]["domain"]["devices"]["logSerialConsole"], False)
        self.assertFalse(hasattr(build, "kubevirt_plain_text_passwd"))
        user_data = vm["spec"]["template"]["spec"]["volumes"][0]["cloudInitNoCloud"]["userData"]
        self.assertIn("lock_passwd: true", user_data)
        self.assertNotIn("plain_text_passwd", user_data)
        # k8s_annotations is commented out in the example: no annotations anywhere.
        self.assertNotIn("annotations", vm["metadata"])
        self.assertNotIn("annotations", vm["spec"]["template"]["metadata"])
        self.assertNotIn("annotations", dv["metadata"])


if __name__ == "__main__":
    unittest.main()
