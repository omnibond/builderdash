#!/usr/bin/python2.7
#Copyright Omnibond Systems, LLC. All rights reserved.
#
#Terms of Service are located at:
#http://www.cloudycluster.com/termsofservice
import argparse
import configparser
import os
import sys
import platform
import subprocess
import ast
import paramiko
import logging
import botocore
import botocore.session
import googleapiclient.discovery
import time
import datetime
import select
import re
import json
import yaml
import random
from textwrap import dedent

class Build():
    def setup(self, configSection):
        for key in configSection:
            config_key = key
 
        # First parse and set all attributes from this section.
        for option in configSection[config_key]:
            for key in option:
                name = key
            setattr(self, name, option[name])

        # Now set tagList.
        try:
            self.tagList = [self.buildtype.lower(), self.ostype.lower(), self.cloudservice.lower()]
        except:
            logging.exception("tagList element not found!")
            logging.info("Check input .yaml for buildtype, ostype, and cloudservice.")
            logging.info("Exiting...")
            sys.exit(1)
        if hasattr(self, "customtags"):
            self.tagList += self.customtags

        logging.info("List of Tags is %s" % self.tagList)

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
    commandString = 'sudo sed -i \'$ aexport CLOUD='+str(myBuild.cloudservice)+'\' /etc/profile'
    runCommand(ssh, commandString, myBuild)
    commandString = 'source /etc/profile'
    runCommand(ssh, commandString, myBuild)
    logging.info("end of cloudy vars")

def processInitSection(configSection, myBuild):
    logging.info("Entered processInitSection")
    myBuild.setup(configSection)

    logging.debug("in init")
    if myBuild.local == 'True':
        logging.debug("running in local mode")
        return None
    else:
        logging.debug("Running in remote mode")
        if hasattr(myBuild, 'sshkey'):
            pass
        else:
            logging.info("no sshkey please configure one")
            sys.exit(1)
        logging.info("instance type is %s", str(myBuild.instancetype))
        response = launchInstance(myBuild)
        myBuild = response
        logging.info("remoteIp is %s", str(myBuild.remoteIp))

        # *******   loop / sleep until userapps
        if hasattr(myBuild, 'instancetype'):
            logging.info("instance type is %s", str(myBuild.instancetype))
        # Give instance time to boot, for ssh service to come up, and for cloudinit to run, etc.
        time.sleep(20)
        ssh = ssh_connect(myBuild)
        logging.info('osType inside process init is %s', str(myBuild.ostype))

        # TODO why are we installing wget here? Can this be cleaned up?
        run_cmd = 'sudo yum install wget -y'
        logging.info('calling exec_command on: %s', run_cmd)
        stdin, stdout, stderr = ssh.get_target_session().exec_command(run_cmd, get_pty=True)
        run_ret = stdout.channel.recv_exit_status()
        logging.info('exec_command returned: %d', run_ret)
        #logging.info('exec_command stdout: %s', stdout)
        #logging.info('exec_command stderr: %s', stderr)
        #setCloudyClusterEnvVars(ssh, myBuild)

        return ssh

def launchInstance(myBuild):
    if myBuild.cloudservice == 'aws':
        result = awsInstance(myBuild)
    elif myBuild.cloudservice == 'gcp':
        result = googleInstance(myBuild)
    elif myBuild.cloudservice == 'kubevirt':
        result = kubevirt_instance(myBuild)
    else:
        logging.info("No cloudservice was found in the cfg file. Please put one under the init section in your cfg file")
        sys.exit(1)
    myBuild = result
    return(myBuild)

def get_instance_name(myBuild, sourcename):
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
    myBuild = handleUserData(myBuild)

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
        get_instance_name(myBuild, sourcename)
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
        try:
            description = client.describe_instances(InstanceIds = [myBuild.instanceId])
        except:
            pass
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
        return(myBuild)                

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

    get_instance_name(myBuild, myBuild.sourceimage)

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
    return(myBuild)

def kubevirt_instance(myBuild):
    # log source image
    print("Source image is " + str(myBuild.sourceimage))
    get_instance_name(myBuild, myBuild.sourceimage)
    if hasattr(myBuild, "disksize"):
        disksize = myBuild.disksize
    else:
        disksize = "55"
    kv_inst_tmpl = dedent('''\
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
                accessModes:
                - {data_volume_pvc_access_mode}
                resources:
                  requests:
                    storage: {data_volume_pvc_storage_capacity}
              source:
                http:
                  url: {data_volume_source_http_url}''')

    with open(str(myBuild.pubkeypath), 'r') as f:
        kubevirt_public_key_openssh = f.read()

    d = {'name': myBuild.instancename,
         'namespace': 'default',
         'labels': {},
         'instance_state': 'true',
         'instance_type_kind': 'VirtualMachineInstancetype',
         'instance_type_name': myBuild.instancetype,
         'mac_address': 'ee:ee:ee:ee:ee:ee',
         'data_volume_disk_name': 'data-volume-disk',
         'ssh_user': str(myBuild.sshkeyuser),
         'public_key_openssh': kubevirt_public_key_openssh,
         'data_volume_name': 'data-volume-' + myBuild.instancename,
         'data_volume_pvc_access_mode': 'ReadWriteOnce',
         'data_volume_pvc_storage_capacity': disksize,
         'data_volume_source_http_url': myBuild.sourceimage,
         'plain_text_passwd': myBuild.plain_text_passwd
        }
    rendered = kv_inst_tmpl.format(**d)
    with open('/tmp/builderdash-kubevirt-instance-manifest.yaml', 'w') as wf:
        wf.write(rendered)
    logging.info('myBuild.cloudservice is: %s', myBuild.cloudservice)
    logging.info('myBuild.instancename is: %s', myBuild.instancename)
    logging.info('Applying generated kubevirt vm manifest for build instance.')
    try:
        manifest_output = subprocess.check_output(['kubectl', 'apply', '-f', '-'], universal_newlines=True,
                                                  input=rendered).strip()
    except subprocess.CalledProcessError:
        manifest_output = None
    logging.info('Output from applying manifest is %s', manifest_output)

    instance_ready = False
    counter = 0
    while not instance_ready and counter < 60:
        try:
            vm_output = subprocess.check_output(['kubectl', 'get', 'vm', myBuild.instancename, '-o', 'json']).strip()
        except subprocess.CalledProcessError:
            vm_output = None
            break
        # TODO add try for loading json
        vm_data = json.loads(vm_output)
        #logging.info('VM output is %s', yaml.dump(vm_data))
        if vm_data.get('status') and vm_data.get('status').get('ready'):
            logging.info('kubevirt VM is READY: %s', myBuild.instancename)
            instance_ready = True
        else:
            logging.info('kubevirt VM is NOT READY. printableStatus: %s', vm_data.get('status').get('printableStatus'))
            #logging.info("vm_data['status'] = \n%s", yaml.dump(vm_data['status']))
            counter += 1
            time.sleep(10)
    # Gather remote ip of pod (with kubevirt instance inside)
    try:
        vmi_output = subprocess.check_output(['kubectl', 'get', 'vmi', myBuild.instancename, '-o', 'json']).strip()
    except subprocess.CalledProcessError:
        vmi_output = None
    # TODO add try for loading json
    vmi_data = json.loads(vmi_output)
    remoteIp = vmi_data['status']['interfaces'][0]['ipAddress']
    myBuild.remoteIp = remoteIp
    myBuild.instanceId = None
    return myBuild

def dispatchOption(option, args, ssh, myBuild):
    logging.info("%s %s", option, args)
    if option == "testtouch":
        testtouch(args, ssh, myBuild)
    elif option == "mkdir":
        makeDirectory(args, ssh, myBuild)
    elif option == "filetransfer":
        fileTransferSFTP(args, ssh, myBuild)
        #fileTransfer(args, ssh, myBuild)
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
        compressOrExtract(args, ssh, myBuild)
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

class SSHConnection:
    def __init__(self, target_username, target_key_file, target_ip, target_port=22, jump_host_enabled=False,
                 jump_host_username=None, jump_host_key_file=None, jump_host_external_ip=None,
                 jump_host_internal_ip=None, jump_host_port=22):
        self.jump_host_enabled = jump_host_enabled
        self.target_username = target_username
        self.target_key_file = target_key_file
        self.target_ip = target_ip
        self.target_port = target_port
        self.jump_host_username = jump_host_username
        self.jump_host_key_file = jump_host_key_file
        self.jump_host_external_ip = jump_host_external_ip
        self.jump_host_internal_ip = jump_host_internal_ip
        self.jump_host_port = jump_host_port
        self.jump_host_session = None
        self.target_session = None

    def connect(self):
        logging.info('SSHConnection.connect called')
        if self.jump_host_enabled:
            self.__jump_host_connect()
        else:
            self.__direct_target_connect()

    # Private method called by connect for establishing a ssh connection directly to the target.
    def __direct_target_connect(self, retry_limit=6, retry_delay=30):
        logging.info('SSHConnection.__direct_target_connect called')
        # TODO: what about this function?
        ssh_key_obj = paramiko.RSAKey.from_private_key_file(self.target_key_file)
        target_session = paramiko.SSHClient()
        logging.info('target_session is: %s', str(target_session))
        target_session.set_missing_host_key_policy(paramiko.WarningPolicy())
        counter = 0
        while True:
            try:
                target_session.connect(hostname=self.target_ip, port=self.target_port,
                                       username=self.target_username, pkey=ssh_key_obj, look_for_keys=False)
                logging.info('Connection successful')
                break
            except Exception as e:
                logging.error('Error connecting: %s', e)
                counter += 1
                if counter < retry_limit:
                    logging.info('Trying again after %d seconds', retry_delay)
                    time.sleep(retry_delay)
                else:
                    logging.error('__direct_target_connect failed after %d attempts. Disconnected', retry_limit)
                    # sys.exit(1)
                    return
        self.target_session = target_session

    # Private method called by connect for establishing connection to target via a jump host.
    def __jump_host_connect(self, retry_limit=6, retry_delay=30):
        logging.info('SSHConnection.__jump_host_connect called')
        jump_host_session = paramiko.SSHClient()
        jump_host_session.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # NOTE there is no retry for connection to the jump_host as it is assumed to be ready and operational.
        # TODO this assumes jump host uses OpenSSH formatted ssh key. What if PEM formatted? Let's support both types.
        try:
            logging.info('Attempting ssh connection via jump_host: %s', {
                'jump_host_username': self.jump_host_username,
                'jump_host_key_file': self.jump_host_key_file,
                'jump_host_external_ip': self.jump_host_external_ip
            })
            jump_host_session.connect(hostname=self.jump_host_external_ip, port=self.jump_host_port,
                                       username=self.jump_host_username, key_filename=self.jump_host_key_file)
            logging.info('jump_host connection successful')
        except Exception as e:
            logging.error('Error connecting to jump_host: %s', e)
            #sys.exit(1)
            return
        jump_host_transport = jump_host_session.get_transport()
        channel_src_addr = (self.jump_host_internal_ip, self.jump_host_port)
        channel_dest_addr = (self.target_ip, self.target_port)
        counter = 0
        while True:
            try:
                logging.info('Opening channel from jump_host to target: %s', {
                    'channel_dest_addr': channel_dest_addr,
                    'channel_src_addr': channel_src_addr
                })
                jump_host_channel = jump_host_transport.open_channel('direct-tcpip', channel_dest_addr,
                                                                     channel_src_addr)
                break
            except Exception as e:
                logging.error('Error opening channel from jump_host to target: %s', e)
                counter += 1
                if counter < retry_limit:
                    logging.info('Trying again after %d seconds', retry_delay)
                    time.sleep(retry_delay)
                else:
                    logging.error('__direct_target_connect failed after %d attempts. Disconnected', retry_limit)
                    jump_host_session.close()
                    #sys.exit(1)
                    return
        target_session = paramiko.SSHClient()
        target_session.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        counter = 0
        while True:
            try:
                logging.info('connecting to target via jump_host: %s', {
                    'target_ip': self.target_ip,
                    'target_username': self.target_username,
                    'target_key_file': self.target_key_file,
                    'jump_host_channel': jump_host_channel
                })
                target_session.connect(hostname=self.target_ip, username=self.target_username,
                                        key_filename=self.target_key_file, sock=jump_host_channel)
                logging.info('target connection successful')
                break
            except Exception as e:
                logging.error('error connecting to target: %s', e)
                counter += 1
                if counter < retry_limit:
                    logging.info('Trying again after %d seconds', retry_delay)
                    time.sleep(retry_delay)
                else:
                    logging.error('__direct_target_connect failed after %d attempts. Disconnected', retry_limit)
                    jump_host_session.close()
                    #sys.exit(1)
                    return
        self.jump_host_session = jump_host_session
        self.target_session = target_session

    def disconnect(self):
        logging.info("SSHConnection.disconnect called: %s", self)
        try:
            logging.info('calling self.target_session.close()')
            self.target_session.close()
            self.target_session = None
        except Exception as e:
            logging.warning('self.target_session.close() failed: %s', e)
        logging.info('successfully closed self.target_session')

        if self.jump_host_enabled:
            try:
                logging.info('calling self.jump_host_session.close()')
                self.jump_host_session.close()
                self.jump_host_session = None
            except Exception as e:
                logging.warning('self.jump_host_session.close() failed: %s', e)
            logging.info('successfully closed self.jump_host_session')

    def reconnect(self):
        logging.info("SSHConnection.reconnect called: %s", self)
        self.disconnect()
        self.connect()

    def is_alive(self):
        logging.info("SSHConnection.is_alive called: %s", self)
        if self.target_session is None:
            return False

        transport = self.target_session.get_transport()
        if not transport.is_active():
            return False

        try:
            transport.send_ignore()
        except EOFError as e:
            return False
        return True

    def get_target_session(self):
        return self.target_session


def ssh_connect(myBuild):
    logging.info('ssh_connect called')
    if hasattr(myBuild, 'jump_host_enabled') and myBuild.jump_host_enabled:
        ssh = SSHConnection(myBuild.sshkeyuser, myBuild.sshkey, myBuild.remoteIp, myBuild.target_ssh_port,
                            myBuild.jump_host_enabled, myBuild.jump_host_ssh_user,
                            myBuild.jump_host_priv_ssh_key_path, myBuild.jump_host_external_ip_address,
                            myBuild.jump_host_internal_ip_address, myBuild.jump_host_ssh_port)
    else:
        ssh = SSHConnection(myBuild.sshkeyuser, myBuild.sshkey, myBuild.remoteIp, myBuild.target_ssh_port)
    # TODO handle connection failures and implement retry here
    ssh.connect()
    if ssh.is_alive():
        logging.info('SSH Connection IS ALIIIIIIVE!')
        #return ssh.get_target_session()
        return ssh
    else:
        logging.error('failed to establish ssh connection to target')
        ssh.disconnect()
        #sys.exit(1)
        return None


###########Run Commands on Instance######################################
def runCommand(ssh, commandString, myBuild, **kwargs):
    if 'local' in kwargs:
        local = kwargs['local']
    else:
        local = myBuild.local
    if local is False or local == 'False':
        try:
            logging.info("running command as remote: %s", commandString)
            # Send the command (non-blocking)
            stdin, stdout, stderr = ssh.get_target_session().exec_command(commandString, get_pty=True)
            # Wait for the command to terminate
            buffer = b""
            # TODO: Can select wait on exit_status as well?
            while not stdout.channel.exit_status_ready():
                rl, wl, xl = select.select([stdout.channel, stderr.channel], [], [], 1)
                # Standard output and standard error are mixed.
                for s in rl:
                    output = s.recv(1024)
                    if output != None:
                        sys.stdout.buffer.write(output)
                        sys.stdout.flush()
                        buffer += output
                # Buffer until newline since the log module will insert
                # its own newline.  Logging output will not go to stdout
                # since that is already done.
                list = buffer.split(b"\n")
                for line in list[:-1]:
                    logging.info("%s", line.decode(), extra={"commandoutput": True})
                buffer = list[-1]
            while stdout.channel.recv_ready():
                output = stdout.channel.recv(1024)
                if output != None:
                    sys.stdout.buffer.write(output)
                    sys.stdout.flush()
                    buffer += output
            while stderr.channel.recv_ready():
                output = stderr.channel.recv(1024)
                if output != None:
                    sys.stdout.buffer.write(output)
                    sys.stdout.flush()
                    buffer += output
            list = buffer.split(b"\n")
            for line in list[:-1]:
                logging.info("%s", line.decode(), extra={"commandoutput": True})
            buffer = list[-1]
            if len(buffer):
                logging.info("%s", buffer.decode(), extra={"commandoutput": True})
            status = stdout.channel.recv_exit_status()
            logging.info("Exit status is %d", status)
            if status != 0:
                logging.exception('ERROR running command')
                stopInstance(myBuild)
                sys.exit(1)
            lines = stdout.readlines()
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
    if myBuild.cloudservice == 'aws':
        session = botocore.session.get_session()
        client = session.create_client('ec2', region_name = str(myBuild.region))
        response = client.stop_instances(InstanceIds = [str(myBuild.instanceId)]) 
    elif myBuild.cloudservice == 'gcp':
        compute = googleapiclient.discovery.build('compute', 'v1', cache_discovery=False)
        result = compute.instances().stop(project=myBuild.projectname, zone=myBuild.region, instance=str(myBuild.instancename)).execute()
    elif myBuild.cloudservice == 'kubevirt':
        # TODO - actually stop the build for kubevirt.
        logging.warning("stopInstance is not implemented for cloudservice: kubevirt")
        pass
    else:
        logging.info("No cloudservice was found in the cfg file. Please put one under the init section in your cfg file")
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
    if myBuild.cloudservice == 'azure':
        logging.info('This feature not supported')
    elif myBuild.cloudservice == 'aws':
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
    elif myBuild.cloudservice == 'gcp':
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
        savedImage = response
    elif myBuild.cloudservice == 'kubevirt':
        # TODO
        logging.warning('Saving kubevirt image: NOT IMPLEMENTED YET !!!')
        '''
        examples using the kubevirt Export API:
    
        https://kubevirt.io/user-guide/operations/export_api/
        
        # raw
        virtctl vmexport download vmexportname --vm=builderdash-jdenton-cj-control-20240229-b1f6cf7plus-cc2d --volume=data-volume-disk --format=raw --output=/tmp/disk.img --keep-vme --port-forward
        
        # gzip
        virtctl vmexport download vmexportname --vm=builderdash-jdenton-cj-control-20240229-b1f6cf7plus-cc2d --volume=data-volume-disk --format=gzip --output=/tmp/disk.img.gz --keep-vme --port-forwar    
        '''
        savedImage = 'NONE'
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
def compressOrExtract(tarlist, ssh, myBuild):
    for key in tarlist:
        logging.info(key)
        local = tarlist[key][0]
        action = tarlist[key][1]
        location = tarlist[key][2]
        tarName = key
        if local == False or local == 'False':
            if action == 'compress':
                commandString = 'sudo tar -zcvf '+str(tarName)+' '+str(location)
                runCommand(ssh, commandString, myBuild, local=False)
            elif action == 'extract':
                commandString = 'sudo tar -zxvf '+str(tarName)+' -C '+str(location)
                runCommand(ssh, commandString, myBuild, local=False)
            else:
                logging.info("No action was specified in cfg file.  Could not compress or extract tar.")
                
        elif local == True or local == 'True':
            if action == 'compress':
                commandString = 'tar -zcvf '+str(tarName)+' '+str(location)
                runCommand(ssh, commandString, myBuild, local=True)
            elif action == 'extract':
                commandString = 'tar -zxvf '+str(tarName)+' -C '+str(location)
                runCommand(ssh, commandString, myBuild, local=True)
            else:
                logging.info("No action was specified in cfg file.  Could not compress or extract tar.")

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


def fileTransferSFTP(file_transfer_list, ssh, build):
    logging.info("This is file_transfer_list: %s", file_transfer_list)
    # TODO - implement tostring for SSHConnection
    logging.info('ssh connection details: %s', ssh)
    sftp = ssh.get_target_session().open_sftp()
    for key in file_transfer_list:
        local_path = file_transfer_list[key][0]
        remote_path = file_transfer_list[key][1]
        upload = file_transfer_list[key][2]
        logging.info({'upload': upload, 'type(upload)': type(upload),
                      'local_path': local_path, 'remote_path': remote_path})
        # FIXME: This approach might not be correct and needs more attention
        # TODO should I implement this workaround the other way? Is upload=False / get even used?
        if remote_path == '.':
            remote_path = os.path.basename(local_path)
            logging.info('remote_path has been automatically changed to basename(local_path), which is: %s',
                         remote_path)
        if upload:
            logging.info("before sftp file put")
            sftp.put(local_path, remote_path)
        else:
            logging.info("before sftp file get")
            sftp.get(remote_path, local_path)
        logging.info("after sftp transfer")
    sftp.close()


def fileTransfer(ftlist, ssh, myBuild):
    user = "%s@%s:" % (myBuild.sshkeyuser, myBuild.remoteIp)
    logging.info("This is ftlist: %s", ftlist)
    for key in ftlist:
        sourcepath = ftlist[key][0]
        destination = ftlist[key][1]
        upload = ftlist[key][2]
        logging.info("This is sourcepath: %s \n This is user: %s \n This is destination: %s \n" % (sourcepath, user, destination))
        logging.info("upload is %s; type %s", upload, type(upload))
        if upload:
            logging.info("It this one!")
            commandString = "scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i %s %s %s" % (myBuild.sshkey, sourcepath, user + destination)
        else:
            logging.info("No this one!")
            commandString = "scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i %s %s %s" % (myBuild.sshkey, user + sourcepath, destination)
        logging.info("commandString is %s", commandString)
        logging.info("before runCommand FT")
        runCommand(ssh, commandString, myBuild, local=True)
        logging.info("after runCommand FT")

def deleteInstance(delList, myBuild):
    if myBuild.cloudservice == 'aws':
        session = botocore.session.get_session()
        client = session.create_client('ec2', region_name = str(myBuild.region))
        try:
            response = client.terminate_instances(InstanceIds=[str(myBuild.instanceId)])
            logging.info(response)
        except:
            logging.info("Failed to delete instance.  Please do so manually")
    elif myBuild.cloudservice == 'gcp':
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
    elif myBuild.cloudservice == 'kubevirt':
        # TODO
        logging.error('TODO implement deleteInstance for kubevirt!!!')
    else:
        logging.info("No proper cloud service listed in Init Section of cfg file.")

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
    if myBuild.cloudservice in ("aws", "gcp", "kubevirt"):
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
    return myBuild


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
    except Exception as e:
        logging.exception("Error in initReturnList")
        stopInstance(myBuild)

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
