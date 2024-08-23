import json
import logging
import os
import time
from textwrap import dedent

import yaml
from kubernetes import client as client
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

vm_template = dedent('''\
    apiVersion: kubevirt.io/v1
    kind: VirtualMachine
    metadata:
      name: {name}
      namespace: {namespace}
      labels: {labels}
    spec:
      running: {instance_state}
      instancetype:
        kind: {instance_type_kind}
        name: {instance_type_name}
      template:
        metadata:
          labels: {labels}
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
                    lock_passwd: false
                    plain_text_passwd: {plain_text_passwd}
                    ssh_authorized_keys:
                      - {public_key_openssh}
          - name: {root_disk_name}
            dataVolume:
              name: {pvc_name}
      dataVolumeTemplates:
      - metadata:
          name: {pvc_name}
        spec:
          pvc:
            storageClassName: {pvc_storage_class}
            accessModes:
            - {pvc_access_mode}
            resources:
              requests:
                storage: {pvc_storage_capacity}
          source: {data_volume_source}''')


def generate_vm_template_substitution_dictionary(my_build):
    if hasattr(my_build, "disksize"):
        disksize = my_build.disksize
    else:
        disksize = "55"

    f = open(os.path.expanduser(my_build.pubkeypath), 'r')
    kubevirt_public_key_openssh = f.read()
    f.close()

    if my_build.kubevirt_storage_class_name:
        pvc_storage_class = ''
    else:
        pvc_storage_class = f"storageClassName: {my_build.kubevirt_storage_class_name}"

    return {
        'name': my_build.instancename,
        'namespace': my_build.k8s_namespace,
        'labels': {},  # TODO - support assigning labels
        'instance_state': 'true',
        'instance_type_kind': 'VirtualMachineInstancetype',
        'instance_type_name': my_build.instancetype,
        'mac_address': 'ee:ee:ee:ee:ee:ee',
        'root_disk_name': 'root-disk',
        'ssh_user': str(my_build.sshkeyuser),
        'public_key_openssh': kubevirt_public_key_openssh,
        'pvc_name': my_build.instancename,
        'pvc_storage_class': pvc_storage_class,
        'pvc_access_mode': 'ReadWriteOnce',
        'pvc_storage_capacity': disksize,
        'data_volume_source': my_build.sourceimage,
        'plain_text_passwd': my_build.kubevirt_plain_text_passwd  # TODO - shall we keep this enabled?
    }


def generate_rendered_vm_yaml_manifest(my_build):
    return vm_template.format(**generate_vm_template_substitution_dictionary(my_build))


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


def get_pv_name_from_pvc(client_core_v1_api, namespace, pvc_name):
    # Retrieve the PVC object from the specified namespace
    try:
        pvc = client_core_v1_api.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
    except ApiException as e:
        logging.error(f"Exception when reading namespaced PVC '{pvc_name}' from namespace '{namespace}': {e}")
        raise

    # Return the name of the PV bound to this PVC
    return pvc.spec.volume_name


def patch_pv_to_retain(client_core_v1_api, pv_name):
    # Define the patch to set the PV reclaim policy to Retain
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


def patch_pv_to_nullify_claim_ref(client_core_v1_api, pv_name):
    # Define the patch to set the claimRef to null (Unbind the PV)
    pv_patch = {
        "spec": {
            "claimRef": None
        }
    }
    try:
        client_core_v1_api.patch_persistent_volume(pv_name, pv_patch)
        logging.info(f"Successfully patched PV '{pv_name}' to set the claimRef to null.")
    except ApiException as e:
        logging.error(f"Exception when patching PV '{pv_name}' to set the claimRef to null: {e}")
        raise


def wait_for_pvc_to_be_deleted(client_core_v1_api, namespace, pvc_name, timeout, interval):
    logging.info(f"Waiting for PVC '{pvc_name}' to be deleted...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            client_core_v1_api.read_namespaced_persistent_volume_claim(pvc_name, namespace)
            logging.info(f"PVC '{pvc_name}' still exists, waiting...")
            time.sleep(interval)  # Wait and retry
        except ApiException as e:
            if e.status == 404:
                logging.info(f"PVC '{pvc_name}' has been deleted.")
                return True
            else:
                raise
    logging.error("Timeout waiting for kubevirt VMI to become ready")
    return False


def create_pvc_for_retained_pv(my_build, pv_name):
    """
     This function creates a new PVC to replace the PVC that was deleted when the VM was deleted.
     This function is intended to be executed after the VM, that was associated with this PVC, is deleted.
     The VM, its previous PVC, and its new PVC all share the same name -- by convention.
    """

    d = generate_vm_template_substitution_dictionary(my_build)

    # naming PVC after VM name
    pvc_name = d['name']

    pvc_manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": pvc_name,
            "namespace": d['namespace']
        },
        "spec": {
            "accessModes": [d['pvc_access_mode']],
            "resources": {
                "requests": {
                    "storage": d['pvc_storage_capacity']
                }
            },
            "volumeName": pv_name
        }
    }

    if my_build.kubevirt_storage_class_name:
        pvc_manifest['spec']['storageClassName'] = my_build.kubevirt_storage_class_name

    try:
        response = my_build.k8s_client_core_v1_api.create_namespaced_persistent_volume_claim(
            d['namespace'],
            pvc_manifest
        )
    except client.exceptions.ApiException as e:
        logging.error(f"Exception when creating PVC: {e}")
        raise
    else:
        logging.info(f"PVC '{pvc_name}' created successfully.")
        return response


def wait_for_pvc_deletion_then_recreate(my_build, timeout=600, interval=1):
    """
    Re-create the PVC after the VM has been deleted. The first PVC associated with the VM must be deleted by the time
    create_pvc_for_retained_pv is called or else it will likely raise an exception.

    Background:
    For builderdash, the lifecycle of the PVC should be managed separately from the kubevirt VM.

    When a kubevirt VM is created with a dataVolumeTemplates section, a PVC is created and its lifecycle is bound to
    that of the VM; however, we want the PVC to exist beyond the deletion of the VM so that it may be reused later on
    without additional steps being required of the user of the desired build output image.

    To support that, another PVC is created after the VM and its original PVC have been deleted. The new PVC is bound to
    the original PV that was retained.
    """
    pvc_name = my_build.instancename
    try:
        pv_name = get_pv_name_from_pvc(my_build.k8s_client_core_v1_api, my_build.k8s_namespace, pvc_name)
    except Exception:
        return False

    ret = wait_for_pvc_to_be_deleted(my_build.k8s_client_core_v1_api, my_build.k8s_namespace, pvc_name, timeout,
                                     interval)
    if ret:
        try:
            patch_pv_to_nullify_claim_ref(my_build.k8s_client_core_v1_api, pv_name)
            create_pvc_for_retained_pv(my_build, pv_name)
        except Exception:
            return False
        else:
            return True
    return False


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
        if interfaces:
            ip_address = interfaces[0].get('ipAddress')
            return ip_address
        else:
            logging.error("No interfaces found in VMI")
            return None
    except Exception as e:
        logging.error(f"Exception when extracting IP address: {e}")
        return None


def set_retainment_of_root_volume(
        client_core_v1_api,
        k8s_namespace,
        vm_name,
):
    try:
        pvc_name = vm_name
        pv_name = get_pv_name_from_pvc(client_core_v1_api, k8s_namespace, pvc_name)
        patch_pv_to_retain(client_core_v1_api, pv_name)
    except ApiException:
        raise
    except Exception as e:
        logging.error(f"Unexpected exception when trying to get PV name and retain PV: {e}")
        raise


def create_vm_and_wait_for_ip(
        client_core_v1_api,
        custom_objects_api,
        k8s_namespace,
        vm_name,
        manifest,
        timeout=600,
        interval=10,
        retain_root_volume=True,
):
    vm = create_vm(custom_objects_api, k8s_namespace, manifest)
    if vm:
        vmi = wait_for_vmi_running(custom_objects_api, k8s_namespace, vm_name, timeout, interval)
        if vmi:
            if retain_root_volume:
                try:
                    set_retainment_of_root_volume(client_core_v1_api, k8s_namespace, vm_name)
                except Exception:
                    delete_vm(custom_objects_api, k8s_namespace, vm_name)
                    return None
            else:
                logging.warning(f"retain_root_volume is False, root volume WILL BE DELETED when VM is deleted.")
            return extract_ip_address(vmi)
        else:
            logging.error('failed to get VMI data')
    else:
        logging.error('failed create kubevirt VM')
    return None


def stop_vmi(custom_objects_api, k8s_namespace, vmi_name):
    try:
        response = custom_objects_api.patch_namespaced_custom_object(
            group='kubevirt.io',
            version='v1',
            namespace=k8s_namespace,
            plural='virtualmachines',
            name=vmi_name,
            body={
                "spec": {
                    "running": False
                }
            }
        )
    except ApiException as e:
        logging.error(f'Exception when stopping VMI: {vmi_name}, {e}')
        raise
    else:
        logging.info(f'Successfully stopped VMI: {vmi_name}')
        return response


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