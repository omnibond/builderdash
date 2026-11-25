import unittest

import yaml

from builderdash import kubevirt_operations as ops
from tests.helpers import make_kubevirt_build


class VmManifestTests(unittest.TestCase):
    def test_backward_compatible_namespaced_instancetype_and_run_strategy(self):
        vm = ops.generate_vm_manifest(make_kubevirt_build())
        self.assertEqual(vm["spec"]["instancetype"],
                         {"kind": "VirtualMachineInstancetype", "name": "my-instancetype"})
        self.assertEqual(vm["spec"]["runStrategy"], "Always")
        self.assertNotIn("running", vm["spec"])
        self.assertNotIn("dataVolumeTemplates", vm["spec"])
        volumes = {v["name"]: v for v in vm["spec"]["template"]["spec"]["volumes"]}
        self.assertEqual(volumes["root-disk"]["dataVolume"]["name"], "builderdash-nrp-control-20261119-abcd")
        for key in ("affinity", "tolerations", "nodeSelector"):
            self.assertNotIn(key, vm["spec"]["template"]["spec"])

    def test_rendered_yaml_round_trips(self):
        build = make_kubevirt_build()
        self.assertEqual(yaml.safe_load(ops.generate_rendered_vm_yaml_manifest(build)),
                         ops.generate_vm_manifest(build))

    def test_default_is_cluster_u1_xlarge(self):
        for value in (None, "", "null"):
            vm = ops.generate_vm_manifest(make_kubevirt_build(instancetype=value))
            self.assertEqual(vm["spec"]["instancetype"],
                             {"kind": "VirtualMachineClusterInstancetype", "name": "u1.xlarge"}, msg=repr(value))

    def test_cluster_instancetype_kind_and_aliases(self):
        for kind in ("VirtualMachineClusterInstancetype", "cluster", "CLUSTER"):
            vm = ops.generate_vm_manifest(make_kubevirt_build(instancetype="u1.2xlarge", k8s_instancetype_kind=kind))
            self.assertEqual(vm["spec"]["instancetype"],
                             {"kind": "VirtualMachineClusterInstancetype", "name": "u1.2xlarge"})
        vm = ops.generate_vm_manifest(make_kubevirt_build(k8s_instancetype_kind="namespaced"))
        self.assertEqual(vm["spec"]["instancetype"]["kind"], "VirtualMachineInstancetype")

    def test_invalid_instancetype_kind(self):
        with self.assertRaises(ValueError):
            ops.generate_vm_manifest(make_kubevirt_build(k8s_instancetype_kind="Bogus"))

    def test_preference(self):
        vm = ops.generate_vm_manifest(make_kubevirt_build(k8s_preference="almalinux"))
        self.assertEqual(vm["spec"]["preference"], {"kind": "VirtualMachineClusterPreference", "name": "almalinux"})
        vm = ops.generate_vm_manifest(make_kubevirt_build(k8s_preference="p", k8s_preference_kind="namespaced"))
        self.assertEqual(vm["spec"]["preference"]["kind"], "VirtualMachinePreference")
        self.assertNotIn("preference", ops.generate_vm_manifest(make_kubevirt_build())["spec"])

    def test_explicit_resources_without_instancetype(self):
        resources = {"requests": {"cpu": "8", "memory": "16Gi", "ephemeral-storage": "10Gi"},
                     "limits": {"cpu": "8", "memory": "16Gi", "ephemeral-storage": "10Gi"}}
        vm = ops.generate_vm_manifest(make_kubevirt_build(instancetype=None, k8s_resources=resources,
                                                          k8s_cpu_cores=8))
        self.assertNotIn("instancetype", vm["spec"])
        domain = vm["spec"]["template"]["spec"]["domain"]
        self.assertEqual(domain["resources"], resources)
        self.assertEqual(domain["cpu"], {"cores": 8})

    def test_resources_without_instancetype_need_memory(self):
        with self.assertRaises(ValueError):
            ops.generate_vm_manifest(make_kubevirt_build(
                instancetype=None, k8s_resources={"requests": {"cpu": "2"}}))

    def test_ephemeral_storage_with_instancetype(self):
        resources = {"requests": {"ephemeral-storage": "5Gi"}, "limits": {"ephemeral-storage": "5Gi"}}
        vm = ops.generate_vm_manifest(make_kubevirt_build(instancetype="u1.xlarge", k8s_instancetype_kind="cluster",
                                                          k8s_resources=resources))
        self.assertEqual(vm["spec"]["instancetype"]["name"], "u1.xlarge")
        self.assertEqual(vm["spec"]["template"]["spec"]["domain"]["resources"], resources)

    def test_invalid_resources_shape(self):
        with self.assertRaises(ValueError):
            ops.generate_vm_manifest(make_kubevirt_build(k8s_resources={"cpu": "2"}))

    def test_placement_passthrough(self):
        node_affinity = {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "nvidia.com/gpu.product", "operator": "In",
                                   "values": ["NVIDIA-A10"]}]}]}}
        tolerations = [{"key": "nautilus.io/reservation", "operator": "Exists", "effect": "PreferNoSchedule"}]
        vm = ops.generate_vm_manifest(make_kubevirt_build(
            k8s_node_affinity=node_affinity, k8s_tolerations=tolerations, k8s_node_selector={"zone": "unl"}))
        tspec = vm["spec"]["template"]["spec"]
        self.assertEqual(tspec["affinity"], {"nodeAffinity": node_affinity})
        self.assertEqual(tspec["tolerations"], tolerations)
        self.assertEqual(tspec["nodeSelector"], {"zone": "unl"})

    def test_full_affinity_passthrough(self):
        affinity = {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}}
        vm = ops.generate_vm_manifest(make_kubevirt_build(k8s_affinity=affinity))
        self.assertEqual(vm["spec"]["template"]["spec"]["affinity"], affinity)

    def test_tolerations_must_be_list(self):
        with self.assertRaises(ValueError):
            ops.generate_vm_manifest(make_kubevirt_build(k8s_tolerations={"key": "x"}))

    def test_labels_on_vm_and_template(self):
        vm = ops.generate_vm_manifest(make_kubevirt_build(k8s_labels={"eureka.omnibond.com/build": "nrp-rel10"}))
        expected = {
            "app.kubernetes.io/managed-by": "builderdash",
            "builderdash.omnibond.com/build": "control",
            "builderdash.omnibond.com/run-id": "run123",
            "builderdash.omnibond.com/image": "builderdash-nrp-control-20261119-abcd",
            "eureka.omnibond.com/build": "nrp-rel10",
        }
        self.assertEqual(vm["metadata"]["labels"], expected)
        self.assertEqual(vm["spec"]["template"]["metadata"]["labels"], expected)

    def test_run_id_generated_once(self):
        build = make_kubevirt_build(k8s_build_run_id=None)
        first = ops.get_build_run_id(build)
        self.assertTrue(first)
        self.assertEqual(ops.get_build_run_id(build), first)

    def test_label_value_sanitized(self):
        value = ops.k8s_label_value("-" + "a" * 70 + "/x")
        self.assertLessEqual(len(value), 63)
        self.assertTrue(value[0].isalnum() and value[-1].isalnum())

    def test_password_redacted_for_logs(self):
        rendered = ops.generate_rendered_vm_yaml_manifest(make_kubevirt_build())
        self.assertIn("s3cret-pass", rendered)
        redacted = ops.redact_manifest_for_log(rendered)
        self.assertNotIn("s3cret-pass", redacted)
        self.assertIn("plain_text_passwd: <redacted>", redacted)


class DataVolumeManifestTests(unittest.TestCase):
    def test_defaults_are_backward_compatible(self):
        dv = ops.generate_data_volume_manifest(make_kubevirt_build())
        pvc = dv["spec"]["pvc"]
        self.assertEqual(pvc["accessModes"], ["ReadWriteOnce"])
        self.assertNotIn("volumeMode", pvc)
        self.assertEqual(pvc["storageClassName"], "rook-ceph-block-central")
        self.assertEqual(pvc["resources"]["requests"]["storage"], "35Gi")

    def test_explicit_access_modes_and_volume_mode(self):
        dv = ops.generate_data_volume_manifest(make_kubevirt_build(
            k8s_access_modes=["ReadWriteMany"], k8s_volume_mode="block"))
        self.assertEqual(dv["spec"]["pvc"]["accessModes"], ["ReadWriteMany"])
        self.assertEqual(dv["spec"]["pvc"]["volumeMode"], "Block")
        dv = ops.generate_data_volume_manifest(make_kubevirt_build(
            k8s_access_modes="ReadWriteOnce", k8s_volume_mode="Filesystem"))
        self.assertEqual(dv["spec"]["pvc"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(dv["spec"]["pvc"]["volumeMode"], "Filesystem")

    def test_invalid_volume_mode(self):
        with self.assertRaises(ValueError):
            ops.generate_data_volume_manifest(make_kubevirt_build(k8s_volume_mode="raw"))

    def test_storage_class_still_required(self):
        with self.assertRaises(ValueError):
            ops.generate_data_volume_manifest(make_kubevirt_build(k8s_storage_class=None))

    def test_dv_is_standalone_and_labelled(self):
        build = make_kubevirt_build()
        dv = ops.generate_data_volume_manifest(build)
        self.assertNotIn("ownerReferences", dv["metadata"])
        self.assertEqual(dv["metadata"]["labels"]["builderdash.omnibond.com/run-id"], "run123")
        self.assertEqual(dv["metadata"]["labels"], ops.generate_vm_manifest(build)["metadata"]["labels"])


if __name__ == "__main__":
    unittest.main()
