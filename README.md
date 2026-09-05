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