import logging
import os
import select
import time

import paramiko

logger = logging.getLogger(__name__)


def load_proxy_conf_file(proxy_yaml_conf_file):
    from builderdash.main import safe_load_yaml_file
    proxy_yaml_conf_file = os.path.expanduser(proxy_yaml_conf_file)
    # 1-to-1 match with proxy-related params of builderdash.ssher constructor
    valid_keys = ['proxy_hostname',
                  'proxy_port',
                  'proxy_username',
                  'proxy_password',
                  'proxy_key_filename',
                  'proxy_passphrase',
                  'proxy_timeout',
                  'proxy_attempt_limit',
                  'proxy_retry_delay',
                  'proxy_missing_host_key_policy',
                  'proxy_channel_alt_src_hostname']
    try:
        conf = safe_load_yaml_file(proxy_yaml_conf_file)
    except Exception:
        logger.error(f"safe_load_yaml_file({proxy_yaml_conf_file}) failed")
        return None

    for k, v in conf.items():
        if k not in valid_keys:
            logger.error(f"invalid proxy config key: '{k}', check proxy_conf_file: {proxy_yaml_conf_file}")
            return None

    pconf = {}
    for k in valid_keys:
        v = conf.get(k)
        if k == 'proxy_hostname' and v is None:
            logger.error(f"proxy_hostname key missing, check proxy_conf_file")
            return None
        elif k == 'proxy_port' and v is None:
            pconf[k] = 22
        elif k == 'proxy_key_filename':
            pconf[k] = os.path.expanduser(v)
        elif k == 'proxy_missing_host_key_policy':
            # convert proxy_missing_host_key_policy string to actual paramiko policy or bail if not found / valid
            missing_host_key_policy = None
            if v == 'reject':
                missing_host_key_policy = paramiko.RejectPolicy
            elif v == 'warning':
                missing_host_key_policy = paramiko.WarningPolicy
            elif v == 'autoadd':
                missing_host_key_policy = paramiko.AutoAddPolicy
            pconf[k] = missing_host_key_policy
        else:
            # set any remaining vals to be their conf file val or set to None (if key missing in conf file)
            pconf[k] = v
    return pconf


def ssh_run_cmd(ssh_client, command, timeout=1.0, get_pty=True, stdout_log_func=logger.info,
                stderr_log_func=logger.error, ret_stdout=True, ret_stderr=True, stdout_extra=None, stderr_extra=None):
    # Adapted from: https://stackoverflow.com/a/32758464
    stdin, stdout, stderr = ssh_client.exec_command(command=command, get_pty=get_pty)

    # get the shared channel for stdout/stderr/stdin
    chan = stdout.channel
    # our implementation does not support sending data for command via stdin
    chan.shutdown_write()
    stdin.close()

    stdout_chunks = []
    stderr_chunks = []

    left_over_stdout = b''
    left_over_stderr = b''

    while not chan.closed or chan.recv_ready() or chan.recv_stderr_ready():
        # stop if channel was closed prematurely, and there is no data in the buffers.
        got_chunk = False
        rlist, _, _ = select.select([chan], [], [], timeout)
        for c in rlist:
            if c.recv_ready():
                got_chunk = True
                this_stdout_chunk = chan.recv(len(c.in_buffer))

                if stdout_log_func is not None:
                    lines = this_stdout_chunk.splitlines()
                    if len(lines):
                        lines[0] = left_over_stdout + lines[0]

                    if this_stdout_chunk.endswith(b'\n'):
                        left_over_stdout = b''
                    else:
                        left_over_stdout = lines[-1]
                        del (lines[-1])

                    for idx, line in enumerate(lines):
                        line_out = line.decode(encoding='utf-8', errors='ignore')
                        if len(line_out):
                            stdout_log_func("%s", line_out, extra=stdout_extra)
                if ret_stdout:
                    stdout_chunks.append(this_stdout_chunk)
            if c.recv_stderr_ready():
                got_chunk = True
                this_stderr_chunk = chan.recv_stderr(len(c.in_stderr_buffer))

                if stderr_log_func is not None:
                    lines = this_stderr_chunk.splitlines()
                    if len(lines):
                        lines[0] = left_over_stderr + lines[0]

                    if this_stderr_chunk.endswith(b'\n'):
                        left_over_stderr = b''
                    else:
                        left_over_stderr = lines[-1]
                        del (lines[-1])

                    for idx, line in enumerate(lines):
                        line_out = line.decode(encoding='utf-8', errors='ignore')
                        if len(line_out):
                            stderr_log_func("%s", line_out, extra=stderr_extra)
                if ret_stderr:
                    stderr_chunks.append(this_stderr_chunk)

        if not got_chunk \
                and chan.exit_status_ready() \
                and not chan.recv_ready() \
                and not chan.recv_stderr_ready():
            chan.shutdown_read()
            chan.close()
            break

    stdout.close()
    stderr.close()

    if stdout_log_func is not None:
        line_out = left_over_stdout.decode(encoding='utf-8', errors='ignore')
        if len(line_out):
            stdout_log_func("%s", line_out, extra=stdout_extra)
    if stderr_log_func is not None:
        line_out = left_over_stderr.decode(encoding='utf-8', errors='ignore')
        if len(line_out):
            stderr_log_func("%s", line_out, extra=stderr_extra)

    return chan.recv_exit_status(), b''.join(stdout_chunks), b''.join(stderr_chunks)


class SSHConnection:
    def __init__(self, target_hostname, target_port=22, target_username=None, target_password=None,
                 target_key_filename=None, target_passphrase=None, target_timeout=None, target_attempt_limit=None,
                 target_retry_delay=None, target_missing_host_key_policy=None,
                 proxy_hostname=None, proxy_port=22, proxy_username=None, proxy_password=None,
                 proxy_key_filename=None, proxy_passphrase=None, proxy_timeout=None, proxy_attempt_limit=None,
                 proxy_retry_delay=None, proxy_missing_host_key_policy=None, proxy_channel_alt_src_hostname=None):
        logger.debug('called')
        self.target_hostname = target_hostname
        self.target_port = int(target_port)
        self.target_username = target_username
        self.target_password = target_password
        self.target_key_filename = target_key_filename
        self.target_passphrase = target_passphrase
        self.target_timeout = 3600.0 if target_timeout is None else float(target_timeout)
        self.target_attempt_limit = 1 if (target_attempt_limit is None or int(target_attempt_limit) < 1) else int(
            target_attempt_limit)
        self.target_retry_delay = 60.0 if target_retry_delay is None else float(target_retry_delay)
        self.target_missing_host_key_policy = paramiko.RejectPolicy() if target_missing_host_key_policy is None else (
            target_missing_host_key_policy)

        self.proxy_hostname = proxy_hostname
        self.proxy_port = int(proxy_port)
        self.proxy_username = proxy_username
        self.proxy_password = proxy_password
        self.proxy_key_filename = proxy_key_filename
        self.proxy_passphrase = proxy_passphrase
        self.proxy_timeout = 3600.0 if proxy_timeout is None else float(proxy_timeout)
        self.proxy_attempt_limit = 1 if (proxy_attempt_limit is None or int(proxy_attempt_limit) < 1) else int(
            proxy_attempt_limit)
        self.proxy_retry_delay = 60.0 if proxy_retry_delay is None else float(proxy_retry_delay)
        self.proxy_missing_host_key_policy = paramiko.RejectPolicy() if proxy_missing_host_key_policy is None else (
            proxy_missing_host_key_policy)
        self.proxy_channel_alt_src_hostname = proxy_channel_alt_src_hostname

        # client session fields established via calls to SSHClient.connect() and SSHClient.open_sftp()
        self.proxy_client = None
        self.proxy_to_target_channel = None
        self.target_client = None
        self.target_sftp = None

    # Attempts to establish a connection to a target host, potentially via a ssh proxy host.
    def connect(self):
        logger.debug('called')
        if self.proxy_hostname is not None:
            self.__connect_proxy()
            self.__proxy_open_channel_to_target()
        self.__connect_target()
        if self.target_sftp is None:
            try:
                target_sftp = self.target_client.open_sftp()
            except Exception as e:
                logger.error('target_client.open_sftp failed with exception %s', e)
                raise e
            else:
                logger.info('target_client.open_sftp succeeded')
                self.target_sftp = target_sftp

    def __connect_proxy(self):
        logger.debug('called')

        if self.proxy_client is not None:
            return

        # Create a local var for establishing the client connection
        proxy_client = paramiko.SSHClient()
        #TODO
        # proxy_client.load_system_host_keys()
        proxy_client.set_missing_host_key_policy(self.proxy_missing_host_key_policy)

        logger.info('attempting ssh connection via ssh proxy: %s', {
            'proxy_hostname': self.proxy_hostname,
            'proxy_port': self.proxy_port,
            'proxy_username': self.proxy_username,
            'proxy_password': self.proxy_password,
            'proxy_key_filename': self.proxy_key_filename,
            'proxy_passphrase': self.proxy_passphrase,
            'proxy_timeout': self.proxy_timeout,
            'proxy_attempt_limit': self.proxy_attempt_limit,
            'proxy_missing_host_key_policy': self.proxy_missing_host_key_policy,
            'proxy_channel_alt_src_hostname': self.proxy_channel_alt_src_hostname
        })

        for i in range(self.proxy_attempt_limit):
            logger.debug('connect attempt %d of %d', i+1, self.proxy_attempt_limit)
            try:
                proxy_client.connect(hostname=self.proxy_hostname, port=self.proxy_port, username=self.proxy_username,
                                     password=self.proxy_password, key_filename=self.proxy_key_filename,
                                     passphrase=self.proxy_passphrase, timeout=self.proxy_timeout)
            except Exception as e:
                logger.info('proxy_client.connect raised exception', e)
                if (i + 1) < self.proxy_attempt_limit:
                    logger.info('trying again after %f seconds', self.proxy_retry_delay)
                    time.sleep(self.proxy_retry_delay)
                else:
                    logger.info('__connect_proxy exceeded proxy_attempt_limit')
                    raise e
            else:
                logger.info('proxy_client.connect succeeded')
                self.proxy_client = proxy_client
                break

    def __proxy_open_channel_to_target(self):
        logger.debug('called')

        if self.proxy_to_target_channel is not None:
            return

        proxy_transport = self.proxy_client.get_transport()

        # Enable alternate hostname override for proxy channel src such that, for instance, an internal IP may be used.
        if self.proxy_channel_alt_src_hostname is not None:
            proxy_addr_hostname = self.proxy_channel_alt_src_hostname
        else:
            proxy_addr_hostname = self.proxy_hostname

        proxy_addr = (proxy_addr_hostname, self.proxy_port)
        target_addr = (self.target_hostname, self.target_port)

        logger.info('opening channel to target via proxy: %s', {
            'proxy_addr': proxy_addr,
            'target_addr': target_addr
        })

        # Note: reusing the target_attempt_limit here since this is technically a connection to target (via proxy)
        for i in range(self.target_attempt_limit):
            logger.info('open_channel attempt %d of %d', i+1, self.target_attempt_limit)
            try:
                proxy_to_target_channel = proxy_transport.open_channel('direct-tcpip', target_addr, proxy_addr,
                                                                       timeout=self.target_timeout)
            except Exception as e:
                logger.info('proxy_transport.open_channel raised exception: %s', e)
                if (i + 1) < self.target_attempt_limit:
                    logger.info('trying again after %f seconds', self.target_retry_delay)
                    time.sleep(self.target_retry_delay)
                else:
                    logger.info('__proxy_open_channel_to_target exceeded target_attempt_limit')
                    raise e
            else:
                logger.info('proxy_transport.open_channel succeeded')
                self.proxy_to_target_channel = proxy_to_target_channel
                break

    def __connect_target(self):
        logger.debug('called')

        if self.target_client is not None:
            return

        # Create a local var for establishing the client connection
        target_client = paramiko.SSHClient()
        #TODO
        # target_client.load_system_host_keys()
        target_client.set_missing_host_key_policy(self.target_missing_host_key_policy)

        logger.info('attempting ssh connection to target: %s', {
            'target_hostname': self.target_hostname,
            'target_port': self.target_port,
            'target_username': self.target_username,
            'target_password': self.target_password,
            'target_key_filename': self.target_key_filename,
            'target_passphrase': self.target_passphrase,
            'target_timeout': self.target_timeout,
            'target_attempt_limit': self.target_attempt_limit,
            'target_missing_host_key_policy': self.target_missing_host_key_policy
        })

        for i in range(self.target_attempt_limit):
            logger.info('connect attempt %d of %d', i + 1, self.target_attempt_limit)
            try:
                target_client.connect(hostname=self.target_hostname, port=self.target_port,
                                      username=self.target_username, password=self.target_password,
                                      key_filename=self.target_key_filename,
                                      passphrase=self.target_passphrase, timeout=self.target_timeout,
                                      sock=self.proxy_to_target_channel)
            except Exception as e:
                logger.info('target_client.connect raised exception', e)
                if (i + 1) < self.target_attempt_limit:
                    logger.info('trying again after %f seconds', self.target_retry_delay)
                    time.sleep(self.target_retry_delay)
                else:
                    logger.info('__connect_target exceeded target_attempt_limit')
                    raise e
            else:
                logger.info('target_client.connect succeeded')
                self.target_client = target_client
                break

    def disconnect(self):
        logger.debug('called')
        if self.target_sftp is not None:
            self.target_sftp.close()
            self.target_sftp = None

        if self.target_client is not None:
            self.target_client.close()
            self.target_client = None

        if self.proxy_to_target_channel is not None:
            self.proxy_to_target_channel.close()
            self.proxy_to_target_channel = None

        if self.proxy_client is not None:
            self.proxy_client.close()
            self.proxy_client = None
        logger.info('all connections under this SSHConnection object closed: %s', self)

    def reconnect(self):
        logger.debug('called')
        self.disconnect()
        self.connect()

    def is_alive(self):
        logger.debug('called')
        if self.target_client is None or self.target_sftp is None:
            return False

        transport = self.target_client.get_transport()
        if not transport.is_active():
            return False

        try:
            transport.send_ignore()
        except EOFError as e:
            return False

        # try a read-only sftp operation on the target
        try:
            normalized_path = self.target_sftp.normalize(path='.')
        except IOError as e:
            return False
        else:
            logger.debug('self.target_sftp.normalize(path=\'.\') returned: %s', normalized_path)

        return True

    def file_upload(self, src, dst='.'):
        """
        Uploads file at path 'src' on local host to file path 'dst' on target host (of self) via SFTP over SSH.
        :param src: str, required
        :param dst: str, required
        """
        logger.debug("called, local src: '%s' --> target dst: '%s'", src, dst)
        if dst == '.':
            dst = os.path.basename(src)
            logger.debug('dst has been automatically changed to basename(src), which is: %s', dst)
        if dst.endswith(os.path.sep):
            dst += os.path.basename(src)
            logger.debug('dst has been automatically changed to %s', dst)
        try:
            sftp_attrs = self.target_sftp.put(src, dst)
        except Exception as e:
            logger.error('file_upload failed: %s', e)
            raise e
        else:
            logger.debug('file_upload succeeded, remote sftp attrs: %s', sftp_attrs)
            return sftp_attrs

    def file_download(self, src, dst='.'):
        """
        Downloads file at path 'src' on target host (of self) to file path 'dst' on local host via SFTP over SSH.
        :param src: str, required
        :param dst: str, required
        """
        logger.debug("called, target src: '%s' --> local dst: '%s'", src, dst)
        if dst == '.':
            dst = os.path.basename(src)
            logger.debug('dst has been automatically changed to basename(src), which is: %s', dst)
        if dst.endswith(os.path.sep):
            dst += os.path.basename(src)
            logger.debug('dst has been automatically changed to %s', dst)
        try:
            self.target_sftp.get(src, dst)
        except Exception as e:
            logger.error('file_download failed: %s', e)
            raise e
        else:
            dst_stat_attrs = os.stat(dst)
            logger.debug('file_download succeeded, dst_stat_attrs: %s', dst_stat_attrs)
            return dst_stat_attrs

    def get_target_client(self):
        return self.target_client

    def get_proxy_client(self):
        return self.proxy_client

    # Run command using self.target_client as the default client
    def run_command(self, command, get_pty=True, stdout_log_func=logger.info, stderr_log_func=logger.error,
                    ret_stdout=True, ret_stderr=True, stdout_extra=None, stderr_extra=None, ssh_client=None):
        client = self.target_client if ssh_client is None else ssh_client
        return ssh_run_cmd(client, command, get_pty=get_pty, stdout_log_func=stdout_log_func,
                           stderr_log_func=stderr_log_func, ret_stdout=ret_stdout, ret_stderr=ret_stderr,
                           stdout_extra=stdout_extra, stderr_extra=stderr_extra)