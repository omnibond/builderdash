set -ex
module add openmpi/4.1.2
mkdir /custom/lammps
cd /custom/lammps
curl -O https://download.lammps.org/tars/lammps-stable.tar.gz
tar xzf lammps-stable.tar.gz
rm lammps-stable.tar.gz
cd lammps-*/src
make -j60 serial
make -j60 mpi
