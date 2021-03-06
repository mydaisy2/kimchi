#
# Project Kimchi
#
# Copyright IBM, Corp. 2013
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA

import os
import time
import platform
from collections import defaultdict

import psutil
from cherrypy.process.plugins import BackgroundTask

from kimchi import disks
from kimchi import netinfo
from kimchi import xmlutils
from kimchi.basemodel import Singleton
from kimchi.exception import InvalidOperation, NotFoundError, OperationFailed
from kimchi.model.config import CapabilitiesModel
from kimchi.model.tasks import TaskModel
from kimchi.model.vms import DOM_STATE_MAP
from kimchi.repositories import Repositories
from kimchi.swupdate import SoftwareUpdate
from kimchi.utils import add_task, kimchi_log


HOST_STATS_INTERVAL = 1


class HostModel(object):
    def __init__(self, **kargs):
        self.conn = kargs['conn']
        self.objstore = kargs['objstore']
        self.task = TaskModel(**kargs)
        self.host_info = self._get_host_info()

    def _get_host_info(self):
        res = {}
        with open('/proc/cpuinfo') as f:
            for line in f.xreadlines():
                if "model name" in line:
                    res['cpu'] = line.split(':')[1].strip()
                    break

        res['memory'] = psutil.TOTAL_PHYMEM
        # 'fedora' '17' 'Beefy Miracle'
        distro, version, codename = platform.linux_distribution()
        res['os_distro'] = distro
        res['os_version'] = version
        res['os_codename'] = unicode(codename, "utf-8")

        return res

    def lookup(self, *name):
        return self.host_info

    def swupdate(self, *name):
        try:
            swupdate = SoftwareUpdate()
        except:
            raise OperationFailed('KCHPKGUPD0004E')

        pkgs = swupdate.getNumOfUpdates()
        if pkgs == 0:
            raise OperationFailed('KCHPKGUPD0001E')

        kimchi_log.debug('Host is going to be updated.')
        taskid = add_task('', swupdate.doUpdate, self.objstore, None)
        return self.task.lookup(taskid)

    def shutdown(self, args=None):
        # Check for running vms before shutdown
        running_vms = self._get_vms_list_by_state('running')
        if len(running_vms) > 0:
            raise OperationFailed("KCHHOST0001E")

        kimchi_log.info('Host is going to shutdown.')
        os.system('shutdown -h now')

    def reboot(self, args=None):
        # Find running VMs
        running_vms = self._get_vms_list_by_state('running')
        if len(running_vms) > 0:
            raise OperationFailed("KCHHOST0002E")

        kimchi_log.info('Host is going to reboot.')
        os.system('reboot')

    def _get_vms_list_by_state(self, state):
        conn = self.conn.get()
        names = [dom.name().decode('utf-8') for dom in conn.listAllDomains(0)]

        ret_list = []
        for name in names:
            dom = conn.lookupByName(name.encode("utf-8"))
            info = dom.info()
            if (DOM_STATE_MAP[info[0]]) == state:
                ret_list.append(name)
        return ret_list


class HostStatsModel(object):
    __metaclass__ = Singleton

    def __init__(self, **kargs):
        self.host_stats = defaultdict(int)
        self.host_stats_thread = BackgroundTask(HOST_STATS_INTERVAL,
                                                self._update_host_stats)
        self.host_stats_thread.start()

    def lookup(self, *name):
        return {'cpu_utilization': self.host_stats['cpu_utilization'],
                'memory': self.host_stats.get('memory'),
                'disk_read_rate': self.host_stats['disk_read_rate'],
                'disk_write_rate': self.host_stats['disk_write_rate'],
                'net_recv_rate': self.host_stats['net_recv_rate'],
                'net_sent_rate': self.host_stats['net_sent_rate']}

    def _update_host_stats(self):
        preTimeStamp = self.host_stats['timestamp']
        timestamp = time.time()
        # FIXME when we upgrade psutil, we can get uptime by psutil.uptime
        # we get uptime by float(open("/proc/uptime").readline().split()[0])
        # and calculate the first io_rate after the OS started.
        seconds = (timestamp - preTimeStamp if preTimeStamp else
                   float(open("/proc/uptime").readline().split()[0]))

        self.host_stats['timestamp'] = timestamp
        self._get_host_disk_io_rate(seconds)
        self._get_host_network_io_rate(seconds)

        self._get_percentage_host_cpu_usage()
        self._get_host_memory_stats()

    def _get_percentage_host_cpu_usage(self):
        # This is cpu usage producer. This producer will calculate the usage
        # at an interval of HOST_STATS_INTERVAL.
        # The psutil.cpu_percent works as non blocking.
        # psutil.cpu_percent maintains a cpu time sample.
        # It will update the cpu time sample when it is called.
        # So only this producer can call psutil.cpu_percent in kimchi.
        self.host_stats['cpu_utilization'] = psutil.cpu_percent(None)

    def _get_host_memory_stats(self):
        virt_mem = psutil.virtual_memory()
        # available:
        #  the actual amount of available memory that can be given
        #  instantly to processes that request more memory in bytes; this
        #  is calculated by summing different memory values depending on
        #  the platform (e.g. free + buffers + cached on Linux)
        memory_stats = {'total': virt_mem.total,
                        'free': virt_mem.free,
                        'cached': virt_mem.cached,
                        'buffers': virt_mem.buffers,
                        'avail': virt_mem.available}
        self.host_stats['memory'] = memory_stats

    def _get_host_disk_io_rate(self, seconds):
        prev_read_bytes = self.host_stats['disk_read_bytes']
        prev_write_bytes = self.host_stats['disk_write_bytes']

        disk_io = psutil.disk_io_counters(False)
        read_bytes = disk_io.read_bytes
        write_bytes = disk_io.write_bytes

        rd_rate = int(float(read_bytes - prev_read_bytes) / seconds + 0.5)
        wr_rate = int(float(write_bytes - prev_write_bytes) / seconds + 0.5)

        self.host_stats.update({'disk_read_rate': rd_rate,
                                'disk_write_rate': wr_rate,
                                'disk_read_bytes': read_bytes,
                                'disk_write_bytes': write_bytes})

    def _get_host_network_io_rate(self, seconds):
        prev_recv_bytes = self.host_stats['net_recv_bytes']
        prev_sent_bytes = self.host_stats['net_sent_bytes']

        net_ios = psutil.network_io_counters(True)
        recv_bytes = 0
        sent_bytes = 0
        for key in set(netinfo.nics() +
                       netinfo.wlans()) & set(net_ios.iterkeys()):
            recv_bytes = recv_bytes + net_ios[key].bytes_recv
            sent_bytes = sent_bytes + net_ios[key].bytes_sent

        rx_rate = int(float(recv_bytes - prev_recv_bytes) / seconds + 0.5)
        tx_rate = int(float(sent_bytes - prev_sent_bytes) / seconds + 0.5)

        self.host_stats.update({'net_recv_rate': rx_rate,
                                'net_sent_rate': tx_rate,
                                'net_recv_bytes': recv_bytes,
                                'net_sent_bytes': sent_bytes})


class PartitionsModel(object):
    def __init__(self, **kargs):
        pass

    def get_list(self):
        result = disks.get_partitions_names()
        return result


class PartitionModel(object):
    def __init__(self, **kargs):
        pass

    def lookup(self, name):
        if name not in disks.get_partitions_names():
            raise NotFoundError("KCHPART0001E", {'name': name})

        return disks.get_partition_details(name)


class DevicesModel(object):
    def __init__(self, **kargs):
        self.conn = kargs['conn']

    def get_list(self, _cap=None):
        conn = self.conn.get()
        if _cap is None:
            dev_names = [name.name() for name in conn.listAllDevices(0)]
        elif _cap == 'fc_host':
            dev_names = self._get_devices_fc_host()
        else:
            # Get devices with required capability
            dev_names = conn.listDevices(_cap, 0)
        return dev_names

    def _get_devices_fc_host(self):
        conn = self.conn.get()
        # Libvirt < 1.0.5 does not support fc_host capability
        if not CapabilitiesModel().fc_host_support:
            ret = []
            scsi_hosts = conn.listDevices('scsi_host', 0)
            for host in scsi_hosts:
                xml = conn.nodeDeviceLookupByName(host).XMLDesc(0)
                path = '/device/capability/capability/@type'
                if 'fc_host' in xmlutils.xpath_get_text(xml, path):
                    ret.append(host)
            return ret
        return conn.listDevices('fc_host', 0)


class DeviceModel(object):
    def __init__(self, **kargs):
        self.conn = kargs['conn']

    def lookup(self, nodedev_name):
        conn = self.conn.get()
        try:
            dev_xml = conn.nodeDeviceLookupByName(nodedev_name).XMLDesc(0)
        except:
            raise NotFoundError('KCHHOST0003E', {'name': nodedev_name})
        cap_type = xmlutils.xpath_get_text(
            dev_xml, '/device/capability/capability/@type')
        wwnn = xmlutils.xpath_get_text(
            dev_xml, '/device/capability/capability/wwnn')
        wwpn = xmlutils.xpath_get_text(
            dev_xml, '/device/capability/capability/wwpn')
        return {
            'name': nodedev_name,
            'adapter_type': cap_type[0] if len(cap_type) >= 1 else '',
            'wwnn': wwnn[0] if len(wwnn) == 1 else '',
            'wwpn': wwpn[0] if len(wwpn) == 1 else ''}


class PackagesUpdateModel(object):
    def __init__(self, **kargs):
        try:
            self.host_swupdate = SoftwareUpdate()
        except:
            self.host_swupdate = None
        self.objstore = kargs['objstore']
        self.task = TaskModel(**kargs)

    def get_list(self):
        if self.host_swupdate is None:
            raise OperationFailed('KCHPKGUPD0004E')

        return self.host_swupdate.getUpdates()


class PackageUpdateModel(object):
    def __init__(self, **kargs):
        pass

    def lookup(self, name):
        try:
            swupdate = SoftwareUpdate()
        except Exception:
            raise OperationFailed('KCHPKGUPD0004E')

        return swupdate.getUpdate(name)


class RepositoriesModel(object):
    def __init__(self, **kargs):
        try:
            self.host_repositories = Repositories()
        except:
            self.host_repositories = None

    def get_list(self):
        if self.host_repositories is None:
            raise InvalidOperation('KCHREPOS0014E')

        return self.host_repositories.getRepositories().keys()

    def create(self, params):
        if self.host_repositories is None:
            raise InvalidOperation('KCHREPOS0014E')

        repo_id = params.get('repo_id', None)

        # Create a repo_id if not given by user. The repo_id will follow
        # the format kimchi_repo_<integer>, where integer is the number of
        # seconds since the Epoch (January 1st, 1970), in UTC.
        if repo_id is None:
            repo_id = "kimchi_repo_%s" % int(time.time())
            while repo_id in self.get_list():
                repo_id = "kimchi_repo_%s" % int(time.time())
            params.update({'repo_id': repo_id})

        if repo_id in self.get_list():
            raise InvalidOperation("KCHREPOS0006E", {'repo_id': repo_id})
        self.host_repositories.addRepository(params)
        return repo_id


class RepositoryModel(object):
    def __init__(self, **kargs):
        try:
            self._repositories = Repositories()
        except:
            self._repositories = None

    def lookup(self, repo_id):
        if self._repositories is None:
            raise InvalidOperation('KCHREPOS0014E')

        return self._repositories.getRepository(repo_id)

    def enable(self, repo_id):
        if self._repositories is None:
            raise InvalidOperation('KCHREPOS0014E')

        if not self._repositories.enableRepository(repo_id):
            raise OperationFailed("KCHREPOS0007E", {'repo_id': repo_id})

    def disable(self, repo_id):
        if self._repositories is None:
            raise InvalidOperation('KCHREPOS0014E')

        if not self._repositories.disableRepository(repo_id):
            raise OperationFailed("KCHREPOS0008E", {'repo_id': repo_id})

    def update(self, repo_id, params):
        if self._repositories is None:
            raise InvalidOperation('KCHREPOS0014E')

        try:
            self._repositories.updateRepository(repo_id, params)
        except:
            raise OperationFailed("KCHREPOS0009E", {'repo_id': repo_id})
        return repo_id

    def delete(self, repo_id):
        if self._repositories is None:
            raise InvalidOperation('KCHREPOS0014E')

        return self._repositories.removeRepository(repo_id)
