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
import io
import logging
import requests
import botocore
import botocore.session
import googleapiclient.discovery
import time, datetime
import select
import json    
import re
import resource
import pdb
import traceback
import json
import yaml
import pty
import random

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
def setCloudyClusterEnvVars(connectionObj, myBuild):
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
    CC_AWS_SSH_USERNAME = myBuild.sshkeyuser
    commandString = 'sudo sed -i \'$ aexport CC_BUILD_TYPE='+CC_BUILD_TYPE+'\' /etc/profile'
    runCommand(connectionObj, commandString, myBuild)
    commandString = 'sudo sed -i \'$ aexport CC_OS_NAME='+CC_OS_NAME+'\' /etc/profile'
    runCommand(connectionObj, commandString, myBuild)
    commandString = 'sudo sed -i \'$ aexport CC_AWS_SSH_USERNAME='+CC_AWS_SSH_USERNAME+'\' /etc/profile'
    runCommand(connectionObj, commandString, myBuild)
    commandString = 'sudo sed -i \'$ aexport CLOUD='+str(myBuild.cloudservice)+'\' /etc/profile'
    runCommand(connectionObj, commandString, myBuild)
    commandString = 'source /etc/profile'
    runCommand(connectionObj, commandString, myBuild)
    logging.info("end of cloudy vars")

def processInitSection(configSection, connectionObj, myBuild):
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
        sshKey = myBuild.sshkey
        connectionObj = sshControl('connect', myBuild, connectionObj)
        osType = myBuild.ostype
        logging.info('osType inside process init is %s', str(myBuild.ostype))
        stdin, stdout, stderr = connectionObj.exec_command('sudo yum install wget -y', get_pty=True)
        stdout.channel.recv_exit_status()
        #setCloudyClusterEnvVars(connectionObj, myBuild)
        return connectionObj

def launchInstance(myBuild):
    if myBuild.cloudservice == 'aws':
        result = awsInstance(myBuild)
    elif myBuild.cloudservice == 'gcp':
        result = googleInstance(myBuild)
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
                logging.info(response['SpotPriceHistory'][x]['SpotPrice'])
                currentSpot = response['SpotPriceHistory'][x]['SpotPrice']
            myBuild.awsspotprice = currentSpot * 1.2
            logging.info("awsspotprice is %s", str(myBuild.awsspotprice))
        blockDeviceStuff = [{'DeviceName': '/dev/sda1', "Ebs": {"DeleteOnTermination": True, "VolumeSize": disksize, "VolumeType": "gp2"}}]
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
        blockDeviceStuff = [{'DeviceName': '/dev/sda1', "Ebs": {"DeleteOnTermination": True, "VolumeSize": disksize, "VolumeType": "gp2"}}]
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
    if (myBuild.buildtype == "base" or myBuild.buildtype == "kernel") and \
        hasattr(myBuild, "imagefamily") and myBuild.imagefamily != "none":
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
        body["metadata"]["items"].append({"key": "startup-script", "value": "echo '{\"lookupTableName\": \"delete\"}' > /opt/CloudyCluster/var/dbName.json"})
    myBuild.tempsshkey = tempsshkey
    myBuild.machine_type = machine_type
    x = compute.instances().insert(project=myBuild.projectname, zone=zone, body=body).execute()
    place = None
    counter = 0
    while place != True and counter < 60:
        result = compute.instances().list(project=myBuild.projectname, zone=zone, filter='(status eq RUNNING) (name eq ' + str(myBuild.instancename) + ')').execute()
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

def dispatchOption(option, args, connectionObj, myBuild):
    logging.info("%s %s", option, args)
    if option == "testtouch":
        testtouch(args, connectionObj, myBuild)
    elif option == "mkdir":
        makeDirectory(args, connectionObj, myBuild)
    elif option == "filetransfer":
        fileTransfer(args, connectionObj, myBuild)
    elif option == "downloads":
        downloads(args, connectionObj, myBuild)
    elif option == "extract":
        extract(args, connectionObj, myBuild)
    elif option == "reporpms":
        repoRpms(args, connectionObj, myBuild)
    elif option == "pathrpms":
        pathRpms(args, connectionObj, myBuild)
    elif option == "builderdash":
        builderdash(args, connectionObj, myBuild)
    elif option == "copyfiles":
        copyFiles(args, connectionObj, myBuild)
    elif option == "movefiles":
        moveFiles(args, connectionObj, myBuild)
    elif option == "copysubtree":
        copySubtree(args, connectionObj, myBuild)
    elif option == "chmod":
        chmod(args, connectionObj, myBuild)
    elif option == "chown":
        chown(args, connectionObj, myBuild)
    elif option == "sourcescripts":
        sourceScripts(args, connectionObj, myBuild)
    elif option == "delete":
        deleteFiles(args, connectionObj, myBuild)
    elif option == "commands":
        commandsexec(args, connectionObj, myBuild)
    elif option == "saveimage":
        savedImage = saveImage(args, myBuild)
    elif option == "deleteinstance":
        deleteInstance(args, myBuild)
    elif option == "append":
        append(args, connectionObj, myBuild)
    elif option == "replace":
        replaceText(args, connectionObj, myBuild)
    elif option == "npm":
        npm(args, connectionObj, myBuild)
    elif option == "reboot":
        connectionObj = rebootFunc(args, connectionObj, myBuild)
        return {'newConnect': connectionObj}
    elif option == "envvar":
        envVariables(args, connectionObj, myBuild)
    elif option == "tar":
        compressOrExtract(args, connectionObj, myBuild)
    elif option == "cloudyvars":
        setCloudyClusterEnvVars(connectionObj, myBuild)
    else:
        logging.error("Option %s not recognized", option)
        sys.exit(1)

def processSection(configSection, connectionObj, myBuild):
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
                dispatchOption(newoption, option[name], connectionObj, myBuild)
            else:
                logging.info("Permission Tags Not in Build Type")
        except Exception as e:
            logging.exception("Error in processing the section")
            sys.exit(1)
    myBuild.timesprefix = myBuild.timesprefix[:-1]
    end_time = time.time()
    myBuild.times.append((myBuild.timesprefix + config_key, end_time - start_time))

######### Handles the creation of the client and connectionObj with paramiko###############
def sshControl(sshControlCommand, myBuild, connectionObj):
    logging.info("enteredsshControl")
    if sshControlCommand == 'connect':
        logging.info('Connecting...')
        time.sleep(20)
        logging.info("Attempting to Connect with ssh client")
        sshKeyObj = paramiko.RSAKey.from_private_key_file(myBuild.sshkey)
        connectionObj = paramiko.SSHClient()
        logging.info('connectionObj returns %s', str(connectionObj))
        connectionObj.set_missing_host_key_policy(paramiko.WarningPolicy())
        counter  = 0
        conn = False
        

        while True:
            try:
                connectionObj.connect(hostname = myBuild.remoteIp, port = 22, username = myBuild.sshkeyuser, pkey = sshKeyObj, look_for_keys = False)
                logging.info("Connection successful")
                break
            except Exception as e:
                logging.error("Error connecting: %s", e)
                counter += 1
                if counter < 6:
                    logging.info("Trying again")
                    time.sleep(30)
                else:
                    logging.error("Failed after 6 attempts.  Disconnected")
                    sys.exit(1)
                    
        z = 0
        return (connectionObj)
    elif sshControlCommand == 'disconnect':
        logging.info("Attempting to Disconnect")
        connectionObj.close()   
        logging.info("Disconnect Successful")
    elif sshControlCommand == 'local':
        logging.info("Local operation")

###########Run Commands on Instance######################################
def runCommand(connectionObj, commandString, myBuild, **kwargs):
    if 'local' in kwargs:
        local = kwargs['local']
    else:
        local = myBuild.local
    # TODO: Local command output should be logged as well.
    if local == False or local == 'False':
        try:
            logging.info("running command as remote: %s", commandString)
            # Send the command (non-blocking)
            stdin, stdout, stderr = connectionObj.exec_command(commandString, get_pty=True)
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
    elif local == True or local == 'True':
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
    logging.info("stopping instance...")
    if myBuild.cloudservice == 'aws':
        session = botocore.session.get_session()
        client = session.create_client('ec2', region_name = str(myBuild.region))
        response = client.stop_instances(InstanceIds = [str(myBuild.instanceId)]) 
    elif myBuild.cloudservice == 'gcp':
        compute = googleapiclient.discovery.build('compute', 'v1', cache_discovery=False)
        result = compute.instances().stop(project=myBuild.projectname, zone=myBuild.region, instance=str(myBuild.instancename)).execute()
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
        response = client.create_image(Description='Builderdash', Name=str(imageName), InstanceId = str(myBuild.instanceId), BlockDeviceMappings=[{'DeviceName': '/dev/sda1','Ebs': {'VolumeType': 'gp2'}}], TagSpecifications=[{"ResourceType": "image", "Tags": tags}])
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
    return(savedImage)

###########Execute a Command As Directly Typed##############################
def commandsexec(commando, connectionObj, myBuild):
    for key in commando:
        commandString = str(key)
        runCommand(connectionObj, commandString, myBuild)

#########Just a test function for touching .txt files########################
def testtouch(touchy, connectionObj, myBuild):
    for key in touchy:
        logging.info(key)
        commandString = 'sudo touch ~/'+str(key)
        runCommand(connectionObj, commandString, myBuild)

#########Tar Compress or Tar Extract Files#######################################
def compressOrExtract(tarlist, connectionObj, myBuild):
    for key in tarlist:
        logging.info(key)
        local = tarlist[key][0]
        action = tarlist[key][1]
        location = tarlist[key][2]
        tarName = key
        if local == False or local == 'False':
            if action == 'compress':
                commandString = 'sudo tar -zcvf '+str(tarName)+' '+str(location)
                runCommand(connectionObj, commandString, myBuild, local=False) 
            elif action == 'extract':
                commandString = 'sudo tar -zxvf '+str(tarName)+' -C '+str(location)
                runCommand(connectionObj, commandString, myBuild, local=False)
            else:
                logging.info("No action was specified in cfg file.  Could not compress or extract tar.")
                
        elif local == True or local == 'True':
            if action == 'compress':
                commandString = 'tar -zcvf '+str(tarName)+' '+str(location)
                runCommand(connectionObj, commandString, myBuild, local=True) 
            elif action == 'extract':
                commandString = 'tar -zxvf '+str(tarName)+' -C '+str(location)
                runCommand(connectionObj, commandString, myBuild, local=True)
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
def sourceScripts(sslist, connectionObj, myBuild):
    for key in sslist:
        logging.info(key)
        commandString = 'sudo chmod +x '+str(key)
        runCommand(connectionObj, commandString, myBuild)
        commandString = 'sudo '+str(key)
        runCommand(connectionObj, commandString, myBuild)

#################Downloads Files#####################
def downloads(dllist, connectionObj, myBuild):
    for source in dllist:
        commandString = 'sudo wget -P '+str(dllist[source])+ ' '+str(source)
        runCommand(connectionObj, commandString, myBuild)

#################Extract Files######################################       
def extract(exlist, connectionObj, myBuild):
    for key in exlist:
        filelocation = key
        destination = exlist[key][0]
        extract_method = exlist[key][1]
        commandString = 'sudo tar '+str(extract_method)+' '+str(filelocation)+' -C '+str(destination)
        runCommand(connectionObj, commandString, myBuild)

###############Install from packages###############################
def repoRpms(rrlist, connectionObj, myBuild):
    runCommand(connectionObj, "sudo yum install -y " + " ".join(rrlist), myBuild)

##############Yum localinstall###########
def pathRpms(prlist, connectionObj, myBuild):
    for key in prlist:
        logging.info(key)
        commandString = 'sudo yum localinstall '+str(key)+' -y'
        runCommand(connectionObj, commandString, myBuild)

##############Calls another builderdash script###############
def builderdash(blist, connectionObj, myBuild):
    for key in blist:
        logging.info(key)
        logging.info("STARTING======>>>>>>>>>>"+str(key))
        subprocess.call('pwd', shell=True)
        runBuild(False, myBuild, connectionObj, str(key))

#############Copies Files from one location to Another###############
def copyFiles(cflist, connectionObj, myBuild):
    for key in cflist:
        commandString = 'sudo cp '+str(key)+' '+str(cflist[key])
        runCommand(connectionObj, commandString, myBuild)

#############Move Files from one location to Another###############
def moveFiles(mflist, connectionObj, myBuild):
    for key in mflist:
        commandString = 'sudo mv '+str(key)+' '+str(mflist[key])
        runCommand(connectionObj, commandString, myBuild)

################Delete Files#######################################
def deleteFiles(delist, connectionObj, myBuild):
    for key in delist:
        commandString = 'sudo rm '+str(key)
        runCommand(connectionObj, commandString, myBuild)        

#############Copies a Subtree######################################
def copySubtree(cslist, connectionObj, myBuild):
    for key in cslist:
        commandString = 'sudo cp -R '+str(key)+' '+str(cslist[key])
        runCommand(connectionObj, commandString, myBuild)

#############Change Permissions###############################
def chmod(cmlist, connectionObj, myBuild):
    for key in cmlist:
        commandString = 'sudo chmod '+str(cmlist[key])+' '+str(key)
        runCommand(connectionObj, commandString, myBuild)

#############Change Ownsership###############################
def chown(colist, connectionObj, myBuild):
    for key in colist:
        options = ''
        group = ''
        file = ''
        if colist[key][0] != '':
            options = str(colist[key][0])+' '
        if colist[key][1] != '':
            group = ':'+str(colist[key][1])
        if colist[key][2] != '':
            file = str(colist[key][2])
        commandString = 'sudo chown '+options+str(key)+group+' '+file
        runCommand(connectionObj, commandString, myBuild)

def makeDirectory(mklist, connectionObj, myBuild):
    for key in mklist:
        commandString = 'sudo mkdir '+ str(key)
        runCommand(connectionObj, commandString, myBuild)

def fileTransfer(ftlist, connectionObj, myBuild):
    user = "%s@%s:" % (myBuild.sshkeyuser, myBuild.remoteIp)
    for key in ftlist:
        sourcepath = ftlist[key][0]
        destination = ftlist[key][1]
        upload = ftlist[key][2]
        logging.info("upload is %s; type %s", upload, type(upload))
        if upload:
            commandString = "scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i %s %s %s" % (myBuild.sshkey, sourcepath, user + destination)
        else:
            commandString = "scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i %s %s %s" % (myBuild.sshkey, user + sourcepath, destination)
        logging.info("commandString is %s", commandString)
        logging.info("before runCommand FT")
        runCommand(connectionObj, commandString, myBuild, local=True)
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
    else:
        logging.info("No proper cloud service listed in Init Section of cfg file.")

########Append Files############
def append(applist, connectionObj, myBuild):
    for key in applist:
        file = str(key)
        appendtext = re.escape(applist[key])
        commandString = "sudo sed -i '$ a\\"+appendtext+"' "+file
        runCommand(connectionObj, commandString, myBuild)

#####Replace text Function->>>Work in progress#########
def replaceText(replace, connectionObj, myBuild):
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
            runCommand(connectionObj, commandString, myBuild)

######Handle npm's################
def npm(nplist, connectionObj, myBuild):
    for x in range(len(nplist)):
        targ = nplist[x]
        for y in targ:
            key = y
            value = targ[y]
            if value != '':
                commandString = "sudo npm install --prefix "+str(value)+" "+str(key)
                runCommand(connectionObj, commandString, myBuild)
            else:
                commandString = "sudo npm install "+str(key)
                runCommand(connectionObj, commandString, myBuild)

#########Handle reboots ##########################
def rebootFunc(rebootCheck, connectionObj, myBuild):
    if myBuild.cloudservice == "aws" or "gcp":
        commandString = "sudo reboot"
        runCommand(connectionObj, commandString, myBuild)
        counter = 0
        logging.info("Attempting to reconnect after reboot")
        while True:
            time.sleep(180)
            try:
                connectionObj = sshControl('connect', myBuild, connectionObj)
                logging.info("Connection successful")
                break
            except:
                logging.info("Error reconnecting, trying again")
                counter += 1
                if counter < 3:
                    pass
                else:
                    logging.exception("Reboot Failed")
                    sys.exit(1)
                    break
    return(connectionObj)
#######Set Environment Variables.  TODO##############################
def envVariables(varlist, connectionObj, myBuild):
    for key in varlist:
        commandString = "sudo sed -i \'$ aexport "+str(key)+"='"+str(varlist[key])+"'\' /etc/profile"
        runCommand(connectionObj, commandString, myBuild)

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

def runBuild(root, myBuild, connectionObj, scriptName):
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
            initSection = config[0]
            connectionObj = processInitSection(config[0], connectionObj, myBuild)
            rest = config[1:]
        else:
            rest = config

        for section in rest:
            x = processSection(section, connectionObj, myBuild)
            if hasattr(x, 'newConnect'):
                connectionObj = x['newConnect']
    except Exception as e:
        logging.exception("Error in initReturnList")
        stopInstance(myBuild)

    if root:
        try:
            sshControl('disconnect', myBuild, connectionObj)
        except Exception as e:
            logging.exception("No connection exists, no need to disconnect")

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
