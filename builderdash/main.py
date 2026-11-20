#!/usr/bin/python2.7
#Copyright Omnibond Systems, LLC. All rights reserved.
#
#Terms of Service are located at:
#http://www.cloudycluster.com/termsofservice
from __future__ import annotations
import argparse
import ast
import atexit
import configparser
import datetime
import json
import logging
import os
import platform
import random
import re
import shlex
import subprocess
import sys
import time
from enum import Enum, unique
from typing import Any, List

import botocore
import botocore.session
import googleapiclient.discovery
import paramiko
import yaml
import kubernetes

from builderdash.redaction import redact_mapping, redact_value
from builderdash.kubevirt_operations import (
    LABEL_RUN_ID,
    _as_bool,
    create_vm_and_wait_for_ip,
    delete_vm,
    generate_data_volume_manifest,
    generate_rendered_vm_yaml_manifest,
    get_build_run_id,
    get_pvc,
    get_vm,
    get_vmi,
    redact_manifest_for_log,
    stop_vmi,
)
from builderdash.kubevirt_port_forward import (
    PortForward,
    find_binary,
    find_launcher_pod,
    kubectl_base_args,
    virtctl_proxy_command,
    wait_for_launcher_pod,
)
from builderdash.ssh_target import (
    SSH_TRANSPORT_KUBECTL,
    SSH_TRANSPORT_PROXY,
    SSH_TRANSPORT_VIRTCTL,
    ssh_transport_is_explicit,
    resolve_ssh_target_hostname,
    resolve_ssh_transport,
)
from builderdash.ssher import SSHConnection, load_proxy_conf_file
from builderdash.ssh_config import ssh_alias, ssh_config_path, ssh_config_text, write_ssh_config


@unique
class EnvProvider(str, Enum):
    AWS = 'aws'
    AZURE = 'azure'
    GCP = 'gcp'
    K8S_CONTAINER = 'container'
    K8S_VM = 'kubevirt'

    @staticmethod
    def is_valid_provider(provider):
        return provider in EnvProvider.valid_providers()

    @staticmethod
    def providers():
        return [e.value for e in EnvProvider]

    @staticmethod
    def valid_providers() -> List[EnvProvider]:
        return [
            EnvProvider.AWS,
            # TODO: EnvProvider.AZURE,
            EnvProvider.GCP,
            # TODO: EnvProvider.K8S_CONTAINER,
            EnvProvider.K8S_VM,
        ]


def normalize_kubernetes_client_bearer_token():
    kube_config = kubernetes.client.Configuration.get_default_copy()
    if 'authorization' not in kube_config.api_key or 'BearerToken' in kube_config.api_key:
        return False

    # kubernetes 36 loads kubeconfig tokens as "authorization", while generated APIs expect "BearerToken".
    kube_config.api_key['BearerToken'] = kube_config.api_key['authorization']
    kubernetes.client.Configuration.set_default(kube_config)
    return True


class Build:
    tagList = None
    env_provider = None

    def setup(self, config_section):
        config_key = None
        for key in config_section:
            config_key = key
            logging.info(f'config_key: {config_key}')
        # TODO What is the purpose of the loop above? Is it to determine the last key in the config_section?

        # First parse and set all attributes from this section.
        if config_key:
            name = None
            for option in config_section[config_key]:
                for key in option:
                    name = key
                    logging.info(f"option = {redact_mapping(option)}\tname = {name}")
                # TODO What is the purpose of the inner loop above? Is it to determine the last key in the option?
                logging.info(f"setting attribute on instance of class Build: {name}: {redact_value(name, option[name])}")
                if name:
                    setattr(self, name, option[name])

        # Now set tagList.
        try:
            build_type = getattr(self, 'buildtype')
            os_type = getattr(self, 'ostype')
            cloud_service = getattr(self, 'cloudservice')
            self.tagList = [tag.lower() for tag in [build_type, os_type, cloud_service]]
        except Exception as e:
            logging.exception(f"tagList element not found! Exception: {e}")
            logging.info("Check input .yaml for buildtype, ostype, and cloudservice.")
            logging.info("Exiting...")
            sys.exit(1)
        if hasattr(self, "customtags"):
            self.tagList += self.customtags

        logging.info("List of Tags is %s" % self.tagList)

        env_provider_str = getattr(self, 'cloudservice').lower()
        delattr(self, 'cloudservice')
        if not EnvProvider.is_valid_provider(env_provider_str):
            logging.error(f"cloudservice in input .yaml is {env_provider_str} but must be one of: "
                          f"{EnvProvider.valid_providers()}")
            sys.exit(1)

        self.env_provider = EnvProvider(env_provider_str)
        if self.env_provider == EnvProvider.K8S_VM or self.env_provider == EnvProvider.K8S_CONTAINER:
            self.k8s_setup_client()

    def k8s_setup_client(self):
        config_file = getattr(self, 'k8s_kubeconfig_path', None)
        if config_file is not None:
            config_file = os.path.expanduser(config_file)

        kubernetes.config.load_kube_config(
            config_file=config_file,
            context=getattr(self, 'k8s_kubeconfig_context', None),
            persist_config=False
        )
        normalize_kubernetes_client_bearer_token()

        contexts, current_context = kubernetes.config.list_kube_config_contexts(config_file)

        # Extract the namespace from the current context
        current_namespace = current_context['context'].get('namespace', 'default')

        # If namespace WAS NOT specified in build.config (or was null), use namespace from kubeconfig (or 'default')
        if getattr(self, 'k8s_namespace', None) is None:
            setattr(self, 'k8s_namespace', current_namespace)

    def k8s_save_config(self, path, output_format='json'):
        try:
            d = {
                'k8s_config': {
                    'namespace': getattr(self, 'k8s_namespace'),
                    'config_file': getattr(self, 'k8s_kubeconfig_path'),
                    'context': getattr(self, 'k8s_kubeconfig_context'),
                }
            }
            with open(os.path.expanduser(path), 'w') as fp:
                if output_format == 'json':
                    json.dump(d, fp, indent=4)
                elif output_format == 'yaml':
                    yaml.dump(d, fp, indent=4)
                else:
                    raise ValueError(f"k8s_save_config was provided invalid output_format: '{output_format}'."
                                     "Must be 'json' or 'yaml'.")
        except Exception as e:
            logging.error(f"k8s_save_config raised exception: {e}")


def safe_load_yaml_file(yaml_file):
    try:
        yaml_file = os.path.expanduser(yaml_file)
        f = open(yaml_file)
    except Exception as e:
        print(f"open({yaml_file}) raised exception:", e, file=sys.stderr)
        raise e
    try:
        loaded_yaml = yaml.safe_load(f)
    except Exception as e:
        print(f"yaml.safe_load raised exception:", e, file=sys.stderr)
        f.close()
        raise e
    else:
        f.close()
        return loaded_yaml


#########Set Environment Variables that CC Needs#########
def setCloudyClusterEnvVars(ssh, myBuild):
    if myBuild.buildtype == 'userapps' or myBuild.buildtype == 'base':
        CC_BUILD_TYPE = 'UserApps'
    elif myBuild.buildtype == 'dev':
        CC_BUILD_TYPE = 'CCDev'
    elif myBuild.buildtype == 'prod':
        CC_BUILD_TYPE = 'CCProd'
    else:
        CC_BUILD_TYPE = str(myBuild.buildtype)
    if myBuild.ostype == 'centos':
        CC_OS_NAME = 'centos'
    elif myBuild.ostype == 'rhel':
        CC_OS_NAME = 'rhel'
    elif myBuild.ostype == 'almalinux':
        CC_OS_NAME = 'almalinux'
    elif myBuild.ostype == 'ubuntu':
        CC_OS_NAME = 'ubuntu'
    CC_AWS_SSH_USERNAME = myBuild.sshkeyuser
    commandString = 'sudo sed -i \'$ aexport CC_BUILD_TYPE='+CC_BUILD_TYPE+'\' /etc/profile'
    runCommand(ssh, commandString, myBuild)
    commandString = 'sudo sed -i \'$ aexport CC_OS_NAME='+CC_OS_NAME+'\' /etc/profile'
    runCommand(ssh, commandString, myBuild)
    commandString = 'sudo sed -i \'$ aexport CC_AWS_SSH_USERNAME='+CC_AWS_SSH_USERNAME+'\' /etc/profile'
    runCommand(ssh, commandString, myBuild)
    commandString = 'sudo sed -i \'$ aexport CLOUD='+str(myBuild.env_provider.value)+'\' /etc/profile'
    runCommand(ssh, commandString, myBuild)
    commandString = 'source /etc/profile'
    runCommand(ssh, commandString, myBuild)
    logging.info("end of cloudy vars")


def write_operating_env_provider_file(ssh, my_build, output_file='/etc/eureka-operating-env-provider'):
    logging.info(f"writing eureka operating env provider: '{my_build.env_provider.value}' to file: {output_file}")
    command_string = f"echo '{my_build.env_provider.value}' | sudo tee \"{output_file}\""
    runCommand(ssh, command_string, my_build)


def processInitSection(configSection, myBuild):
    logging.info("Entered processInitSection")
    myBuild.setup(configSection)

    logging.debug("in init")
    if myBuild.local == 'True':
        logging.debug("running in local mode")
        return None
    else:
        logging.debug("Running in remote mode")
        if not hasattr(myBuild, 'sshkey'):
            logging.error("no sshkey please configure one")
            sys.exit(1)
        if hasattr(myBuild, 'proxy_conf_path'):
            logging.info(f'proxy_conf_path: {myBuild.proxy_conf_path}')
            proxy_conf = load_proxy_conf_file(myBuild.proxy_conf_path)
            if proxy_conf is None:
                logging.error(f'proxy_conf_path detected but loaded proxy_conf is None')
                sys.exit(1)
            else:
                setattr(myBuild, 'proxy_conf', proxy_conf)
                logging.info(f'myBuild.proxy_conf: {redact_mapping(myBuild.proxy_conf)}')
        logging.info("instance type is %s", str(getattr(myBuild, 'instancetype', None)))
        launchInstance(myBuild)
        logging.info("remoteIp is %s", str(myBuild.remoteIp))

        # *******   loop / sleep until userapps
        if hasattr(myBuild, 'instancetype'):
            logging.info("instance type is %s", str(myBuild.instancetype))
        # Give instance time to boot, for ssh service to come up, and for cloudinit to run, etc.
        time.sleep(20)
        ssh = ssh_connect(myBuild)
        if ssh is None:
            logging.error('ssh_connect returned None. Stopping instance and aborting build!')
            stopInstance(myBuild)
            sys.exit(1)

        logging.info('osType inside process init is %s', str(myBuild.ostype))
        write_operating_env_provider_file(ssh, myBuild)
        # TODO why are we installing wget here? Can this be cleaned up?
        run_cmd = 'sudo yum install wget -y'
        logging.info('calling exec_command on: %s', run_cmd)
        # TODO use ssh.run_command instead, as is done further down in this file
        stdin, stdout, stderr = ssh.get_target_client().exec_command(run_cmd, get_pty=True)
        run_ret = stdout.channel.recv_exit_status()
        logging.info('exec_command returned: %d', run_ret)
        #logging.info('exec_command stdout: %s', stdout)
        #logging.info('exec_command stderr: %s', stderr)
        #setCloudyClusterEnvVars(ssh, myBuild)

        return ssh

def launchInstance(myBuild):
    if myBuild.env_provider == EnvProvider.AWS:
        awsInstance(myBuild)
    elif myBuild.env_provider == EnvProvider.GCP:
        googleInstance(myBuild)
    elif myBuild.env_provider == EnvProvider.K8S_VM:
        kubevirt_instance(myBuild)
    else:
        logging.error("build has invalid env_provider")
        sys.exit(1)


def generate_and_set_instance_name(myBuild, sourcename):
    if hasattr(myBuild, "instancename"):
        image_start = f"builderdash-{myBuild.instancename}-{myBuild.buildtype}"
    else:
        image_start = f"builderdash-{myBuild.buildtype}"

    tags_okay = False
    if "-dev" in sourcename or "-prod" in sourcename:
        if "-dev" in sourcename and "-dev" in image_start:
            tags_okay = True
        elif "-prod" in sourcename and "-prod" in image_start:
            tags_okay = True
    if not tags_okay:
        if "-dev" in sourcename:
            image_start = f"{image_start}-dev"
        elif "-prod" in sourcename:
            image_start = f"{image_start}-prod"

    image_time = time.strftime("%Y%m%d", time.gmtime())

    if hasattr(myBuild, "addhash") and myBuild.addhash:
        try:
            image_hash = subprocess.check_output(["git", "describe", "--always", "--dirty=plus"]).strip().decode()
            image_hash = image_hash.replace(".", "-")
        except FileNotFoundError:
            image_hash = None
        except subprocess.CalledProcessError:
            image_hash = None
    else:
        image_hash = None

    image_random = os.urandom(2).hex()

    if image_hash:
        myBuild.instancename = f"{image_start}-{image_time}-{image_hash}-{image_random}"
    else:
        myBuild.instancename = f"{image_start}-{image_time}-{image_random}"

def awsInstance(myBuild):
    logging.info("Running awsInstance")
    if hasattr(myBuild, "region"):
        logging.info("Region is "+str(myBuild.region))
    session = botocore.session.get_session()
    client = session.create_client('ec2', region_name = str(myBuild.region))
    response = client.describe_account_attributes(AttributeNames=['supported-platforms'])['AccountAttributes'][0]['AttributeValues']
    for attr in response:
        if attr['AttributeValue'] == 'EC2':
            if myBuild.subnet != None:
                pass
            else:
                logging.info("Your account has the EC2 Classic attribute.  You must specify a subnet in the init section of your cfg file")
                sys.exit(1)
        else:
            pass
    handleUserData(myBuild)

    if not hasattr(myBuild, "rootdev"):
        r = client.describe_images(ImageIds=[myBuild.sourceimage])
        myBuild.rootdev = r["Images"][0]["RootDeviceName"]

    if hasattr(myBuild, "disksize"):
        disksize = int(myBuild.disksize)
    else:
        disksize = 55

    if hasattr(myBuild, "awsspot"):
        if hasattr(myBuild, "awsspotprice"):
            logging.info("awsspotprice is %s", str(myBuild.awsspotprice))
            try:
                az = myBuild.az
            except Exception as e:
                logging.exception("There was an error getting the Availability Zone")
                logging.exception("Availability zone is required in your .cfg file init section.  Example:  az = us-west-1a")
                sys.exit(1)
        else:
            logging.info("use current spot price + 20%")
            response = client.describe_spot_price_history(AvailabilityZone = str(myBuild.az), InstanceTypes=['t3.small'], ProductDescriptions=['Linux/UNIX'], StartTime = datetime.datetime.now(), EndTime = datetime.datetime.now())
            for thing in range(len(response['SpotPriceHistory'])):
                # FIXME: Mary, the variable x below is used before assignment.
                logging.info(response['SpotPriceHistory'][x]['SpotPrice'])
                currentSpot = response['SpotPriceHistory'][x]['SpotPrice']
            myBuild.awsspotprice = currentSpot * 1.2
            logging.info("awsspotprice is %s", str(myBuild.awsspotprice))
        blockDeviceStuff = [{'DeviceName': myBuild.rootdev, "Ebs": {"DeleteOnTermination": True, "VolumeSize": disksize, "VolumeType": "gp2"}}]
        launchSpecs = {"BlockDeviceMappings": blockDeviceStuff, "ImageId": str(myBuild.sourceimage), "KeyName": str(myBuild.sshkeyname), "InstanceType": str(myBuild.instancetype)}
        response = client.request_spot_instances(AvailabilityZoneGroup = 'eu-west-1a', DryRun=False, LaunchSpecification=launchSpecs, SpotPrice=str(myBuild.awsspotprice), Type=str(myBuild.spottype), ValidFrom=myBuild.spotfrom, ValidUntil=myBuild.spotuntil)
        logging.info("Spot Instance spinning up")
        for i in response:
            if i == 'Instances':
                for u in range(len(response[i])):
                    for x in response[i][u]:
                        if x == 'InstanceId':
                            myBuild.instanceId = response[i][u][x]
                            break
    else:
        logging.info("Using on demand")
        ######Spin up instance###########
        response = client.describe_images(ImageIds=[myBuild.sourceimage])
        try:
            sourcename = response["Images"][0]["Name"]
        except:
            logging.error("could not describe source image")
        generate_and_set_instance_name(myBuild, sourcename)
        logging.info("Spinning up the instance")
        iamstuff = {'Name': 'instance-admin'}
        blockDeviceStuff = [{'DeviceName': myBuild.rootdev, "Ebs": {"DeleteOnTermination": True, "VolumeSize": disksize, "VolumeType": "gp2"}}]
        if hasattr(myBuild, "subnet"):
            if hasattr(myBuild, "securitygroup"):
                response = client.run_instances(BlockDeviceMappings = blockDeviceStuff, DryRun=False, ImageId = str(myBuild.sourceimage), MinCount = 1, MaxCount = 1, SecurityGroupIds=[myBuild.securitygroup], SubnetId = str(myBuild.subnet), KeyName = str(myBuild.sshkeyname), InstanceType = myBuild.instancetype, IamInstanceProfile = iamstuff, UserData = str(myBuild.userdata), TagSpecifications = [{'ResourceType':'instance','Tags':[{'Key':'Name', 'Value': str(myBuild.instancename)}]}])
            else:
                response = client.run_instances(BlockDeviceMappings = blockDeviceStuff, DryRun=False, ImageId = str(myBuild.sourceimage), MinCount = 1, MaxCount = 1, SubnetId = str(myBuild.subnet), KeyName = str(myBuild.sshkeyname), InstanceType = myBuild.instancetype, IamInstanceProfile = iamstuff, UserData = str(myBuild.userdata), TagSpecifications = [{'ResourceType':'instance','Tags':[{'Key':'Name', 'Value': str(myBuild.instancename)}]}])
        else:
            if hasattr(myBuild, "securitygroup"):
                response = client.run_instances(BlockDeviceMappings = blockDeviceStuff, DryRun=False, ImageId = str(myBuild.sourceimage), MinCount = 1, MaxCount = 1, SecurityGroupIds=[myBuild.securitygroup], KeyName = str(myBuild.sshkeyname), InstanceType = str(myBuild.instancetype), IamInstanceProfile = iamstuff, UserData = str(myBuild.userdata), TagSpecifications = [{'ResourceType':'instance','Tags':[{'Key':'Name', 'Value': str(myBuild.instancename)}]}])
            else:
                response = client.run_instances(BlockDeviceMappings = blockDeviceStuff, DryRun=False, ImageId = str(myBuild.sourceimage), MinCount = 1, MaxCount = 1, KeyName = str(myBuild.sshkeyname), InstanceType = str(myBuild.instancetype), IamInstanceProfile = iamstuff, UserData = str(myBuild.userdata), TagSpecifications = [{'ResourceType':'instance','Tags':[{'Key':'Name', 'Value': str(myBuild.instancename)}]}])

        logging.info("Instance spun up")
        instanceId = None
        for i in response:
            if i == 'Instances':
                for u in range(len(response[i])):
                    for x in response[i][u]:
                        if x == 'InstanceId':
                            myBuild.instanceId = response[i][u][x]
                            break
    time.sleep(7)
    keyexists = False
    while keyexists == False:
        description = client.describe_instances(InstanceIds = [myBuild.instanceId])
        for i in description:
            if i == 'Reservations':
                for u in range(len(description[i])):
                    for x in description[i][u]:
                        if x == 'Instances':
                            for y in range(len(description[i][u][x])):
                                key = 'PublicIpAddress'
                                for z in description[i][u][x][y]:
                                    #print "z is "+ str(z)
                                    if z == 'PublicIpAddress':
                                        remoteIp = description[i][u][x][y][z]
                                        logging.info("Remote ip is "+ str(remoteIp))
                                        myBuild.remoteIp = remoteIp
                                        keyexists = True
                                        break
        counter = 0
        logging.info(description['Reservations'][0]['Instances'][0]['State']['Name'])
        while description['Reservations'][0]['Instances'][0]['State']['Name'] != 'running' and counter < 60:
            description = client.describe_instances(InstanceIds = [myBuild.instanceId])
            logging.info(description['Reservations'][0]['Instances'][0]['State']['Name'])
            time.sleep(10)
            logging.info('Waiting for instance to come alive')
            counter += 1
        logging.info('Remote IP is %s', str(myBuild.remoteIp))
        myBuild.projectName = 'None'


#######Google Launch is Next##########
def googleInstance(myBuild):
    autoDelete = True
    try:
        if "diskdelete" in myBuild.tagList:
            autoDelete = False
    except Exception as e:
        autoDelete = True
    logging.info("autoDelete boot disk is set to " + str(autoDelete))
    compute = googleapiclient.discovery.build('compute', 'v1', cache_discovery=False)
    zone = myBuild.region
    projectName = myBuild.projectname
    bucketName = myBuild.bucketname
    machine_type = "zones/%s/machineTypes/%s" % (zone, str(myBuild.instancetype))

    # use image family if applicable
    if hasattr(myBuild, "imagefamily") and myBuild.imagefamily != "none":
        try:
            # Name of the image family to search for.
            family = myBuild.imagefamily
            familyproject = myBuild.imagefamilyproject
            print("Image Family:" + str(family))
            request = compute.images().getFromFamily(project=familyproject, family=family)
            response = request.execute()
            myBuild.sourceimage = '/projects/' + familyproject + '/global/images/' + response['name']
        except Exception as e:
            logging.exception("Image Family not found.")
            stopInstance(myBuild)
            sys.exit(1)  

    # log source image
    print("Source image is " + str(myBuild.sourceimage))

    generate_and_set_instance_name(myBuild, myBuild.sourceimage)

    if hasattr(myBuild, "disksize"):
        disksize = myBuild.disksize
    else:
        disksize = "55"

    with open(str(myBuild.pubkeypath), 'rb') as f:
        tempsshkey = str(myBuild.sshkeyuser)+':'+f.read().decode()
    body = {
        'name': myBuild.instancename,
        'machineType': machine_type,
        'disks': [
            {
                'boot': True,
                'autoDelete': autoDelete,
                'initializeParams': {
                    'sourceImage': myBuild.sourceimage,
                    'diskSizeGb': disksize
                }
            }
        ],

        'networkInterfaces': [{
            'network': 'global/networks/default',
            'accessConfigs': [
                {'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}
            ]
        }],

        'metadata': {
            'items': [
                {'key': 'bucket', 'value': myBuild.bucketname},
                {'key': 'ssh-keys', 'value': tempsshkey},
                {'key': 'block-project-ssh-keys', 'value': True}
            ]
        }
    }
    if hasattr(myBuild, "inhibitstartup") and myBuild.inhibitstartup:
        body["metadata"]["items"].append(
            {
                "key": "startup-script",
                "value": "echo '{\"lookupTableName\": \"delete\"}' > /opt/CloudyCluster/var/dbName.json"
            }
        )
    myBuild.tempsshkey = tempsshkey
    myBuild.machine_type = machine_type
    x = compute.instances().insert(project=myBuild.projectname, zone=zone, body=body).execute()
    place = None
    counter = 0
    while not place and counter < 60:
        result = compute.instances().list(
            project=myBuild.projectname,
            zone=zone,
            filter='(status eq RUNNING) (name eq ' + str(myBuild.instancename) + ')'
        ).execute()
        logging.info("myBuild.instancename is: " + str(myBuild.instancename))
        logging.info("result is: " + str(result))
        if "items" in result:
            logging.info("result['items'] is: " + str(result['items']))
            for temp in range(len(result['items'])):
                logging.info("temp is: " + str(result['items'][temp]))

                if result['items'][temp]['name'] == str(myBuild.instancename):
                    status = result['items'][temp]['status']
                    logging.info("status is: " + str(status))
                    if status == 'RUNNING':
                        logging.info("Google Cloud VM is ready!")
                        remoteIp = result['items'][temp]['networkInterfaces'][0]['accessConfigs'][0]['natIP']
                        place = True

                    elif status == 'PROVISIONING':
                        logging.info("VM is still spinning up")
                        counter += 1
                        time.sleep(10)
                    elif status == 'TERMINATED':
                        logging.info("VM has terminated, now exiting")
                        sys.exit(1)
        time.sleep(5)
    remoteIp = result['items'][0]['networkInterfaces'][0]['accessConfigs'][0]['natIP']
    myBuild.remoteIp = remoteIp
    myBuild.instanceId = None


def kubevirt_instance(my_build, timeout=3600, interval=10):
    logging.info('kubevirt_instance called')
    logging.info('my_build.env_provider is: %s', my_build.env_provider)

    if my_build.buildtype == 'control':
        k8s_save_config_format = 'yaml'
        k8s_save_config_path = os.path.join(os.getcwd(), f'k8s_config.{k8s_save_config_format}')
        logging.info(f"Saving k8s configuration to local file so it may be reused by env.py via provider data.")
        logging.info(f"k8s_save_config_path: {k8s_save_config_path}")
        my_build.k8s_save_config(k8s_save_config_path, k8s_save_config_format)

    my_build.k8s_client_core_v1_api = kubernetes.client.CoreV1Api()
    my_build.k8s_custom_objects_api = kubernetes.client.CustomObjectsApi()

    logging.info(f"k8s_custom_objects_api is: {my_build.k8s_custom_objects_api}")
    logging.info(f"Source image is:\n# BEGIN\n{json.dumps(my_build.sourceimage, indent=4)}\n# END")

    # Preflight the SSH transport before anything is created in the cluster.
    try:
        kubevirt_preflight_ssh_transport(my_build)
    except ValueError as e:
        logging.error(str(e))
        sys.exit(1)
    get_build_run_id(my_build)

    generate_and_set_instance_name(my_build, my_build.sourceimage)

    logging.info('my_build.instancename is: %s', my_build.instancename)
    kubevirt_write_ssh_config(my_build)

    # TODO What should we use for the instanceId with kubevirt?
    my_build.instanceId = None

    try:
        rendered_manifest = generate_rendered_vm_yaml_manifest(my_build)
        data_volume_manifest = generate_data_volume_manifest(my_build)
    except ValueError as e:
        logging.error(str(e))
        sys.exit(1)

    logging.info('Rendered kubevirt VM instance yaml manifest:\n# BEGIN\n%s\n# END',
                 redact_manifest_for_log(rendered_manifest))
    logging.info(
        "Rendered standalone kubevirt DataVolume yaml manifest:\n# BEGIN\n%s# END",
        yaml.safe_dump(data_volume_manifest, sort_keys=False)
    )

    try:
        vm_manifest = yaml.safe_load(rendered_manifest)
    except Exception as e:
        logging.error(f"failed to yaml.safe_load the VM rendered_manifest. Exception is {e}")
        sys.exit(1)

    logging.info('Applying generated kubevirt VM manifest for build instance and waiting for its IP...')

    ip_address = create_vm_and_wait_for_ip(
        my_build.k8s_client_core_v1_api,
        my_build.k8s_custom_objects_api,
        my_build.k8s_namespace,
        my_build.instancename,
        vm_manifest,
        data_volume_manifest,
        timeout=timeout,
        interval=interval,
        retain_root_volume=_as_bool(getattr(my_build, 'k8s_retain_root_pv', None), default=False),
    )
    if ip_address is None:
        logging.error(f"VM IP Address is None. Exiting.")
        sys.exit(1)

    logging.info(f"VM IP Address: {ip_address}")
    my_build.remoteIp = ip_address

    if my_build.k8s_ssh_transport_resolved == SSH_TRANSPORT_KUBECTL:
        try:
            kubevirt_start_port_forward(my_build)
        except Exception as e:
            logging.error('failed to start kubectl port-forward to the builder VM: %s', e)
            sys.exit(1)


def kubevirt_preflight_ssh_transport(my_build):
    """Resolve k8s_ssh_transport and check the CLI it needs, before creating cluster objects."""
    transport = resolve_ssh_transport(my_build, is_kubevirt=True)
    my_build.k8s_ssh_transport_resolved = transport
    if transport == SSH_TRANSPORT_KUBECTL:
        kubectl = find_binary(getattr(my_build, 'k8s_kubectl_path', None), 'kubectl')
        if not kubectl:
            raise ValueError(
                "k8s_ssh_transport is kubectl but kubectl was not found; "
                "install kubectl, set k8s_kubectl_path, or choose k8s_ssh_transport: virtctl|direct|proxy")
        my_build.k8s_kubectl_bin = kubectl
    elif transport == SSH_TRANSPORT_VIRTCTL:
        virtctl = find_binary(getattr(my_build, 'k8s_virtctl_path', None), 'virtctl')
        if not virtctl:
            if ssh_transport_is_explicit(my_build):
                raise ValueError("k8s_ssh_transport is virtctl but virtctl was not found; "
                                 "install virtctl or set k8s_virtctl_path")
            raise ValueError(
                "virtctl was not found. KubeVirt builds reach the builder through "
                "`virtctl port-forward --stdio` by default (needs virtualmachineinstances/portforward). "
                "Install virtctl or set k8s_virtctl_path, or set k8s_ssh_transport: kubectl "
                "(kubectl port-forward to the virt-launcher pod, needs only pods/portforward)")
        my_build.k8s_virtctl_bin = virtctl
    logging.info('kubevirt build SSH transport: %s', transport)
    return transport


def kubevirt_virtctl_proxy_argv(my_build, remote_port=None):
    """virtctl port-forward --stdio argv for this build (kubeconfig, context, namespace)."""
    if remote_port is None:
        remote_port = getattr(my_build, 'build_host_ssh_port', 22) or 22
    return virtctl_proxy_command(
        getattr(my_build, 'k8s_virtctl_bin', None) or 'virtctl', my_build.k8s_namespace, my_build.instancename,
        remote_port, getattr(my_build, 'k8s_kubeconfig_path', None), getattr(my_build, 'k8s_kubeconfig_context', None))


def kubevirt_write_ssh_config(my_build):
    """With the virtctl transport, write an OpenSSH config for `ssh -F <file> <alias>`.

    Not written for kubectl (its local port is only valid while builderdash
    runs), direct or proxy. A write error is a warning; the build goes on.
    """
    my_build.k8s_ssh_config_file = None
    if getattr(my_build, 'k8s_ssh_transport_resolved', None) != SSH_TRANSPORT_VIRTCTL:
        return None
    path = ssh_config_path(my_build)
    text = ssh_config_text(
        ssh_alias(my_build), my_build.sshkeyuser, getattr(my_build, 'sshkey', None),
        kubevirt_virtctl_proxy_argv(my_build),
        note=f"builder VM {my_build.k8s_namespace}/{my_build.instancename}")
    try:
        write_ssh_config(path, text)
    except OSError as e:
        logging.warning('could not write the builder ssh config %s: %s', path, e)
        return None
    my_build.k8s_ssh_config_file = path
    logging.info('builder ssh config: %s  (ssh -F %s %s)', path, shlex.quote(path), ssh_alias(my_build))
    return path


def kubevirt_start_port_forward(my_build):
    """Supervised `kubectl port-forward pod/<virt-launcher> LOCAL:<build_host_ssh_port>`."""
    namespace = my_build.k8s_namespace
    vm_name = my_build.instancename
    core = my_build.k8s_client_core_v1_api
    vmi = get_vmi(my_build.k8s_custom_objects_api, namespace, vm_name) or {}
    vmi_uid = (vmi.get('metadata') or {}).get('uid')
    pod = wait_for_launcher_pod(core, namespace, vm_name, vmi_uid=vmi_uid)
    remote_port = int(getattr(my_build, 'build_host_ssh_port', 22) or 22)
    forward = PortForward(
        kubectl_base_args(my_build.k8s_kubectl_bin,
                          getattr(my_build, 'k8s_kubeconfig_path', None),
                          getattr(my_build, 'k8s_kubeconfig_context', None)),
        namespace, pod,
        local_port=int(getattr(my_build, 'k8s_ssh_local_port', 0) or 0),
        remote_port=remote_port,
        log_path=getattr(my_build, 'k8s_port_forward_log', None) or 'port-forward.log',
        pod_resolver=lambda: find_launcher_pod(core, namespace, vm_name, vmi_uid=vmi_uid),
    )
    forward.start(wait=int(getattr(my_build, 'k8s_port_forward_timeout', 60) or 60))
    atexit.register(forward.stop)
    my_build.k8s_port_forward = forward
    my_build.k8s_ssh_target = ('127.0.0.1', forward.local_port)
    logging.info('builder VM %s/%s is reached through kubectl port-forward pod/%s 127.0.0.1:%d -> %d',
                 namespace, vm_name, pod, forward.local_port, remote_port)
    return forward


def kubevirt_stop_port_forward(my_build):
    forward = getattr(my_build, 'k8s_port_forward', None)
    if forward is not None:
        try:
            forward.stop()
        except Exception as e:
            logging.warning('stopping kubectl port-forward raised: %s', e)
        my_build.k8s_port_forward = None


def kubevirt_kubectl_prefix(my_build):
    args = ['kubectl']
    if getattr(my_build, 'k8s_kubeconfig_path', None):
        args += ['--kubeconfig', os.path.expanduser(str(my_build.k8s_kubeconfig_path))]
    if getattr(my_build, 'k8s_kubeconfig_context', None):
        args += ['--context', str(my_build.k8s_kubeconfig_context)]
    args += ['-n', str(my_build.k8s_namespace)]
    return ' '.join(shlex.quote(a) for a in args)


def kubevirt_virtctl_prefix(my_build):
    args = [getattr(my_build, 'k8s_virtctl_bin', None) or 'virtctl']
    if getattr(my_build, 'k8s_kubeconfig_path', None):
        args += ['--kubeconfig', os.path.expanduser(str(my_build.k8s_kubeconfig_path))]
    if getattr(my_build, 'k8s_kubeconfig_context', None):
        args += ['--context', str(my_build.k8s_kubeconfig_context)]
    args += ['-n', str(my_build.k8s_namespace)]
    return ' '.join(shlex.quote(a) for a in args)


def kubevirt_failed_build_help(my_build, vm_halted):
    ns = my_build.k8s_namespace
    name = my_build.instancename
    kubectl = kubevirt_kubectl_prefix(my_build)
    virtctl = kubevirt_virtctl_prefix(my_build)
    ssh_config_file = getattr(my_build, 'k8s_ssh_config_file', None)
    run_id = getattr(my_build, 'k8s_build_run_id', None)
    state = 'stopped (runStrategy: Halted)' if vm_halted else 'not running or could not be stopped (check it)'
    lines = [
        'KubeVirt build FAILED. Nothing was deleted; cleanup is up to you.',
        f'  builder VM:          {ns}/{name} ({state})',
        f'  image DataVolume/PVC: {ns}/{name} (kept)',
        f'  start it again:      {virtctl} start {shlex.quote(name)}'
        f"   (or: {kubectl} patch vm {shlex.quote(name)} --type merge -p '{{\"spec\":{{\"runStrategy\":\"Always\"}}}}')",
    ]
    if ssh_config_file:
        lines.append(f'  ssh into it:         ssh -F {shlex.quote(ssh_config_file)} {ssh_alias(my_build)}'
                     '   (after it is running again)')
    lines += [
        f'  reach its sshd:      {kubectl} port-forward pod/<virt-launcher-pod> 2222:22'
        f'   (pod: {kubectl} get pod -l vm.kubevirt.io/name={name})',
        f'  stop it again:       {virtctl} stop {shlex.quote(name)}',
        f'  clean up by hand:    {kubectl} delete vm {shlex.quote(name)} && {kubectl} delete dv {shlex.quote(name)}',
    ]
    if run_id:
        lines.append(f'  objects of this run: {kubectl} get vm,vmi,dv,pvc,pod -l {LABEL_RUN_ID}={run_id}')
    return '\n'.join(lines)


def kubevirt_stop_failed_build(my_build):
    """On failure: stop the port-forward, halt (not delete) the builder VM, keep VM and DV/PVC, log how to resume."""
    kubevirt_stop_port_forward(my_build)
    if getattr(my_build, 'k8s_failure_handled', False):
        return
    api = getattr(my_build, 'k8s_custom_objects_api', None)
    name = getattr(my_build, 'instancename', None)
    if api is None or not name:
        return
    my_build.k8s_failure_handled = True
    vm_halted = False
    if getattr(my_build, 'k8s_vm_deleted', False):
        logging.info('builder VM %s was already deleted; nothing to stop', name)
    elif get_vm(api, my_build.k8s_namespace, name) is not None:
        try:
            stop_vmi(api, my_build.k8s_namespace, name)
            vm_halted = True
        except Exception as e:
            logging.error('failed to stop kubevirt VM %s: %s', name, e)
    logging.error(kubevirt_failed_build_help(my_build, vm_halted))


def dispatchOption(option, args, ssh, myBuild):
    logging.info("%s %s", option, args)
    if option == "testtouch":
        testtouch(args, ssh, myBuild)
    elif option == "mkdir":
        makeDirectory(args, ssh, myBuild)
    elif option == "upload_files":
        upload_files(args, ssh)
    # TODO
    #elif option == "download_files":
    #    download_files(args, ssh)
    elif option == "downloads":
        downloads(args, ssh, myBuild)
    elif option == "extract":
        extract(args, ssh, myBuild)
    elif option == "reporpms":
        repoRpms(args, ssh, myBuild)
    elif option == "pathrpms":
        pathRpms(args, ssh, myBuild)
    elif option == "builderdash":
        builderdash(args, ssh, myBuild)
    elif option == "copyfiles":
        copyFiles(args, ssh, myBuild)
    elif option == "movefiles":
        moveFiles(args, ssh, myBuild)
    elif option == "copysubtree":
        copySubtree(args, ssh, myBuild)
    elif option == "chmod":
        chmod(args, ssh, myBuild)
    elif option == "chown":
        chown(args, ssh, myBuild)
    elif option == "sourcescripts":
        sourceScripts(args, ssh, myBuild)
    elif option == "delete":
        deleteFiles(args, ssh, myBuild)
    elif option == "commands":
        commandsexec(args, ssh, myBuild)
    elif option == "saveimage":
        savedImage = saveImage(args, myBuild)
    elif option == "deleteinstance":
        deleteInstance(args, myBuild)
    elif option == "append":
        append(args, ssh, myBuild)
    elif option == "replace":
        replaceText(args, ssh, myBuild)
    elif option == "npm":
        npm(args, ssh, myBuild)
    elif option == "reboot":
        rebootFunc(args, ssh, myBuild)
        # FIXME: this return value is never used by caller of dispatchOption (the function: processSection)
        #connectionObj = rebootFunc(args, connectionObj, myBuild)
        #return {'newConnect': connectionObj}
    elif option == "envvar":
        envVariables(args, ssh, myBuild)
    elif option == "tar":
        createOrExtract(args, ssh, myBuild)
    elif option == "cloudyvars":
        setCloudyClusterEnvVars(ssh, myBuild)
    else:
        logging.error("Option %s not recognized", option)
        sys.exit(1)


def processSection(configSection, ssh, myBuild):
    myBuild.timesprefix = myBuild.timesprefix + " "
    start_time = time.time()
    for key in configSection:
        config_key = key
    logging.info("entered processSection %s", str(config_key))
    for option in configSection[config_key]:
        for key in option:
            name = key
        try:
            prefix = None
            runCheck = True
            newoption = name
            ###Get Prefix Tags if Any#####
            if ")" in name:
                location = newoption.index(')')
                prefix = newoption[:location+1]
                newoption = newoption[location+1:]
            ###Add items from prefix Tag to a List#####
            if prefix != None:
                tagList = []
                end = ')'
                begin = '('
                prefix = (prefix.split(begin))[1]
                for item in prefix:
                    if item == ',' or item ==  ')':
                        tempLoc = prefix.index(item)
                        tagList.append(prefix[:tempLoc])
                        prefix = prefix[tempLoc+1:]
                ####Compare this list to the one in the Class to see if it's ok to run the command####
                result = set(tagList).issubset(myBuild.tagList)
                if result == True:
                    runCheck = True
                else:
                    runCheck = False
            if runCheck == True:
                # TODO: Check with Mary: processSection expects this function to return a value (in the case the first arg is 'reboot')
                dispatchOption(newoption, option[name], ssh, myBuild)
            else:
                logging.info("Permission Tags Not in Build Type")
        except Exception as e:
            logging.exception("Error in processing the section")
            sys.exit(1)
    myBuild.timesprefix = myBuild.timesprefix[:-1]
    end_time = time.time()
    myBuild.times.append((myBuild.timesprefix + config_key, end_time - start_time))


def ssh_connect(myBuild, timeout=None, attempt_limit=60, retry_delay=10.0):
    logging.info('ssh_connect called')
    transport = resolve_ssh_transport(myBuild, is_kubevirt=myBuild.env_provider == EnvProvider.K8S_VM)
    target_hostname = resolve_ssh_target_hostname(myBuild)
    target_port = getattr(myBuild, 'build_host_ssh_port', 22) or 22
    sock_factory = None
    if transport == SSH_TRANSPORT_KUBECTL:
        target_hostname, target_port = myBuild.k8s_ssh_target
    elif transport == SSH_TRANSPORT_VIRTCTL:
        proxy_command = ' '.join(shlex.quote(a) for a in kubevirt_virtctl_proxy_argv(myBuild, target_port))
        logging.info('ssh ProxyCommand: %s', proxy_command)
        sock_factory = lambda: paramiko.ProxyCommand(proxy_command)
        target_hostname = myBuild.instancename
    logging.info('ssh transport %s, target %s:%s (remoteIp %s)', transport, target_hostname, target_port,
                 myBuild.remoteIp)
    if hasattr(myBuild, 'proxy_conf') and transport != SSH_TRANSPORT_PROXY:
        logging.info('proxy_conf is ignored because k8s_ssh_transport is %s', transport)
    # TODO clean up variable names below
    if transport == SSH_TRANSPORT_PROXY:
        ssh = SSHConnection(target_hostname=target_hostname, target_port=myBuild.build_host_ssh_port,
                            target_username=myBuild.sshkeyuser, target_key_filename=myBuild.sshkey,
                            target_timeout=timeout, target_attempt_limit=attempt_limit, target_retry_delay=retry_delay,
                            target_missing_host_key_policy=paramiko.AutoAddPolicy(),
                            proxy_hostname=myBuild.proxy_conf['proxy_hostname'],
                            proxy_port=myBuild.proxy_conf['proxy_port'],
                            proxy_username=myBuild.proxy_conf['proxy_username'],
                            proxy_key_filename=myBuild.proxy_conf['proxy_key_filename'],
                            proxy_timeout=myBuild.proxy_conf['proxy_timeout'],
                            proxy_attempt_limit=myBuild.proxy_conf['proxy_attempt_limit'],
                            proxy_retry_delay=myBuild.proxy_conf['proxy_retry_delay'],
                            proxy_missing_host_key_policy=myBuild.proxy_conf['proxy_missing_host_key_policy'],
                            proxy_channel_alt_src_hostname=myBuild.proxy_conf['proxy_channel_alt_src_hostname'])
    else:
        ssh = SSHConnection(target_hostname=target_hostname, target_port=target_port,
                            target_username=myBuild.sshkeyuser, target_key_filename=myBuild.sshkey,
                            target_timeout=timeout, target_attempt_limit=attempt_limit,
                            target_missing_host_key_policy=paramiko.AutoAddPolicy(),
                            target_sock_factory=sock_factory)
    try:
        ssh.connect()
    except Exception as e:
        logging.error('SSH Connection failed: %s', e)
        ssh.disconnect()
        return None
    else:
        if ssh.is_alive():
            logging.info('SSH Connection IS ALIIIIIIVE!')
            return ssh


###########Run Commands on Instance######################################
def runCommand(ssh, commandString, myBuild, **kwargs):
    if 'local' in kwargs:
        local = kwargs['local']
    else:
        local = myBuild.local
    if local is False or local == 'False':
        try:
            logging.info("running command as remote: %s", commandString)
            # Send the command (blocking)
            status, _, _ = ssh.run_command(commandString, get_pty=True,
                                           stdout_log_func=logging.info, stderr_log_func=None,
                                           ret_stdout=False, ret_stderr=False,
                                           stdout_extra={"commandoutput": True}, stderr_extra=None)
            logging.info("Exit status is %d", status)
            if status != 0:
                logging.exception('ERROR running command')
                stopInstance(myBuild)
                sys.exit(1)
        except Exception as e:
            logging.exception('Exception is %s', e)
            sys.exit(1)
    elif local is True or local == 'True':
        logging.info("running command as local: %s", commandString)
        try:
            # Use a pty so that commands which call isatty don't change behavior.
            pid, fd = os.forkpty()
            if pid == 0:
                os.execlp("sh", *["sh", "-c", commandString])
            buffer = b""
            while True:
                # A pty master returns EIO when the slave is closed.
                try:
                    new = os.read(fd, 1024)
                except OSError:
                    new = ""
                if len(new) == 0:
                    break
                sys.stdout.buffer.write(new)
                sys.stdout.flush()
                buffer = buffer + new
                list = buffer.split(b"\n")
                for line in list[:-1]:
                    logging.info("%s", line.decode(), extra={"commandoutput": True})
                buffer = list[-1]
            list = buffer.split(b"\n")
            for line in list[:-1]:
                logging.info("%s", line.decode(), extra={"commandoutput": True})
            if list[-1] != "":
                logging.info("%s", list[-1].decode(), extra={"commandoutput": True})
            os.close(fd)
            pid, status = os.waitpid(pid, 0)
            status = status >> 8
            logging.error("Exit status is %d", status)
            if status != 0:
                logging.exception('ERROR running command locally')
                sys.exit(1)
        except Exception:
            logging.exception('ERROR in runCommand()')
            sys.exit(1)
    else:
        logging.info("Couldn't get a local status")


###########Stops the running instance##############################
def stopInstance(myBuild):
    # TODO make stopInstance optional for debugging purposes.
    logging.info("stopping instance...")
    if myBuild.env_provider == EnvProvider.AWS:
        session = botocore.session.get_session()
        client = session.create_client('ec2', region_name = str(myBuild.region))
        response = client.stop_instances(InstanceIds = [str(myBuild.instanceId)]) 
    elif myBuild.env_provider == EnvProvider.GCP:
        compute = googleapiclient.discovery.build('compute', 'v1', cache_discovery=False)
        result = compute.instances().stop(project=myBuild.projectname, zone=myBuild.region, instance=str(myBuild.instancename)).execute()
    elif myBuild.env_provider == EnvProvider.K8S_VM:
        # Halt (do not delete) the builder VM; keep it and its DV/PVC for debugging.
        kubevirt_stop_failed_build(myBuild)
    else:
        logging.error("build has invalid env_provider")
        sys.exit(1)
    logging.info("instance stopped...")


############### Saves the Image depending on the Cloud Service Being Used.##########################
def saveImage(slist, myBuild):
    for key in slist:
        imageName = slist[key]
    if imageName == '' or imageName == None:
        imageName = myBuild.instancename
    else:
        imageName = "%s-%s" % (imageName, random.SystemRandom().getrandbits(16))
    if myBuild.env_provider == EnvProvider.AZURE:
        logging.info('This feature not supported')
    elif myBuild.env_provider == EnvProvider.AWS:
        session = botocore.session.get_session()
        client = session.create_client('ec2', region_name = str(myBuild.region))
        logging.info('Stopping instance')
        response = client.stop_instances(InstanceIds = [str(myBuild.instanceId)])
        ###Check to make sure ami is stopped###
        stopped = False
        while stopped != True:
            description = client.describe_instances(InstanceIds = [myBuild.instanceId])
            state = description['Reservations'][0]['Instances'][0]['State']['Name']
            if state == 'stopped':
                stopped = True
                time.sleep(5)
            else:
                time.sleep(20)
        logging.info('Saving ami')
        tags = [{"Key": "sourceimage", "Value": myBuild.sourceimage}]
        response = client.describe_images(ImageIds=[myBuild.sourceimage])
        try:
            tags.append({"Key": "sourcename", "Value": response["Images"][0]["Name"]})
        except:
            logging.error("could not describe source image")
        response = client.create_image(Description='Builderdash', Name=str(imageName), InstanceId = str(myBuild.instanceId), BlockDeviceMappings=[{'DeviceName': myBuild.rootdev,'Ebs': {'VolumeType': 'gp2'}}], TagSpecifications=[{"ResourceType": "image", "Tags": tags}])
        logging.info(str(response))
        savedImage = response['ImageId']
        counter = 0
        status = None
        while status != 'available':
            response = client.describe_images(ImageIds=[str(savedImage)])
            for i in range(len(response['Images'])):
                status = response['Images'][i]['State']
            time.sleep(60)
            counter += 1
            # min wait time potentially
            if counter == 3600:
                logging.info("Saving Image Timed out.  Exiting Builderdash")
                sys.exit(1)
    elif myBuild.env_provider == EnvProvider.GCP:
        logging.info("Saving Image")
        zone = myBuild.region
        source_disk = 'zones/' + str(zone) + '/disks/' + str(myBuild.instancename)
        sourceimage = myBuild.sourceimage.split("/")[4]
        data = {'name': str(imageName).lower(), 'sourceDisk': source_disk, "labels": {"sourceimage": sourceimage}}
        service = googleapiclient.discovery.build('compute', 'v1', cache_discovery=False)
        request = service.images().insert(project=myBuild.projectid, body=data, forceCreate=True)
        # This is so we don't end up with empty files
        time.sleep(120)
        response = request.execute()
        logging.info(response)

        service.globalOperations().wait(project=myBuild.projectid, operation=response["name"])

        savedImage = response
    elif myBuild.env_provider == EnvProvider.K8S_VM:
        logging.info('Saving kubevirt image -- really just recording a reference to the build instance persistent volume claim name and namespace.')
        pvc_name = myBuild.instancename
        try:
            pvc = get_pvc(myBuild.k8s_client_core_v1_api, myBuild.k8s_namespace, pvc_name)
        except Exception:
            logging.exception(
                "Failed to read KubeVirt image PVC '%s/%s' before recording image metadata.",
                myBuild.k8s_namespace,
                pvc_name,
            )
            sys.exit(1)

        savedImage = {
            "pvc": {
                "name": pvc_name,
                "namespace": myBuild.k8s_namespace
            }
        }
        if pvc.spec.storage_class_name is not None:
            savedImage["pvc"]["storageClassName"] = pvc.spec.storage_class_name
        if pvc.spec.volume_name is not None:
            savedImage["pvc"]["volumeName"] = pvc.spec.volume_name

        logging.info('kubevirt pvc: ' + json.dumps(savedImage))
    # TODO: even though savedImage is returned is it ever really used?
    return(savedImage)


###########Execute a Command As Directly Typed##############################
def commandsexec(commando, ssh, myBuild):
    for key in commando:
        commandString = str(key)
        runCommand(ssh, commandString, myBuild)


#########Just a test function for touching .txt files########################
def testtouch(touchy, ssh, myBuild):
    for key in touchy:
        logging.info(key)
        commandString = 'sudo touch ~/'+str(key)
        runCommand(ssh, commandString, myBuild)


#########Tar Compress or Tar Extract Files#######################################
def createOrExtract(tarlist, ssh, myBuild):
    for key in tarlist:
        tarName = key
        logging.info(key)
        local = tarlist[key][0]
        action = tarlist[key][1]
        change_dir = tarlist[key][2]
        if local is False or local == 'False':
            if action == 'create':
                paths = ' '.join(tarlist[key][3:])
                commandString = f'sudo tar -C {change_dir} -zcvf {tarName} {paths}'
                runCommand(ssh, commandString, myBuild, local=False)
            elif action == 'extract':
                commandString = f'sudo tar -C {change_dir} -zxvf {tarName}'
                runCommand(ssh, commandString, myBuild, local=False)
            else:
                logging.info('No action was specified in cfg file.  Could not compress or extract tar.')
                
        elif local is True or local == 'True':
            if action == 'create':
                paths = ' '.join(tarlist[key][3:])
                # Added --no-xattrs for local create since BSD tar on macOS adds xattrs and Linux extract complains
                commandString = f'tar --no-xattrs -C {change_dir} -zcvf {tarName} {paths}'
                runCommand(ssh, commandString, myBuild, local=True)
            elif action == 'extract':
                commandString = f'tar -C {change_dir} -zxvf {tarName} '
                runCommand(ssh, commandString, myBuild, local=True)
            else:
                logging.info('No action was specified in cfg file.  Could not compress or extract tar.')


##########Get Distribution of Local Operating System###############
def get_distribution():
    dist = platform.dist()
    for i in dist:
        distro = dist[0]
        version = dist[1]
        supportdist = dist[2]
    return(distro)


##############Run Scripts###########################
def sourceScripts(sslist, ssh, myBuild):
    for key in sslist:
        logging.info(key)
        commandString = 'sudo chmod +x ' + str(key)
        runCommand(ssh, commandString, myBuild)
        commandString = 'sudo ' + str(key)
        runCommand(ssh, commandString, myBuild)


#################Downloads Files#####################
def downloads(dllist, ssh, myBuild):
    for source in dllist:
        commandString = 'sudo wget -P ' + str(dllist[source]) + ' ' + str(source)
        runCommand(ssh, commandString, myBuild)


#################Extract Files######################################       
def extract(exlist, ssh, myBuild):
    for key in exlist:
        filelocation = key
        destination = exlist[key][0]
        extract_method = exlist[key][1]
        commandString = 'sudo tar ' + str(extract_method) + ' ' + str(filelocation) + ' -C ' + str(destination)
        runCommand(ssh, commandString, myBuild)


###############Install from packages###############################
def repoRpms(rrlist, ssh, myBuild):
    runCommand(ssh, "sudo yum install -y " + " ".join(rrlist), myBuild)


##############Yum localinstall###########
def pathRpms(prlist, ssh, myBuild):
    for key in prlist:
        logging.info(key)
        commandString = 'sudo yum localinstall ' + str(key) + ' -y'
        runCommand(ssh, commandString, myBuild)


##############Calls another builderdash script###############
def builderdash(blist, ssh, myBuild):
    for key in blist:
        logging.info(key)
        logging.info("STARTING======>>>>>>>>>>"+str(key))
        subprocess.call('pwd', shell=True)
        runBuild(False, myBuild, ssh, str(key))


#############Copies Files from one location to Another###############
def copyFiles(cflist, ssh, myBuild):
    for key in cflist:
        commandString = 'sudo cp ' + str(key) + ' ' + str(cflist[key])
        runCommand(ssh, commandString, myBuild)


#############Move Files from one location to Another###############
def moveFiles(mflist, ssh, myBuild):
    for key in mflist:
        commandString = 'sudo mv ' + str(key) + ' ' + str(mflist[key])
        runCommand(ssh, commandString, myBuild)


################Delete Files#######################################
def deleteFiles(delist, ssh, myBuild):
    for key in delist:
        commandString = 'sudo rm ' + str(key)
        runCommand(ssh, commandString, myBuild)


#############Copies a Subtree######################################
def copySubtree(cslist, ssh, myBuild):
    for key in cslist:
        commandString = 'sudo cp -R ' + str(key) + ' ' + str(cslist[key])
        runCommand(ssh, commandString, myBuild)


#############Change Permissions###############################
def chmod(cmlist, ssh, myBuild):
    for key in cmlist:
        commandString = 'sudo chmod ' + str(cmlist[key]) + ' ' + str(key)
        runCommand(ssh, commandString, myBuild)


#############Change Ownsership###############################
def chown(colist, ssh, myBuild):
    for key in colist:
        options = ''
        group = ''
        file = ''
        if colist[key][0] != '':
            options = str(colist[key][0]) + ' '
        if colist[key][1] != '':
            group = ':' + str(colist[key][1])
        if colist[key][2] != '':
            file = str(colist[key][2])
        commandString = 'sudo chown ' + options + str(key) + group + ' ' + file
        runCommand(ssh, commandString, myBuild)


def makeDirectory(mklist, ssh, myBuild):
    for key in mklist:
        commandString = 'sudo mkdir ' + str(key)
        runCommand(ssh, commandString, myBuild)


def upload_files(uploads, ssh):
    logging.info('upload_files called with uploads: %s', uploads)
    for upload in uploads:
        src = upload[0]
        dst = upload[1]
        try:
            ssh.file_upload(src, dst)
        except Exception as e:
            logging.error("upload_files raised an exception: %s", e)
            #stopInstance(myBuild)  # TODO
            sys.exit(1)
    logging.info("upload_files raised no exceptions.")

# TODO download_files


def deleteInstance(delList, myBuild):
    if myBuild.env_provider == EnvProvider.AWS:
        session = botocore.session.get_session()
        client = session.create_client('ec2', region_name = str(myBuild.region))
        try:
            response = client.terminate_instances(InstanceIds=[str(myBuild.instanceId)])
            logging.info(response)
        except:
            logging.info("Failed to delete instance.  Please do so manually")
    elif myBuild.env_provider == EnvProvider.GCP:
        compute = googleapiclient.discovery.build('compute', 'v1', cache_discovery=False)

        deleted = None
        while not deleted:
            time.sleep(10)
            logging.info("the instance we're going to delete is: " + str(myBuild.instancename))
            deleteResponse = compute.instances().delete(project=myBuild.projectname, zone=myBuild.region, instance=str(myBuild.instancename)).execute()
            logging.info("\ndeleteResponse is: ")
            logging.info(deleteResponse)
            if deleteResponse['status'] == "PENDING" or deleteResponse['status'] == "RUNNING":
                deleted = True
    elif myBuild.env_provider == EnvProvider.K8S_VM:
        logging.info(f"Deleting kubevirt instance: {myBuild.instancename}")
        kubevirt_stop_port_forward(myBuild)
        delete_vm(myBuild.k8s_custom_objects_api, myBuild.k8s_namespace, myBuild.instancename)
        myBuild.k8s_vm_deleted = True
        logging.info(
            "KubeVirt VM deleted. Standalone DataVolume/PVC '%s/%s' remains as the saved image artifact.",
            myBuild.k8s_namespace,
            myBuild.instancename,
        )
    else:
        logging.error("build has invalid env_provider")


########Append Files############
def append(applist, ssh, myBuild):
    for key in applist:
        file = str(key)
        appendtext = re.escape(applist[key])
        commandString = "sudo sed -i '$ a\\" + appendtext + "' " + file
        runCommand(ssh, commandString, myBuild)


#####Replace text Function->>>Work in progress#########
def replaceText(replace, ssh, myBuild):
    for key in replace:
        for subkey in replace[key]:
            file = key
            oldtext = str(subkey)
            newtext = str(replace[key][subkey])
            regexpress = "s/"+oldtext+"/"+newtext+"/g"
            totaltext = "sudo sed -i s'"+re.escape(regexpress)+"' "+file
            totaltext = "sudo sed -i 's/${"+oldtext+"}/${"+newtext+"}/g' file"
            logging.info(file)
            logging.info(oldtext)
            logging.info(newtext)
            logging.info(totaltext)
            #commandString = "sudo sed -i 's/"+str(oldtext)+"/"+str(newtext)+"/g' "+str(file)
            commandString = totaltext
            runCommand(ssh, commandString, myBuild)


######Handle npm's################
def npm(nplist, ssh, myBuild):
    for x in range(len(nplist)):
        targ = nplist[x]
        for y in targ:
            key = y
            value = targ[y]
            if value != '':
                commandString = "sudo npm install --prefix " + str(value) + " " + str(key)
                runCommand(ssh, commandString, myBuild)
            else:
                commandString = "sudo npm install " + str(key)
                runCommand(ssh, commandString, myBuild)


#########Handle reboots ##########################
def rebootFunc(rebootCheck, ssh, myBuild, connection_delay=180, retry_limit=3):
    if myBuild.env_provider in (EnvProvider.AWS, EnvProvider.GCP, EnvProvider.K8S_VM):
        commandString = "sudo reboot"
        runCommand(ssh, commandString, myBuild)
        counter = 0
        logging.info("Attempting to reconnect after reboot")
        while True:
            time.sleep(connection_delay)
            try:
                ssh = ssh_connect(myBuild)
                logging.info("Connection successful")
                break
            except Exception as e:
                logging.info("Error reconnecting, trying again")
                counter += 1
                if counter < retry_limit:
                    pass
                else:
                    logging.exception("Reboot Failed")
                    sys.exit(1)
    # TODO how does this ever get used? It seems to not be passed up to processSection
    #return connectionObj
    return ssh


#######Set Environment Variables.  TODO##############################
def envVariables(varlist, ssh, myBuild):
    for key in varlist:
        commandString = "sudo sed -i \'$ aexport " + str(key) + "='" + str(varlist[key]) + "'\' /etc/profile"
        runCommand(ssh, commandString, myBuild)


##############Mod File#######################
def modFile():
    pass


######Handle User Data ####################
def handleUserData(myBuild):
    if hasattr(myBuild, 'userdata'):
        pass
    elif hasattr(myBuild, "inhibitstartup") and myBuild.inhibitstartup:
        myBuild.userdata = "#!/bin/bash\necho '{\"lookupTableName\": \"delete\"}' > /opt/CloudyCluster/var/dbName.json"
    else:
        myBuild.userdata = ""
    myBuild.userdata = str(myBuild.userdata)


def parseConfig(scriptName):
    cp = configparser.SafeConfigParser()
    cp.read(scriptName)

    config_list = []
    for section in cp.sections():
        config_list.append(section)
    config_list = sorted(config_list)

    config = []
    for section in config_list:
        list = []
        for cp_option in cp.options(section):
            option = cp_option.split(".")[0]
            try:
                list.append({option: ast.literal_eval(cp.get(section, cp_option))})
            except:
                list.append({option: cp.get(section, cp_option)})
        config.append({section: list})

    return config


def runBuild(root, myBuild, ssh, scriptName):
    if scriptName.endswith(".json"):
        with open(scriptName) as f:
            config = json.load(f.read())
    elif scriptName.endswith(".yaml"):
        with open(scriptName) as f:
            config = yaml.safe_load(f.read())
    else:
        config = parseConfig(scriptName)
    if len(config) < 1:
        logging.critical("Configuration must have at least one section.")
        sys.exit(1)

    logging.info("##############################################################################################")
    logging.info("Reached Log Stage")

    kubevirt_failed = False
    try:
        if root:
            ssh = processInitSection(config[0], myBuild)
            rest = config[1:]
        else:
            rest = config

        for section in rest:
            processSection(section, ssh, myBuild)
            # FIXME: processSection never returns a value so the following never runs
            '''
            x = processSection(section, connectionObj, myBuild)
            if hasattr(x, 'newConnect'):
                connectionObj = x['newConnect']
            '''
    except SystemExit as e:
        # KubeVirt: a failed build stops (halts) its builder VM and keeps the VM and DV/PVC.
        if root and e.code not in (None, 0) and myBuild.env_provider == EnvProvider.K8S_VM:
            kubevirt_stop_failed_build(myBuild)
        raise
    except Exception as e:
        logging.exception("Error in initReturnList")
        stopInstance(myBuild)
        kubevirt_failed = myBuild.env_provider == EnvProvider.K8S_VM
    finally:
        if root and myBuild.env_provider == EnvProvider.K8S_VM:
            kubevirt_stop_port_forward(myBuild)

    if root and kubevirt_failed:
        if ssh is not None:
            ssh.disconnect()
        sys.exit(1)

    if root:
        ssh.disconnect()
        # TODO delete this after testing refactor of connectionObj to SSHConnection
        '''
        try:
            sshControl('disconnect', myBuild, connectionObj)    
        except Exception as e:
            logging.exception("No connection exists, no need to disconnect")
        '''


class CommandFilter(logging.Filter):
    def filter(self, record):
        if "commandoutput" in record.__dict__:
            return 0
        else:
            return 1


def main(**kwargs):
    aparser = argparse.ArgumentParser(description="Builderdash - a utility to mash a bunch of stuff into someplace (cloud, or elsewhere) so others can use it.")
    aparser.add_argument('-V', '--version', action='version', version='Builderdash version 0.01')
    aparser.add_argument('-c', '--cfile', help="Config filename", required=False, default="")
    aparser.add_argument('-l', '--lfile', help="Log filename", required=False, default="builderdash.log")
    args = aparser.parse_args()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.addFilter(CommandFilter())
    logging.root.addHandler(stdout_handler)

    file_handler = logging.FileHandler(args.lfile, "a", "utf-8")
    formatter = logging.Formatter("%(asctime)s>%(levelname)s:%(module)s:%(funcName)s-%(message)s")
    file_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)

    logging.root.setLevel(logging.INFO)

    myBuild = Build()
    myBuild.times = []
    myBuild.timesprefix = ""
    
    runBuild(True, myBuild, None, args.cfile)
    if len(myBuild.times):
        logging.info("Section                             Time")
    for time in myBuild.times:
        seconds = int(time[1]) % 60
        minutes = int(time[1]) // 60
        logging.info(f"{time[0]:32s}{minutes:5d}:{seconds:02d}")


if __name__ == "__main__":
    main()
