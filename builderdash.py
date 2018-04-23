#!/usr/bin/python2.7
#Copyright Omnibond Systems, LLC. All rights reserved.
#
#Terms of Service are located at:
#http://www.cloudycluster.com/termsofservice
import argparse
import ConfigParser
import os
import sys
import platform
import subprocess
import ast
import paramiko
import StringIO
import logging
import requests
import botocore
import botocore.session
import googleapiclient.discovery
import time, datetime
import select
import uuid
import json    
import re
import pdb
import traceback

class Build(object):
    def __init__(self):
        pass
    # def setBuildTag(self):
    #     if self.buildtype == 'userapps':
    #         self.buildTag = 'userapps'
    #     elif self.buildtype == 'dev':
    #         self.buildTag = 'dev'
    #     elif self.buildtype == 'prod':
    #         self.buildTag = 'prod'
    def setOsTag(self):
        if self.ostype == 'centos':
            self.osTag = 'centos'
        elif self.ostype == 'rhel':
            self.osTag = 'rhel'
    def setCloudTag(self):
        if self.cloudservice == 'aws':
            self.cloudTag = 'aws'
        elif self.cloudservice == 'gcp':
            self.cloudTag = 'gcp'
        elif self.cloudservice == 'azure':
            self.cloudTag = 'azure'
    def setTagList(self):
        self.tagList = [self.osTag, self.cloudTag]
        if hasattr(self, 'customtags'):
            for item in range(len(self.customtags)):
                self.tagList.append(self.customtags[item])
        print "List of Tags is "
        print self.tagList



def processInitSection(configSection, config, connectionObj, myBuild):
        logging.info("Entered processInitSection")
        print bool(myBuild)
        if hasattr(myBuild, 'exists'):
            return(myBuild, connectionObj)
            
        else: 
            myBuild.exists = 'True'
            cstring = str(configSection).strip('"[]')
            cstring = cstring.strip("'")
            if str(configSection) == "['0000.init']":
                optList = config.options(cstring)
            for x in optList:
                y = config.get(cstring, str(x))
                try:
                    y = ast.literal_eval(y)
                except Exception as e:
                    logging.info("Encountered an exception in processInitSection")
                    pass
                setattr(myBuild, x, y)
            #myBuild.setBuildTag()
            myBuild.setOsTag()
            myBuild.setCloudTag()
            myBuild.setTagList()

            logging.debug("in init")
            if myBuild.local == 'True':
                print "Running in local mode"
                logging.debug("running in local mode")
            else:
                print "Running in remote mode"
                logging.debug("Running in remote mode")
                if hasattr(myBuild, 'sshkey'):
                    pass
                else:
                    print "no sshkey please configure one"
                    sys.exit(0)
                logging.info("instance type is %s", str(myBuild.instancetype))
                response = launchInstance(myBuild)
                myBuild = response
                print "remoteIp is " + str(myBuild.remoteIp)
                
                # *******   loop / sleep until userapps
                if hasattr(myBuild, 'instancetype'):
                    logging.info("instance type is %s", str(myBuild.instancetype))
                sshKey = config.get(cstring, "sshkey")
                connectionObj = sshControl('connect', myBuild, connectionObj)
                osType = config.get(cstring, "ostype")
                logging.info('osType inside process init is %s', str(myBuild.ostype))
                stdin, stdout, stderr = connectionObj.exec_command('sudo yum install wget -y', get_pty=True)
                stdout.channel.recv_exit_status()
                return (myBuild, connectionObj)   

                



def launchInstance(myBuild):
    if myBuild.cloudservice == 'aws':
        result = awsInstance(myBuild)
    elif myBuild.cloudservice == 'gcp':
        result = googleInstance(myBuild)
    else:
        print "No cloudservice was found in the cfg file. Please put one under the init section in your cfg file"
        sys.exit(0)
    myBuild = result
    return(myBuild)

def awsInstance(myBuild):
    logging.info("Running awsInstance")
    if hasattr(myBuild, "region"):
        print "Region is "+str(myBuild.region)
    session = botocore.session.get_session()
    client = session.create_client('ec2', region_name = str(myBuild.region))
    response = client.describe_account_attributes(AttributeNames=['supported-platforms'])['AccountAttributes'][0]['AttributeValues']
    for attr in response:
        if attr['AttributeValue'] == 'EC2':
            if myBuild.subnet != None:
                pass
            else:
                print "Your account has the EC2 Classic attribute.  You must specify a subnet in the init section of your cfg file"
                sys.exit(0)
        else:
            pass
    myBuild = handleUserData(myBuild)

    if hasattr(myBuild, "awsspot"):
        if hasattr(myBuild, "awsspotprice"):
            logging.info("awsspotprice is %s", str(myBuild.awsspotprice))
            try:
                az = myBuild.az
            except Exception as e:
                logging.info("There was an error getting the Availability Zone")
                print "Availability zone is required in your .cfg file init section.  Example:  az = us-west-1a"
                sys.exit(0)
        else:
            print "use current spot price + 20%"
            response = client.describe_spot_price_history(AvailabilityZone = str(myBuild.az), InstanceTypes=['t2.small'], ProductDescriptions=['Linux/UNIX'], StartTime = datetime.datetime.now(), EndTime = datetime.datetime.now())
            for thing in range(len(response['SpotPriceHistory'])):
                print response['SpotPriceHistory'][x]['SpotPrice']
                currentSpot = response['SpotPriceHistory'][x]['SpotPrice']
            myBuild.awsspotprice = currentSpot * 1.2
            logging.info("awsspotprice is %s", str(myBuild.awsspotprice))
        blockDeviceStuff = [{'DeviceName': '/dev/sda1', "Ebs": {"DeleteOnTermination": True, "VolumeSize": 45}}]
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
        print "Using on demand"
        logging.info("Using on demand")
        ######Spin up instance###########
        genUUID = str(uuid.uuid4())
        imageUUID = genUUID[:4]
        if hasattr(myBuild, "instancename"):
            myBuild.instancename = str(myBuild.instancename)+ '-'+ imageUUID
        else:
            myBuild.instancename = 'builderdash'+'-'+imageUUID
        logging.info("Spinning up the instance")
        iamstuff = {'Name': 'instance-admin'}
        blockDeviceStuff = [{'DeviceName': '/dev/sda1', "Ebs": {"DeleteOnTermination": True, "VolumeSize": 45}}]
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
                                        print "Remote ip is "+ str(remoteIp)
                                        myBuild.remoteIp = remoteIp
                                        keyexists = True
                                        break
        counter = 0
        print description['Reservations'][0]['Instances'][0]['State']['Name']
        while description['Reservations'][0]['Instances'][0]['State']['Name'] != 'running' and counter < 60:
            description = client.describe_instances(InstanceIds = [myBuild.instanceId])
            print description['Reservations'][0]['Instances'][0]['State']['Name']
            time.sleep(10)
            logging.info('Waiting for instance to come alive')
            print 'Waiting for instance to come alive'
            counter += 1
        logging.info('Remote IP is %s', str(myBuild.remoteIp))
        myBuild.projectName = 'None'
        return(myBuild)                
#######Google Launch is Next##########
def googleInstance(myBuild):
    compute = googleapiclient.discovery.build('compute', 'v1')
    zone = myBuild.region
    projectName = myBuild.projectname
    bucketName = myBuild.bucketname
    machine_type = "zones/%s/machineTypes/%s" % (zone, str(myBuild.instancetype))
    sourceImage = myBuild.sourceimage
    genUUID = str(uuid.uuid4())
    imageUUID = genUUID[:4]
    if hasattr(myBuild, "instancename"):
        myBuild.instancename = str(myBuild.instancename)+ '-'+ imageUUID
    else:
        myBuild.instancename = 'builderdash'+'-'+imageUUID
    with open(str(myBuild.pubkeypath), 'rb') as f:
        tempsshkey = str(myBuild.sshkeyuser)+':'+f.read()
    body = {
        'name': myBuild.instancename,
        'machineType': machine_type,
        'disks': [
            {
                'boot': True,
                'autoDelete': True,
                'initializeParams': {'sourceImage': myBuild.sourceimage}
            }
        ],

        'networkInterfaces': [{
            'network': 'global/networks/default',
            'accessConfigs': [
                {'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}
            ]
        }],

        'serviceAccounts': [{
            'email': os.environ['SERVICE_ACCOUNT_EMAIL'],
            'scopes': [
                'https://www.googleapis.com/auth/devstorage.read_write',
                'https://www.googleapis.com/auth/logging.write',
                'https://www.googleapis.com/auth/cloud-platform'
            ]
        }],

        'metadata': {
            'items': [{
                'key': 'bucket',
                'value': myBuild.bucketname
            }, {
                'key': 'ssh-keys',
                'value': tempsshkey
            }]
        }
    }
    myBuild.tempsshkey = tempsshkey
    myBuild.machine_type = machine_type
    x = compute.instances().insert(project=myBuild.projectname, zone=zone, body=body).execute()
    place = None
    counter = 0
    while place != True and counter < 60:
        result = compute.instances().list(project=myBuild.projectname, zone=zone, filter='(status eq RUNNING) (name eq ' + str(myBuild.instancename) + ')').execute()
        print("\nmyBuild.instancename is: " + str(myBuild.instancename))
        print("\nresult is: " + str(result))

        try:
            print("\nresult['items'] is: " + str(result['items']))
            for temp in range(len(result['items'])):
                print("\n\n\ntemp is: " + str(result['items'][temp]))

                if result['items'][temp]['name'] == str(myBuild.instancename):
                    status = result['items'][temp]['status']
                    print("\nstatus is: " + str(status))
                    if status == 'RUNNING':
                        print "Google Cloud VM is ready!"
                        remoteIp = result['items'][temp]['networkInterfaces'][0]['accessConfigs'][0]['natIP']
                        place = True

                    elif status == 'PROVISIONING':
                        print "VM is still spinning up"
                        counter += 1
                        time.sleep(10)
                    elif status == 'TERMINATED':
                        print "VM has terminated, now exiting"
                        sys.exit(0)
        except Exception as ex:
            print("\ngot an exception on line 375: " + str(ex))
            print(traceback.format_exc(ex))
        time.sleep(5)
    remoteIp = result['items'][0]['networkInterfaces'][0]['accessConfigs'][0]['natIP']
    myBuild.remoteIp = remoteIp
    myBuild.instanceId = None
    return(myBuild)

def processSection(configSection, config, connectionObj, myBuild):
        e = 'no error'
        try:
            cstring = str(configSection).strip('"[]')
            cstring = cstring.strip("'")
            if str(configSection) != "['0000.init']":
                logging.info("entered processSection %s", str(configSection))
                for option in config.options(cstring):
                    prefix = None
                    runCheck = True
                    newoption = option
                    ###Get Prefix Tags if Any#####
                    if ")" in option:
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
                        if "." in newoption:
                            location = newoption.index('.')
                            newoption = newoption[0:location]
                        else:
                            newoption = newoption

                        if newoption == "testtouch":
                            testtouchnameList = config.get(cstring, str(option))
                            testtouch(testtouchnameList, connectionObj, myBuild)
                        if newoption == "mkdir":
                            mkdirlist = config.get(cstring, str(option))
                            makeDirectory(mkdirlist, connectionObj, myBuild)
                        if newoption == "filetransfer":
                            transferlist = config.get(cstring, str(option))
                            fileTransfer(transferlist, connectionObj, myBuild)
                        if newoption == "addrepos":
                            reposList = config.get(cstring, str(option))
                            addrepos(reposList, connectionObj, myBuild)
                        if newoption == "downloads":
                            downloadsList = config.get(cstring, str(option))
                            downloads(downloadsList, connectionObj, myBuild)
                        if newoption == "extract":
                            extractList = config.get(cstring, str(option))
                            extract(extractList, connectionObj, myBuild)
                        if newoption == "reporpms":
                            repoRpmsList = config.get(cstring, str(option))
                            repoRpms(repoRpmsList, connectionObj, myBuild)
                        if newoption == "pip":
                            pipList = config.get(cstring, str(option))
                            pipInstall(pipList, connectionObj, myBuild)
                        if newoption == "pathrpms":
                            pathRpmsList = config.get(cstring, str(option))
                            pathRpms(pathRpmsList, connectionObj, myBuild)
                        if newoption == "builderdash":
                            builderdashList = config.get(cstring, str(option))
                            builderdash(builderdashList, connectionObj, myBuild)
                        if newoption == "copyfiles":
                            copyFilesList = config.get(cstring, str(option))
                            copyFiles(copyFilesList, connectionObj, myBuild)
                        if newoption == "movefiles":
                            moveFilesList = config.get(cstring, str(option))
                            moveFiles(moveFilesList, connectionObj, myBuild)
                        if newoption == "copysubtree":
                            copySubtreeList = config.get(cstring, str(option))
                            copySubtree(copySubtreeList, connectionObj, myBuild)
                        if newoption == "chmod":
                            chmodList = config.get(cstring, str(option))
                            chmod(chmodList, connectionObj, myBuild)
                        if newoption == "chown":
                            chownList = config.get(cstring, str(option))
                            chown(chownList, connectionObj, myBuild)
                        if newoption == "sourcescripts":
                            sourceScriptsList = config.get(cstring, str(option))
                            sourceScripts(sourceScriptsList, connectionObj, myBuild)
                        if newoption == "delete":
                            deleteList = config.get(cstring, str(option))
                            deleteFiles(deleteList, connectionObj, myBuild)
                        if newoption == "commands":
                            commandsList = config.get(cstring, str(option))
                            commandsexec(commandsList, connectionObj, myBuild)
                        if newoption == "saveimage":
                            savelist = config.get(cstring, str(option))
                            savedImage = saveImage(savelist, config, cstring, myBuild)
                        if newoption == "deleteinstance":
                            print "Deleting instance"
                            deleteIList = config.get(cstring, str(option))
                            deleteInstance(deleteIList, myBuild)
                        if newoption == "append":
                            appendlist = config.get(cstring, str(option))
                            append(appendlist, connectionObj, myBuild)
                        if newoption == "replace":
                            rpTextlist = config.get(cstring, str(option))
                            replaceText(rpTextlist, connectionObj, myBuild)
                        if newoption == "npm":
                            npmlist = config.get(cstring, str(option))
                            npm(npmlist, connectionObj, myBuild)
                        if newoption == "reboot":
                            rebootOption = config.get(cstring, str(option))
                            connectionObj = rebootFunc(rebootOption, connectionObj, myBuild)
                            return {'newConnect': connectionObj}
                        if newoption == "tar":
                            tarStuff = config.get(cstring, str(option))
                            compressOrExtract(tarStuff, connectionObj, myBuild)
                    else:
                        logging.info("Permission Tags Not in Build Type")
        except Exception as e:
            print "Error in processing the section"
            logging.info("Error in processing the section")
            print e
            logging.info(str(e))
            pass

######### Handles the creation of the client and connectionObj with paramiko###############
def sshControl(sshControlCommand, myBuild, connectionObj):
    print "in sshControl"
    logging.info("enteredsshControl")
    if sshControlCommand == 'connect':
        print 'Connecting...'
        time.sleep(20)
        logging.info("Attempting to Connect with ssh client")
        sshKeyObj = paramiko.RSAKey.from_private_key_file(myBuild.sshkey)
        connectionObj = paramiko.SSHClient()
        logging.info('connectionObj returns %s', str(connectionObj))
        connectionObj.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        counter  = 0
        conn = False
        

        while True:
            try:
                connectionObj.connect(hostname = myBuild.remoteIp, port = 22, username = myBuild.sshkeyuser, pkey = sshKeyObj, look_for_keys = False)
                print "Connected. "
                logging.info("Connection successful")
                break
            except Exception as e:
                print "Error connecting"
                counter += 1
                if counter < 6:
                    print "Trying again"
                    time.sleep(30)
                else:
                    print "Failed after 6 attempts, disconnecting"
                    logging.error("Failed after 6 attempts.  Disconnected")
                    sys.exit(0)
                    
        z = 0
        return (connectionObj)
    elif sshControlCommand == 'disconnect':
        print 'disconnecting'
        logging.info("Attempting to Disconnect")
        connectionObj.close()   
        logging.info("Disconnect Successful")
    elif sshControlCommand == 'local':
        print 'local operation'
        logging.info("Local operation")

###########Run Commands on Instance######################################
def runCommand(connectionObj, commandString, myBuild, **kwargs):
    logging.info("commandString is %s", str(commandString))
    print commandString
    if 'local' in kwargs:
        local = kwargs['local']
    else:
        local = myBuild.local
    print 'local is ' + str(local)
    if local == False or local == 'False':
        try:
            logging.info("running command as remote")
            # Send the command (non-blocking)
            stdin, stdout, stderr = connectionObj.exec_command(commandString, get_pty=True)
            # Wait for the command to terminate
            while not stdout.channel.exit_status_ready():
                # Only print data if there is data to read in the channel
                if stdout.channel.recv_ready():
                    rl, wl, xl = select.select([stdout.channel], [], [], 0.0)
                    if len(rl) > 0:
                        #Print data from stdout
                        output = stdout.channel.recv(1024)
                        if output != None:
                            print output
            x = stdout.channel.recv_exit_status()
            if x != 0:
                print "Error"
                if 'output' in locals():
                    error = output
                else:
                    error = stderr.channel.recv(1024)
                print error
                logging.info('ERROR')
                logging.info(str(error))
            print "Exit status is "+ str(x)
            logging.info("Exit status is %s", str(x))
            lines = stdout.readlines()
            
        except Exception as e:
            print ("Error has occurred", e)
            logging.info('Exception is %s', e)
    elif local == True or local == 'True':
        logging.info("running command as local")
        try :
            x = subprocess.call(str(commandString), shell=True)
            logging.info("Exit status is "+str(x))
        except Exception as e:
            print "ERROR"
            logging.info('ERROR')
            print "Exit status is "+str(e.errno)
            logging.info('Exit status is %s', str(e.errno))
            print str(e.strerror)
            logging.info(str(e.strerror))
    else:
        print "Couldn't get a local status"
        logging.info("Couldn't get a local status")

############### Saves the Image depending on the Cloud Service Being Used.##########################
def saveImage(savelist, config, cstring, myBuild):
    logging.info('saving image')
    slist = ast.literal_eval(savelist)
    for key in slist:
        imageName = slist[key]
    if imageName == '' or imageName == None:
        imageName = myBuild.instancename
    else:
        genUUID = str(uuid.uuid4())
        imageUUID = genUUID[:4]
        imageName = str(imageName)+'-'+str(imageUUID) 
    if myBuild.cloudservice == 'azure':
        print 'This feature not supported'
    elif myBuild.cloudservice == 'aws':
        session = botocore.session.get_session()
        client = session.create_client('ec2', region_name = str(myBuild.region))
        print 'Saving ami'
        response = client.create_image(Description='Builderdash', Name=str(imageName), InstanceId = str(myBuild.instanceId))
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
                print "Saving Image Timed out.  Exiting Builderdash"
                sys.exit(0)
    elif myBuild.cloudservice == 'gcp':
        print "Saving Image"
        zone = myBuild.region
        source_disk = 'zones/' + str(zone) + '/disks/' + str(myBuild.instancename)
        data = {'name': str(imageName), 'sourceDisk': source_disk}
        service = googleapiclient.discovery.build('compute', 'v1')
        request = service.images().insert(project=myBuild.projectid, body=data, forceCreate=True)
        # This is so we don't end up with empty files
        time.sleep(120)
        response = request.execute()
        print("\n" + str(response))
        savedImage = response
    return(savedImage)

###########Execute a Command As Directly Typed##############################
def commandsexec(commandsList, connectionObj, myBuild):
    logging.info('in commandsexec')
    commando = ast.literal_eval(commandsList)
    for key in commando:
        commandString = str(key)
        runCommand(connectionObj, commandString, myBuild)

#########Just a test function for touching .txt files########################
def testtouch(testtouchnameList, connectionObj, myBuild):
    logging.info('in testtouch')
    touchy = ast.literal_eval(testtouchnameList)
    for key in touchy:
        print key
        commandString = 'sudo touch ~/'+str(key)
        runCommand(connectionObj, commandString, myBuild)

#########Tar Compress or Tar Extract Files#######################################
def compressOrExtract(tarStuff, connectionObj, myBuild):
    logging.info('in tarLocal')
    tarlist = ast.literal_eval(tarStuff)
    for key in tarlist:
        print key
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

################Add Repo#################################
def addrepos(reposList, connectionObj, myBuild):
    logging.info('in addrepos function. repo list %s', str(reposList))
    rplist = ast.literal_eval(reposList)
    for repo in rplist:
        if rplist[repo][2] != '':
            commandString = "sudo wget "+str(rplist[repo][2])
            runCommand(connectionObj, commandString, myBuild)
            commandString = 'sudo rpm --import '+ str(rplist[repo][2])
            runCommand(connectionObj, commandString, myBuild)
        try:
            commandString = 'sudo yum install -y '+ str(rplist[repo][0])
            x = runCommand(connectionObj, commandString, myBuild)
        except Exception as e:
            logging.info("There was an exception in addrepos\n"+str(e))
        if str(x) != '0':
            commandString = "sudo rpm -Uvh "+str(rplist[repo][0])
            runCommand(connectionObj, commandString, myBuild)
        commandString = 'sudo yum-config-manager --enable '+ str(rplist[repo][1])
        runCommand(connectionObj, commandString, myBuild)

##############Run Scripts###########################
def sourceScripts(sourceScriptsList, connectionObj, myBuild):
    logging.info('in sourceScripts %s', str(sourceScriptsList))
    sslist = ast.literal_eval(sourceScriptsList)
    for key in sslist:
        print key
        commandString = 'sudo chmod +x '+str(key)
        runCommand(connectionObj, commandString, myBuild)
        commandString = 'sudo '+str(key)
        runCommand(connectionObj, commandString, myBuild)

#################Downloads Files#####################
def downloads(downloadsList, connectionObj, myBuild):
    dllist = ast.literal_eval(downloadsList)
    for source in dllist:
        commandString = 'sudo wget -P '+str(dllist[source])+ ' '+str(source)
        runCommand(connectionObj, commandString, myBuild)

#################Extract Files######################################       
def extract(extractList, connectionObj, myBuild):
    exlist = ast.literal_eval(extractList)
    for key in exlist:
        filelocation = key
        destination = exlist[key][0]
        extract_method = exlist[key][1]
        commandString = 'sudo tar '+str(extract_method)+' '+str(filelocation)+' -C '+str(destination)
        runCommand(connectionObj, commandString, myBuild)

###############Install from packages###############################
def repoRpms(repoRpmsList, connectionObj, myBuild):
    logging.info('in repoRpms %s', str(repoRpmsList))
    rrlist = ast.literal_eval(repoRpmsList)
    for key in rrlist:
        commandString = 'sudo yum install '+str(key)+ ' -y'
        runCommand(connectionObj, commandString, myBuild)

################Handle PIP Installs##############################################
def pipInstall(pipList, connectionObj, myBuild):
    logging.info('in pipInstall %s', str(pipList))
    pList = ast.literal_eval(pipList)
    for key in pList:
        commandString = 'sudo pip install '+str(key)
        runCommand(connectionObj, commandString, myBuild)


##############Yum localinstall###########
def pathRpms(pathRpmsList, connectionObj, myBuild):
    logging.info('in pathRpms %s', str(pathRpmsList))
    prlist = ast.literal_eval(pathRpmsList)
    for key in prlist:
        print key
        commandString = 'sudo yum localinstall '+str(key)+' -y'
        runCommand(connectionObj, commandString, myBuild)



def configToList(config): 
    configList=[]
    for each_section in config.sections():
        configList.append([each_section])
    configList=sorted(configList)
    return configList

##############Calls another builderdash script###############
def builderdash(builderdashList, connectionObj, myBuild):
    logging.info('in builderdash %s', str(builderdashList))
    blist = ast.literal_eval(builderdashList)
    for key in blist:
        print key
        print "STARTING======>>>>>>>>>>"+str(key)
        subprocess.call('pwd', shell=True)
        main(myBuild=myBuild, scriptName=str(key), connectionObj=connectionObj)

#############Copies Files from one location to Another###############
def copyFiles(copyFilesList, connectionObj, myBuild):
    logging.info('in copyFiles %s', str(copyFilesList))
    cflist = ast.literal_eval(copyFilesList)
    for key in cflist:
        commandString = 'sudo cp '+str(key)+' '+str(cflist[key])
        runCommand(connectionObj, commandString, myBuild)

#############Move Files from one location to Another###############
def moveFiles(moveFilesList, connectionObj, myBuild):
    logging.info('in moveFiles %s', str(moveFilesList))
    mflist = ast.literal_eval(moveFilesList)
    for key in mflist:
        commandString = 'sudo mv '+str(key)+' '+str(mflist[key])
        runCommand(connectionObj, commandString, myBuild)

################Delete Files#######################################
def deleteFiles(deleteList, connectionObj, myBuild):
    logging.info('in deleteFiles %s', str(deleteList))
    delist = ast.literal_eval(deleteList)
    for key in delist:
        commandString = 'sudo rm '+str(key)
        runCommand(connectionObj, commandString, myBuild)        

#############Copies a Subtree######################################
def copySubtree(copySubtreeList, connectionObj, myBuild):
    logging.info('in copySubtree. list %s', str(copySubtreeList))
    cslist = ast.literal_eval(copySubtreeList)
    for key in cslist:
        commandString = 'sudo cp -R '+str(key)+' '+str(cslist[key])
        runCommand(connectionObj, commandString, myBuild)

#############Change Permissions###############################
def chmod(chmodList, connectionObj, myBuild):
    logging.info('in chmod.  chmod list %s', str(chmodList))
    cmlist = ast.literal_eval(chmodList)
    for key in cmlist:
        commandString = 'sudo chmod '+str(cmlist[key])+' '+str(key)
        runCommand(connectionObj, commandString, myBuild)

#############Change Ownsership###############################
def chown(chownList, connectionObj, myBuild):
    logging.info('in chown.  chown list %s', str(chownList))
    colist = ast.literal_eval(chownList)
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

def makeDirectory(mkdirlist, connectionObj, myBuild):
    logging.info('Inside makeDirectory')
    mklist = ast.literal_eval(mkdirlist)
    for key in mklist:
        commandString = 'sudo mkdir '+ str(key)
        runCommand(connectionObj, commandString, myBuild)

def fileTransfer(transferlist, connectionObj, myBuild):
    logging.info('Inside fileTransfer')
    ftlist = ast.literal_eval(transferlist)
    logging.info('ftlist is %s', str(ftlist))
    user = str(myBuild.sshkeyuser)+'@'+str(myBuild.remoteIp)+':'
    for key in ftlist:
        sourcepath = ftlist[key][0]
        destination = str(user)+str(ftlist[key][1])
        tempLocal = ftlist[key][2]
        commandString = "scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "+str(myBuild.sshkey)+' '+sourcepath+' '+destination
        logging.info('commandString is %s', str(commandString))
        logging.info('before runCommand FT')
        runCommand(connectionObj, commandString, myBuild, local=tempLocal)
        logging.info('after runCommand FT')

def deleteInstance(deleteIList, myBuild):
    logging.info('Inside deleteInstance')
    delList = ast.literal_eval(deleteIList)
    if myBuild.cloudservice == 'aws':
        session = botocore.session.get_session()
        client = session.create_client('ec2', region_name = str(myBuild.region))
        try:
            response = client.terminate_instances(InstanceIds=[str(myBuild.instanceId)])
            print response
        except:
            print "Failed to delete instance.  Please do so manually"
    elif myBuild.cloudservice == 'gcp':
        compute = googleapiclient.discovery.build('compute', 'v1')

        deleted = None
        while not deleted:
            time.sleep(10)
            print("\nthe instance we're going to delete is: " + str(myBuild.instancename))
            deleteResponse = compute.instances().delete(project=myBuild.projectname, zone=myBuild.region, instance=str(myBuild.instancename)).execute()
            print("\ndeleteResponse is: ")
            print(deleteResponse)
            if deleteResponse['status'] == "PENDING":
                deleted = True
    else:
        print "No proper cloud service listed in Init Section of cfg file."

########Append Files############
def append(appendlist, connectionObj, myBuild):
    logging.info('Inside append')
    applist = ast.literal_eval(appendlist)
    logging.info('applist is %s', str(applist))
    for key in applist:
        file = str(key)
        appendtext = re.escape(applist[key])
        commandString = "sudo sed -i '$ a\\"+appendtext+"' "+file
        runCommand(connectionObj, commandString, myBuild)

#####Replace text Function->>>Work in progress#########
def replaceText(rpTextlist, connectionObj, myBuild):
    logging.info('Inside replaceText')
    replace = ast.literal_eval(rpTextlist)
    logging.info('rpList is %s', str(replace))
    for key in replace:
        for subkey in replace[key]:
            file = key
            oldtext = str(subkey)
            newtext = str(replace[key][subkey])
            regexpress = "s/"+oldtext+"/"+newtext+"/g"
            totaltext = "sudo sed -i s'"+re.escape(regexpress)+"' "+file
            totaltext = "sudo sed -i 's/${"+oldtext+"}/${"+newtext+"}/g' file"
            print str(file)
            print str(oldtext)
            print str(newtext)
            print str(totaltext)
            #commandString = "sudo sed -i 's/"+str(oldtext)+"/"+str(newtext)+"/g' "+str(file)
            commandString = totaltext
            runCommand(connectionObj, commandString, myBuild)

######Handle npm's################
def npm(npmlist, connectionObj, myBuild):
    logging.info('Inside npm')
    nplist = ast.literal_eval(npmlist)
    logging.info('nplist is %s', str(nplist))
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
def rebootFunc(rebootOption, connectionObj, myBuild):
    logging.info("Inside rebootFunc")
    rebootCheck = ast.literal_eval(rebootOption)
    if myBuild.cloudservice == "aws" or "gcp":
        commandString = "sudo reboot"
        runCommand(connectionObj, commandString, myBuild)
        counter = 0
        print "Attempting to reconnect after reboot"
        while True:
            time.sleep(180)
            try:
                connectionObj = sshControl('connect', myBuild, connectionObj)
                print "connected. "
                logging.info("Connection successful")
                break
            except:
                print "Error reconnecting, trying again"
                counter += 1
                if counter < 3:
                    pass
                else:
                    print "Reconnect after reboot failed.  Exiting Builderdash"
                    logging.error("Reboot Failed")
                    sys.exit(0)
                    break
    return(connectionObj)

######Handle User Data ####################
def handleUserData(myBuild):
    if hasattr(myBuild, 'userdata'):
        pass
    else:
        myBuild.userdata = ""
    myBuild.userdata = str(myBuild.userdata)
    return myBuild

def main(**kwargs):
    aparser = argparse.ArgumentParser(description="Builderdash - a utility to mash a bunch of stuff into someplace (cloud, or elsewhere) so others can use it.")
    aparser.add_argument('-V', '--version', action='version', version='Builderdash version 0.01')
    aparser.add_argument('-c', '--cfile', help="Config filename", required=False, default="")
    aparser.add_argument('-l', '--lfile', help="Log filename", required=False, default="builderdash")
    aparser.add_argument('-v', '--verbosity', type=int, help= "Specifies the verbosity level for logging (0 none, 10 debug, 20 error, 30 warning, 40 info, 50 critical)", required=False, default=30)
    aparser.add_argument('-i', '--image', help="Image name", required=False, default=None)
    aparser.add_argument('-m', '--myBuild')

    args = aparser.parse_args()
    lfile = args.lfile
    image = args.image
    if 'connectionObj' in kwargs:
        connectionObj = kwargs['connectionObj']
    else:
        connectionObj = None
    if 'myBuild' in kwargs:
        myBuild = kwargs['myBuild']
    else:
        myBuild = None
    if 'scriptName' in kwargs:
        cfile = kwargs['scriptName']
    elif args.cfile != None:
        cfile = args.cfile
    
    
    config = ConfigParser.SafeConfigParser()
    config.read(cfile)
    configList = configToList(config)
    if 'myBuild' not in locals():
        myBuild = Build()
    elif myBuild == None:
        myBuild = Build()
        myBuild.log = lfile

    logging.basicConfig(format='%(asctime)s>%(levelname)s:%(module)s:%(funcName)s-%(message)s', filename=str(myBuild.log)+'.log', level = logging.INFO)
    logging.info("##############################################################################################")
    logging.info("Reached Log Stage")
    try:
        initReturnList = processInitSection(configList[0], config, connectionObj, myBuild)
        myBuild = initReturnList[0]
        connectionObj = initReturnList[1]
        count=0
        for configSection in configList:
            x = processSection(configSection, config, connectionObj, myBuild)
            if hasattr(x, 'newConnect'):
                connectionObj = x['newConnect'] 
            count+=1
    except Exception as e:
        print "Error in initReturnList"
        print traceback.format_exc(e)
        print e
        logging.info(str(e))
    return(myBuild, connectionObj)
x = main()
try:
    sshControl('disconnect', x[0], x[1])
except Exception as e:
    print "No connection exists, no need to disconnect"