# Copyright (c) 2015 Mirantis, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.


import socket
import time
import random
import json
import yaml
import logging
import telnetlib

import requests
import testresources
import testtools

from heatclient import client as heatclient
from keystoneclient.v2_0 import client as keystoneclient
from muranoclient import client as muranoclient
import muranoclient.common.exceptions as exceptions

import config as cfg

CONF = cfg.cfg.CONF

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)
fh = logging.FileHandler('runner.log')
fh.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - '
                              '%(levelname)s - %(message)s')
fh.setFormatter(formatter)
LOG.addHandler(fh)


class MuranoTestsCore(testtools.TestCase, testtools.testcase.WithAttributes,
                      testresources.ResourcedTestCase):
    """This manager provides access to Murano-api service
    """

    @classmethod
    def setUpClass(cls):
        super(MuranoTestsCore, cls).setUpClass()

        cfg.load_config()
        cls.keystone = keystoneclient.Client(username=CONF.murano.user,
                                             password=CONF.murano.password,
                                             tenant_name=CONF.murano.tenant,
                                             auth_url=CONF.murano.auth_url)
        murano_url = cls.keystone.service_catalog.url_for(
            service_type='application_catalog', endpoint_type='publicURL')
        cls.murano_url = murano_url if 'v1' not in murano_url else "/".join(
            murano_url.split('/')[:murano_url.split('/').index('v1')])
        cls.heat_url = cls.keystone.service_catalog.url_for(
            service_type='orchestration', endpoint_type='publicURL')
        cls.murano_endpoint = cls.murano_url + '/v1/'
        cls.keyname = CONF.murano.keyname

    @classmethod
    def upload_package(cls, package_name, body, app, ):
        files = {'%s' % package_name: open(app, 'rb')}
        return cls.murano.packages.create(body, files)

    def setUp(self):
        super(MuranoTestsCore, self).setUp()
        self.keystone = keystoneclient.Client(username=CONF.murano.user,
                                              password=CONF.murano.password,
                                              tenant_name=CONF.murano.tenant,
                                              auth_url=CONF.murano.auth_url)
        self.heat = heatclient.Client('1', endpoint=self.heat_url,
                                      token=self.keystone.auth_token)
        self.murano = muranoclient.Client(
            '1', endpoint=self.murano_url, token=self.keystone.auth_token)
        self.headers = {'X-Auth-Token': self.murano.auth_token,
                        'content-type': 'application/json'}

        self.environments = []
        LOG.debug('Running test: {0}'.format(self._testMethodName))

    def tearDown(self):
        super(MuranoTestsCore, self).tearDown()
        for env in self.environments:
            try:
                self.environment_delete(env)
                time.sleep(60)
            except Exception:
                pass

    def rand_name(self, name='murano_env'):
        return name + str(random.randint(1, 0x7fffffff))

    def environment_delete(self, environment_id, timeout=180):
        self.murano.environments.delete(environment_id)

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                self.murano.environments.get(environment_id)
            except exceptions.HTTPNotFound:
                return
        raise Exception(
            'Environment {0} was not deleted in {1} seconds'.format(
                environment_id, timeout))

    def wait_for_environment_deploy(self, environment):
        start_time = time.time()
        status = environment.manager.get(environment.id).status
        while status != 'ready':
            status = environment.manager.get(environment.id).status
            if time.time() - start_time > 1800:
                time.sleep(60)
                self._log_report(environment)
                self.fail(
                    'Environment deployment is not finished in 1200 seconds')
            elif status == 'deploy failure':
                self._log_report(environment)
                time.sleep(60)
                self.fail('Environment has incorrect status {0}'.format(status))
            time.sleep(5)

        return environment.manager.get(environment.id)

    def check_port_access(self, ip, port):
        result = 1
        start_time = time.time()
        while time.time() - start_time < 600:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex((str(ip), port))
            sock.close()

            if result == 0:
                break
            time.sleep(5)
        self.assertEqual(0, result, '%s port is closed on instance' % port)
        # TODO: Add functionality to wait docker containers to spawn.

    def check_k8s_deployment(self, ip, port):
        start_time = time.time()
        while time.time() - start_time < 600:
            try:
                LOG.debug('Checking: {0}:{1}'.format(ip, port))
                self.verify_connection(ip, port)
                return
            except RuntimeError as e:
                time.sleep(10)
                LOG.debug(e)
        self.fail('Containers are not ready')

    def verify_connection(self, ip, port):
        tn = telnetlib.Telnet(ip, port)
        tn.write('GET / HTTP/1.0\n\n')
        buf = tn.read_all()
        LOG.debug('Data:\n {0}'.format(buf))
        if len(buf) != 0:
            tn.sock.sendall(telnetlib.IAC + telnetlib.NOP)
            return
        else:
            raise RuntimeError('Resource at {0}:{1} not exist'.
                               format(ip, port))

    def deployment_success_check(self, environment, *ports):
        """
        :param environment:
        :param ports:
        """
        deployment = self.murano.deployments.list(environment.id)[-1]

        self.assertEqual('success', deployment.state,
                         'Deployment status is {0}'.format(deployment.state))

        ip = environment.services[0]['instance']['floatingIpAddress']

        if ip:
            for port in ports:
                self.check_port_access(ip, port)
        else:
            self.fail('Instance does not have floating IP')

    def status_check(self, environment, configurations, kubernetes=False):
        """
        Function which gives opportunity to check multiple instances
        :param environment: Murano environment
        :param configurations: Array of configurations.
        :param kubernetes: Used for parsing multiple instances in one service
               False by default.
        Example: [[instance_name, *ports], [instance_name, *ports]] ...
        Example k8s: [[cluster['name'], instance_name, *ports], [...], ...]
        """
        for configuration in configurations:
            if kubernetes:
                service_name = configuration[0]
                LOG.debug('Service: {0}'.format(service_name))
                inst_name = configuration[1]
                LOG.debug('Instance: {0}'.format(inst_name))
                ports = configuration[2:]
                LOG.debug('Acquired ports: {0}'.format(ports))
                ip = self.get_k8s_ip_by_instance_name(environment, inst_name,
                                                      service_name)
                if ip and ports:
                    for port in ports:
                        self.check_port_access(ip, port)
                        self.check_k8s_deployment(ip, port)
                else:
                    self.fail('Instance does not have floating IP')
            else:
                inst_name = configuration[0]
                ports = configuration[1:]
                ip = self.get_ip_by_instance_name(environment, inst_name)
                if ip and ports:
                    for port in ports:
                        self.check_port_access(ip, port)
                else:
                    self.fail('Instance does not have floating IP')

    def get_ip_by_appname(self, environment, appname):
        """
        Returns ip of instance with a deployed application using
        application name
        :param environment: Murano environment
        :param appname: Application name or substring of application name
        :return:
        """
        for service in environment.services:
            if appname in service['name']:
                return service['instance']['floatingIpAddress']

    def get_ip_by_instance_name(self, environment, inst_name):
        """
        Returns ip of instance using instance name
        :param environment: Murano environment
        :param name: String, which is substring of name of instance or name of
        instance
        :return:
        """
        for service in environment.services:
            if inst_name in service['instance']['name']:
                return service['instance']['floatingIpAddress']

    def get_k8s_ip_by_instance_name(self, environment, inst_name, service_name):
        """
        Returns ip of specific kubernetes node (gateway, master, minion) based.
        Search depends on service name of kubernetes and names of spawned
        instances
        :param environment: Murano environment
        :param inst_name: Name of instance or substring of instance name
        :param service_name: Name of Kube Cluster application in Murano
        environment
        :return: Ip of Kubernetes instances
        """
        for service in environment.services:
            if service_name in service['name']:
                if "gateway" in inst_name:
                    for gateway in service['gatewayNodes']:
                        if inst_name in gateway['instance']['name']:
                            LOG.debug(gateway['instance']['floatingIpAddress'])
                            return gateway['instance']['floatingIpAddress']
                elif "master" in inst_name:
                    LOG.debug(service['masterNode']['instance'][
                        'floatingIpAddress'])
                    return service['masterNode']['instance'][
                        'floatingIpAddress']
                elif "minion" in inst_name:
                    for minion in service['minionNodes']:
                        if inst_name in minion['instance']['name']:
                            LOG.debug(minion['instance']['floatingIpAddress'])
                            return minion['instance']['floatingIpAddress']

    def create_env(self):
        name = self.rand_name('MuranoTe')
        environment = self.murano.environments.create({'name': name})
        self.environments.append(environment.id)
        LOG.debug('Created Environment:\n {0}'.format(environment))
        return environment

    def create_session(self, environment):
        return self.murano.sessions.configure(environment.id)

    def delete_session(self, environment, session):
        return self.murano.sessions.delete(environment.id, session.id)

    def add_service(self, environment, data, session):
        """
        This function adding a specific service to environment
        Returns a specific class <Service>
        :param environment:
        :param data:
        :param session:
        :return:
        """

        LOG.debug('Added service:\n {0}'.format(data))
        return self.murano.services.post(environment.id,
                                         path='/', data=data,
                                         session_id=session.id)

    def create_service(self, environment, session, json_data):
        """
        This function adding a specific service to environment
        Returns a JSON object with a service
        :param environment:
        :param session:
        :param json_data:
        :return:
        """
        LOG.debug('Added service:\n {0}'.format(json_data))
        headers = self.headers.copy()
        headers.update({'x-configuration-session': session.id})
        endpoint = '{0}environments/{1}/services'.format(self.murano_endpoint,
                                                         environment.id)
        return requests.post(endpoint, data=json.dumps(json_data),
                             headers=headers).json()

    def deploy_environment(self, environment, session):
        self.murano.sessions.deploy(environment.id, session.id)
        return self.wait_for_environment_deploy(environment)

    def get_environment(self, environment):
        return self.murano.environments.get(environment.id)

    def get_service_as_json(self, environment):
        service = self.murano.services.list(environment.id)[0]
        service = service.to_dict()
        service = json.dumps(service)
        return yaml.load(service)

    def _quick_deploy(self, name, *apps):
        environment = self.murano.environments.create({'name': name})
        self.environments.append(environment.id)

        session = self.murano.sessions.configure(environment.id)

        for app in apps:
            self.murano.services.post(environment.id,
                                      path='/',
                                      data=app,
                                      session_id=session.id)

        self.murano.sessions.deploy(environment.id, session.id)

        return self.wait_for_environment_deploy(environment)

    def _get_stack(self, environment_id):

        for stack in self.heat.stacks.list():
            if environment_id in stack.description:
                return stack

    def check_path(self, env, path, inst_name=None):
        environment = env.manager.get(env.id)
        if inst_name:
            ip = self.get_ip_by_instance_name(environment, inst_name)
        else:
            ip = environment.services[0]['instance']['floatingIpAddress']
        resp = requests.get('http://{0}/{1}'.format(ip, path))
        if resp.status_code == 200:
            pass
        else:
            self.fail("Service path unavailable")

    # TODO: Add function to check that environment removed.

    def get_last_deployment(self, environment):
        deployments = self.murano.deployments.list(environment.id)
        return deployments[0]

    def get_deployment_report(self, environment, deployment):
        history = ''
        report = self.murano.deployments.reports(environment.id, deployment.id)
        for status in report:
            history += '\t{0} - {1}\n'.format(status.created, status.text)
        return history

    def _log_report(self, environment):
        deployment = self.get_last_deployment(environment)
        details = deployment.result['result']['details']
        LOG.error('Exception found:\n {0}'.format(details))
        report = self.get_deployment_report(environment, deployment)
        LOG.debug('Report:\n {0}\n'.format(report))
