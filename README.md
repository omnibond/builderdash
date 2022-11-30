# builderdash
Builderdash is a cross cloud build system for images

WARNING: Most of the README is now out of date.

Dependencies:  
	-python 2.7  
	-botocore  
	-AWS CLI  
	-paramiko  
	-google-api-python-client  
  
AWS Credentials:  
	Make sure you have your AWS credentials set up.  If you have the AWS CLI installed just run 'aws configure' from the command line.
	Follow the link below to read their Docs.  
	https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-getting-started.html  

Basic Instructions:  
	To run builderdash first install with python3 setup.py install then use the builderdash command which will be installed (ensure PATH is set up if not installing as root).
  
List of arguments:  
	-V, --version  
	-c, --cfile - Config Filename (Mandatory)  
	-l, --lfile - Log filename (Defaults to 'builderdash')  Note: Log file only appends to file, it never replaces.  
 	-v, --verbosity - Specifies the verbosity level for logging (0 none, 10 debug, 20 error, 30 warning, 40 info, 50 critical)", required=False, default=30)  

Constructing your cfg File:  
  
Blank Example of an Init Section  
  
[0000.init]  
local =  
sshkeyname =   
sshkey =  
cloudservice =   
instancetype =  
region =  
ostype =  
instanceName =  
sourceimage =  
buildType =  
subnet =  
securitygroupid =  
customtags = ['', '']  
  
The Init section is REQUIRED in the initial cfg file being passed to builderdash  

>local local option needs to be set to False if you want to launch a remote connection to a googlecloud or aws instance  
  
>sshkeyuser  
	-Needs to be set to the name that is used to ssh into your builder instance.  e.g. 'centos' or 'rhel'  
	-For googlecloud this will be the username that you make  

>sshkeyname  
	-The name of the pem key needed to ssh into your instance  
  
>sshkey  
	-The full path to the pem key  
  
>cloudservice  
	-Which service platform you are using  
  
>instancetype  
	-What type of instance you want to use e.g. t2.micro, c4.4xlarge, etc... for AWS, n1-standard-1 etc... for GCP.  
  
>region  
	-What region you want to launch your instance in e.g. us-west-1 for AWS, us-central1-f for GCP.  
  
>ostype  
	-What OS system your builder instance is going to have.  centos or rhel  
  
>instanceName  
	-What you want the instance to be called  
  
>sourceimage  
	-AWS the ami ID you want to build off of.  
	-GCP The path to the image you want to build off of  
  
>subnet  
	-AWS only. Subnet you need for your region.  If you have EC2 classic you have to put a subnet in.  
  
>securitygroupid  
	-AWS only. If you need to build in a specific security group, put the ID of the security group otherwise it sets to a default security group.  
  
>customtags  
	-If you wish to add custom tags to your build.  Options with custom tags in front of them will only run if you put their tag here in the custom tag list.  Must be in a list [] format  
	-EX   [1000.copyFiles]  
		  (project)copyfiles = {"/path/to/foo": "/path/to/bar"}  
	-This will only run if you have     customtags = ["project"]  
  
>pubkeyypath  
	-GCP only.  The path to your public key  
  
>projectID  
	-GCP only.  The ID of the project that you use for you account.  
  
>bucketName  
	-GCP only.  The name of the bucket for your project  
  
  
Other Sections:  
  
Options:  
	mkdir = ['-p /path/to/file']  
		-A list  
		-Makes directories that you put in the list.  Put the full path and any options you want before each path.  
  
	filetransfer = {"name_of_file": ("/path/to/file", "/path/to/transfer/to", Boolean)}  
		-Transfers files to an instance or from an instance  
		-A True value for the Boolean means you want to transfer from your local machine to the instance  
		-A False value for the Boolean means you want to transfer from your instance to your local machine  
  
	addrepos = {'name_of_repo': ('link_to_repo', '', 'link_to_keys_for_import')}  
		-Add repos to your build  
  
	downloads = {'link_to_download': '/path/to/directory'}  
		-Downloads things to your build instance  
  
	extract = {'path/to/file/to/extract': ('/where/to/extract/', '-xzf')}  
		-Extract files  
		-The key is the path of the file to extract  
		-The first part of the tuple is where to extract  
		-The second part of the tuple is the arguments needed to untar the file  
  
	reporpms = ['gcc']  
		-Yum installs packages  
		-A list of packages to install  
  
	pathrpms = ['gcc']  
		-Do a yum localinstall on the packages in the list  
  
	pip = ['python-wheel', 'setuptools==33.1.1']  
		-Installs your list with pip  
  
	builderdash = ['userapps.cfg']  
		-Calls another builderdash cfg  
  
	copyfiles = {'/location/of/file': '/path/to/destination'}  
		-Copies files from one location to another  
  
	movefiles = {'/path/to/file': '/path/to/destination'}  
		-Moves files from one location to another  
  
	copysubtree = {'location/of/directory': 'destination/of/copy'}  
		-Recursively copies a directory  
  
	chmod = {'/path/to/chmod': 'options'}  
		-chmod a file or directory  
  
	chown = {'root': ('-R', 'root', '/path/to/chown')}  
		-Change the ownership of a file or directory  
  
	sourcescripts = ['/path/to/script']  
		-Run the script/scripts you put in the list on your instance  
  
	delete = ['/file/to/delete']  
		-Deletes files in the path you list here  
		-If it's a directory put a -R in front  
  
	commands = ['sudo sed...']  
		-Executes linux commands you put in the list  
  
	saveimage = {'true': 'builderdash'}  
		-Saves the image on your cloud platform  
		-Set the key to true  
		-The value is the name you wish to call the instance you are saving  
  
	deleteinstance = True  
		-Deletes the instance you are building on  
  
	npm = [{'amdefine@0.1.0': '/path/to/install'}]  
		-A list of dictionaries to install packages with npm  
		-The keys are the name and version you want to install  
		-The value is the location you want to install to  
  
	tar = {'/path/to/file': (True/False, 'compress', '../src')}  
		-Compresses or extracts a file either locally or on the remote instance.  
		-If you are compressing look here:  
			The key is what the name of the compressed file will be  
			The first tuple value will be True if this is a local file and False if it's on your instance  
			The second tuple value will be 'compress'  
			The third tuple value will be the directory you are compressing  
		-If you are extracting look here:  
			The key is the file path to the file you want to extract  
			The first tuple value will be True if this is a local file and False if it's on your instance  
			The second tuple value will be 'extract'  
			The third tuple value will be where you wish to extract the files to  
  
  
Each section must have a 4 digit number preciding a . and whatever you wish to name the section.  Sections will be processed in order of their number.  Skipping a few numbers in between sections gives you some wiggle room in case you need to insert a new section.  IMPORTANT:  The init section must be formatted as shown above with [0000.init]   
  
Each option under the section will be run in the order you place them.  Note:  If you need to use the same option more than once, use a '.'' and a number to denote the 2nd, 3rd, 4th, etc versions of that command you wish to run.  
  
Sample Sections:  
  
[1000.UploadAndRunScript]  
filetransfer.1 = {"arbitraryName": ("/source/path", "/destination/path", True)}  
sourcescripts = ["filePathToScript"]  
filetransfer.2 = {"foo": ("/source/path", "destination/path", True)}  
  
[1010.saveimage]  
saveimage = {'true': 'builderdash'}  
  
[1020.deleteInstance]  
deleteinstance = True  

WARNING!  
If you do not include the saveimage option, your instance will not automatically save.  
If you do not include the deleteinstance option, your instance will not automatically delete and you will have to do so manually.  
