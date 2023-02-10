# builderdash

Builderdash is a cross cloud build system for images developed by
Omnibond Systems.  It was originally developed for use with
CloudyCluster although it may now be used on its own.

See https://www.omnibond.com/ and https://www.cloudycluster.com/ for
more information.

## Example

A basic example of its use to modify a CloudyCluster image to include
the LAMMPS simulation package is given in the examples directory.

Take startCLOUD.yaml.example where cloud is AWS or GCP, copy to
startCLOUD.yaml, and modify as appropriate for your configuration.

Notably ensure it is specified to use the most recent version of
CloudyCluster as a base and that credentials are available (see the
documentation for the provider's Python cloud library).

Then run `builderdash -c startCLOUD.yaml` to start the build.

Instructions for launching the generated image are available at
http://docs.aws.cloudycluster.com/software/add-sw-custom-ami/ or
https://docs.gcp.cloudycluster.com/software/add-sw-custom-image/.

## Basic Instructions

Install builderdash with `python3 setup.py install` which will install
the builderdash command.  Ensure PATH is set correctly if not installing
as root.

List of arguments:  

* `-V, --version`
* `-c, --cfile`: Config Filename (Mandatory)  
* `-l, --lfile`: Log filename (Defaults to 'builderdash')  Note: Log
  file only appends to file, it never replaces.  
* `-v, --verbosity`: Specifies the verbosity level for logging (0 none,
  10 debug, 20 error, 30 warning, 40 info, 50 critical)",
  required=False, default=30)
