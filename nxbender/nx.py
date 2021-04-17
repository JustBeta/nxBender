#!/usr/bin/env python2
import requests
import logging
from . import ppp
import pyroute2
import ipaddress
import atexit
import os

from requests.adapters import HTTPAdapter
from requests.packages.urllib3.poolmanager import PoolManager

try:
    unicode
except NameError:
    unicode = str

class FingerprintAdapter(HTTPAdapter):
    """"Transport adapter" that allows us to pin a fingerprint for the `requests` library."""
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        super(FingerprintAdapter, self).__init__()

    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = PoolManager(
            num_pools=connections, maxsize=maxsize,
            block=block, assert_fingerprint=self.fingerprint)

class NXSession(object):
    def __init__(self, options):
        self.options = options

    def run(self):
        self.host = self.options.server + ':%d' % self.options.port
        self.session = requests.Session()

        if self.options.fingerprint:
            self.session.verify = False
            self.session.mount('https://', FingerprintAdapter(self.options.fingerprint))

        self.session.headers = {
                'User-Agent': 'Dell SonicWALL NetExtender for Linux 8.1.789',
        }

        logging.info("Logging in...")
        self.login(
                self.options.username,
                self.options.password,
                self.options.domain
            )

        logging.info("Starting session...")
        self.start_session()

        logging.info("Update remote DNS...")
        self.dns_tunnel()

        logging.info("Dialing up tunnel...")
        self.tunnel()

    def login(self, username, password, domain):
        resp = self.session.post('https://%s/cgi-bin/userLogin' % self.host,
                                 data={
                                     'username': username,
                                     'password': password,
                                     'domain': domain,
                                     'login': 'true',
                                 },
                                 headers={
                                     'X-NE-SESSIONPROMPT': 'true',
                                 },
                                )

        error = resp.headers.get('X-NE-Message', None)
        error = resp.headers.get('X-NE-message', error)
        if error:
            raise IOError('Server returned error: %s' % error)

        atexit.register(self.logout)

    def logout(self):
        # We need to try, but if we went down because we can't talk to the server? - not a big deal.
        try:
            self.session.get('https://%s/cgi-bin/userLogout' % self.host)
        except:
            pass

    def start_session(self):
        """
        Start a VPN session with the server.

        Must be logged in.
        Stores srv_options and routes returned from the server.
        """

        resp = self.session.get('https://%s/cgi-bin/sslvpnclient' % self.host,
                                params={
                                    'launchplatform': 'mac',
                                    'neProto': 3,
                                    'supportipv6': 'no',
                                },
                               )
        error = resp.headers.get('X-NE-Message', None)
        error = resp.headers.get('X-NE-message', error)
        if error:
            raise IOError('Server returned error: %s' % error)

        srv_options = {}
        routes = []
        nameservers = []
        searchs = []

        # Very dodgily avoid actually parsing the HTML
        for line in resp.iter_lines():
            line = line.strip().decode('utf-8', errors='replace')
            if line.startswith('<'):
                continue
            if line.startswith('}<'):
                continue

            try:
                key, value = line.split(' = ', 1)
            except ValueError:
                logging.warn("Unexpected line in session start message: '%s'" % line)

            if key == 'Route':
                routes.append(value)
            elif key == 'dns1':
                nameservers.append(value)
            elif key == 'dns2':
                nameservers.append(value)
            elif key == 'dnsSuffixes':
                searchs.append(value)
            elif key not in srv_options:
                srv_options[key] = value
            else:
                logging.info('Duplicated srv_options value %s = %s' % (key, value))

            logging.debug("srv_option '%s' = '%s'" % (key, value))

        self.srv_options = srv_options
        self.routes = routes
        self.nameservers = nameservers
        self.searchs = searchs

    def dns_tunnel(self):
        """
        Apply remote DNS
        """
        resolv = '/etc/resolv.conf'
        
        # lit le fichier resolv.conf
        refile = open(resolv,'r')
        resolvbak = refile.read()
        refile.close()
        
        # ecrit le fichier resolv.conf.bak
        refile = open(resolv + '.bak','w')
        refile.write(resolvbak)
        refile.close()
        
        # vide le fichier resolv.conf
        refile = open(resolv,'w')
        refile.write('')
        refile.close()

        # ecrit le nouveau fichier resolv.conf
        refile = open(resolv,'a')
        for nameserver in set(self.nameservers):
            refile.writelines('nameserver %s\n' % (nameserver))
            logging.debug("nameserver '%s' " % (nameserver))

        refile.writelines('# DNS requests are forwarded to the host. DHCP DNS options are ignored.\n')
        # refile.writelines('nameserver 192.168.65.5\n\n')
        refile.writelines(resolvbak)
        refile.writelines('\n')

        refile.writelines('search ')
        for search in set(self.searchs):
            refile.writelines(search + ' ')

        refile.writelines('\n')
        refile.close()
        logging.info("Remote DNS configured.")

    def tunnel(self):
        """
        Begin PPP tunneling.
        """

        tunnel_version = self.srv_options.get('NX_TUNNEL_PROTO_VER', None)

        if tunnel_version is None:
            auth_key = self.session.cookies['swap']
        elif tunnel_version == '2.0':
            auth_key = self.srv_options['SessionId']
        else:
            logging.warn("Unknown tunnel version '%s'" % tunnel_version)
            auth_key = self.srv_options['SessionId']    # a guess

        pppd = ppp.PPPSession(self.options, auth_key, routecallback=self.setup_routes)
        pppd.run()

        resolv = '/etc/resolv.conf'
        
        # lit le fichier resolv.conf.bak
        refile = open(resolv + '.bak','r')
        resolvorg = refile.read()
        refile.close()
        
        # ecrit le fichier resolv.conf
        refile = open(resolv,'w')
        refile.write(resolvorg)
        refile.close()

        # efface le fichier resolv.conf.bak
        os.remove(resolv + '.bak')

    def setup_routes(self, gateway):
        ip = pyroute2.IPRoute()

        for route in set(self.routes):
            net = ipaddress.IPv4Network(unicode(route))
            dst = '%s/%d' % (net.network_address, net.prefixlen)
            ip.route("add", dst=dst, gateway=gateway)

        logging.info("Remote routing configured, VPN is up")
