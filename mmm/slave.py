import copy
import time
import uuid
import logging

import bson
import gevent
#import pprint
from .triggers import Triggers

log = logging.getLogger(__name__)

MMM_DB_NAME = 'mmm'
MMM_REPL_FLAG = '__mmm'

class ReplicationSlave(object):
    '''Sets up replication based on info in the local 'mmm' collection.

    Each 'master' connection has its own document in mmm:
    
    {  _id: some_uuid,
      checkpoint: ...,  // timestamp offset in the oplog
      replication: [ {
          dst: 'slave-db.collection',
          src: 'master-db.collection',
          ops:'iud' },
         ... ]
    }
    '''

    def __init__(self, topology, name, connect=None):
        if connect: self.connect = connect
        self._topology = topology
        self.name = name
        topo = topology[name]
        self.id = topo['id']
        if isinstance(self.id, basestring):
            self.id = uuid.UUID(self.id)
        self.uri = topo['uri']
        self._conn = self.connect(self.uri)
        self._coll = self._conn.local[MMM_DB_NAME]
        self._config = {}
        self._greenlets = []
        self._master_checkpoint = None

    def connect(self, *args, **kwargs):
        '''Connect to a mongod server. Factored into a method so we can delay
        importing Connection until gevent.monkey.patch_all() is called. You could
        also insert another connect function to set options if you so desire.
        '''
        from pymongo import Connection
        return Connection(*args, **kwargs)

    def start(self, checkpoint=None):
        for gl in self._greenlets:
            gl.kill()
        self.load_config()

        def exception_callback(greenlet):
            log.exception("Exception happened in %s", greenlet)

        self._greenlets = [gevent.spawn(self.periodic_checkpoint, 5)]
        self._greenlets[-1].link_exception(exception_callback)

        for master_uri in self._config:
            self._greenlets.append(gevent.spawn(self.replicate, master_uri, checkpoint))
            self._greenlets[-1].link_exception(exception_callback)


    def load_config(self):
        from pymongo import Connection
        self._config = {}
        name_by_id = dict(
            (sconf['id'], name)
            for name, sconf in self._topology.items())
        for master in self._coll.find():
            if master['_id'] != 'master_checkpoint':
                self._config[name_by_id[master['_id']]] = master

                master_uri = self._topology[name_by_id[master['_id']]]['uri']
                conn = Connection(master_uri)
                coll = conn.local[MMM_DB_NAME]
                for cp in coll.find(dict(_id='master_checkpoint')):
                    self._master_checkpoint = cp['ts']
                conn.disconnect()

    def clear_config(self):
        self._config = {}
        self._coll.remove()

    def dump_config(self):
        result = {}
        for name, sconfig in self._config.items():
            d = copy.deepcopy(sconfig)
            d.pop('checkpoint', None)
            result[name] = d
        return result

    def set_replication(self, master_name, ns_dst, ns_src, ops='iud'):
        master_id = self._topology[master_name]['id']
        self._coll.update(
            dict(_id=master_id),
            { '$pull': { 'replication': dict(dst=ns_dst, src=ns_src) } })
        master = self._coll.find_and_modify(
            dict(_id=master_id),
            { '$push': {'replication': dict(dst=ns_dst, src=ns_src, ops=ops) }},
            upsert=True,
            new=True)
        self._config[master_name] = master

    def unset_replication(self, master_name, ns_dst=None, ns_src=None):
        master_id = self._topology[master_name]['id']
        to_pull = dict()
        if ns_dst is not None: to_pull['dst'] = ns_dst
        if ns_src is not None: to_pull['src'] = ns_src
        if ns_dst or ns_src:
            # Stop replication on one namespace
            master = self._coll.find_and_modify(
                dict(_id=master_id),
                { '$pull': { 'replication': to_pull } },
                new=True)
            self._config[master_name] = master
        else:
            # Stop replication on the whole master
            self._coll.remove(dict(_id=master_id))
            self._config.pop(master_name, None)
        
    def checkpoint(self, master_uri=None):
        if master_uri is None:
            masters = self._config.items()
        else:
            masters = [
                (master_uri, self._config[master_uri]) ]
        for _id, master in masters:
            self._coll.update(
                dict(_id=_id),
                { '$set': { 'checkpoint': master['checkpoint'] } })

    def replicate(self, master_name, checkpoint=None):
        '''Actual replication loop for replicating off of master_uri'''
        master_repl_config = self._config[master_name]
        master_info = self._topology[master_name]
        master_id = master_repl_config['_id']
        conn = self.connect(master_info['uri'])
        if checkpoint is None:
            checkpoint = master_repl_config.get('checkpoint')
        if checkpoint is None:
            # By default, start replicating as of NOW
            checkpoint = bson.Timestamp(long(time.time()), 0)
        triggers = Triggers(conn, checkpoint)
        for repl in master_repl_config['replication']:
            triggers.register(
                repl['src'], repl['ops'], 
                self._replicate_to_trigger(master_id, repl['dst']))
        for checkpoint in triggers.run():
            master_repl_config['checkpoint'] = checkpoint

    def periodic_checkpoint(self, period=1.0):
        '''Periodically call self.checkpoint() to allow restarts'''
        while True:
            gevent.sleep(period)
            self.checkpoint()

    def _replicate_to_trigger(self, src_id, dst):
        if isinstance(src_id, basestring):
            src_id = uuid.UUID(src_id)
        db, cname = dst.split('.', 1)
        collection = self._conn[db][cname]
        def trigger(ts, h, op, ns, o, v, o2=None, b=False):
            log.info('%s <= %s: %s %s', self.id, src_id, op, ns)
            if op == 'i':
                if o.get(MMM_REPL_FLAG) == self.id:
                    log.debug('%s: skip', self.id)
                    return
                o.setdefault(MMM_REPL_FLAG, src_id)
                collection.insert(o)
            elif op == 'u':
                log.debug('o %s, o2 %s', o, o2)
                upsert = b
                if any(k.startswith('$') for k in o):
                    # With modifiers, check & update setters
                    setters = o.setdefault('$set', {})
                else:
                    # Without modifiers, check & update the doc directly
                    setters = o
                if setters.get(MMM_REPL_FLAG) == self.id:
                    log.debug('%s: skip', self.id)
                    return
                setters.setdefault(MMM_REPL_FLAG, src_id)
                    
                log.debug('o %s, o2 %s', o, o2)
                collection.update(o2, o, upsert)
            elif op == 'd':
                justOne = b
                collection.remove(o)
        return trigger

    def __getitem__(self, ns_dst):
        return _ReplLHS(self, ns_dst)

class _ReplLHS(object):

    def __init__(self, slave, ns_dst):
        self.slave = slave
        self.ns_dst = ns_dst

    def __lshift__(self, (master_uri, ns_src)):
        self.slave.set_replication(
            master_uri, ns_dst=self.ns_dst, ns_src=ns_src)
        return self
        
