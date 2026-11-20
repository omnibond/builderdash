import copy
import json
import logging
import os
import re
import time
import uuid
from textwrap import dedent

import yaml
from kubernetes import client as client
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

CDI_GROUP = "cdi.kubevirt.io"
CDI_VERSION = "v1beta1"
DATAVOLUME_PLURAL = "datavolumes"

_UNSET_STORAGE_CLASS_VALUES = {"", "none", "null"}

LABEL_PREFIX = "builderdash.omnibond.com"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
LABEL_BUILD = f"{LABEL_PREFIX}/build"
LABEL_RUN_ID = f"{LABEL_PREFIX}/run-id"
LABEL_IMAGE = f"{LABEL_PREFIX}/image"

INSTANCETYPE_KIND_NAMESPACED = "VirtualMachineInstancetype"
INSTANCETYPE_KIND_CLUSTER = "VirtualMachineClusterInstancetype"
PREFERENCE_KIND_NAMESPACED = "VirtualMachinePreference"
PREFERENCE_KIND_CLUSTER = "VirtualMachineClusterPreference"
# Default when a KubeVirt build sets neither instancetype nor k8s_resources:
# a cluster-wide common-instancetypes size (4 vCPU / 16Gi).
DEFAULT_CLUSTER_INSTANCETYPE = "u1.xlarge"

_INSTANCETYPE_KIND_ALIASES = {
    "namespaced": INSTANCETYPE_KIND_NAMESPACED,
    "virtualmachineinstancetype": INSTANCETYPE_KIND_NAMESPACED,
    "cluster": INSTANCETYPE_KIND_CLUSTER,
    "virtualmachineclusterinstancetype": INSTANCETYPE_KIND_CLUSTER,
}
_PREFERENCE_KIND_ALIASES = {
    "namespaced": PREFERENCE_KIND_NAMESPACED,
    "virtualmachinepreference": PREFERENCE_KIND_NAMESPACED,
    "cluster": PREFERENCE_KIND_CLUSTER,
    "virtualmachineclusterpreference": PREFERENCE_KIND_CLUSTER,
}
_VOLUME_MODES = {"filesystem": "Filesystem", "block": "Block"}


def _is_unset(value):
    return value is None or (isinstance(value, str) and value.strip().lower() in _UNSET_STORAGE_CLASS_VALUES)


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def k8s_label_value(value):
    """Coerce a string into a valid Kubernetes label value (<=63 chars, alnum at both ends)."""
    value = re.sub(r"[^A-Za-z0-9._-]", "-", str(value))[:63]
    return re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", value)


def get_build_run_id(my_build):
    run_id = getattr(my_build, "k8s_build_run_id", None)
    if _is_unset(run_id):
        run_id = uuid.uuid4().hex[:12]
        setattr(my_build, "k8s_build_run_id", run_id)
    return str(run_id)


def get_build_labels(my_build):
    """Labels put on every Kubernetes object a build creates (VM, VMI/launcher pod, DataVolume/PVC).

    Extra labels can be added with the ``k8s_labels`` mapping.
    """
    labels = {
        LABEL_MANAGED_BY: "builderdash",
        LABEL_RUN_ID: k8s_label_value(get_build_run_id(my_build)),
    }
    build_type = getattr(my_build, "buildtype", None)
    if not _is_unset(build_type):
        labels[LABEL_BUILD] = k8s_label_value(build_type)
    image = getattr(my_build, "instancename", None)
    if not _is_unset(image):
        labels[LABEL_IMAGE] = k8s_label_value(image)
    extra = getattr(my_build, "k8s_labels", None) or {}
    if not isinstance(extra, dict):
        raise ValueError("k8s_labels must be a mapping of label keys to values")
    labels.update({str(k): str(v) for k, v in extra.items()})
    return {k: v for k, v in labels.items() if v != ""}


def get_build_instancetype(my_build):
    """(kind, name) of the builder VM's instancetype, or None when explicit k8s_resources size it.

    ``instancetype`` set and ``k8s_instancetype_kind`` unset keeps the original
    namespaced VirtualMachineInstancetype. With ``instancetype`` unset (or null)
    and no ``k8s_resources``, the default is the cluster-wide common-instancetype
    ``u1.xlarge``.
    """
    name = getattr(my_build, "instancetype", None)
    kind = getattr(my_build, "k8s_instancetype_kind", None)
    if _is_unset(name):
        if getattr(my_build, "k8s_resources", None):
            return None
        name = DEFAULT_CLUSTER_INSTANCETYPE
        default_kind = INSTANCETYPE_KIND_CLUSTER
    else:
        default_kind = INSTANCETYPE_KIND_NAMESPACED
    if _is_unset(kind):
        kind = default_kind
    else:
        resolved = _INSTANCETYPE_KIND_ALIASES.get(str(kind).strip().lower())
        if resolved is None:
            raise ValueError(
                f"k8s_instancetype_kind must be {INSTANCETYPE_KIND_NAMESPACED} or "
                f"{INSTANCETYPE_KIND_CLUSTER} (or namespaced/cluster); got {kind!r}")
        kind = resolved
    return kind, str(name)


def get_build_preference(my_build):
    name = getattr(my_build, "k8s_preference", None)
    if _is_unset(name):
        return None
    kind = getattr(my_build, "k8s_preference_kind", None)
    if _is_unset(kind):
        kind = PREFERENCE_KIND_CLUSTER
    else:
        resolved = _PREFERENCE_KIND_ALIASES.get(str(kind).strip().lower())
        if resolved is None:
            raise ValueError(
                f"k8s_preference_kind must be {PREFERENCE_KIND_NAMESPACED} or "
                f"{PREFERENCE_KIND_CLUSTER} (or namespaced/cluster); got {kind!r}")
        kind = resolved
    return kind, str(name)


def get_build_access_modes(my_build):
    modes = getattr(my_build, "k8s_access_modes", None)
    if _is_unset(modes) or modes == []:
        return ["ReadWriteOnce"]
    if isinstance(modes, str):
        modes = [m.strip() for m in modes.split(",") if m.strip()]
    return [str(m) for m in modes]


def get_build_volume_mode(my_build):
    mode = getattr(my_build, "k8s_volume_mode", None)
    if _is_unset(mode):
        return None
    resolved = _VOLUME_MODES.get(str(mode).strip().lower())
    if resolved is None:
        raise ValueError(f"k8s_volume_mode must be Filesystem or Block; got {mode!r}")
    return resolved


def normalize_k8s_storage_class(value):
    if value is None:
        return None

    storage_class = str(value).strip()
    if storage_class.lower() in _UNSET_STORAGE_CLASS_VALUES:
        return None

    return storage_class


def get_build_k8s_storage_class(my_build):
    storage_class = normalize_k8s_storage_class(
        getattr(my_build, "k8s_storage_class", None)
    )
    legacy_storage_class = normalize_k8s_storage_class(
        getattr(my_build, "kubevirt_storage_class_name", None)
    )

    if storage_class and legacy_storage_class and storage_class != legacy_storage_class:
        raise ValueError(
            "Builderdash KubeVirt config defines both k8s_storage_class and legacy "
            f"kubevirt_storage_class_name with different values: {storage_class!r} != {legacy_storage_class!r}"
        )

    storage_class = storage_class or legacy_storage_class
    if storage_class is None:
        raise ValueError(
            "KubeVirt image builds require k8s_storage_class in the builderdash config. "
            "Set it to a StorageClass that exists on the target Kubernetes cluster."
        )

    return storage_class


vm_template = dedent('''\
    apiVersion: kubevirt.io/v1
    kind: VirtualMachine
    metadata:
      name: {name}
      namespace: {namespace}
      labels: {{}}
    spec:
      runStrategy: {run_strategy}
      instancetype:
        kind: {instance_type_kind}
        name: {instance_type_name}
      template:
        metadata:
          labels: {{}}
        spec:
          domain:
            devices:
              interfaces:
              - name: default
                masquerade: {{}}
                macAddress: {mac_address}
              disks:
              - name: {root_disk_name}
                disk:
                  bus: virtio
              - name: cloudinitdisk
                disk:
                  bus: virtio
          networks:
          - name: default
            pod: {{}}
          volumes:
          - name: cloudinitdisk
            cloudInitNoCloud:
              userData: |
                #cloud-config
                users:
                  - name: {ssh_user}
                    groups: sudo
                    shell: /bin/bash
                    sudo: ALL=(ALL) NOPASSWD:ALL
                    {password_lines}
                    ssh_authorized_keys:
                      - {public_key_openssh}
          - name: {root_disk_name}
            dataVolume:
              name: {pvc_name}''')


def generate_vm_template_substitution_dictionary(my_build):
    disksize = getattr(my_build, "disksize", "55")

    f = open(os.path.expanduser(my_build.pubkeypath), 'r')
    kubevirt_public_key_openssh = f.read()
    f.close()

    instancetype = get_build_instancetype(my_build)
    instance_type_kind, instance_type_name = instancetype if instancetype else (None, None)

    return {
        'name': my_build.instancename,
        'namespace': my_build.k8s_namespace,
        'labels': get_build_labels(my_build),
        'instance_state': 'true',
        'run_strategy': 'Always',
        'instance_type_kind': instance_type_kind,
        'instance_type_name': instance_type_name,
        'mac_address': 'ee:ee:ee:ee:ee:ee',
        'root_disk_name': 'root-disk',
        'ssh_user': str(my_build.sshkeyuser),
        'public_key_openssh': kubevirt_public_key_openssh,
        'pvc_name': my_build.instancename,
        'pvc_access_mode': 'ReadWriteOnce',
        'pvc_access_modes': get_build_access_modes(my_build),
        'pvc_volume_mode': get_build_volume_mode(my_build),
        'pvc_storage_capacity': disksize,
        'data_volume_source': my_build.sourceimage,
        'password_lines': cloud_init_password_lines(my_build),
    }


def get_build_plain_text_passwd(my_build):
    """The optional dev-only builder password, or None when unset/null/empty."""
    value = getattr(my_build, "kubevirt_plain_text_passwd", None)
    if value is None:
        return None
    value = str(value)
    if value.strip() == "" or value.strip().lower() in ("null", "none", "~"):
        return None
    return value


# Indent of the cloud-init user keys inside vm_template (after dedent).
_CLOUD_INIT_USER_INDENT = " " * 16


def cloud_init_password_lines(my_build):
    """cloud-init password lines for the builder user.

    Without kubevirt_plain_text_passwd the account is locked (SSH key only).
    With one, the password is set (JSON-quoted, a valid YAML scalar) for
    development only: virtctl console access when SSH setup fails.
    """
    password = get_build_plain_text_passwd(my_build)
    if password is None:
        return "lock_passwd: true"
    if not getattr(my_build, "_dev_password_warned", False):
        logging.warning("kubevirt_plain_text_passwd is set: the builder user gets a console password. "
                        "This is for development only (virtctl console testing); leave it unset otherwise.")
        setattr(my_build, "_dev_password_warned", True)
    return f"lock_passwd: false\n{_CLOUD_INIT_USER_INDENT}plain_text_passwd: {json.dumps(password)}"


def generate_vm_manifest(my_build):
    """Builder VirtualMachine manifest (dict) with labels, sizing and placement applied.

    Optional build keys: k8s_instancetype_kind, k8s_preference, k8s_preference_kind,
    k8s_resources ({requests: {...}, limits: {...}}, e.g. ephemeral-storage),
    k8s_cpu_cores, k8s_affinity, k8s_node_affinity, k8s_node_selector,
    k8s_tolerations, k8s_log_serial_console and k8s_labels.
    """
    d = generate_vm_template_substitution_dictionary(my_build)
    manifest = yaml.safe_load(vm_template.format(**d))
    spec = manifest["spec"]
    template_spec = spec["template"]["spec"]
    domain = template_spec["domain"]

    labels = d["labels"]
    manifest["metadata"]["labels"] = dict(labels)
    spec["template"]["metadata"]["labels"] = dict(labels)

    if d["instance_type_name"] is None:
        del spec["instancetype"]

    preference = get_build_preference(my_build)
    if preference:
        spec["preference"] = {"kind": preference[0], "name": preference[1]}

    resources = getattr(my_build, "k8s_resources", None)
    if resources:
        if not isinstance(resources, dict) or set(resources) - {"requests", "limits"}:
            raise ValueError("k8s_resources must be a mapping with optional 'requests' and 'limits' mappings")
        domain["resources"] = copy.deepcopy(resources)
        cpu_mem = {"cpu", "memory"}
        sized = set((resources.get("requests") or {})) | set((resources.get("limits") or {}))
        if d["instance_type_name"] is not None and sized & cpu_mem:
            logging.warning(
                "k8s_resources sets cpu/memory while instancetype %s also sizes the VM; KubeVirt may reject "
                "the VM as conflicting. Set instancetype to null to size the builder with k8s_resources only.",
                d["instance_type_name"])
        if d["instance_type_name"] is None and "memory" not in (resources.get("requests") or {}):
            raise ValueError("k8s_resources.requests.memory is required when no instancetype is used")

    cpu_cores = getattr(my_build, "k8s_cpu_cores", None)
    if not _is_unset(cpu_cores):
        domain["cpu"] = {"cores": int(cpu_cores)}

    affinity = getattr(my_build, "k8s_affinity", None)
    if affinity:
        template_spec["affinity"] = copy.deepcopy(affinity)
    node_affinity = getattr(my_build, "k8s_node_affinity", None)
    if node_affinity:
        template_spec.setdefault("affinity", {})["nodeAffinity"] = copy.deepcopy(node_affinity)
    node_selector = getattr(my_build, "k8s_node_selector", None)
    if node_selector:
        template_spec["nodeSelector"] = {str(k): str(v) for k, v in node_selector.items()}
    tolerations = getattr(my_build, "k8s_tolerations", None)
    if tolerations:
        if not isinstance(tolerations, list):
            raise ValueError("k8s_tolerations must be a list of Kubernetes tolerations")
        template_spec["tolerations"] = copy.deepcopy(tolerations)

    log_serial_console = getattr(my_build, "k8s_log_serial_console", None)
    if not _is_unset(log_serial_console):
        # false drops virt-launcher's guest-console-log sidecar (its default
        # requests/limits break namespaces that want them within 20%).
        domain.setdefault("devices", {})["logSerialConsole"] = _as_bool(log_serial_console)

    return manifest


def generate_rendered_vm_yaml_manifest(my_build):
    return yaml.safe_dump(generate_vm_manifest(my_build), sort_keys=False)


def redact_manifest_for_log(rendered_manifest):
    """Hide the cloud-init password in a rendered VM manifest before it is logged."""
    return re.sub(r"(plain_text_passwd:\s*).*", r"\1<redacted>", rendered_manifest)


def generate_data_volume_manifest(my_build):
    d = generate_vm_template_substitution_dictionary(my_build)
    pvc_spec = {
        "accessModes": list(d['pvc_access_modes']),
        "resources": {
            "requests": {
                "storage": d['pvc_storage_capacity']
            }
        }
    }

    pvc_spec["storageClassName"] = get_build_k8s_storage_class(my_build)
    if d['pvc_volume_mode']:
        pvc_spec["volumeMode"] = d['pvc_volume_mode']

    # Standalone DataVolume: no ownerReferences, so deleting the builder VM leaves
    # the image DV/PVC Bound and usable (no PV Retain patch needed).
    return {
        "apiVersion": f"{CDI_GROUP}/{CDI_VERSION}",
        "kind": "DataVolume",
        "metadata": {
            "name": d['pvc_name'],
            "namespace": d['namespace'],
            "labels": dict(d['labels']),
        },
        "spec": {
            "pvc": pvc_spec,
            "source": d['data_volume_source'],
        }
    }


def create_data_volume(custom_objects_api, namespace, manifest):
    try:
        api_response = custom_objects_api.create_namespaced_custom_object(
            group=CDI_GROUP,
            version=CDI_VERSION,
            namespace=namespace,
            plural=DATAVOLUME_PLURAL,
            body=manifest
        )
        logging.info('Standalone DataVolume created successfully')
        return api_response
    except ApiException as e:
        logging.error(f"Exception when creating standalone DataVolume: {e}")
        return None


def create_vm(custom_objects_api, namespace, manifest):
    try:
        api_response = custom_objects_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=manifest
        )
        logging.info('VM created successfully')
        return api_response
    except ApiException as e:
        logging.error(f"Exception when creating VM: {e}")
        return None


def get_vm(custom_objects_api, namespace, vm_name):
    try:
        vm = custom_objects_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=vm_name
        )
        return vm
    except ApiException as e:
        logging.warning(f"Exception when getting VM: {e}")
        return None


def get_vmi(custom_objects_api, namespace, vmi_name):
    try:
        vmi = custom_objects_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachineinstances",
            name=vmi_name
        )
        return vmi
    except ApiException as e:
        logging.warning(f"Exception when getting VMI: {e}")
        logging.info(
            "The previous exception may occur when the kubevirt VMI associated with the VM has not been created yet."
        )
        return None


def get_pvc(client_core_v1_api, namespace, pvc_name):
    try:
        return client_core_v1_api.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
    except ApiException as e:
        logging.error(f"Exception when reading namespaced PVC '{pvc_name}' from namespace '{namespace}': {e}")
        raise


def maybe_patch_pv_to_retain(client_core_v1_api, pv_name):
    """Opt-in Retain (k8s_retain_root_pv: true). Returns True if patched/already Retain.

    PVs are cluster-scoped, so namespaced accounts get 401/403; that is logged
    and the build continues, because the image PVC (a standalone DataVolume) is
    the saved artifact either way.
    """
    try:
        patch_pv_to_retain(client_core_v1_api, pv_name)
    except ApiException as e:
        if e.status in (401, 403):
            logging.warning(
                "k8s_retain_root_pv is set but this account may not read/patch cluster-scoped PV '%s' "
                "(HTTP %s); continuing without Retain. The image PVC remains the saved artifact.",
                pv_name, e.status)
            return False
        raise
    return True


def patch_pv_to_retain(client_core_v1_api, pv_name):
    try:
        pv = client_core_v1_api.read_persistent_volume(pv_name)
    except ApiException as e:
        logging.error(f"Exception when reading PV '{pv_name}' before retain patch: {e}")
        raise

    if pv.spec.persistent_volume_reclaim_policy == "Retain":
        logging.info(f"PV '{pv_name}' already has reclaim policy Retain; skipping patch.")
        return

    pv_patch = {
        "spec": {
            "persistentVolumeReclaimPolicy": "Retain"
        }
    }
    try:
        client_core_v1_api.patch_persistent_volume(pv_name, pv_patch)
        logging.info(f"Successfully patched PV '{pv_name}' to retain.")
    except ApiException as e:
        logging.error(f"Exception when patching PV '{pv_name}' to retain: {e}")
        raise


def wait_for_pvc_bound(client_core_v1_api, namespace, pvc_name, timeout, interval):
    logging.info(f"Waiting for PVC '{pvc_name}' in namespace '{namespace}' to bind...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            pvc = client_core_v1_api.read_namespaced_persistent_volume_claim(pvc_name, namespace)
        except ApiException as e:
            if e.status == 404:
                logging.info(f"PVC '{pvc_name}' does not exist yet, waiting...")
            else:
                raise
        else:
            phase = pvc.status.phase
            volume_name = pvc.spec.volume_name
            if phase == "Bound" and volume_name:
                logging.info(f"PVC '{pvc_name}' is Bound to PV '{volume_name}'.")
                return pvc
            logging.info(f"PVC '{pvc_name}' phase is '{phase}', waiting...")
        time.sleep(interval)
    logging.error(f"Timeout waiting for PVC '{pvc_name}' to bind")
    return None


def wait_for_vmi_running(custom_objects_api, namespace, vmi_name, timeout, interval):
    start_time = time.time()
    while time.time() - start_time < timeout:
        vmi = get_vmi(custom_objects_api, namespace, vmi_name)
        if vmi and vmi.get('status', {}).get('phase', 'Unknown') == 'Running':
            logging.info('kubevirt VMI status phase is "Running" for instance: %s', vmi_name)
            return vmi
        else:
            logging.info('kubevirt VMI status phase is NOT YET "Running" for instance: %s', vmi_name)
        time.sleep(interval)
    logging.error("Timeout waiting for kubevirt VMI to become ready")
    return None


def extract_ip_address(vmi):
    try:
        interfaces = vmi.get('status', {}).get('interfaces', [])
        if not interfaces:
            logging.error("No interfaces found in VMI")
            return None

        iface = interfaces[0]
        ip_address = iface.get('ipAddress')

        # Some KubeVirt versions populate ipAddresses instead / as well
        if not ip_address:
            ip_addresses = iface.get('ipAddresses') or []
            if ip_addresses:
                ip_address = ip_addresses[0]

        if not ip_address:
            logging.info("VMI has interfaces but no IP address yet")
        return ip_address
    except Exception as e:
        logging.error(f"Exception when extracting IP address: {e}")
        return None


def wait_for_vmi_ip(custom_objects_api, namespace, vmi_name, timeout, interval):
    """
    After the VMI reaches Running, there can still be a delay before
    KubeVirt/CNI assign an IP and populate status.interfaces[*].ipAddress.

    This helper polls for an IP, and returns the IP string or None on failure/timeout.
    """
    start_time = time.time()
    last_phase = None

    logging.info(
        "Waiting for VMI %s in namespace %s to acquire an IP address (timeout=%s, interval=%s)",
        vmi_name, namespace, timeout, interval,
    )

    while time.time() - start_time < timeout:
        vmi = get_vmi(custom_objects_api, namespace, vmi_name)
        if not vmi:
            # get_vmi already logged; VMI may not exist yet or have disappeared
            time.sleep(interval)
            continue

        status = vmi.get('status', {})
        phase = status.get('phase', 'Unknown')
        ip = extract_ip_address(vmi)

        if phase != last_phase or not ip:
            logging.info(
                "VMI %s poll: phase=%s ip=%s",
                vmi_name,
                phase,
                ip or "<None>",
            )
            last_phase = phase

        # If IP is present, we’re done
        if ip:
            logging.info("VMI %s acquired IP %s", vmi_name, ip)
            return ip

        # If the VMI has terminated, no point in continuing
        if phase in ("Failed", "Succeeded"):
            logging.error(
                "VMI %s reached terminal phase %s before acquiring an IP",
                vmi_name,
                phase,
            )
            return None

        time.sleep(interval)

    logging.error(
        "Timeout waiting for VMI %s to acquire an IP address",
        vmi_name,
    )
    return None


def create_vm_and_wait_for_ip(
        client_core_v1_api,
        custom_objects_api,
        k8s_namespace,
        vm_name,
        manifest,
        data_volume_manifest,
        timeout=600,          # total budget for running+IP
        interval=10,
        retain_root_volume=False,
):
    """Create the image DataVolume and the builder VM, then wait for the VMI IP.

    Returns the IP, or None on failure. Nothing is deleted on failure: the
    caller stops (halts) the builder VM and keeps the VM and DV/PVC for a dev
    to inspect.
    """
    start_time = time.time()

    data_volume = create_data_volume(custom_objects_api, k8s_namespace, data_volume_manifest)
    if not data_volume:
        logging.error('failed to create standalone DataVolume')
        return None

    vm = create_vm(custom_objects_api, k8s_namespace, manifest)
    if not vm:
        logging.error('failed to create kubevirt VM; standalone DataVolume %s/%s is kept',
                      k8s_namespace, data_volume_manifest['metadata']['name'])
        return None

    # ---- 1) Wait for the standalone DataVolume's PVC to bind ----
    remaining = timeout - (time.time() - start_time)
    logging.info(f"[builderdash] Remaining time before wait_for_pvc_bound: {remaining:.1f}s")

    if remaining <= 0:
        logging.error("Timeout expired before waiting for root PVC to bind")
        return None

    pvc = wait_for_pvc_bound(
        client_core_v1_api,
        k8s_namespace,
        vm_name,
        timeout=remaining,
        interval=interval,
    )
    if not pvc:
        logging.error('failed to get bound root PVC')
        return None

    # ---- 2) Optional (opt-in) PV Retain ----
    if retain_root_volume:
        try:
            maybe_patch_pv_to_retain(client_core_v1_api, pvc.spec.volume_name)
        except Exception:
            logging.exception("Failed to set Retain on the root PV of VM %s; failing the build", vm_name)
            return None
    else:
        logging.info(
            "Not patching PV '%s' to Retain: the image is standalone DataVolume/PVC '%s/%s', which is not "
            "owned by the builder VM and stays Bound after the VM is deleted.",
            pvc.spec.volume_name, k8s_namespace, vm_name)

    # ---- 3) Wait for VMI to be Running ----
    remaining = timeout - (time.time() - start_time)
    logging.info(f"[builderdash] Remaining time before wait_for_vmi_running: {remaining:.1f}s")

    if remaining <= 0:
        logging.error("Timeout expired before waiting for VMI to reach Running")
        return None

    vmi = wait_for_vmi_running(
        custom_objects_api,
        k8s_namespace,
        vm_name,
        timeout=remaining,
        interval=interval,
    )
    if not vmi:
        logging.error('failed to get VMI data (never reached Running)')
        return None

    # ---- 4) Wait for VMI IP ----
    remaining = timeout - (time.time() - start_time)
    logging.info(f"[builderdash] Remaining time before wait_for_vmi_ip: {remaining:.1f}s")

    if remaining <= 0:
        logging.error("Timeout expired before waiting for VMI IP")
        return None

    ip = wait_for_vmi_ip(
        custom_objects_api,
        k8s_namespace,
        vm_name,
        timeout=remaining,
        interval=interval,
    )
    logging.info(f"[builderdash] IP wait completed with remaining time: {timeout - (time.time() - start_time):.1f}s")

    return ip


def stop_vmi(custom_objects_api, k8s_namespace, vmi_name):
    """Stop (halt) the builder VM without deleting it: runStrategy Halted.

    The VM, its DataVolume and PVC are kept, so it can be started again with
    ``virtctl start``. ``running: null`` clears the deprecated field on VMs
    created by older builderdash versions (the two fields are exclusive).
    """
    try:
        response = custom_objects_api.patch_namespaced_custom_object(
            group='kubevirt.io',
            version='v1',
            namespace=k8s_namespace,
            plural='virtualmachines',
            name=vmi_name,
            body={
                "spec": {
                    "running": None,
                    "runStrategy": "Halted"
                }
            }
        )
    except ApiException as e:
        logging.error(f'Exception when stopping VMI: {vmi_name}, {e}')
        raise
    else:
        logging.info(f'Successfully stopped VMI: {vmi_name}')
        return response


stop_vm = stop_vmi


def delete_vm(custom_objects_api, k8s_namespace, vm_name):
    try:
        response = custom_objects_api.delete_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=k8s_namespace,
            plural="virtualmachines",
            name=vm_name,
            body=client.V1DeleteOptions()
        )
        status = response.get('status', 'Unknown')
        logging.info(f"VM deleted. status='{status}'")
        logging.info(f"VM '{vm_name}' deleted successfully")
    except ApiException as e:
        logging.error(f"Exception when deleting VM '{vm_name}': {e}")
        raise


def create_subdomain_headless_service(client_core_v1_api, namespace, subdomain):
    # Define the service
    service = client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(name=subdomain),
        spec=client.V1ServiceSpec(
            selector={"expose": subdomain},
            cluster_ip="None"
        )
    )
    logger.info(f"creating k8s headless service for subdomain: {subdomain}")
    try:
        response = client_core_v1_api.create_namespaced_service(namespace=namespace, body=service)
    except client.exceptions.ApiException as e:
        logger.error(f"Exception when creating service: {e}")
        raise
    else:
        logger.info(f"Service created. status='{response.status}'")
        logger.info('Service creation succeeded')


def delete_subdomain_headless_service(client_core_v1_api, namespace, subdomain):
    logger.info(f"deleting k8s headless service for subdomain: {subdomain}")
    try:
        response = client_core_v1_api.delete_namespaced_service(name=subdomain, namespace=namespace)
    except client.exceptions.ApiException as e:
        logger.error(f"Exception when deleting service: {e}")
        raise
    else:
        logger.info(f"Service deleted. status='{response.status}'")
        logger.info('Service deletion succeeded')
