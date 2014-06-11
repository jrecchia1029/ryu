# Copyright (C) 2014 Nippon Telegraph and Telephone Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from oslo.config import cfg
import socket

from limelib import jsonlog as apgw_log

import netaddr
import logging
from ryu.base import app_manager
from ryu.controller import handler
from ryu.services.protocols.vrrp import event as vrrp_event
from ryu.services.protocols.vrrp import api as vrrp_api
from ryu.lib import rpc
from ryu.lib import hub
from ryu.lib import mac

CONF = cfg.CONF


class RPCError(Exception):
    pass


class Peer(object):
    def __init__(self, queue):
        super(Peer, self).__init__()
        self.queue = queue

    def _handle_vrrp_request(self, data):
        self.queue.put((self, data))


class RpcVRRPManager(app_manager.RyuApp):
    LOGGER_NAME = 'vrrp'

    def __init__(self, *args, **kwargs):
        super(RpcVRRPManager, self).__init__(*args, **kwargs)
        self._args = args
        self._kwargs = kwargs
        self._peers = []
        self._rpc_events = hub.Queue(128)
        self.server_thread = hub.spawn(self._peer_accept_thread)
        self.event_thread = hub.spawn(self._rpc_request_loop_thread)
        self.log = apgw_log.initialize('vrrp')
        self.states_log = apgw_log.DictAndLogTypeAdapter(self.log,
                                                         log_type='states')

    def _rpc_request_loop_thread(self):
        while True:
            (peer, data) = self._rpc_events.get()
            msgid, target_method, params = data
            error = None
            result = None
            try:
                self.log.info({'msgid': msgid,
                               'target_method': target_method,
                               'params': params})
                if target_method == "vrrp_config":
                    result = self._config(msgid, params)
                elif target_method == "vrrp_list":
                    result = self._list(msgid, params)
                elif target_method == "vrrp_config_change":
                    result = self._config_change(msgid, params)
                elif target_method == "vrrp_shutdown":
                    result = self._shutdown(msgid, params)
                else:
                    error = 'Unknown method %s' % (target_method)
            except RPCError as e:
                error = str(e)
            params = {'msgid': msgid, 'error': error, 'result': result}
            self.log.info(params)
            peer._endpoint.send_response(msgid, error=error, result=result)

    def _peer_loop_thread(self, peer):
        peer._endpoint.serve()
        # the peer connection is closed
        self._peers.remove(peer)

    def peer_accept_handler(self, new_sock, addr):
        peer = Peer(self._rpc_events)
        table = {
            rpc.MessageType.REQUEST: peer._handle_vrrp_request,
            }
        peer._endpoint = rpc.EndPoint(new_sock, disp_table=table)
        self._peers.append(peer)
        hub.spawn(self._peer_loop_thread, peer)

    def _peer_accept_thread(self):
        server = hub.StreamServer(('', CONF.vrrp_rpc_port),
                                  self.peer_accept_handler)
        server.serve_forever()

    def _params_to_dict(self, params, keys):
        d = {}
        for k, v in params.items():
            if k in keys:
                d[k] = v
        return d

    def _config(self, msgid, params):
        try:
            param_dict = params[0]
        except:
            raise RPCError('parameters are missing')

        if_params = self._params_to_dict(param_dict,
                                         ('ip_address',
                                          'ifname'))
        try:
            if_params['primary_ip_address'] = if_params.pop('ip_address')
        except:
            raise RPCError('ip_addr parameter is missing')
        try:
            if_params['device_name'] = if_params.pop('ifname')
        except:
            raise RPCError('ifname parameter is missing')
        # drop vlan support later
        if_params['vlan_id'] = None
        if_params['mac_address'] = mac.DONTCARE_STR
        try:
            interface = vrrp_event.VRRPInterfaceNetworkDevice(**if_params)
        except:
            raise RPCError('parameters are invalid, %s' % (str(param_dict)))

        config_params = self._params_to_dict(param_dict,
                                             ('vrid',  # mandatory
                                              'ip_addr',  # mandatory
                                              'version',
                                              'admin_state',
                                              'priority',
                                              'advertisement_interval',
                                              'preempt_mode',
                                              'preempt_delay',
                                              'statistics_interval'))
        if CONF.vrrp_use_vmac:
            config_params.update({'use_virtual_mac': True})
        try:
            config_params['ip_addresses'] = [config_params.pop('ip_addr')]
        except:
            raise RPCError('ip_addr parameter is missing')
        try:
            config = vrrp_event.VRRPConfig(**config_params)
        except:
            raise RPCError('parameters are invalid, %s' % (str(param_dict)))
        config.contexts = param_dict.get('contexts')

        config_result = vrrp_api.vrrp_config(self, interface, config)

        api_result = [
            config_result.config.vrid,
            config_result.config.priority,
            str(netaddr.IPAddress(config_result.config.ip_addresses[0]))]
        return api_result

    def _lookup_instance(self, vrid):
        for instance in vrrp_api.vrrp_list(self).instance_list:
            if vrid == instance.config.vrid:
                return instance.instance_name
        return None

    def _shutdown(self, msgid, params):
        try:
            config_values = params[0]
        except:
            raise RPCError('parameters are missing')

        vrid = config_values.get('vrid')
        instance_name = self._lookup_instance(vrid)
        if not instance_name:
            raise RPCError('vrid %d is not found' % (vrid))
        vrrp_api.vrrp_shutdown(self, instance_name)
        return [{}]

    def _config_change(self, msgid, params):
        try:
            config_values = params[0]
        except:
            raise RPCError('parameters are missing')

        vrid = config_values.get('vrid')
        instance_name = self._lookup_instance(vrid)
        if not instance_name:
            raise RPCError('vrid %d is not found' % (vrid))

        priority = config_values.get('priority')
        interval = config_values.get('advertisement_interval')
        vrrp_api.vrrp_config_change(self, instance_name, priority=priority,
                                    advertisement_interval=interval)
        return {}

    def _list(self, msgid, params):
        result = vrrp_api.vrrp_list(self)
        instance_list = result.instance_list
        ret_list = []
        for instance in instance_list:
            c = instance.config
            info_dict = {
                "instance_name": instance.instance_name,
                "vrid": c.vrid,
                "version": c.version,
                "advertisement_interval": c.advertisement_interval,
                "priority": c.priority,
                "virtual_ip_address": str(netaddr.IPAddress(c.ip_addresses[0]))
                }
            ret_list.append(info_dict)
        return ret_list

    @handler.set_ev_cls(vrrp_event.EventVRRPStateChanged)
    def vrrp_state_changed_handler(self, ev):
        name = ev.instance_name
        old_state = ev.old_state
        new_state = ev.new_state
        vrid = ev.config.vrid
        params = {'vrid': vrid, 'old_state': old_state, 'new_state': new_state}
        self.states_log.critical(params)
        for peer in self._peers:
            peer._endpoint.send_notification("notify_status", [params])
