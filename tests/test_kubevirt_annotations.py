import logging
import unittest
from unittest import mock

from builderdash import kubevirt_operations as ops
from tests.helpers import make_kubevirt_build


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


class AnnotationsUnsetTests(unittest.TestCase):
    def test_unset_renders_exactly_as_before(self):
        base = make_kubevirt_build()
        expected_vm = ops.generate_rendered_vm_yaml_manifest(base)
        expected_dv = ops.generate_data_volume_manifest(base)
        for value in (None, {}, {"all": None}, {"vmi": {}}, {"all": {}, "datavolume": {}}):
            build = make_kubevirt_build(k8s_annotations=value)
            self.assertEqual(ops.generate_rendered_vm_yaml_manifest(build), expected_vm, msg=repr(value))
            self.assertEqual(ops.generate_data_volume_manifest(build), expected_dv, msg=repr(value))

    def test_no_annotations_key_when_unset(self):
        build = make_kubevirt_build()
        self.assertNotIn("annotations", list(_walk_keys(ops.generate_vm_manifest(build))))
        self.assertNotIn("annotations", list(_walk_keys(ops.generate_data_volume_manifest(build))))
        for kind in ops.ANNOTATION_KINDS:
            self.assertEqual(ops.get_build_annotations(build, kind), {})


class AnnotationsPerKindTests(unittest.TestCase):
    CONFIG = {
        "datavolume": {"cdi.kubevirt.io/storage.bind.immediate.requested": "true"},
        "virtualmachine": {"example.com/owner": "eureka"},
        "vmi": {"example.com/pod": "launcher"},
        "service": {"example.com/svc": "headless"},
    }

    def test_datavolume(self):
        dv = ops.generate_data_volume_manifest(make_kubevirt_build(k8s_annotations=self.CONFIG))
        self.assertEqual(dv["metadata"]["annotations"],
                         {"cdi.kubevirt.io/storage.bind.immediate.requested": "true"})

    def test_virtualmachine(self):
        vm = ops.generate_vm_manifest(make_kubevirt_build(k8s_annotations=self.CONFIG))
        self.assertEqual(vm["metadata"]["annotations"], {"example.com/owner": "eureka"})

    def test_vmi_template(self):
        vm = ops.generate_vm_manifest(make_kubevirt_build(k8s_annotations=self.CONFIG))
        self.assertEqual(vm["spec"]["template"]["metadata"]["annotations"], {"example.com/pod": "launcher"})

    def test_kinds_do_not_leak(self):
        build = make_kubevirt_build(k8s_annotations={"vmi": {"example.com/only": "vmi"}})
        vm = ops.generate_vm_manifest(build)
        self.assertNotIn("annotations", vm["metadata"])
        self.assertNotIn("annotations", ops.generate_data_volume_manifest(build)["metadata"])
        self.assertEqual(vm["spec"]["template"]["metadata"]["annotations"], {"example.com/only": "vmi"})

    def test_service(self):
        build = make_kubevirt_build(k8s_annotations=self.CONFIG)
        api = mock.Mock()
        ops.create_subdomain_headless_service(api, "omnibond", "sub",
                                              annotations=ops.get_build_annotations(build, "service"))
        body = api.create_namespaced_service.call_args.kwargs["body"]
        self.assertEqual(body.metadata.annotations, {"example.com/svc": "headless"})

    def test_service_without_annotations(self):
        api = mock.Mock()
        ops.create_subdomain_headless_service(api, "omnibond", "sub")
        self.assertIsNone(api.create_namespaced_service.call_args.kwargs["body"].metadata.annotations)

    def test_kind_aliases_case_insensitive(self):
        build = make_kubevirt_build(k8s_annotations={
            "DV": {"a.example/dv": "1"}, "VM": {"a.example/vm": "1"},
            "VirtualMachineInstance": {"a.example/vmi": "1"}, "svc": {"a.example/svc": "1"},
            "common": {"a.example/all": "1"}})
        self.assertEqual(ops.get_build_annotations(build, "datavolume"),
                         {"a.example/all": "1", "a.example/dv": "1"})
        self.assertEqual(ops.get_build_annotations(build, "virtualmachine"),
                         {"a.example/all": "1", "a.example/vm": "1"})
        self.assertEqual(ops.get_build_annotations(build, "vmi"), {"a.example/all": "1", "a.example/vmi": "1"})
        self.assertEqual(ops.get_build_annotations(build, "service"), {"a.example/all": "1", "a.example/svc": "1"})


class AnnotationsMergeTests(unittest.TestCase):
    def test_all_applies_to_every_kind(self):
        build = make_kubevirt_build(k8s_annotations={"all": {"example.com/team": "omnibond"}})
        vm = ops.generate_vm_manifest(build)
        dv = ops.generate_data_volume_manifest(build)
        for meta in (vm["metadata"], vm["spec"]["template"]["metadata"], dv["metadata"]):
            self.assertEqual(meta["annotations"], {"example.com/team": "omnibond"})
        self.assertEqual(ops.get_build_annotations(build, "service"), {"example.com/team": "omnibond"})

    def test_per_kind_wins_over_all(self):
        build = make_kubevirt_build(k8s_annotations={
            "all": {"example.com/team": "omnibond", "example.com/tier": "dev"},
            "datavolume": {"example.com/tier": "image"}})
        dv = ops.generate_data_volume_manifest(build)
        self.assertEqual(dv["metadata"]["annotations"], {"example.com/team": "omnibond", "example.com/tier": "image"})
        vm = ops.generate_vm_manifest(build)
        self.assertEqual(vm["metadata"]["annotations"], {"example.com/team": "omnibond", "example.com/tier": "dev"})

    def test_labels_untouched(self):
        build = make_kubevirt_build(k8s_annotations={"all": {"example.com/x": "y"}})
        self.assertEqual(ops.generate_vm_manifest(build)["metadata"]["labels"],
                         ops.generate_vm_manifest(make_kubevirt_build())["metadata"]["labels"])

    def test_config_not_mutated(self):
        config = {"all": {"example.com/a": "1"}, "vm": {"example.com/b": "2"}}
        ops.generate_vm_manifest(make_kubevirt_build(k8s_annotations=config))
        self.assertEqual(config, {"all": {"example.com/a": "1"}, "vm": {"example.com/b": "2"}})


class AnnotationsPrecedenceTests(unittest.TestCase):
    def test_builderdash_required_annotation_wins_and_warns_without_value(self):
        required = {kind: {} for kind in ops.ANNOTATION_KINDS}
        required["virtualmachine"] = {"builderdash.omnibond.com/required": "bd-value"}
        build = make_kubevirt_build(kubevirt_plain_text_passwd=None, k8s_annotations={
            "all": {"builderdash.omnibond.com/required": "caller-secret-value"},
            "virtualmachine": {"example.com/keep": "yes"}})
        with mock.patch.object(ops, "BUILDERDASH_ANNOTATIONS", required), \
                self.assertLogs(level=logging.WARNING) as logs:
            vm = ops.generate_vm_manifest(build)
        self.assertEqual(vm["metadata"]["annotations"],
                         {"builderdash.omnibond.com/required": "bd-value", "example.com/keep": "yes"})
        # The vmi kind is not managed here, so the caller value stays there.
        self.assertEqual(vm["spec"]["template"]["metadata"]["annotations"],
                         {"builderdash.omnibond.com/required": "caller-secret-value"})
        text = "\n".join(logs.output)
        self.assertIn("builderdash.omnibond.com/required", text)
        self.assertNotIn("caller-secret-value", text)
        self.assertNotIn("bd-value", text)

    def test_same_value_does_not_warn(self):
        required = {kind: {} for kind in ops.ANNOTATION_KINDS}
        required["datavolume"] = {"builderdash.omnibond.com/required": "v"}
        build = make_kubevirt_build(kubevirt_plain_text_passwd=None,
                                    k8s_annotations={"datavolume": {"builderdash.omnibond.com/required": "v"}})
        with mock.patch.object(ops, "BUILDERDASH_ANNOTATIONS", required), \
                mock.patch.object(ops.logging, "warning") as warn:
            dv = ops.generate_data_volume_manifest(build)
        warn.assert_not_called()
        self.assertEqual(dv["metadata"]["annotations"], {"builderdash.omnibond.com/required": "v"})


class AnnotationsValidationTests(unittest.TestCase):
    def _bad(self, config):
        with self.assertRaises(ValueError) as ctx:
            ops.generate_vm_manifest(make_kubevirt_build(k8s_annotations=config))
        return str(ctx.exception)

    def test_not_a_mapping(self):
        self._bad(["example.com/a=1"])
        self._bad("example.com/a=1")

    def test_unknown_kind(self):
        self.assertIn("pod", self._bad({"pod": {"example.com/a": "1"}}))
        self.assertIn("unknown resource kind", self._bad({"example.com/a": "1"}))

    def test_kind_value_must_be_mapping(self):
        self._bad({"vm": ["example.com/a"]})
        self._bad({"vm": "example.com/a"})

    def test_values_must_be_strings_and_are_not_echoed(self):
        for value in (True, 1, 1.5, ["x"], {"a": "b"}, None):
            msg = self._bad({"vmi": {"example.com/a": value}})
            self.assertIn("must be a string", msg)
        msg = self._bad({"vmi": {"example.com/a": 987654321}})
        self.assertNotIn("987654321", msg)

    def test_keys_must_be_valid_strings(self):
        for key in (1, "", "has space", "a/b/c", "-bad", "UPPER.Example/ok", "x" * 64):
            self._bad({"vm": {key: "v"}})

    def test_valid_keys(self):
        build = make_kubevirt_build(k8s_annotations={"vm": {
            "simple": "v", "example.com/name": "v", "a.b-c.d/n_a.m-e": "v", "x" * 63: "v"}})
        self.assertEqual(len(ops.generate_vm_manifest(build)["metadata"]["annotations"]), 4)

    def test_duplicate_kind_via_alias(self):
        self.assertIn("more than once", self._bad({"vm": {"a": "1"}, "virtualmachine": {"b": "2"}}))

    def test_datavolume_validates_too(self):
        with self.assertRaises(ValueError):
            ops.generate_data_volume_manifest(make_kubevirt_build(k8s_annotations={"dv": {"a": 1}}))


if __name__ == "__main__":
    unittest.main()
