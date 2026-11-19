# builderdash

Builderdash is a cross cloud build system for images developed by
Omnibond.  It was originally developed for use with CloudyCluster
although it may now be used on its own.

See https://www.omnibond.com/ and https://www.cloudycluster.com/ for
more information.

## Example

A basic example of its use to modify a CloudyCluster image to include
the LAMMPS simulation package is given in the `examples` directory.

Take `startCLOUD.yaml.example` where `CLOUD` is `AWS` or `GCP`, copy to
`startCLOUD.yaml`, and modify as appropriate for your configuration.

Notably ensure it is specified to use the most recent version of
CloudyCluster as a base and that credentials are available (see the
documentation for the provider's Python cloud library).

Then run `builderdash -c startCLOUD.yaml` to start the build.

Optional start-YAML key `build_host_ssh_hostname` overrides the SSH
target (default is the instance `remoteIp`). Unset keeps GCE/AWS and
KubeVirt-on-GCE unchanged. This is a destination replace, not
`proxy_conf` (that remains a jump). Pair it with existing
`build_host_ssh_port`. Example for a localhost port-forward:

```
  - build_host_ssh_hostname: 127.0.0.1
  - build_host_ssh_port: 2222
```

## KubeVirt builds

A KubeVirt build (`cloudservice: kubevirt`) creates two objects in
`k8s_namespace`, both named after the generated `instancename`:

* a standalone CDI **DataVolume** (and its PVC) imported from
  `sourceimage`. This is the image. It has no ownerReference to the VM,
  so it stays Bound and usable after the builder VM is deleted.
* a **VirtualMachine** (the builder) that boots from that DataVolume.

Both carry the labels `app.kubernetes.io/managed-by=builderdash`,
`builderdash.omnibond.com/build=<buildtype>`,
`builderdash.omnibond.com/run-id=<run id>` and
`builderdash.omnibond.com/image=<instancename>`, plus any `k8s_labels`.
The VM template labels also land on the VMI and virt-launcher pod.

**Success:** `deleteinstance` deletes the builder VM only; the
DataVolume/PVC is the saved image.

**Failure:** nothing is deleted. Builderdash stops the builder VM
(`runStrategy: Halted`) so it no longer uses CPU or memory, keeps the
VM and its DataVolume/PVC, and logs the names plus how to resume
(`virtctl start <vm> -n <ns>`) and how to clean up by hand
(`kubectl delete vm <vm>`, `kubectl delete dv <vm>`, or by
`-l builderdash.omnibond.com/run-id=<run id>`). Cleanup is the
developer's job.

**PV Retain:** not done by default. It needs cluster-scoped PV rights
that namespaced accounts lack, and it isn't needed because the image is
a standalone DataVolume. `k8s_retain_root_pv: true` opts back in; a 401
or 403 then logs a warning and the build continues.

### SSH transport (`k8s_ssh_transport`)

| value | how builderdash reaches the builder's sshd |
|---|---|
| `kubectl` | **Default for KubeVirt.** Supervised `kubectl port-forward pod/<virt-launcher> <local>:<build_host_ssh_port>` on 127.0.0.1, restarted if kubectl exits, stopped at the end of the build. Needs `pods/portforward` and `kubectl` on the build host. |
| `virtctl` | `virtctl port-forward --stdio` as an SSH ProxyCommand. Needs `virtualmachineinstances/portforward` and `virtctl`. |
| `direct` | SSH straight to the VMI pod IP, or to `build_host_ssh_hostname` if set. |
| `proxy` | Jump host from `proxy_conf_path` (unchanged behavior). |

When `k8s_ssh_transport` is unset, existing configs keep their
behavior: `proxy_conf_path` selects `proxy`, and
`build_host_ssh_hostname` selects `direct`. Otherwise KubeVirt builds
use `kubectl`. The transport and its CLI are checked before anything
is created in the cluster.

Related keys: `k8s_kubectl_path`, `k8s_virtctl_path`,
`k8s_ssh_local_port` (default: a free port),
`k8s_port_forward_log` (default `port-forward.log`),
`k8s_port_forward_timeout` (seconds, default 60). `k8s_kubeconfig_path`
and `k8s_kubeconfig_context` are passed to kubectl/virtctl.

### Sizing and placement

* `instancetype` + `k8s_instancetype_kind`: `VirtualMachineInstancetype`
  (namespaced, the default when `instancetype` is set, as before) or
  `VirtualMachineClusterInstancetype` (cluster-wide
  common-instancetypes, e.g. `u1.xlarge`, `u1.2xlarge`, `cx1.4xlarge`).
  `namespaced`/`cluster` are accepted as shorthands. If `instancetype`
  is unset and `k8s_resources` is not given, the builder uses the
  cluster instancetype `u1.xlarge`.
* `k8s_preference` + `k8s_preference_kind` (default
  `VirtualMachineClusterPreference`): optional preference.
* `k8s_resources`: `{requests: {...}, limits: {...}}` copied to
  `domain.resources`, e.g. `ephemeral-storage`. To size the VM with
  explicit cpu/memory instead of an instancetype, set
  `instancetype: null` (memory request required) and optionally
  `k8s_cpu_cores`. KubeVirt may reject cpu/memory here when an
  instancetype also sizes the VM.
* `k8s_affinity` (full affinity), `k8s_node_affinity` (nodeAffinity
  only), `k8s_node_selector`, `k8s_tolerations`: copied to the VM
  template.

### DataVolume

* `k8s_storage_class` (required, as before)
* `k8s_access_modes` (default `[ReadWriteOnce]`)
* `k8s_volume_mode` (`Filesystem` or `Block`; default: the cluster's)

See `examples/startKubeVirt.yaml.example`.

Instructions for launching the generated image are available at
http://docs.aws.cloudycluster.com/software/add-sw-custom-ami/ or
https://docs.gcp.cloudycluster.com/software/add-sw-custom-image/.

## Basic Instructions

Install builderdash with `python3 setup.py install` which will install
the `builderdash` command.  Ensure `PATH` is set correctly if not
installing
as root.

List of arguments:  

* `-V, --version`
* `-c, --cfile`: Config Filename (Mandatory)  
* `-l, --lfile`: Log filename (Defaults to 'builderdash')  Note: Log
  file only appends to file, it never replaces.  
* `-v, --verbosity`: Specifies the verbosity level for logging (0 none,
  10 debug, 20 error, 30 warning, 40 info, 50 critical)",
  required=False, default=30)

### Development / Editable Installation
NOTE: For an editable, development installation of builderdash (along with its dependencies), run:
`pip install -e .`