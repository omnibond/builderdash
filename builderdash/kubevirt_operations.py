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
              - name: {data_volume_disk_name}
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
          - name: {data_volume_disk_name}
            dataVolume:
              name: {data_volume_name}
      dataVolumeTemplates:
      - metadata:
          name: {data_volume_name}
        spec:
          pvc:
            {data_volume_pvc_storage_class}
            accessModes:
            - {data_volume_pvc_access_mode}
            resources:
              requests:
                storage: {data_volume_pvc_storage_capacity}
          source: {data_volume_source}''')


def generate_template_substitution_dictionary(my_build):
    if hasattr(my_build, "disksize"):
        disksize = my_build.disksize
    else:
        disksize = "55"

    f = open(os.path.expanduser(my_build.pubkeypath), 'r')
    kubevirt_public_key_openssh = f.read()
    f.close()

    if my_build.kubevirt_storage_class_name is None or my_build.kubevirt_storage_class_name == 'None':
        data_volume_pvc_storage_class = ''
    else:
        data_volume_pvc_storage_class = f"storageClassName: {my_build.kubevirt_storage_class_name}"

    return {
        'name': my_build.instancename,
        'namespace': my_build.k8s_namespace,
        'labels': {},  # TODO - support assigning labels
        'instance_state': 'true',
        'instance_type_kind': 'VirtualMachineInstancetype',
        'instance_type_name': my_build.instancetype,
        'mac_address': 'ee:ee:ee:ee:ee:ee',
        'data_volume_disk_name': 'data-volume-disk',
        'ssh_user': str(my_build.sshkeyuser),
        'public_key_openssh': kubevirt_public_key_openssh,
        'data_volume_name': 'root-data-volume-' + my_build.instancename,
        'data_volume_pvc_storage_class': data_volume_pvc_storage_class,
        'data_volume_pvc_access_mode': 'ReadWriteOnce',
        'data_volume_pvc_storage_capacity': disksize,
        'data_volume_source': my_build.sourceimage,
        'plain_text_passwd': my_build.kubevirt_plain_text_passwd  # TODO - shall we keep this enabled?
    }


def generate_rendered_vm_yaml_manifest(my_build):
    return vm_template.format(**generate_template_substitution_dictionary(my_build))


def create_vm(api_instance, namespace, manifest):
    try:
        api_response = api_instance.create_namespaced_custom_object(
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


def get_vmi(api_instance, namespace, vmi_name):
    try:
        vmi = api_instance.get_namespaced_custom_object(
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


def wait_for_vmi_running(api_instance, namespace, vmi_name, timeout, interval):
    start_time = time.time()
    while time.time() - start_time < timeout:
        vmi = get_vmi(api_instance, namespace, vmi_name)
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


def create_vm_and_wait_for_ip(kubevirt_api, k8s_namespace, vm_name, manifest, timeout=600, interval=10):
    vm = create_vm(kubevirt_api, k8s_namespace, manifest)
    if vm:
        vmi = wait_for_vmi_running(kubevirt_api, k8s_namespace, vm_name, timeout, interval)
        if vmi:
            return extract_ip_address(vmi)
        else:
            logging.error('failed to get VMI data')
    else:
        logging.error('failed create kubevirt VM')
    return None


def stop_vmi(kubevirt_api, k8s_namespace, vmi_name):
    try:
        response = kubevirt_api.patch_namespaced_custom_object(
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
        logging.info(f'Successfully stopped VMI: {vmi_name}')
        return response
    except ApiException as e:
        logging.error(f'Exception when stopping VMI: {vmi_name}, {e}')
        raise


# TODO delete_vm/vmi
def delete_vm(kubevirt_api, k8s_namespace, vm_name):
    pass


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