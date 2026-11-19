"""Retain is opt-in, failed builds halt (never delete), successful builds delete only the VM."""
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from kubernetes.client import ApiClient
from kubernetes.client.rest import ApiException

from builderdash import kubevirt_operations as ops
from tests.helpers import make_kubevirt_build


def bound_pvc(volume_name="pvc-1234"):
    return SimpleNamespace(status=SimpleNamespace(phase="Bound"), spec=SimpleNamespace(volume_name=volume_name))


class FakeCustomObjects:
    def __init__(self, fail_create_vm=False):
        self.created = []
        self.deleted = []
        self.patched = []
        self.fail_create_vm = fail_create_vm

    def create_namespaced_custom_object(self, group, version, namespace, plural, body):
        if plural == "virtualmachines" and self.fail_create_vm:
            raise ApiException(status=422, reason="Invalid")
        self.created.append((plural, body["metadata"]["name"]))
        return body

    def delete_namespaced_custom_object(self, group, version, namespace, plural, name, body=None):
        self.deleted.append((plural, name))
        return {"status": "Success"}

    def patch_namespaced_custom_object(self, group, version, namespace, plural, name, body):
        self.patched.append((plural, name, body))
        return {}


class FakeCore:
    def __init__(self, pv_error=None):
        self.pv_error = pv_error
        self.pv_reads = 0
        self.pv_patches = 0

    def read_namespaced_persistent_volume_claim(self, name, namespace):
        return bound_pvc()

    def read_persistent_volume(self, name):
        self.pv_reads += 1
        if self.pv_error:
            raise self.pv_error
        return SimpleNamespace(spec=SimpleNamespace(persistent_volume_reclaim_policy="Delete"))

    def patch_persistent_volume(self, name, body):
        self.pv_patches += 1


class CreateVmTests(unittest.TestCase):
    def setUp(self):
        self.build = make_kubevirt_build()
        self.vm = ops.generate_vm_manifest(self.build)
        self.dv = ops.generate_data_volume_manifest(self.build)
        patches = [
            mock.patch.object(ops, "wait_for_vmi_running", return_value={"status": {"phase": "Running"}}),
            mock.patch.object(ops, "wait_for_vmi_ip", return_value="10.244.0.7"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_create(self, core, custom, **kwargs):
        return ops.create_vm_and_wait_for_ip(core, custom, "omnibond", self.build.instancename,
                                             self.vm, self.dv, timeout=60, interval=0, **kwargs)

    def test_default_does_not_touch_cluster_scoped_pv(self):
        core, custom = FakeCore(pv_error=AssertionError("PV must not be read")), FakeCustomObjects()
        self.assertEqual(self.run_create(core, custom), "10.244.0.7")
        self.assertEqual(core.pv_reads, 0)
        self.assertEqual(custom.created, [("datavolumes", self.build.instancename),
                                          ("virtualmachines", self.build.instancename)])

    def test_opt_in_retain_patches_pv(self):
        core, custom = FakeCore(), FakeCustomObjects()
        self.assertEqual(self.run_create(core, custom, retain_root_volume=True), "10.244.0.7")
        self.assertEqual(core.pv_patches, 1)

    def test_opt_in_retain_forbidden_warns_and_continues(self):
        for status in (401, 403):
            core, custom = FakeCore(pv_error=ApiException(status=status)), FakeCustomObjects()
            with self.assertLogs(level=logging.WARNING) as logs:
                self.assertEqual(self.run_create(core, custom, retain_root_volume=True), "10.244.0.7")
            self.assertIn("continuing without Retain", "\n".join(logs.output))
            self.assertEqual(custom.deleted, [])

    def test_opt_in_retain_other_error_fails_without_deleting(self):
        core, custom = FakeCore(pv_error=ApiException(status=500)), FakeCustomObjects()
        self.assertIsNone(self.run_create(core, custom, retain_root_volume=True))
        self.assertEqual(custom.deleted, [])

    def test_vm_create_failure_keeps_data_volume(self):
        core, custom = FakeCore(), FakeCustomObjects(fail_create_vm=True)
        self.assertIsNone(self.run_create(core, custom))
        self.assertEqual(custom.created, [("datavolumes", self.build.instancename)])
        self.assertEqual(custom.deleted, [])

    def test_no_vm_deletion_on_any_failure_path(self):
        core, custom = FakeCore(), FakeCustomObjects()
        with mock.patch.object(ops, "wait_for_vmi_ip", return_value=None):
            self.assertIsNone(self.run_create(core, custom))
        self.assertEqual(custom.deleted, [])


class ImageSurvivesVmDeletionTests(unittest.TestCase):
    def test_image_dv_not_owned_by_vm(self):
        build = make_kubevirt_build()
        vm = ops.generate_vm_manifest(build)
        dv = ops.generate_data_volume_manifest(build)
        # Not a dataVolumeTemplate (which the VM would own and garbage-collect) ...
        self.assertNotIn("dataVolumeTemplates", vm["spec"])
        # ... and no ownerReference that would cascade a VM delete to the DV/PVC.
        self.assertNotIn("ownerReferences", dv["metadata"])
        self.assertEqual(dv["kind"], "DataVolume")

    def test_delete_vm_deletes_only_the_vm(self):
        custom = FakeCustomObjects()
        ops.delete_vm(custom, "omnibond", "builderdash-x")
        self.assertEqual(custom.deleted, [("virtualmachines", "builderdash-x")])


class StopVmTests(unittest.TestCase):
    def test_stop_halts_and_clears_deprecated_running(self):
        custom = FakeCustomObjects()
        ops.stop_vmi(custom, "omnibond", "builderdash-x")
        (plural, name, body), = custom.patched
        self.assertEqual((plural, name), ("virtualmachines", "builderdash-x"))
        self.assertEqual(body, {"spec": {"running": None, "runStrategy": "Halted"}})
        # The client must send running as JSON null (merge-patch removes the field).
        self.assertEqual(ApiClient().sanitize_for_serialization(body),
                         {"spec": {"running": None, "runStrategy": "Halted"}})
        self.assertEqual(custom.deleted, [])


class FailedBuildHandlingTests(unittest.TestCase):
    def setUp(self):
        import builderdash.main as main
        self.main = main
        self.custom = FakeCustomObjects()
        self.build = make_kubevirt_build(k8s_custom_objects_api=self.custom, k8s_kubeconfig_path="~/.kube/nrp",
                                         k8s_kubeconfig_context="nautilus", env_provider=main.EnvProvider.K8S_VM)

    def test_failed_build_halts_vm_keeps_everything_and_logs_help(self):
        forward = mock.Mock()
        self.build.k8s_port_forward = forward
        with mock.patch.object(self.main, "get_vm", return_value={"metadata": {}}), \
                self.assertLogs(level=logging.ERROR) as logs:
            self.main.kubevirt_stop_failed_build(self.build)
        forward.stop.assert_called_once()
        self.assertEqual(self.custom.deleted, [])
        self.assertEqual(self.custom.patched[0][2]["spec"]["runStrategy"], "Halted")
        text = "\n".join(logs.output)
        name = self.build.instancename
        self.assertIn(f"builder VM:          omnibond/{name} (stopped (runStrategy: Halted))", text)
        self.assertIn(f"virtctl start {name} -n omnibond", text)
        self.assertIn(f"delete vm {name}", text)
        self.assertIn(f"delete dv {name}", text)
        self.assertIn("--context nautilus", text)
        self.assertIn("builderdash.omnibond.com/run-id=run123", text)

    def test_failure_handling_is_idempotent(self):
        with mock.patch.object(self.main, "get_vm", return_value={"metadata": {}}), self.assertLogs(level=logging.ERROR):
            self.main.kubevirt_stop_failed_build(self.build)
            self.main.kubevirt_stop_failed_build(self.build)
        self.assertEqual(len(self.custom.patched), 1)

    def test_missing_vm_is_not_patched(self):
        with mock.patch.object(self.main, "get_vm", return_value=None), self.assertLogs(level=logging.ERROR):
            self.main.kubevirt_stop_failed_build(self.build)
        self.assertEqual(self.custom.patched, [])
        self.assertEqual(self.custom.deleted, [])

    def test_stop_instance_for_kubevirt_halts_not_deletes(self):
        with mock.patch.object(self.main, "get_vm", return_value={"metadata": {}}), self.assertLogs(level=logging.ERROR):
            self.main.stopInstance(self.build)
        self.assertEqual(len(self.custom.patched), 1)
        self.assertEqual(self.custom.deleted, [])

    def test_successful_build_delete_instance_keeps_image(self):
        forward = mock.Mock()
        self.build.k8s_port_forward = forward
        self.main.deleteInstance({}, self.build)
        forward.stop.assert_called_once()
        self.assertEqual(self.custom.deleted, [("virtualmachines", self.build.instancename)])
        self.assertTrue(self.build.k8s_vm_deleted)

    def _config_file(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write("- init:\n  - cloudservice: kubevirt\n")
        self.addCleanup(os.remove, path)
        return path

    def test_run_build_halts_on_sys_exit(self):
        path = self._config_file()
        with mock.patch.object(self.main, "processInitSection", side_effect=SystemExit(1)), \
                mock.patch.object(self.main, "kubevirt_stop_failed_build") as stop_failed, \
                mock.patch.object(self.main, "kubevirt_stop_port_forward") as stop_forward:
            with self.assertRaises(SystemExit) as cm:
                self.main.runBuild(True, self.build, None, path)
        self.assertEqual(cm.exception.code, 1)
        stop_failed.assert_called_once_with(self.build)
        stop_forward.assert_called_once_with(self.build)

    def test_run_build_exception_halts_and_exits_nonzero(self):
        path = self._config_file()
        with mock.patch.object(self.main, "processInitSection", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.main, "stopInstance") as stop_instance, \
                self.assertLogs(level=logging.ERROR):
            with self.assertRaises(SystemExit) as cm:
                self.main.runBuild(True, self.build, None, path)
        self.assertEqual(cm.exception.code, 1)
        stop_instance.assert_called_once_with(self.build)


if __name__ == "__main__":
    unittest.main()
